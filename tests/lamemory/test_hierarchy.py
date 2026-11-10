"""M2：来源传播、隔离、摘要失败/重启、整理和分层预算。"""
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

from common import MemoryTestCase
from core.lamemory.store import SQLiteStore


class HierarchyCases(MemoryTestCase):
    def profile(self):
        return self.memory.db.one("SELECT summary_id FROM lamemory_summaries WHERE user_id='alice' AND kind='profile'")["summary_id"]

    def test_profile_tracks_revisions_and_forget_without_losing_raw_sources(self):
        source, _ = self.turn()
        old = self.memory.store("开发偏好中文", [source], type="preference").item
        self.memory.refresh_summaries()
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")
        summary_id = self.profile()
        summary = self.memory.inspect_summary(summary_id)
        self.assertEqual(summary["sources"][0]["source_revision"], 1)
        self.assertEqual(summary["sources"][0]["source_hash"], old.content_hash)
        self.assertIn("中文", self.memory.build_context().diagnostics["summaries"][0]["content"])
        new = self.memory.update(old.memory_id, "开发偏好英文", [source]).item
        self.assertEqual(self.memory.inspect_summary(summary_id)["dirty"], 1)
        self.assertEqual(self.memory.search_summaries("开发"), [])
        self.assertEqual(self.memory.build_context().diagnostics["summaries"], [])
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")
        summary = self.memory.inspect_summary(summary_id)
        self.assertNotIn("中文", summary["content"])
        self.assertEqual(summary["sources"][0]["memory_id"], new.memory_id)
        self.assertEqual(summary["sources"][0]["source_revision"], 2)
        self.memory.forget(new.memory_id)
        self.assertEqual(self.memory.search_summaries("开发"), [])
        self.memory.flush(timeout=1)
        self.assertEqual(self.memory.inspect_summary(summary_id)["content"], "")
        self.assertEqual(len(self.memory.history()), 2)

    def test_project_and_manual_topic_isolation(self):
        project = self.service(project_id="atlas")
        item = project.store("Atlas 使用 uv", scope_kind="project").item
        topic = project.define_topic("构建方案", [item.memory_id], project=True)
        project.refresh_summaries()
        project.flush(timeout=1)
        same = self.service(session="second", project_id="atlas")
        other = self.service(session="other", project_id="other")
        self.assertEqual(same.search("uv")[0].item.memory_id, item.memory_id)
        self.assertTrue(same.search_summaries("构建"))
        self.assertEqual(other.search("uv"), [])
        with self.assertRaises(KeyError):
            other.inspect_summary(topic)
        with self.assertRaises(KeyError):
            other.inspect(item.memory_id)
        self.assertNotIn("使用 uv", json.dumps(other.search_summaries("uv"), ensure_ascii=False))
        with self.assertRaises(ValueError):
            project.define_topic("泄露项目内容", [item.memory_id])
        stranger = self.service(user="bob", project_id="atlas")
        self.assertEqual(stranger.search_summaries("构建"), [])
        with self.assertRaises(KeyError):
            stranger.inspect_summary(topic)

    def test_manual_membership_follows_replacement_and_new_source_dirties_summary(self):
        source, _ = self.turn()
        old = self.memory.store("使用 pip", [source]).item
        topic = self.memory.define_topic("包管理", [old.memory_id])
        self.memory.flush(timeout=1)
        new = self.memory.update(old.memory_id, "使用 uv").item
        self.assertEqual(self.memory.inspect_summary(topic)["dirty"], 1)
        self.memory.flush(timeout=1)
        self.assertEqual(self.memory.inspect_summary(topic)["content"], "使用 uv")
        next_source, _ = self.turn()
        self.memory.store("使用 uv", [next_source])
        self.assertEqual(self.memory.inspect_summary(topic)["dirty"], 1)
        self.memory.flush(timeout=1)
        self.assertEqual(self.memory.inspect_summary(topic)["sources"][0]["memory_id"], new.memory_id)
        self.memory.define_topic("包管理", [])
        self.assertEqual(self.memory.search_summaries("包管理"), [])
        self.memory.flush(timeout=1)
        self.assertEqual(self.memory.inspect_summary(topic)["content"], "")

    def test_profile_excludes_events_and_session_instructions(self):
        self.memory.store("永久偏好中文", type="preference")
        self.memory.store("昨天下雨", type="event")
        self.memory.store("临时英文", scope_kind="session")
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        profile = self.memory.inspect_summary(self.profile())
        self.assertEqual(profile["content"], "永久偏好中文")
        with self.assertRaises(ValueError):
            self.memory.define_topic("临时", [self.memory.search("英文")[0].item.memory_id])

    def test_bad_provider_output_retains_dirty_content_and_retry_is_observable(self):
        item = self.memory.store("偏好中文").item
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        self.memory.update(item.memory_id, "偏好英文")
        self.provider.summarize_hierarchy = Mock(return_value={"summary": []})
        self.assertEqual(self.memory.flush(timeout=1).status, "failed")
        row = self.memory.inspect_summary(self.profile())
        self.assertEqual(row["content"], "偏好中文")
        self.assertEqual(row["dirty"], 1)
        self.assertEqual(self.memory.search_summaries("中文"), [])
        failed = next(j for j in self.memory.jobs() if j["status"] == "failed")
        resumed = self.service(session="maintenance")
        self.assertTrue(resumed.retry_failed(failed["job_id"]))
        self.provider.summarize_hierarchy.return_value = {"summary": "偏好英文"}
        self.assertEqual(resumed.flush(timeout=1).status, "complete")
        self.assertEqual(resumed.inspect_summary(row["summary_id"])["content"], "偏好英文")

    def test_provider_race_never_publishes_old_snapshot(self):
        item = self.memory.store("使用 pip").item
        self.memory.refresh_summaries()
        def summarize(data, *, timeout):
            self.assertFalse(self.memory.db.connection.in_transaction)
            self.memory.update(item.memory_id, "使用 uv")
            self.provider.summarize_hierarchy = Mock(return_value={"summary": "使用 uv"})
            return {"summary": "使用 pip"}
        self.provider.summarize_hierarchy = summarize
        result = self.memory.process_pending(1)
        self.assertNotEqual(result[0]["status"], "completed")
        self.assertEqual(self.memory.search_summaries("pip"), [])
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")
        self.assertEqual(self.memory.inspect_summary(self.profile())["content"], "使用 uv")

    def test_flush_waits_for_summary_jobs_created_by_extraction(self):
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        _, turn = self.turn()
        self.memory.commit_turn(turn)
        result = self.memory.flush(timeout=1)
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.remaining, 0)
        self.assertIn("中文", self.memory.inspect_summary(self.profile())["content"])
        self.assertEqual(self.memory.inspect_summary(self.profile())["dirty"], 0)

    def test_consolidation_is_precise_idempotent_and_bounded(self):
        item = self.memory.store("开发使用 Python").item
        with self.memory.db.connection:
            self.memory.db.execute("INSERT INTO lamemory_items SELECT ?,user_id,scope_kind,scope_key,type,content,content_hash,slot_key,occurred_at,status,revision,superseded_by,origin,created_at,updated_at FROM lamemory_items WHERE memory_id=?", ("lam_duplicate", item.memory_id))
        self.memory.store("出差北京", type="event")
        self.memory.store("出差北京", type="event")
        result = self.memory.consolidate(max_jobs=0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(result["processed"], 0)
        self.assertGreater(result["dirty_summaries"], 0)
        self.assertEqual(len(self.memory.search("北京")), 2)
        result = self.memory.consolidate(max_jobs=10)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(result["dirty_summaries"], 0)
        revision = self.memory.inspect_summary(self.profile())["revision"]
        self.memory.consolidate(max_jobs=10)
        self.assertEqual(self.memory.inspect_summary(self.profile())["revision"], revision)

    def test_layers_share_memory_and_context_budgets(self):
        self.memory.store("开发使用中文")
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        self.memory.append_message({"role": "user", "content": "开发"})
        context = self.memory.build_context(budget=160)
        self.assertLessEqual(context.diagnostics["estimated_tokens"], 160)
        self.assertEqual(context.messages[-1]["content"], "开发")

    def test_new_topic_invalidates_profile_navigation(self):
        item = self.memory.store("偏好中文").item
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        topic = self.memory.define_topic("开发偏好", [item.memory_id])
        self.assertEqual(self.memory.inspect_summary(self.profile())["dirty"], 1)
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")
        profile = self.memory.inspect_summary(self.profile())
        self.assertIn("开发偏好", profile["content"])
        self.assertEqual(profile["navigation"][0]["summary_id"], topic)

    def test_many_sources_keep_context_navigation_bounded(self):
        for number in range(30):
            self.memory.store(f"稳定背景信息编号 {number}")
        self.memory.refresh_summaries()
        self.memory.flush(timeout=1)
        self.memory.append_message({"role": "user", "content": "你好"})
        context = self.memory.build_context()
        layer = context.diagnostics["summaries"][0]
        self.assertEqual(layer["source_count"], 30)
        self.assertEqual(len(layer["sources"]), self.memory.config.top_k)
        self.assertEqual(len(self.memory.inspect_summary(layer["summary_id"])["sources"]), 30)

    def test_topic_selection_ranks_relevance_before_profile_navigation(self):
        memory = self.service(top_k=1)
        generic = memory.store("构建工具 Python").item
        specific = memory.store("构建工具 uv").item
        memory.define_topic("a.通用", [generic.memory_id])
        selected = memory.define_topic("b.uv", [specific.memory_id])
        memory.refresh_summaries()
        memory.flush(timeout=1)
        summaries = memory.search_summaries("构建 uv")
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["summary_id"], selected)
        self.assertEqual(summaries[0]["mode"], "keyword")
        self.assertIn("uv", summaries[0]["reason"])

    def test_v1_database_upgrades_without_losing_sources_or_jobs(self):
        path = Path(self.tmp.name) / "v1.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript((Path(__file__).parents[2] / "core/lamemory/migrations/001_initial.sql").read_text())
            db.execute("INSERT INTO lamemory_migrations VALUES (1,'old')")
            db.execute("INSERT INTO lamemory_sessions VALUES ('alice','first','old')")
            db.execute("INSERT INTO lamemory_turns VALUES ('alice','first','turn','old')")
            db.execute("INSERT INTO lamemory_messages VALUES ('msg','alice','first','turn',1,'user','{}','old')")
            db.execute("INSERT INTO lamemory_items VALUES ('lam_old','alice','user','alice','fact','旧事实','hash',NULL,NULL,'active',1,NULL,'explicit','old','old')")
            db.execute("INSERT INTO lamemory_sources VALUES ('alice','lam_old','msg')")
            db.execute("INSERT INTO lamemory_jobs VALUES ('job','alice','first','turn','extract','key','pending',0,0,NULL,'{}')")
        db.close()
        upgraded = SQLiteStore(path)
        self.addCleanup(upgraded.close)
        self.assertEqual([r["version"] for r in upgraded.rows("SELECT version FROM lamemory_migrations ORDER BY version")], [1, 2, 3, 4])
        self.assertEqual(upgraded.one("SELECT * FROM lamemory_sources")["message_id"], "msg")
        self.assertEqual(upgraded.one("SELECT * FROM lamemory_jobs")["status"], "pending")
        self.assertEqual(upgraded.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertEqual(upgraded.rows("PRAGMA foreign_key_check"), [])
        with self.assertRaises(sqlite3.IntegrityError):
            with upgraded.connection:
                upgraded.execute("INSERT INTO lamemory_sources VALUES ('bob','lam_old','msg')")
