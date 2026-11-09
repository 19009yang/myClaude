"""LaMemory 的行为回归；所有数据和模型均与真实会话隔离。"""
import json
import unittest
from common import MemoryTestCase


class MemoryAcceptance(MemoryTestCase):

    def test_cross_session_sources_and_isolation(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")
        other = self.service(session="second")
        hit = other.search("开发语言")[0]
        self.assertEqual(hit.item.source_message_ids, [source])
        self.assertEqual(other.inspect(hit.item.memory_id)["sources"][0]["payload"]["role"], "user")
        stranger = self.service(user="bob")
        self.assertEqual(stranger.search("中文"), [])
        with self.assertRaises(KeyError):
            stranger.inspect(hit.item.memory_id)
        with self.assertRaises(ValueError):
            stranger.store("偷用来源", [source])

    def test_idempotent_commit_dedup_and_retry(self):
        source, turn = self.turn()
        first = self.memory.commit_turn(turn)
        self.assertEqual(first, self.memory.commit_turn(turn))
        self.provider.fail = True
        self.assertEqual(self.memory.process_pending(1)[0]["status"], "pending")
        self.provider.fail = False
        self.assertEqual(self.memory.flush(timeout=2).status, "complete")
        self.assertEqual(len(self.memory.search("中文")), 1)
        duplicate = self.memory.store("开发回答使用中文", [source], type="preference")
        self.assertEqual(duplicate.status, "duplicate")

    def test_update_and_forget_do_not_resurrect(self):
        source, _ = self.turn()
        old = self.memory.store("回答用中文", [source], slot_key="answer.language")
        new = self.memory.update(old.item.memory_id, "回答用英文", [source])
        self.assertEqual(self.memory.inspect(old.item.memory_id)["item"]["status"], "superseded")
        self.assertEqual(self.memory.search("英文")[0].item.revision, 2)
        self.memory.forget(new.item.memory_id)
        self.assertEqual(self.memory.search("英文"), [])
        result = self.memory.store("回答用中文", [source], slot_key="answer.language", origin="extracted")
        self.assertEqual(result.status, "suppressed")
        self.assertEqual(self.memory.store("回答用中文", [source], slot_key="answer.language").status, "suppressed")
        new_source = self.memory.append_message({"role": "user", "content": "请重新记住回答用中文"})
        self.assertEqual(self.memory.store("回答用中文", [new_source], slot_key="answer.language").status, "saved")

    def test_session_and_event_time_filters(self):
        self.memory.store("临时使用英文", scope_kind="session")
        self.assertEqual(self.service(session="second").search("英文"), [])
        for date in ["2026-01-01", "2026-02-01"]:
            self.memory.store("出差北京", type="event", occurred_at=date)
        self.assertEqual(len(self.memory.search("北京")), 2)
        hits = self.memory.search("北京", filters={"occurred_after": "2026-02-01"})
        self.assertEqual(len(hits), 1)

    def test_context_budget_and_raw_evidence(self):
        for _ in range(8):
            self.memory.append_message({"role": "user", "content": "历史" * 100})
            self.memory.append_message({"role": "assistant", "content": "回复" * 100})
        self.memory.append_message({"role": "user", "content": "当前问题"})
        result = self.memory.build_context("系统", budget=160)
        self.assertLessEqual(result.diagnostics["estimated_tokens"], 160)
        self.assertEqual(result.messages[-1]["content"], "当前问题")
        self.assertEqual(len(self.memory.history()), 17)


if __name__ == "__main__":
    unittest.main()
