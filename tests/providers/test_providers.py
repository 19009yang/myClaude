"""供应商适配合同；使用隔离环境和本地 mock transport，不请求真实 API。"""
import json
import unittest
from unittest.mock import Mock

import httpx

from core.providers import ChatModel, ModelConfig, ProviderConfigError, ProviderError, create_default_registry


TOOLS = [{"type": "function", "function": {"name": "read", "description": "读取文件", "parameters": {
    "type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]


class ProviderTests(unittest.TestCase):
    def openai_client(self, response):
        client = Mock()
        client.with_options.return_value = client
        client.chat.completions.create.return_value = response
        return client

    def test_presets_and_environment_auth_isolation(self):
        registry = create_default_registry()
        self.assertEqual(set(registry.list_providers()), {"openai", "anthropic", "deepseek", "qwen", "openai-compatible"})
        cfg = ModelConfig.from_env(env={"LLM_PROVIDER": "qwen", "LLM_MODEL_NAME": "test-qwen",
                                        "OPENAI_API_KEY": "wrong", "DASHSCOPE_API_KEY": "right", "QWEN_API_KEY": "second"})
        self.assertEqual(cfg.api_key, "right")
        self.assertNotIn("right", repr(cfg))
        self.assertEqual(cfg.provider, "qwen")
        with self.assertRaises(ProviderConfigError):
            ModelConfig.from_env(env={"LLM_PROVIDER": "anthropic", "LLM_MODEL_NAME": "test", "OPENAI_API_KEY": "wrong"})

    def test_openai_and_legacy_output_fields(self):
        response = {"model": "test", "choices": [{"message": {"content": "回复"}, "finish_reason": "stop"}], "usage": {
            "prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}}
        for provider, field in [("openai", "max_completion_tokens"), ("openai-compatible", "max_tokens")]:
            client = self.openai_client(response)
            model = ChatModel(ModelConfig(provider=provider, model="test", api_key="test-key"), client=client)
            result = model.complete([{"role": "user", "content": "你好"}], tools=TOOLS, max_tokens=123)
            params = client.chat.completions.create.call_args.kwargs
            self.assertEqual(params[field], 123)
            self.assertEqual(result["content"], "回复")
            self.assertEqual(result["usage"]["total_tokens"], 12)
            self.assertEqual(params["tools"], TOOLS)

    def test_deepseek_reasoning_survives_tool_roundtrip(self):
        response = {"choices": [{"message": {"content": "", "reasoning_content": "先读取文件", "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
            "finish_reason": "tool_calls"}]}
        client = self.openai_client(response)
        model = ChatModel(ModelConfig(provider="deepseek", model="test-reasoner", api_key="test"), client=client)
        reply = model.complete([{"role": "user", "content": "读取 a"}], tools=TOOLS)
        model.complete([{"role": "user", "content": "读取 a"}, reply,
                        {"role": "tool", "tool_call_id": "call_1", "content": "文件内容"}], tools=TOOLS)
        history = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(history[1]["reasoning_content"], "先读取文件")
        self.assertNotIn("usage", history[1])
        self.assertNotIn("provider_data", history[1])

    def test_anthropic_native_conversion_and_usage(self):
        captured = []
        def handle(request):
            captured.append((request, json.loads(request.content)))
            return httpx.Response(200, json={"id": "msg_test", "model": "test-claude", "stop_reason": "tool_use", "content": [
                {"type": "text", "text": "读取文件"}, {"type": "tool_use", "id": "toolu_1", "name": "read", "input": {"path": "a"}}],
                "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            model = ChatModel(ModelConfig(provider="anthropic", model="test-claude", api_key="secret"), client=client)
            reply = model.complete([{"role": "system", "content": "规则"}, {"role": "user", "content": "读取 a"}], tools=TOOLS)
            model.complete([{"role": "user", "content": "读取 a"}, reply,
                            {"role": "tool", "tool_call_id": "toolu_1", "content": "内容"}], tools=TOOLS)
        request, params = captured[0]
        self.assertEqual(str(request.url), "https://api.anthropic.com/v1/messages")
        self.assertEqual(request.headers["x-api-key"], "secret")
        self.assertEqual(params["system"], "规则")
        self.assertEqual(params["tools"][0]["input_schema"], TOOLS[0]["function"]["parameters"])
        self.assertEqual(reply["tool_calls"][0]["function"]["arguments"], '{"path": "a"}')
        self.assertEqual(reply["usage"]["total_tokens"], 20)
        result_block = captured[1][1]["messages"][-1]["content"][0]
        self.assertEqual(result_block, {"type": "tool_result", "tool_use_id": "toolu_1", "content": "内容"})

    def test_anthropic_errors_are_normalized_without_secrets(self):
        def handle(request):
            return httpx.Response(401, json={"error": {"type": "authentication_error", "message": "secret-in-response"}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as client:
            model = ChatModel(ModelConfig(provider="anthropic", model="test", api_key="secret-key"), client=client)
            with self.assertRaises(ProviderError) as raised:
                model.complete([{"role": "user", "content": "你好"}])
        self.assertEqual(raised.exception.kind, "authentication")
        self.assertFalse(raised.exception.retryable)
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
