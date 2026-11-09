"""短事务、隔离约束、原始证据和幂等写入。"""
import sqlite3
from unittest.mock import patch

from common import MemoryTestCase


class StoreCases(MemoryTestCase):
    def test_foreign_keys_reject_cross_user_source_even_with_raw_sql(self):
        source, _ = self.turn()
        stranger = self.service(user="bob")
        item = stranger.store("bob 自己的事实").item
        with self.assertRaises(sqlite3.IntegrityError):
            with stranger.db.connection:
                stranger.db.execute("INSERT INTO lamemory_sources VALUES (?,?,?)", ("bob", item.memory_id, source))

    def test_dedup_merges_sources_and_preserves_raw_payload(self):
        first, _ = self.turn()
        result = self.memory.store("使用 Python", [first])
        second, _ = self.turn()
        duplicate = self.memory.store("使用 Python", [second])
        self.assertEqual(duplicate.status, "duplicate")
        self.assertEqual(duplicate.item.memory_id, result.item.memory_id)
        self.assertEqual(set(duplicate.item.source_message_ids), {first, second})
        self.assertEqual(self.memory.history()[-1]["payload"]["usage"], {})

    def test_model_call_occurs_outside_write_transaction(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        def extract(*args, **kwargs):
            self.assertFalse(self.memory.db.connection.in_transaction)
            with sqlite3.connect(self.path, timeout=0.1) as other:
                other.execute("UPDATE lamemory_sessions SET created_at=created_at")
            other.close()
            return []
        self.provider.extract = extract
        self.assertEqual(self.memory.flush(timeout=1).status, "complete")

    def test_commit_failure_rolls_back_entire_extraction_batch(self):
        source, turn = self.turn()
        self.memory.commit_turn(turn)
        self.provider.extract = lambda *args, **kwargs: [{"content": name, "source_message_ids": [source]} for name in ("第一条", "第二条")]
        original = self.memory._save
        def save(candidate, origin):
            if candidate["content"] == "第二条":
                raise sqlite3.OperationalError("simulated write failure")
            return original(candidate, origin)
        with patch.object(self.memory, "_save", side_effect=save):
            job = self.memory.process_pending()[0]
        self.assertEqual(job["status"], "pending")
        self.assertEqual(self.memory.search("第一条"), [])
        self.assertEqual(len(self.memory.history()), 2)

    def test_session_scoped_id_cannot_be_used_from_other_session(self):
        item = self.memory.store("仅此会话使用", scope_kind="session").item
        other = self.service(session="other")
        for action in (other.inspect, other.forget):
            with self.assertRaises(KeyError):
                action(item.memory_id)
        with self.assertRaises(KeyError):
            other.update(item.memory_id, "串会话更正")
