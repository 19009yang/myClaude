"""验证模型故障、状态版本、工作视图和工具链边界。"""
import json
from unittest.mock import Mock

from common import MemoryTestCase
from core.lamemory import BudgetError
from core.memory import Memory


class FailureCases(MemoryTestCase):
    def test_malformed_batch_rolls_back_and_exhausts(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        self.provider.extract = lambda *a, **k: [
            {"content": "不应部分提交", "source_message_ids": [source]},
            {"content": "非法来源", "source_message_ids": ["foreign"]}]
        result = self.memory.flush(timeout=2)
        self.assertEqual(result.status, "failed")
        self.assertEqual(self.memory.search("提交"), [])
        job = self.memory.jobs()[0]
        self.assertEqual(job["attempts"], 3)
        self.assertIn("来源", job["last_error"])
        self.assertTrue(self.memory.retry_failed(job["job_id"]))

    def test_restart_recovers_pending_jobs(self):
        _, turn = self.turn()
        job = self.memory.commit_turn(turn)
        resumed = self.service()
        self.assertEqual(resumed.jobs()[0]["job_id"], job)
        self.assertEqual(resumed.flush(timeout=1).status, "complete")
        self.assertEqual(len(resumed.history()), 2)

    def test_slot_conflict_and_explicit_correction(self):
        old = self.memory.store("使用 pip", slot_key="package.manager")
        conflict = self.memory.store("使用 uv", slot_key="package.manager")
        self.assertEqual(conflict.status, "conflict")
        self.assertEqual(self.memory.search("uv"), [])
        new = self.memory.store("使用 uv", slot_key="package.manager", correction=True)
        self.assertEqual(new.status, "saved")
        self.assertEqual(self.memory.search("pip"), [])
        self.assertEqual(self.memory.inspect(old.item.memory_id)["item"]["superseded_by"], new.item.memory_id)

    def test_no_slot_update_old_evidence_stays_historical(self):
        source, _ = self.turn()
        old = self.memory.store("偏好中文", [source])
        self.memory.update(old.item.memory_id, "偏好英文", [source])
        self.assertEqual(self.memory.store("偏好中文", [source], origin="extracted").status, "suppressed")
        self.assertEqual(self.memory.search("中文"), [])

    def test_tool_chain_budget_and_crash_archive(self):
        self.memory.append_message({"role": "user", "content": "旧的未完成任务"})
        self.memory.append_message({"role": "assistant", "content": "", "tool_calls": [
            {"id": "unfinished", "type": "function", "function": {"name": "read", "arguments": "{}"}}]})
        with self.assertRaises(ValueError):
            self.memory.build_context("系统")
        self.memory.append_message({"role": "user", "content": "新任务"})
        calls = [{"id": n, "type": "function", "function": {"name": "read", "arguments": "{}"}} for n in ("a", "b")]
        self.memory.append_message({"role": "assistant", "content": "", "tool_calls": calls})
        for call in calls:
            self.memory.append_message({"role": "tool", "tool_call_id": call["id"], "content": "结果" * 1000})
        context = self.memory.build_context("系统", budget=400)
        messages = context.messages
        self.assertEqual([m["tool_call_id"] for m in messages if m["role"] == "tool"], ["a", "b"])
        self.assertNotIn("旧的未完成任务", json.dumps(messages, ensure_ascii=False))
        self.assertEqual(len(self.memory.history()), 6)
        self.assertGreater(context.diagnostics["truncated_tool_views"], 0)

    def test_irreducible_budget_error(self):
        self.memory.append_message({"role": "user", "content": "不能截断当前请求" * 300})
        with self.assertRaises(BudgetError):
            self.memory.build_context("系统", budget=100)
        with self.assertRaises(BudgetError):
            self.memory.build_context("系统", budget=100, tools=[{"description": "x" * 2000}])

    def test_summary_validation_and_array_update_regression(self):
        for _ in range(4):
            self.turn()
        self.provider.summarize = Mock(return_value={"summary": "有效历史摘要", "memory_update": ["兼容旧数组"]})
        self.assertEqual(self.memory.compress()["status"], "compressed")
        first = self.memory.build_context("系统").messages
        self.provider.summarize.return_value = {"summary": []}
        self.assertEqual(self.memory.compress()["status"], "failed")
        self.assertEqual(self.memory.build_context("系统").messages, first)
        self.assertEqual(len(self.memory.history()), 8)

    def test_usage_independent_compatibility_and_post_reply_extraction(self):
        facade = Memory(database_path=self.path, user_id="facade", session_id="one", provider=self.provider)
        self.addCleanup(facade.close)
        facade.add_message({"role": "user", "content": "开发回答使用中文"})
        facade.add_message({"role": "assistant", "content": "已经回复", "usage": {"total_tokens": 0}})
        self.assertEqual(self.provider.calls, 0)
        self.assertEqual(len(facade.service.jobs()), 1)
        self.provider.fail = True
        facade.process_pending()
        self.assertEqual(facade.messages[-1]["content"], "已经回复")

    def test_untrusted_identity_and_time_rejected(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        self.provider.extract = lambda *a, **k: [{"content": "跨用户", "user_id": "bob", "source_message_ids": [source]}]
        self.memory.process_pending()
        self.assertIn("不受信任", self.memory.jobs()[0]["last_error"])
        with self.assertRaises(ValueError):
            self.memory.store("事件", type="event", occurred_at="2026-01-01T12:00:00")
        with self.assertRaises(ValueError):
            self.memory.store("事件", type="event", occurred_at="2026-02-30")

    def test_working_summary_invalidated_on_update(self):
        for _ in range(4):
            self.turn()
        self.provider.summarize = Mock(return_value={"summary": "历史偏好中文"})
        self.memory.compress()
        item = self.memory.store("偏好中文")
        self.memory.update(item.item.memory_id, "偏好英文")
        self.assertIsNone(self.memory.db.one("SELECT * FROM lamemory_working_views WHERE user_id=?", (self.memory.user_id,)))

    def test_flush_timeout_does_not_claim_success(self):
        _, turn = self.turn()
        self.memory.commit_turn(turn)
        result = self.memory.flush(timeout=0)
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.remaining, 1)
