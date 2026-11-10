"""按固定项目标签/手动主题整理；失效标记在来源写事务内传播。"""
import hashlib
import json
import uuid

from .retrieval import terms
from .store import utc_now


class Hierarchy:
    def __init__(self, service):
        self.service = service
        self.db = service.db
        self.user = service.user_id

    def define(self, kind, topic_key, *, project_id=None, memory_ids=None):
        if kind not in {"profile", "topic"}:
            raise ValueError("无效的摘要类型")
        self.service._identity(topic_key)
        if kind == "profile" and (topic_key != "profile" or project_id or memory_ids):
            raise ValueError("用户概览使用固定 profile 键及稳定用户记录")
        if project_id is not None and project_id != self.service.config.project_id:
            raise ValueError("项目摘要只能绑定当前项目")
        if memory_ids is not None:
            if not isinstance(memory_ids, list) or not all(isinstance(i, str) for i in memory_ids):
                raise ValueError("主题来源必须为记忆 ID 数组")
            for memory_id in memory_ids:
                item = self.service._row(memory_id)
                if item["scope_kind"] == "session" or item["status"] != "active":
                    raise ValueError("跨会话主题只能使用有效的用户或当前项目记忆")
                if item["scope_kind"] == "project" and item["scope_key"] != project_id:
                    raise ValueError("项目记忆只能加入绑定同一项目的主题")
        with self.db.connection:
            existing = self.db.one("SELECT * FROM lamemory_summaries WHERE user_id=? AND kind=? AND topic_key=?",
                                   (self.user, kind, topic_key))
            if existing and existing["project_id"] != project_id:
                raise ValueError("主题已绑定其他范围")
            summary_id = existing["summary_id"] if existing else "sum_" + uuid.uuid4().hex
            if not existing:
                self.db.execute("INSERT INTO lamemory_summaries(summary_id,user_id,kind,topic_key,project_id,updated_at) VALUES (?,?,?,?,?,?)",
                                (summary_id, self.user, kind, topic_key, project_id, utc_now()))
                if kind == "topic":
                    self.db.execute("UPDATE lamemory_summaries SET dirty=1,generation=generation+1 WHERE user_id=? AND kind='profile'", (self.user,))
            if memory_ids is not None:
                old = {r["memory_id"] for r in self.db.rows("SELECT memory_id FROM lamemory_topic_items WHERE user_id=? AND summary_id=?", (self.user, summary_id))}
                if old != set(memory_ids):
                    self.db.execute("DELETE FROM lamemory_topic_items WHERE user_id=? AND summary_id=?", (self.user, summary_id))
                    for memory_id in set(memory_ids):
                        self.db.execute("INSERT INTO lamemory_topic_items VALUES (?,?,?)", (self.user, summary_id, memory_id))
                    self.db.execute("UPDATE lamemory_summaries SET dirty=1,generation=generation+1 WHERE user_id=? AND summary_id=?", (self.user, summary_id))
            self.enqueue_dirty()
        return summary_id

    def refresh(self):
        """显式启用用户概览及当前项目摘要；以后仅刷新脏定义。"""
        self.define("profile", "profile")
        if self.service.config.project_id:
            self.define("topic", "project:" + self.service.config.project_id,
                        project_id=self.service.config.project_id)
        with self.db.connection:
            return self.enqueue_dirty()

    def enqueue_dirty(self):
        jobs = []
        for row in self.db.rows("SELECT * FROM lamemory_summaries WHERE user_id=? AND dirty=1", (self.user,)):
            jobs.append(self.service._enqueue_job("summary", f"summary:{row['summary_id']}:{row['generation']}",
                                                  {"summary_id": row["summary_id"], "generation": row["generation"]}))
        return jobs

    def _row(self, summary_id):
        row = self.db.one("SELECT * FROM lamemory_summaries WHERE user_id=? AND summary_id=?", (self.user, summary_id))
        if not row:
            raise KeyError("摘要不存在")
        return row

    def snapshot(self, row):
        if row["kind"] == "profile":
            items = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND status='active' AND scope_kind='user' AND type IN ('fact','preference') ORDER BY memory_id", (self.user,))
        else:
            # Membership follows the replacement lineage via the database trigger.
            items = self.db.rows("SELECT * FROM lamemory_items WHERE user_id=? AND status='active' AND scope_kind!='session' AND ((scope_kind='project' AND scope_key=?) OR memory_id IN (SELECT memory_id FROM lamemory_topic_items WHERE user_id=? AND summary_id=?) AND (scope_kind='user' OR scope_kind='project' AND scope_key=?)) ORDER BY memory_id",
                                 (self.user, row["project_id"], self.user, row["summary_id"], row["project_id"]))
        # Navigation contains labels only, never project facts from other contexts.
        navigation = []
        topics = []
        if row["kind"] == "profile":
            navigation = [r["scope_key"] for r in self.db.rows("SELECT DISTINCT scope_key FROM lamemory_items WHERE user_id=? AND status='active' AND scope_kind='project' ORDER BY scope_key", (self.user,))]
            topics = self.db.rows("SELECT summary_id,topic_key FROM lamemory_summaries WHERE user_id=? AND kind='topic' ORDER BY topic_key", (self.user,))
        sources = [{"memory_id": i["memory_id"], "revision": i["revision"], "content_hash": i["content_hash"],
                    "content": i["content"], "source_message_ids": self.service._item(i).source_message_ids} for i in items]
        data = {"kind": row["kind"], "topic": row["topic_key"], "sources": sources,
                "project_navigation": navigation, "topic_navigation": topics}
        signature = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return data, signature

    def rebuild(self, payload, *, timeout):
        row = self._row(payload["summary_id"])
        if not row["dirty"] or row["generation"] != payload["generation"]:
            return  # An obsolete job is complete; a newer generation owns the work.
        data, signature = self.snapshot(row)
        if signature == row["source_signature"]:
            content = row["content"]
        elif not data["sources"] and not data["project_navigation"] and not data["topic_navigation"]:
            content = ""
        elif callable(getattr(self.service.provider, "summarize_hierarchy", None)):
            result = self.service.provider.summarize_hierarchy(
                {**data, "max_summary_chars": self.service.config.max_summary_chars}, timeout=timeout)
            if not isinstance(result, dict) or set(result) != {"summary"}:
                raise ValueError("分层摘要必须返回 summary 对象")
            content = result["summary"]
            if not isinstance(content, str) or not content.strip() or len(content) > min(self.service.config.max_content_chars, self.service.config.max_summary_chars):
                raise ValueError("分层摘要为空或过长")
            content = content.strip()
        else:
            # An extractive fallback works offline and does not invent new facts.
            lines = [s["content"] for s in data["sources"]]
            if data["project_navigation"]:
                lines.append("项目导航：" + "、".join(data["project_navigation"]))
            if data["topic_navigation"]:
                lines.append("主题导航：" + "、".join(t["topic_key"] for t in data["topic_navigation"]))
            content = "\n".join(lines)[:min(self.service.config.max_content_chars, self.service.config.max_summary_chars)]
        with self.db.transaction():
            current = self._row(row["summary_id"])
            if current["generation"] != row["generation"] or self.snapshot(current)[1] != signature:
                raise RuntimeError("摘要生成期间来源已变更，等待新版本重建")
            self.db.execute("DELETE FROM lamemory_summary_sources WHERE user_id=? AND summary_id=?", (self.user, row["summary_id"]))
            for source in data["sources"]:
                self.db.execute("INSERT INTO lamemory_summary_sources VALUES (?,?,?,?,?)",
                                (self.user, row["summary_id"], source["memory_id"], source["revision"], source["content_hash"]))
            self.db.execute("UPDATE lamemory_summaries SET content=?,revision=revision+1,dirty=0,source_signature=?,updated_at=? WHERE user_id=? AND summary_id=?",
                            (content, signature, utc_now(), self.user, row["summary_id"]))

    def inspect(self, summary_id):
        row = self._row(summary_id)
        if row["project_id"] and row["project_id"] != self.service.config.project_id:
            raise KeyError("摘要对当前项目不可见")
        row["sources"] = self.db.rows("SELECT memory_id,source_revision,source_hash FROM lamemory_summary_sources WHERE user_id=? AND summary_id=? ORDER BY memory_id", (self.user, summary_id))
        if row["kind"] == "profile":
            row["navigation"] = self.snapshot(row)[0]["topic_navigation"]
        return row

    def search(self, query, top_k=None):
        results = []
        query_terms = terms(query)
        for row in self.db.rows("SELECT * FROM lamemory_summaries WHERE user_id=? AND dirty=0 AND content!='' ORDER BY kind,topic_key", (self.user,)):
            if row["project_id"] and row["project_id"] != self.service.config.project_id:
                continue
            # A final signature check also guards against changes outside the service.
            if self.snapshot(row)[1] != row["source_signature"]:
                continue
            matched = query_terms & terms(row["topic_key"] + " " + row["content"])
            if row["kind"] == "profile" or matched:
                result = self.inspect(row["summary_id"])
                result.update(mode="keyword", score=len(matched) / max(1, len(query_terms)) if row["kind"] == "topic" else 0,
                              reason="主题关键词匹配：" + ", ".join(sorted(matched)) if row["kind"] == "topic" else "用户概览导航")
                results.append(result)
        return sorted(results, key=lambda r: (-r["score"], r["topic_key"]))[:self.service.config.top_k if top_k is None else top_k]
