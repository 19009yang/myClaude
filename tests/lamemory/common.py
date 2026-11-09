"""隔离测试夹具，不访问真实数据或调用外部模型。"""
import tempfile
import unittest
from pathlib import Path

from core.lamemory import LaMemoryConfig, LaMemoryService


class FakeProvider:
    def __init__(self):
        self.calls = 0
        self.fail = False

    def extract(self, messages, *, max_items, timeout):
        self.calls += 1
        if self.fail:
            raise TimeoutError("offline test timeout")
        return [{"content": "开发回答使用中文", "type": "preference",
                 "source_message_ids": [messages[0]["message_id"]]}]


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "memory.sqlite3"
        self.provider = FakeProvider()
        self.memory = self.service()

    def service(self, user="alice", session="first", **kwargs):
        service = LaMemoryService(LaMemoryConfig(database_path=self.path, **kwargs),
                                  user_id=user, session_id=session, provider=self.provider)
        self.addCleanup(service.close)
        return service

    def turn(self, memory=None):
        memory = memory or self.memory
        source = memory.append_message({"role": "user", "content": "开发回答使用中文"})
        memory.append_message({"role": "assistant", "content": "好的", "usage": {}})
        return source, memory.current_turn_id
