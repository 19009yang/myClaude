"""跨协议历史、真实 SDK 编码、可恢复错误和扩展注册行为。"""
import copy
import json
import unittest
from unittest.mock import Mock, patch

import httpx
from openai import OpenAI

from core.llm import call_llm, call_llm_chat
from core.providers import (ChatModel, Compatibility, ModelConfig, ProviderConfigError,
                            ProviderError, ProviderSpec, create_default_registry)
from test_providers import TOOLS


def completion(message=None, **kwargs):
    return {"id": "chatcmpl_test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "message": message or {"role": "assistant", "content": "回复"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}, **kwargs}


def tool_history(call_id="call_1"):
    return [{"role": "user", "content": "读取文件"},
            {"role": "assistant", "content": "", "reasoning_content": "不应跨供应商发送的思考", "tool_calls": [
                {"id": call_id, "type": "function", "function": {"name": "read", "arguments": '{"path":"a"}'}}]},
            {"role": "tool", "tool_call_id": call_id, "content": "文件内容"}]


class ProtocolTests(unittest.TestCase):
    def test_real_openai_sdk_serializes_qwen_thinking(self):
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion())
        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as sdk:
            chat = ChatModel(ModelConfig(provider="qwen", model="test", api_key="test", reasoning_effort="high"), client=sdk)
            response = chat.complete([{"role": "user", "content": "你好"}], max_tokens=222)
        self.assertEqual(requests[0]["enable_thinking"], True)
        self.assertEqual(requests[0]["max_tokens"], 222)
        self.assertNotIn("reasoning_effort", requests[0])
        self.assertEqual(response["usage"]["total_tokens"], 10)

    def test_ids_are_mapped_consistently_without_mutating_archive(self):
        history = tool_history("call|" + "a/" * 30)
        original = copy.deepcopy(history)
        requests = []
        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json=completion())
        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as sdk:
            ChatModel(ModelConfig(provider="openai", model="test"), client=sdk).complete(history, tools=TOOLS)
        converted = requests[0]["messages"]
        normalized = converted[1]["tool_calls"][0]["id"]
        self.assertLessEqual(len(normalized), 40)
        self.assertEqual(converted[2]["tool_call_id"], normalized)
        self.assertNotIn("reasoning_content", converted[1])
        self.assertEqual(history, original)

    def test_anthropic_signed_thinking_and_parallel_tool_results(self):
        captured = []
        blocks = [{"type": "thinking", "thinking": "分析", "signature": "signed-token"},
                  {"type": "redacted_thinking", "data": "opaque-data"},
                  *[{"type": "tool_use", "id": call, "name": "read", "input": {"path": call}} for call in ("a", "b")]]
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"content": blocks, "stop_reason": "tool_use", "usage": {}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            model = ChatModel(ModelConfig(provider="anthropic", model="test", api_key="test", max_tokens=4096, thinking_budget=1024), client=http)
            first = [{"role": "user", "content": "读取两个文件"}]
            response = model.complete(first, tools=TOOLS)
            model.complete([*first, response, *[{"role": "tool", "tool_call_id": call, "content": call} for call in ("a", "b")]], tools=TOOLS)
        self.assertEqual(captured[1]["messages"][1]["content"][:2], blocks[:2])
        self.assertEqual(len(captured[1]["messages"][-1]["content"]), 2)
        self.assertEqual(captured[1]["thinking"], {"type": "enabled", "budget_tokens": 1024})

    def test_signed_thinking_is_removed_when_switching_model(self):
        history = tool_history()
        history[1]["provider_data"] = {"provider": "anthropic", "api": "anthropic-messages", "model": "old", "base_url": "https://api.anthropic.com", "content_blocks": [
            {"type": "thinking", "thinking": "旧推理", "signature": "must-not-leak"}]}
        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"content": [{"type": "text", "text": "回复"}], "usage": {}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            ChatModel(ModelConfig(provider="anthropic", model="new", api_key="test"), client=http).complete(history, tools=TOOLS)
        self.assertNotIn("must-not-leak", json.dumps(captured))

    def test_anthropic_retry_is_bounded_and_timeout_classified(self):
        attempts = []
        def handle(request):
            attempts.append(request)
            return httpx.Response(429, json={"error": {"message": "secret"}})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            model = ChatModel(ModelConfig(provider="anthropic", model="test", api_key="test", max_retries=1), client=http)
            with self.assertRaises(ProviderError) as error:
                model.complete([{"role": "user", "content": "测试"}])
        self.assertEqual(len(attempts), 2)
        self.assertTrue(error.exception.retryable)
        def timeout(request):
            raise httpx.ReadTimeout("private request text", request=request)
        with httpx.Client(transport=httpx.MockTransport(timeout)) as http:
            model = ChatModel(ModelConfig(provider="anthropic", model="test", api_key="test"), client=http)
            with self.assertRaises(ProviderError) as error:
                model.complete([{"role": "user", "content": "测试"}])
        self.assertEqual(error.exception.kind, "timeout")
        self.assertNotIn("private", str(error.exception))

    def test_openai_http_error_is_normalized(self):
        def handle(request):
            return httpx.Response(401, json={"error": {"message": "secret", "type": "invalid_api_key"}})
        with OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handle))) as sdk:
            chat = ChatModel(ModelConfig(provider="openai", model="test"), client=sdk)
            with self.assertRaises(ProviderError) as error:
                chat.complete([{"role": "user", "content": "测试"}])
        self.assertEqual(error.exception.kind, "authentication")
        self.assertNotIn("secret", str(error.exception))

    def test_invalid_and_incomplete_tools_never_reach_transport(self):
        sdk = Mock()
        model = ChatModel(ModelConfig(provider="openai", model="test"), client=sdk)
        with self.assertRaises(ProviderConfigError):
            model.complete(tool_history()[:-1], tools=TOOLS)
        with self.assertRaises(ProviderConfigError):
            model.complete([{"role": "user", "content": "测试"}], tools=["invalid"])
        sdk.with_options.assert_not_called()

    def test_model_compatibility_overrides_are_local(self):
        sdk = Mock()
        sdk.with_options.return_value = sdk
        sdk.chat.completions.create.return_value = completion()
        model = ChatModel(ModelConfig(provider="openai", model="older", compatibility=Compatibility(
            max_tokens_field="max_tokens", supports_temperature=False, system_role="developer")), client=sdk)
        model.complete([{"role": "system", "content": "规则"}, {"role": "user", "content": "测试"}])
        params = sdk.chat.completions.create.call_args.kwargs
        self.assertIn("max_tokens", params)
        self.assertEqual(params["messages"][0]["role"], "developer")
        with self.assertRaises(ProviderConfigError):
            model.complete([{"role": "user", "content": "测试"}], temperature=0.2)
        defaults = create_default_registry().get("openai").compatibility
        self.assertEqual(defaults.max_tokens_field, "max_completion_tokens")

    def test_legacy_function_and_text_helper_use_same_layer(self):
        sdk = Mock()
        sdk.with_options.return_value = sdk
        sdk.chat.completions.create.return_value = completion()
        with patch.dict("os.environ", {"LLM_API_KEY": "test", "LLM_MODEL_NAME": "test"}, clear=True):
            self.assertEqual(call_llm_chat("你好", client=sdk), "回复")
            self.assertEqual(call_llm([{"role": "user", "content": "你好"}], client=sdk)["content"], "回复")
        self.assertIn("max_tokens", sdk.chat.completions.create.call_args.kwargs)

    def test_custom_registry_extension_and_injected_client_ownership(self):
        registry = create_default_registry()
        adapter = Mock()
        adapter.complete.return_value = {"role": "assistant", "content": "自定义"}
        registry.register(ProviderSpec("local", "fake", "http://localhost"), lambda *a, **k: adapter)
        with ChatModel(ModelConfig(provider="local", model="local-test"), registry=registry) as model:
            self.assertEqual(model.complete([{"role": "user", "content": "你好"}])["content"], "自定义")
        adapter.close.assert_called_once()
        sdk = Mock()
        chat = ChatModel(ModelConfig(provider="openai", model="test"), client=sdk)
        chat.close()
        sdk.close.assert_not_called()
        with self.assertRaises(ProviderConfigError):
            registry.register(ProviderSpec("local", "fake", "http://localhost"), lambda *a, **k: adapter)
        with patch.dict("os.environ", {"LLM_PROVIDER": "local", "LLM_MODEL_NAME": "local-test", "LLM_API_KEY": "local-key"}, clear=True):
            result = call_llm([{"role": "user", "content": "你好"}], registry=registry)
        self.assertEqual(result["content"], "自定义")

    def test_configuration_validation_and_memory_prefix(self):
        env = {"LLM_API_KEY": "agent-secret", "LLM_PROVIDER": "openai", "LLM_MODEL_NAME": "main",
               "MEMORY_LLM_PROVIDER": "deepseek", "MEMORY_LLM_MODEL_NAME": "small", "DEEPSEEK_API_KEY": "memory-secret"}
        config = ModelConfig.from_env(prefix="MEMORY_LLM_", env=env)
        self.assertEqual((config.provider, config.model, config.api_key), ("deepseek", "small", "memory-secret"))
        for kwargs in ({"base_url": "https://secret@example.com"}, {"max_tokens": -1}, {"timeout": 0},
                       {"headers": {"Authorization": "hidden"}}, {"model": ""}, {"timeout": float("nan")},
                       {"max_tokens": True}, {"api_key": 123}, {"temperature": "invalid"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ProviderConfigError):
                ModelConfig(**{"model": "test", **kwargs})

    def test_malformed_response_is_not_a_successful_reply(self):
        def handle(request):
            return httpx.Response(200, json={"content": "wrong"})
        with httpx.Client(transport=httpx.MockTransport(handle)) as http:
            chat = ChatModel(ModelConfig(provider="anthropic", model="test", api_key="test"), client=http)
            with self.assertRaises(ProviderError) as error:
                chat.complete([{"role": "user", "content": "你好"}])
        self.assertEqual(error.exception.kind, "invalid_response")
