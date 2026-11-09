"""真实协议适配器与记忆工具循环联合测试，全程 MockTransport。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from openai import OpenAI

from Agent.chatBot_with_memory.runner import ChatRunner
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.providers import ChatMemoryProvider
from core.providers import ChatModel, ModelConfig
from test_protocols import completion


class MemoryBridgeTests(unittest.TestCase):
    def test_anthropic_agent_and_deepseek_extractor_share_one_memory_contract(self):
        requests = []
        def handle(request):
            data = json.loads(request.content)
            requests.append((request, data))
            if request.url.host == "api.deepseek.com":
                users = json.loads(data["messages"][-1]["content"])
                extracted = [{"content": "开发回答使用中文", "type": "preference", "source_message_ids": [users[0]["message_id"]]}]
                return httpx.Response(200, json=completion({"role": "assistant", "content": json.dumps(extracted, ensure_ascii=False)}))
            has_result = any(b.get("type") == "tool_result" for m in data["messages"] for b in m["content"])
            content = [{"type": "text", "text": "已保存"}] if has_result else [{
                "type": "tool_use", "id": "toolu_store", "name": "lamemory_store", "input": {"content": "开发回答使用中文"}}]
            return httpx.Response(200, json={"content": content, "stop_reason": "end_turn" if has_result else "tool_use", "usage": {
                "input_tokens": 5, "output_tokens": 3}})
        transport = httpx.MockTransport(handle)
        with tempfile.TemporaryDirectory() as temp, httpx.Client(transport=transport) as http, OpenAI(
            api_key="memory-key", http_client=httpx.Client(transport=transport)) as sdk:
            main = ChatModel(ModelConfig(provider="anthropic", model="main-test", api_key="main-key"), client=http)
            extractor = ChatModel(ModelConfig(provider="deepseek", model="memory-test"), client=sdk)
            provider = ChatMemoryProvider(chat=extractor)
            cfg = LaMemoryConfig(database_path=Path(temp) / "memory.sqlite3")
            with LaMemoryService(cfg, user_id="test", session_id="first", provider=provider) as memory:
                result = ChatRunner(memory, chat=main).send("请记住开发回答使用中文")
                self.assertEqual(result["message"]["content"], "已保存")
                self.assertEqual(memory.flush(timeout=2).status, "complete")
                self.assertEqual(len(memory.search("中文")), 1)
                self.assertEqual(len(memory.history()), 4)
                self.assertIn("provider_data", memory.history()[1]["payload"])
                self.assertEqual(provider.usage_events[0]["provider_data"]["provider"], "deepseek")
            with LaMemoryService(cfg, user_id="test", session_id="second") as second:
                hit = second.search("中文")[0]
                self.assertTrue(second.inspect(hit.item.memory_id)["sources"])
            self.assertEqual([body["model"] for _, body in requests], ["main-test", "main-test", "memory-test"])
            self.assertEqual(requests[0][0].headers["x-api-key"], "main-key")
            self.assertEqual(requests[2][0].headers["authorization"], "Bearer memory-key")

    def test_memory_environment_configuration_is_independent(self):
        with patch.dict("os.environ", {"LLM_PROVIDER": "openai", "LLM_MODEL_NAME": "main", "LLM_API_KEY": "main-key",
                                       "MEMORY_LLM_PROVIDER": "qwen", "MEMORY_LLM_MODEL_NAME": "small", "QWEN_API_KEY": "memory-key"}, clear=True):
            with patch("core.llm.call_llm", return_value={"role": "assistant", "content": "[]", "usage": {}}) as chat:
                provider = ChatMemoryProvider()
                provider.extract([{"role": "user", "message_id": "source", "payload": {"content": "事实"}}], max_items=8, timeout=2)
            config = chat.call_args.kwargs["config"]
            self.assertEqual((config.provider, config.model, config.api_key), ("qwen", "small", "memory-key"))
            self.assertEqual(chat.call_args.kwargs["max_retries"], 0)

    def test_explicit_memory_config_never_reads_environment(self):
        config = ModelConfig(provider="deepseek", model="fixed", api_key="fixed-key", timeout=0.5)
        with patch.dict("os.environ", {"MEMORY_LLM_PROVIDER": "invalid"}, clear=True), patch(
            "core.llm.call_llm", return_value={"content": "[]"}) as chat:
            ChatMemoryProvider(model_config=config).extract([], max_items=8, timeout=2)
        self.assertIs(chat.call_args.kwargs["config"], config)
        self.assertEqual(chat.call_args.kwargs["timeout"], 0.5)
