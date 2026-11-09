"""绑定可信用户和会话的 LaMemory 入口，单进程串行消费持久化任务。"""
import hashlib
import json
import time
import uuid

from .config import LaMemoryConfig
from .context import build_context, valid_chain
from .extraction import normalize_time, validate_batch, validate_candidate
from .models import FlushResult, MemoryItem, SearchHit, WriteResult
from .retrieval import normalized, rank
from .store import SQLiteStore, utc_now


def new_id(prefix):
    return prefix + uuid.uuid4().hex


class LaMemoryService:
    """用户级长期记录共享，会话原文和临时要求隔离；不自动启动 worker。"""
    def __init__(self, config=None, *, user_id="local-user", session_id=None, provider=None):
        self.config = config or LaMemoryConfig()
        self.user_id = self._identity(user_id)
        self.session_id = self._identity(session_id or new_id("session_"))
        self.provider = provider
        self.db = SQLiteStore(self.config.database_path)
        self.last_context = None
        self._retrieval_cache = None
        self._processing = False
        with self.db.connection:
            self.db.execute("INSERT OR IGNORE INTO lamemory_sessions VALUES (?,?,?)",
                            (self.user_id, self.session_id, utc_now()))
        last = self.db.one("SELECT turn_id FROM lamemory_messages WHERE user_id=? AND session_id=? ORDER BY seq DESC LIMIT 1",
                           (self.user_id, self.session_id))
        self.current_turn_id = last["turn_id"] if last else None

    @staticmethod
    def _identity(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 200:
            raise ValueError("用户/会话标识必须为非空短字符串")
        return value

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def history(self):
        return self.db.messages(self.user_id, self.session_id)

    def append_message(self, message, *, turn_id=None):
        """保存原始消息，返回宿主生成的来源 ID，不调用模型。"""
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant", "tool", "system"}:
            raise ValueError("消息必须包含有效 role")
        if message.get("tool_calls") and message["role"] != "assistant":
            raise ValueError("只有 assistant 可以发起工具调用")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise ValueError("初版仅支持文本消息 content")
        payload = json.dumps(message, ensure_ascii=False, allow_nan=False)
        if message["role"] == "user":
            turn = turn_id or new_id("turn_")
        else:
            turn = turn_id or self.current_turn_id
            if not turn:
                raise ValueError("请先追加用户消息")
        turn = self._identity(turn)
        message_id = new_id("msg_")
        with self.db.connection:
            committed = self.db.one("SELECT committed_at FROM lamemory_turns WHERE user_id=? AND session_id=? AND turn_id=?",
                                    (self.user_id, self.session_id, turn))
            if committed and committed["committed_at"]:
                raise ValueError("不能向已提交轮次追加消息")
            self.db.execute("INSERT OR IGNORE INTO lamemory_turns VALUES (?,?,?,NULL)",
                            (self.user_id, self.session_id, turn))
            seq = self.db.one("SELECT COALESCE(MAX(seq),0)+1 AS n FROM lamemory_messages WHERE user_id=? AND session_id=?",
                              (self.user_id, self.session_id))["n"]
            self.db.execute("INSERT INTO lamemory_messages VALUES (?,?,?,?,?,?,?,?)",
                            (message_id, self.user_id, self.session_id, turn, seq, message["role"], payload, utc_now()))
        self.current_turn_id = turn
        if message["role"] == "user":
            self._retrieval_cache = None
        return message_id

    def commit_turn(self, turn_id=None):
        """在同一事务中标记完整轮次、创建唯一提取任务。"""
        turn = turn_id or self.current_turn_id
        key = f"extract:{self.session_id}:{turn}"
        job_id = new_id("job_")
        with self.db.connection:
            existing = self.db.one("SELECT job_id FROM lamemory_jobs WHERE user_id=? AND idempotency_key=?", (self.user_id, key))
            if existing:
                return existing["job_id"]
            messages = self.db.messages(self.user_id, self.session_id, turn)
            if not valid_chain([m["payload"] for m in messages], complete=True):
                raise ValueError("只有包含最终助手回复和完整工具链的轮次可以提交")
            self.db.execute("UPDATE lamemory_turns SET committed_at=? WHERE user_id=? AND session_id=? AND turn_id=?",
                            (utc_now(), self.user_id, self.session_id, turn))
            self.db.execute("INSERT INTO lamemory_jobs VALUES (?,?,?,?,? ,?,'pending',0,?,NULL,?)",
                            (job_id, self.user_id, self.session_id, turn, "extract", key, time.time(), "{}"))
        return job_id

    def _visible(self, item):
        return item and item["user_id"] == self.user_id and (
            item["scope_kind"] == "user" and item["scope_key"] == self.user_id or
            item["scope_kind"] == "session" and item["scope_key"] == self.session_id)

    def _row(self, memory_id):
        row = self.db.one("SELECT * FROM lamemory_items WHERE memory_id=? AND user_id=?", (memory_id, self.user_id))
        if not self._visible(row):
            raise KeyError("记忆不存在或对当前会话不可见")
        return row

    def _item(self, row):
        sources = self.db.rows("SELECT message_id FROM lamemory_sources WHERE user_id=? AND memory_id=? ORDER BY message_id",
                               (self.user_id, row["memory_id"]))
        return MemoryItem(**row, source_message_ids=[s["message_id"] for s in sources])

    def _check_sources(self, sources):
        for source in sources:
            if not self.db.one("SELECT 1 FROM lamemory_messages WHERE user_id=? AND message_id=?", (self.user_id, source)):
                raise ValueError("来源不存在或不属于当前用户")

    def store(self, content, source_message_ids=None, *, type="fact", scope_kind="user", slot_key=None,
              occurred_at=None, origin="explicit", correction=False):
        """校验并持久化内容；返回 saved/duplicate/conflict/suppressed。"""
        if origin not in {"explicit", "extracted", "legacy_import"}:
            raise ValueError("无效的记忆 origin")
        candidate = validate_candidate({"content": content, "source_message_ids": source_message_ids or [],
                                       "type": type, "scope_kind": scope_kind, "slot_key": slot_key,
                                       "occurred_at": occurred_at, "correction": correction}, self.config)
        with self.db.connection:
            result = self._save(candidate, origin)
        self._retrieval_cache = None
        return result

    def _save(self, candidate, origin, *, replaces=None):
        """调用方持有短事务；同一抽取批次全部成功或全部回滚。"""
        sources = candidate["source_message_ids"]
        self._check_sources(sources)
        scope = candidate["scope_kind"]
        scope_key = self.user_id if scope == "user" else self.session_id
        digest = hashlib.sha256(normalized(candidate["content"]).encode()).hexdigest()
        slot = candidate["slot_key"]
        params = (self.user_id, scope, scope_key)
        if origin == "extracted":
            explicit = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND scope_kind=? AND scope_key=? AND content_hash=? AND origin='explicit' AND status='active'",
                                    (*params, digest))
            for item in explicit:
                if set(sources) & set(self._item(item).source_message_ids):
                    self._link_sources(item["memory_id"], sources)
                    return WriteResult("duplicate", self._item(item))
        suppressions = self.db.rows("SELECT * FROM lamemory_suppressions WHERE user_id=? AND scope_kind=? AND scope_key=?", params)
        blocked = [s for s in suppressions if s["content_hash"] == digest or
                   slot and s["slot_key"] == slot or s["source_key"] and s["source_key"] in sources]
        if origin != "explicit" and blocked:
            return WriteResult("suppressed", None)
        if origin != "explicit":
            historical = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND scope_kind=? AND scope_key=? AND content_hash=? AND status IN ('superseded','deleted')",
                                      (*params, digest))
            if any(set(sources) & set(self._item(item).source_message_ids) for item in historical):
                return WriteResult("suppressed", None)
        if origin == "explicit":
            # Only an explicit new save can lift the matching suppression, never extraction replay.
            if blocked and sources:
                newest_source = max(self.db.one("SELECT created_at FROM lamemory_messages WHERE user_id=? AND message_id=?",
                                                (self.user_id, source))["created_at"] for source in sources)
                if newest_source <= max(s["created_at"] for s in blocked):
                    return WriteResult("suppressed", None)
            for s in blocked:
                self.db.execute("DELETE FROM lamemory_suppressions WHERE user_id=? AND scope_kind=? AND scope_key=? AND source_key=? AND slot_key=? AND content_hash=?",
                                (*params, s["source_key"], s["slot_key"], s["content_hash"]))
        current = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND scope_kind=? AND scope_key=? AND type=? AND status IN ('active','pending')",
                               (*params, candidate["type"]))
        for item in current:
            same = item["content_hash"] == digest and item["occurred_at"] == candidate["occurred_at"]
            if candidate["type"] == "event" and candidate["occurred_at"] is None:
                same = same and bool(set(sources) & set(self._item(item).source_message_ids))
            if same and replaces is None and not (item["status"] == "pending" and candidate["correction"]):
                if item["slot_key"] is None and slot is not None:
                    self.db.execute("UPDATE lamemory_items SET slot_key=? WHERE user_id=? AND memory_id=?", (slot, self.user_id, item["memory_id"]))
                    item["slot_key"] = slot
                self._link_sources(item["memory_id"], sources)
                return WriteResult("duplicate" if item["status"] == "active" else "conflict", self._item(item))
        conflicts = [i for i in current if slot and i["slot_key"] == slot and i["status"] == "active"]
        if replaces is None and conflicts and candidate["type"] != "event" and candidate["correction"]:
            if len(conflicts) != 1:
                raise ValueError("slot 存在多个当前版本，需要显式指定更新 ID")
            replaces = conflicts[0]
        status = "pending" if conflicts and replaces is None and candidate["type"] != "event" else "active"
        memory_id = new_id("lam_")
        now = utc_now()
        self.db.execute("INSERT INTO lamemory_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (memory_id, self.user_id, scope, scope_key, candidate["type"], candidate["content"], digest, slot,
                         candidate["occurred_at"], status, replaces["revision"] + 1 if replaces else 1,
                         None, origin, now, now))
        self._link_sources(memory_id, sources)
        if replaces:
            self.db.execute("UPDATE lamemory_items SET status='superseded',superseded_by=?,updated_at=? WHERE user_id=? AND memory_id=?",
                            (memory_id, now, self.user_id, replaces["memory_id"]))
            self._invalidate_views()
        if status == "active":
            self.db.execute("UPDATE lamemory_items SET status='superseded',superseded_by=?,updated_at=? WHERE user_id=? AND scope_kind=? AND scope_key=? AND type=? AND content_hash=? AND status='pending'",
                            (memory_id, now, *params, candidate["type"], digest))
        return WriteResult("conflict" if status == "pending" else "saved", self._item(self._row(memory_id)))

    def _link_sources(self, memory_id, sources):
        self.db.connection.executemany("INSERT OR IGNORE INTO lamemory_sources VALUES (?,?,?)",
                                       [(self.user_id, memory_id, source) for source in sources])

    def _invalidate_views(self):
        self.db.execute("DELETE FROM lamemory_working_views WHERE user_id=?", (self.user_id,))
        self._retrieval_cache = None

    def update(self, memory_id, content, source_message_ids=None):
        """创建替代版本，保留旧证据；只允许修改当前有效记录。"""
        with self.db.connection:
            old = self._row(memory_id)
            if old["status"] not in {"active", "pending"}:
                raise ValueError("只能更正当前有效记录或待裁决记录")
            candidate = validate_candidate({"content": content, "type": old["type"], "scope_kind": old["scope_kind"],
                                            "slot_key": old["slot_key"], "occurred_at": old["occurred_at"],
                                            "source_message_ids": source_message_ids or []}, self.config)
            if old["status"] == "pending":
                raise ValueError("待裁决记录请先忘记，再通过当前有效记录 ID 更正")
            result = self._save(candidate, "explicit", replaces=old)
        return result

    def forget(self, memory_id):
        """停止使用该记录和旧版本，保留原始会话与重放抑制标记。"""
        with self.db.connection:
            item = self._row(memory_id)
            # Forget the whole predecessor lineage, so retrying an older source cannot restore it.
            lineage = [item]
            for row in lineage:
                lineage.extend(self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND superseded_by=?",
                                            (self.user_id, row["memory_id"])))
            for row in lineage:
                sources = self._item(row).source_message_ids or [""]
                for source in sources:
                    self.db.execute("INSERT OR IGNORE INTO lamemory_suppressions VALUES (?,?,?,?,?,?,?)",
                                    (self.user_id, row["scope_kind"], row["scope_key"], source, row["slot_key"] or "", row["content_hash"], utc_now()))
                self.db.execute("UPDATE lamemory_items SET status='deleted',updated_at=? WHERE user_id=? AND memory_id=?",
                                (utc_now(), self.user_id, row["memory_id"]))
            self._invalidate_views()
        return {"status": "forgotten", "memory_id": memory_id, "raw_messages_retained": True}

    def inspect(self, memory_id):
        item = self._item(self._row(memory_id))
        sources = []
        for source in item.source_message_ids:
            row = self.db.one("SELECT * FROM lamemory_messages WHERE user_id=? AND message_id=?", (self.user_id, source))
            row["payload"] = json.loads(row.pop("payload_json"))
            sources.append(row)
        lineage = [self._row(memory_id)]
        seen = {memory_id}
        for row in lineage:
            neighbors = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND (superseded_by=? OR memory_id=?)",
                                     (self.user_id, row["memory_id"], row["superseded_by"]))
            for neighbor in neighbors:
                if neighbor["memory_id"] not in seen and self._visible(neighbor):
                    seen.add(neighbor["memory_id"])
                    lineage.append(neighbor)
        versions = sorted((self._item(row).to_dict() for row in lineage), key=lambda v: (v["revision"], v["created_at"]))
        return {"item": item.to_dict(), "sources": sources, "versions": versions}

    def search(self, query, filters=None, top_k=None):
        """在可信身份限定后执行关键词排序，默认仅返回 active 记录。"""
        if not isinstance(query, str) or len(query) > self.config.max_content_chars:
            raise ValueError("查询必须为限长字符串")
        if not query.strip():
            return []
        filters = filters or {}
        allowed = {"type", "scope_kind", "status", "occurred_after", "occurred_before"}
        if not isinstance(filters, dict) or set(filters) - allowed:
            raise ValueError("不支持的检索过滤条件")
        limit = self.config.top_k if top_k is None else top_k
        if not isinstance(limit, int) or not 0 <= limit <= 100:
            raise ValueError("top_k 必须为 0 到 100")
        status = filters.get("status", "active")
        if status not in {"active", "pending", "deleted", "superseded"}:
            raise ValueError("无效的状态过滤条件")
        rows = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND status=? AND ((scope_kind='user' AND scope_key=?) OR (scope_kind='session' AND scope_key=?))",
                            (self.user_id, status, self.user_id, self.session_id))
        items = []
        after = normalize_time(filters.get("occurred_after"))
        before = normalize_time(filters.get("occurred_before"))
        for row in rows:
            if any(row[k] != filters[k] for k in ("type", "scope_kind") if k in filters):
                continue
            occurred = row["occurred_at"]
            if after and (occurred is None or occurred < after):
                continue
            if before and (occurred is None or occurred > before):
                continue
            items.append(self._item(row))
        return [SearchHit(item, score, reason) for item, score, reason in rank(query, items)[:limit]]

    def build_context(self, system_prompt="", budget=None, *, tools=None):
        rows = self.history()
        query = next((r["payload"].get("content", "") for r in reversed(rows) if r["role"] == "user"), "")
        query = query or ""
        query_truncated = len(query) > self.config.max_content_chars
        query = query[:self.config.max_content_chars]
        # Cache only retrieval within a turn. Writes and source appends invalidate it.
        cache_key = (self.current_turn_id, query, self.db.execute("PRAGMA data_version").fetchone()[0])
        if self._retrieval_cache is None or self._retrieval_cache[0] != cache_key:
            self._retrieval_cache = (cache_key, self.search(query))
        view = self.db.one("SELECT * FROM lamemory_working_views WHERE user_id=? AND session_id=?", (self.user_id, self.session_id))
        self.last_context = build_context(rows, self._retrieval_cache[1], system_prompt, self.config,
                                          budget=budget, tools=tools, working_view=view)
        self.last_context.diagnostics["retrieval_query_truncated"] = query_truncated
        return self.last_context

    def jobs(self):
        return self.db.rows("SELECT * FROM lamemory_jobs WHERE user_id=? AND session_id=? ORDER BY available_at,job_id",
                            (self.user_id, self.session_id))

    def retry_failed(self, job_id):
        with self.db.connection:
            changed = self.db.execute("UPDATE lamemory_jobs SET status='pending',attempts=0,available_at=?,last_error=NULL WHERE user_id=? AND session_id=? AND job_id=? AND status='failed'",
                                      (time.time(), self.user_id, self.session_id, job_id)).rowcount
        return bool(changed)

    def process_pending(self, max_jobs=1, *, deadline=None):
        if not isinstance(max_jobs, int) or max_jobs < 0:
            raise ValueError("max_jobs 必须为非负整数")
        if self._processing:
            raise RuntimeError("当前服务已经在消费任务；初版只支持串行 worker")
        self._processing = True
        results = []
        try:
            pending = [j for j in self.jobs() if j["status"] == "pending" and j["available_at"] <= time.time()][:max_jobs]
            for job in pending:
                remaining = deadline - time.monotonic() if deadline else self.config.provider_timeout_seconds
                if remaining <= 0:
                    break
                attempt = job["attempts"] + 1
                try:
                    if self.provider is None:
                        raise RuntimeError("未配置记忆提取 provider")
                    messages = self.db.messages(self.user_id, self.session_id, job["turn_id"])
                    result = self.provider.extract(messages, max_items=self.config.max_extracted_items,
                                                   timeout=min(self.config.provider_timeout_seconds, remaining))
                    candidates = validate_batch(result, messages, self.config)
                    with self.db.connection:
                        for candidate in candidates:
                            self._save(candidate, "extracted")
                        self.db.execute("UPDATE lamemory_jobs SET status='completed',attempts=?,last_error=NULL WHERE job_id=? AND user_id=?",
                                        (attempt, job["job_id"], self.user_id))
                    self._retrieval_cache = None
                except Exception as exc:
                    status = "failed" if attempt >= self.config.job_max_attempts else "pending"
                    with self.db.connection:
                        self.db.execute("UPDATE lamemory_jobs SET status=?,attempts=?,available_at=?,last_error=? WHERE job_id=? AND user_id=?",
                                        (status, attempt, time.time() + self.config.retry_delay_seconds * 2 ** (attempt - 1),
                                         f"{type(exc).__name__}: {exc}"[:2000], job["job_id"], self.user_id))
                results.append(next(j for j in self.jobs() if j["job_id"] == job["job_id"]))
        finally:
            self._processing = False
        return results

    def flush(self, timeout=30):
        if timeout < 0:
            raise ValueError("timeout 不能为负数")
        deadline = time.monotonic() + timeout
        # Snapshot target IDs: future turns are not part of this flush.
        targets = {j["job_id"] for j in self.jobs()}
        while True:
            jobs = [j for j in self.jobs() if j["job_id"] in targets]
            failed = sum(j["status"] == "failed" for j in jobs)
            remaining = sum(j["status"] == "pending" for j in jobs)
            completed = sum(j["status"] == "completed" for j in jobs)
            if not remaining:
                return FlushResult("failed" if failed else "complete", completed, failed, 0)
            if time.monotonic() >= deadline:
                return FlushResult("timeout", completed, failed, remaining)
            self.process_pending(max_jobs=1, deadline=deadline)
            delay = max(0, min(0.05, deadline - time.monotonic()))
            if delay:
                time.sleep(delay)

    def compress(self, *, keep_turns=2):
        """可选摘要优化；失败保留原工作视图，永不改写原始对话。"""
        if keep_turns < 1:
            raise ValueError("至少保留一个完整轮次")
        rows = self.history()
        turns = list(dict.fromkeys(r["turn_id"] for r in rows))
        old_turns = set(turns[:-keep_turns])
        old = [r for r in rows if r["turn_id"] in old_turns]
        if not old:
            return {"status": "unchanged"}
        for turn in old_turns:
            if not valid_chain([r["payload"] for r in old if r["turn_id"] == turn], complete=True):
                return {"status": "failed", "error": "待压缩轮次不完整"}
        try:
            if self.provider is None:
                raise RuntimeError("未配置摘要 provider")
            result = self.provider.summarize([r["payload"] for r in old], timeout=self.config.provider_timeout_seconds)
            if not isinstance(result, dict) or not isinstance(result.get("summary"), str) or not result["summary"].strip():
                raise ValueError("摘要必须为非空字符串")
            if len(result["summary"]) > self.config.max_content_chars:
                raise ValueError("摘要过长")
            with self.db.connection:
                self.db.execute("INSERT OR REPLACE INTO lamemory_working_views VALUES (?,?,?,?,?)",
                                (self.user_id, self.session_id, old[-1]["seq"], result["summary"].strip(), utc_now()))
            return {"status": "compressed", "through_seq": old[-1]["seq"]}
        except Exception as exc:
            return {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
