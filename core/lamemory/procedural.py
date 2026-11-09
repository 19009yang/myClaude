"""经验候选和可审计的反馈；模型自评不构成成功证据。"""
import hashlib
import json
import uuid
from types import SimpleNamespace

from .retrieval import normalized, rank
from .store import utc_now


class ProceduralMemory:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.user = service.user_id

    def _text(self, value):
        if not isinstance(value, str) or not value.strip() or len(value) > self.service.config.max_content_chars:
            raise ValueError("经验字段必须为非空限长字符串")
        return value.strip()

    def validate(self, candidate):
        required = {"task_type", "conditions", "steps", "result_basis", "source_message_ids"}
        if not isinstance(candidate, dict) or set(candidate) != required:
            raise ValueError("经验候选字段不完整或包含不受信任字段")
        steps = candidate["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError("经验步骤必须为 1 到 20 项的数组")
        return {"task_type": self._text(candidate["task_type"]), "conditions": self._text(candidate["conditions"]),
                "steps": [self._text(step) for step in steps], "result_basis": self._text(candidate["result_basis"]),
                "source_message_ids": candidate["source_message_ids"]}

    def _trajectory(self, sources, *, require_user=False, complete=True):
        if not isinstance(sources, list) or not sources or any(not isinstance(s, str) for s in sources):
            raise ValueError("经验和反馈必须有原始消息来源")
        rows = []
        for source in set(sources):
            row = self.db.one("SELECT * FROM lamemory_messages WHERE user_id=? AND session_id=? AND message_id=?",
                              (self.user, self.service.session_id, source))
            if not row or require_user and row["role"] != "user":
                raise ValueError("来源必须属于当前会话；用户反馈必须引用用户原文")
            rows.append(row)
        turns = {r["turn_id"] for r in rows}
        if len(turns) != 1:
            raise ValueError("一次经验/反馈只能引用一个独立任务轨迹")
        turn = rows[0]["turn_id"]
        committed = self.db.one("SELECT committed_at FROM lamemory_turns WHERE user_id=? AND session_id=? AND turn_id=?",
                                (self.user, self.service.session_id, turn))
        if complete and (not committed or not committed["committed_at"]):
            raise ValueError("只能从已完成轮次提取经验或记录测试结果")
        return json.dumps([self.service.session_id, turn], ensure_ascii=False)

    def _row(self, experience_id):
        row = self.db.one("SELECT * FROM lamemory_experiences WHERE user_id=? AND experience_id=?", (self.user, experience_id))
        if not row or row["scope_kind"] == "project" and row["scope_key"] != self.service.config.project_id:
            raise KeyError("经验不存在或对当前项目不可见")
        return row

    def propose(self, candidate, *, scope_kind="user", replaces=None):
        candidate = self.validate(candidate)
        trajectory = self._trajectory(candidate["source_message_ids"])
        if scope_kind not in {"user", "project"} or scope_kind == "project" and not self.service.config.project_id:
            raise ValueError("无效的经验范围")
        scope_key = self.user if scope_kind == "user" else self.service.config.project_id
        content = json.dumps({k: candidate[k] for k in ("task_type", "conditions", "steps", "result_basis")}, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256((scope_kind + ":" + scope_key + ":" + normalized(content)).encode()).hexdigest()
        with self.db.transaction():
            old = self._row(replaces) if replaces else None
            if old and old["status"] == "superseded":
                raise ValueError("不能再次替代历史经验")
            duplicate = self.db.one("SELECT experience_id FROM lamemory_experiences WHERE user_id=? AND trajectory_key=? AND content_hash=?",
                                    (self.user, trajectory, digest))
            if duplicate:
                if old:
                    raise ValueError("替代经验必须包含新的做法或依据")
                return self.inspect(duplicate["experience_id"])
            experience_id = "exp_" + uuid.uuid4().hex
            now = utc_now()
            self.db.execute("INSERT INTO lamemory_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (experience_id, self.user, scope_kind, scope_key, candidate["task_type"], candidate["conditions"],
                             json.dumps(candidate["steps"], ensure_ascii=False), candidate["result_basis"], digest, trajectory,
                             "candidate", old["revision"] + 1 if old else 1, old["lineage_id"] if old else experience_id,
                             None, 0, 0, now, now))
            for source in set(candidate["source_message_ids"]):
                self.db.execute("INSERT INTO lamemory_experience_sources VALUES (?,?,?)", (self.user, experience_id, source))
            if old:
                self.db.execute("UPDATE lamemory_experiences SET status='superseded',superseded_by=?,updated_at=? WHERE user_id=? AND experience_id=?",
                                (experience_id, now, self.user, old["experience_id"]))
        return self.inspect(experience_id)

    def capture(self, turn_id=None):
        turn = turn_id or self.service.current_turn_id
        messages = self.db.messages(self.user, self.service.session_id, turn)
        if not messages:
            raise ValueError("任务轨迹不存在")
        self._trajectory([messages[0]["message_id"]])
        with self.db.connection:
            return self.service._enqueue_job("experience", f"experience:{self.service.session_id}:{turn}",
                                             {"project_id": self.service.config.project_id}, turn_id=turn)

    def extract(self, job, *, timeout):
        if not callable(getattr(self.service.provider, "extract_experiences", None)):
            raise RuntimeError("未配置经验提取 provider")
        messages = self.db.messages(self.user, job["session_id"], job["turn_id"])
        result = self.service.provider.extract_experiences(messages, max_items=self.service.config.max_experience_candidates, timeout=timeout)
        if not isinstance(result, list) or len(result) > self.service.config.max_experience_candidates:
            raise ValueError("经验提取必须返回限长数组")
        candidates = [self.validate(item) for item in result]
        allowed = {m["message_id"] for m in messages}
        for candidate in candidates:
            sources = candidate["source_message_ids"]
            if not isinstance(sources, list) or not sources or any(not isinstance(s, str) or s not in allowed for s in sources):
                raise ValueError("经验来源必须属于本次完成轨迹")
        with self.db.transaction():
            for candidate in candidates:
                self.propose(candidate, scope_kind="project" if self.service.config.project_id else "user")

    def feedback(self, experience_id, outcome, source_message_ids, *, evidence_kind="user", evidence=""):
        if outcome not in {"success", "failure", "approved"} or evidence_kind not in {"user", "test", "model"}:
            raise ValueError("无效的经验反馈")
        if outcome == "approved" and evidence_kind != "user":
            raise ValueError("明确认可只能由用户原文提供")
        trajectory = self._trajectory(source_message_ids, require_user=evidence_kind == "user", complete=evidence_kind != "user")
        if evidence_kind == "test":
            evidence = self._text(evidence)
        elif not evidence:
            evidence = "\n".join(json.loads(self.db.one("SELECT payload_json FROM lamemory_messages WHERE user_id=? AND message_id=?", (self.user, s))["payload_json"]).get("content", "") or "" for s in source_message_ids)
        evidence = self._text(evidence)
        with self.db.connection:
            row = self._row(experience_id)
            if row["status"] == "superseded":
                raise ValueError("历史经验不能继续接受反馈")
            duplicate = self.db.one("SELECT * FROM lamemory_experience_feedback WHERE user_id=? AND lineage_id=? AND trajectory_key=?", (self.user, row["lineage_id"], trajectory))
            if duplicate:
                if duplicate["outcome"] != outcome or duplicate["evidence_kind"] != evidence_kind:
                    raise ValueError("同一任务已有不同反馈，需在新任务验证或替代经验")
                return self.inspect(experience_id)
            # The training trajectory and model self-evaluations are retained but never count as independent successes.
            training = self.db.one("SELECT 1 FROM lamemory_experiences WHERE user_id=? AND lineage_id=? AND trajectory_key=?",
                                   (self.user, row["lineage_id"], trajectory))
            independent = not training and evidence_kind in {"user", "test"}
            successes = row["successes"] + int(independent and outcome == "success")
            failures = row["failures"] + int(evidence_kind != "model" and outcome == "failure")
            status = row["status"]
            if evidence_kind != "model" and outcome == "failure":
                status = "revoked"
            elif outcome == "approved":
                status = "active"
            elif status == "candidate" and successes >= self.service.config.experience_min_successes and failures == 0:
                status = "active"
            self.db.execute("INSERT INTO lamemory_experience_feedback VALUES (?,?,?,?,?,?,?,?,?,?)",
                            ("fb_" + uuid.uuid4().hex, self.user, experience_id, row["lineage_id"], trajectory, outcome,
                             evidence_kind, evidence, json.dumps(source_message_ids), utc_now()))
            self.db.execute("UPDATE lamemory_experiences SET successes=?,failures=?,status=?,updated_at=? WHERE user_id=? AND experience_id=?",
                            (successes, failures, status, utc_now(), self.user, experience_id))
        return self.inspect(experience_id)

    def revoke(self, experience_id):
        with self.db.connection:
            row = self._row(experience_id)
            if row["status"] == "superseded":
                raise ValueError("历史经验不能撤销，请操作当前版本")
            self.db.execute("UPDATE lamemory_experiences SET status='revoked',updated_at=? WHERE user_id=? AND experience_id=?", (utc_now(), self.user, experience_id))
        return self.inspect(experience_id)

    def inspect(self, experience_id):
        row = self._row(experience_id)
        row["steps"] = json.loads(row.pop("steps_json"))
        row["source_message_ids"] = [r["message_id"] for r in self.db.rows("SELECT message_id FROM lamemory_experience_sources WHERE user_id=? AND experience_id=? ORDER BY message_id", (self.user, experience_id))]
        row["feedback"] = self.db.rows("SELECT * FROM lamemory_experience_feedback WHERE user_id=? AND experience_id=? ORDER BY created_at", (self.user, experience_id))
        for feedback in row["feedback"]:
            feedback["source_message_ids"] = json.loads(feedback.pop("source_ids_json"))
        row["sources"] = []
        for source in row["source_message_ids"]:
            message = self.db.one("SELECT * FROM lamemory_messages WHERE user_id=? AND message_id=?", (self.user, source))
            message["payload"] = json.loads(message.pop("payload_json"))
            row["sources"].append(message)
        row["versions"] = self.db.rows("SELECT experience_id,revision,status,superseded_by FROM lamemory_experiences WHERE user_id=? AND lineage_id=? ORDER BY revision", (self.user, row["lineage_id"]))
        return row

    def list(self, status=None):
        if status is not None and status not in {"candidate", "active", "revoked", "superseded"}:
            raise ValueError("无效的经验状态")
        rows = self.db.rows("SELECT experience_id,status FROM lamemory_experiences WHERE user_id=? AND (scope_kind='user' OR scope_kind='project' AND scope_key=?) ORDER BY created_at,experience_id", (self.user, self.service.config.project_id))
        return [self.inspect(row["experience_id"]) for row in rows if status is None or row["status"] == status]

    def search(self, query, top_k=None):
        self.service._validate_query(query)
        limit = self.service.config.top_k if top_k is None else top_k
        if not isinstance(limit, int) or not 0 <= limit <= 100:
            raise ValueError("top_k 必须为 0 到 100")
        items = []
        for row in self.db.rows("SELECT * FROM lamemory_experiences WHERE user_id=? AND status='active' AND (scope_kind='user' OR scope_kind='project' AND scope_key=?)", (self.user, self.service.config.project_id)):
            content = "\n".join([row["task_type"], row["conditions"], *json.loads(row["steps_json"])])
            items.append(SimpleNamespace(content=content, revision=row["revision"], memory_id=row["experience_id"]))
        return [{**self.inspect(item.memory_id), "score": score, "reason": reason, "mode": "keyword"}
                for item, score, reason in rank(query, items)[:limit]]
