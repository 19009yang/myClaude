"""只读迁移、备份、故障回滚与重复导入。"""
import hashlib
import json
from pathlib import Path

from common import MemoryTestCase
from core.lamemory.migration import discover_legacy_paths, import_legacy, preview_legacy
from core.memory import Memory


class MigrationCases(MemoryTestCase):
    def fixtures(self):
        session = Path(self.tmp.name) / r"core\chat_memory\session.jsonl"
        memory = Path(self.tmp.name) / "MEMORY.md"
        messages = [{"role": "user", "content": "记住中文"},
                    {"role": "assistant", "tool_calls": [{"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]},
                    {"role": "tool", "tool_call_id": "a", "content": "日志"},
                    {"role": "assistant", "content": "完成"}]
        session.write_text("\n".join(json.dumps(m, ensure_ascii=False) for m in messages) + "\ninvalid\n", encoding="utf-8")
        memory.write_text("# 长期记忆\n\n- 用户偏好中文\n- 使用 Python\n", encoding="utf-8")
        return session, memory

    def test_preview_import_backup_idempotency(self):
        session, memory = self.fixtures()
        before = session.read_bytes()
        preview = preview_legacy(session_path=session, memory_path=memory)
        self.assertEqual((preview["message_count"], preview["memory_count"]), (4, 2))
        self.assertEqual(preview["invalid_lines"][0]["line"], 5)
        self.assertEqual(len(self.memory.history()), 0)
        self.assertIn(str(session), discover_legacy_paths(self.tmp.name))
        result = import_legacy(self.memory, session_path=session, memory_path=memory)
        self.assertEqual(result["status"], "imported")
        self.assertEqual((Path(result["backup_directory"]) / "session.jsonl").read_bytes(), before)
        self.assertEqual(import_legacy(self.memory, session_path=session, memory_path=memory)["status"], "already_imported")
        self.assertEqual(len(self.memory.history()), 4)
        self.assertEqual(self.memory.history()[2]["payload"]["tool_call_id"], "a")
        item = self.memory.search("中文")[0].item
        self.assertEqual(item.origin, "legacy_import")
        self.assertEqual(item.source_message_ids, [])
        self.assertIsNone(item.occurred_at)
        self.assertEqual(session.read_bytes(), before)

    def test_legacy_path_compatibility(self):
        session, _ = self.fixtures()
        with self.assertWarns(DeprecationWarning):
            facade = Memory(memory_path=str(session), user_id="legacy", provider=self.provider)
        self.addCleanup(facade.close)
        self.assertEqual(len(facade.messages), 4)
        self.assertEqual(facade.service.config.database_path.parent, session.parent.resolve())

    def test_invalid_memory_does_not_partially_import_messages(self):
        session, memory = self.fixtures()
        memory.write_text("- " + "x" * 5000, encoding="utf-8")
        with self.assertRaises(ValueError):
            import_legacy(self.memory, session_path=session, memory_path=memory)
        self.assertEqual(self.memory.history(), [])
