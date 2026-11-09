"""主动工具、模型适配器和实际聊天节点的隔离测试。"""
import contextlib
import io
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from common import MemoryTestCase
from Agent.chatBot_with_memory.runner import ChatRunner
from core.lamemory.providers import ChatMemoryProvider
from core.lamemory.tools import get_memory_tools
from core.llm import call_llm
from core.providers import ModelConfig
from tools.builtins.tool_def import Tool
from tools.executor import ToolCall, ToolExecutor


class ChatCases(MemoryTestCase):
    def test_tool_loop_uses_bound_tools_and_raw_sources(self):
        outputs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "save", "type": "function", "function": {
                "name": "lamemory_store", "arguments": json.dumps({"content": "开发回答使用中文"})}}]},
            {"role": "assistant", "content": "已保存", "usage": {"total_tokens": 0}}]
        chat = Mock(side_effect=outputs)
        runner = ChatRunner(self.memory, chat=chat)
        result = runner.send("记住：开发回答使用中文")
        self.assertEqual(result["errors"], [])
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(len(self.memory.history()), 4)
        self.assertEqual(self.provider.calls, 0)
        self.memory.flush(timeout=1)
        self.assertEqual(len(self.memory.search("中文")), 1)
        stored = self.memory.search("中文")[0].item
        self.assertEqual(stored.source_message_ids, [self.memory.history()[0]["message_id"]])
        self.assertEqual(self.memory.jobs()[0]["status"], "completed")

    def test_tool_cannot_change_identity_and_failure_not_reported_saved(self):
        self.turn()
        executor = ToolExecutor(tools=get_memory_tools(self.memory), interactive=False)
        bad = executor.execute(ToolCall("x", "lamemory_store", {"content": "偷换用户", "user_id": "bob"}))
        self.assertTrue(bad.is_error)
        self.assertEqual(self.memory.search("偷换"), [])
        with patch.object(self.memory, "store", side_effect=OSError("disk full")):
            result = executor.execute(ToolCall("x", "lamemory_store", {"content": "保存失败"}))
        self.assertTrue(result.is_error)
        self.assertIn("disk full", result.content)
        self.assertNotIn('"status": "saved"', result.content)

    def test_noninteractive_confirmation_never_reads_input(self):
        tool = Tool("bash", "test", {}, Mock())
        executor = ToolExecutor(tools=[tool], interactive=False)
        with patch("builtins.input", side_effect=AssertionError("禁止等待终端输入")):
            result = executor.execute(ToolCall("x", "bash", {"command": "sudo echo hello"}))
        self.assertTrue(result.is_error)
        tool.fn.assert_not_called()

    def test_missing_usage_and_explicit_client(self):
        client = Mock()
        client.with_options.return_value = client
        message = SimpleNamespace(content="回复", tool_calls=None)
        client.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=None)
        result = call_llm([{"role": "user", "content": "测试"}], client=client, model="fake", timeout=2, max_tokens=123,
                          config=ModelConfig(provider="openai-compatible", model="fake", base_url="https://example.invalid/v1"))
        self.assertEqual(result["usage"]["total_tokens"], 0)
        client.with_options.assert_called_once_with(timeout=2, max_retries=0, base_url="https://example.invalid/v1")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["max_tokens"], 123)

    def test_provider_only_sends_user_evidence_and_records_usage(self):
        source, turn = self.turn()
        chat = Mock(return_value={"content": "[]", "usage": {"total_tokens": 12}})
        provider = ChatMemoryProvider(chat)
        provider.extract(self.memory.history(), max_items=8, timeout=2)
        payload = json.loads(chat.call_args.kwargs["messages"][1]["content"])
        self.assertEqual(payload, [{"message_id": source, "content": "开发回答使用中文"}])
        self.assertEqual(provider.usage_events[0]["purpose"], "extraction")
        self.assertEqual(chat.call_args.kwargs["max_retries"], 0)

    def test_generated_reply_survives_persistence_failure(self):
        chat = Mock(return_value={"role": "assistant", "content": "已经生成的回复"})
        runner = ChatRunner(self.memory, chat=chat)
        original = self.memory.append_message
        def append(message):
            if message["role"] == "assistant":
                raise OSError("disk full")
            return original(message)
        with patch.object(self.memory, "append_message", side_effect=append):
            result = runner.send("测试故障")
        self.assertEqual(result["message"]["content"], "已经生成的回复")
        self.assertTrue(result["errors"])
        self.assertEqual(chat.call_count, 1)

    def test_terminal_displays_reply_before_extraction(self):
        from Agent.chatBot_with_memory.main import ChatNode, OutputNode
        from core.node import shared
        from core.memory import Memory
        facade = Memory(database_path=self.path, user_id="terminal", session_id="one", provider=self.provider)
        self.addCleanup(facade.close)
        facade.add_message({"role": "user", "content": "开发回答使用中文"})
        buffer = io.StringIO()
        def extract(*args, **kwargs):
            self.assertIn("主回复已生成", buffer.getvalue())
            return []
        self.provider.extract = extract
        with patch.dict(shared, {"memory": facade, "tools": []}, clear=True), contextlib.redirect_stdout(buffer):
            with patch("Agent.chatBot_with_memory.main.call_llm", return_value={"role": "assistant", "content": "主回复已生成"}):
                action, response = ChatNode().exec(None)
            self.assertEqual(action, "output")
            self.assertEqual(facade.service.jobs()[0]["status"], "pending")
            OutputNode().exec(response)
            self.assertEqual(facade.service.jobs()[0]["status"], "completed")
