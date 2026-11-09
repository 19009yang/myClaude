"""原生 Messages 协议，使用 httpx 而非新增供应商 SDK。"""
from dataclasses import replace
import copy
import json
import time

import httpx

from .messages import arguments, safe_call_id, same_origin, usage_value
from .types import ProviderConfigError, ProviderError


class AnthropicMessagesProvider:
    def __init__(self, config, spec, *, client=None):
        if not config.api_key:
            raise ProviderConfigError(f"{config.provider} 未配置 API key")
        self.config = config
        self.spec = spec
        self.compat = spec.compatibility.merge(config.compatibility)
        self._owns_client = client is None
        self.client = client if client is not None else httpx.Client()

    def _messages(self, messages, config):
        system, result = [], []
        for original in messages:
            role = original["role"]
            if role in {"system", "developer"}:
                if original.get("content"):
                    system.append(original["content"])
                continue
            if role == "tool":
                role = "user"
                blocks = [{"type": "tool_result", "tool_use_id": safe_call_id(original["tool_call_id"]),
                           "content": original.get("content") or ""}]
            elif role == "assistant":
                blocks = []
                if same_origin(original, config, self.spec.api, self.spec.base_url):
                    native = original["provider_data"].get("content_blocks", [])
                    blocks.extend(copy.deepcopy(b) for b in native if b.get("type") in {"thinking", "redacted_thinking"})
                if original.get("content"):
                    blocks.append({"type": "text", "text": original["content"]})
                for call in original.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": safe_call_id(call["id"]), "name": call["function"]["name"],
                                   "input": arguments(call["function"].get("arguments", {}))})
                if not blocks:
                    raise ProviderConfigError("Anthropic 不支持空 assistant 消息")
            else:
                blocks = [{"type": "text", "text": original.get("content") or ""}]
            if result and result[-1]["role"] == role:
                result[-1]["content"].extend(blocks)
            else:
                result.append({"role": role, "content": blocks})
        if not result or result[0]["role"] != "user":
            raise ProviderConfigError("Anthropic 历史必须以用户消息开始")
        return "\n\n".join(system), result

    def build_params(self, messages, tools, options):
        config = replace(self.config, model=options.model) if options.model else self.config
        system, converted = self._messages(messages, config)
        params = {"model": config.model, "messages": converted, "max_tokens": options.max_tokens}
        if system:
            params["system"] = system
        if tools:
            if not self.compat.supports_tools:
                raise ProviderConfigError("该模型未启用工具调用能力")
            params["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                                "input_schema": copy.deepcopy(t["function"]["parameters"])} for t in tools]
            params["tool_choice"] = {"type": "auto"}
        if options.reasoning_effort is not None:
            raise ProviderConfigError("Anthropic 当前使用 thinking_budget，不自动推断模型的 adaptive thinking 能力")
        if options.temperature is not None:
            if not self.compat.supports_temperature or options.thinking_budget is not None:
                raise ProviderConfigError("该模型/思考模式不支持 temperature")
            params["temperature"] = options.temperature
        if options.thinking_budget is not None:
            params["thinking"] = {"type": "enabled", "budget_tokens": options.thinking_budget}
        return params

    def complete(self, messages, tools, options):
        payload = self.build_params(messages, tools, options)
        base = (self.config.base_url or self.spec.base_url).rstrip("/")
        url = base + ("/messages" if base.endswith("/v1") else "/v1/messages")
        headers = {**self.config.headers, "x-api-key": self.config.api_key, "anthropic-version": "2023-06-01"}
        deadline = time.monotonic() + options.timeout
        for attempt in range(options.max_retries + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(self.config.provider, "timeout", retryable=True)
            try:
                response = self.client.post(url, headers=headers, json=payload, timeout=remaining)
                status = response.status_code
                if status >= 400:
                    kind = "authentication" if status in {401, 403} else "rate_limit" if status == 429 else "http"
                    raise ProviderError(self.config.provider, kind, status_code=status,
                                        retryable=status in {408, 409, 429} or status >= 500)
                try:
                    return self._parse(response.json(), options.model or self.config.model)
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise ProviderError(self.config.provider, "invalid_response") from None
            except httpx.TimeoutException:
                error = ProviderError(self.config.provider, "timeout", retryable=True)
            except httpx.TransportError:
                error = ProviderError(self.config.provider, "connection", retryable=True)
            except ProviderError as exc:
                error = exc
            if not error.retryable or attempt == options.max_retries:
                raise error from None
            delay = min(0.1 * 2 ** attempt, max(0, deadline - time.monotonic()))
            time.sleep(delay)

    def _parse(self, response, model):
        if not isinstance(response.get("content"), list):
            raise ValueError("缺少响应内容")
        result = {"role": "assistant", "content": ""}
        calls, reasoning = [], []
        seen = set()
        for block in response["content"]:
            match block["type"]:
                case "text":
                    result["content"] += block["text"]
                case "tool_use":
                    safe_call_id(block["id"])
                    if block["id"] in seen or not isinstance(block.get("name"), str) or not block["name"]:
                        raise ValueError("响应的工具 ID 重复或名称为空")
                    seen.add(block["id"])
                    calls.append({"id": block["id"], "type": "function", "function": {
                        "name": block["name"], "arguments": json.dumps(arguments(block["input"]), ensure_ascii=False)}})
                case "thinking":
                    if not isinstance(block.get("signature"), str):
                        raise ValueError("思考块缺少签名")
                    reasoning.append(block["thinking"])
                case "redacted_thinking":
                    if not isinstance(block.get("data"), str):
                        raise ValueError("无效的隐藏思考块")
                case _:
                    raise ValueError("尚未支持的响应块")
        if calls:
            if response.get("stop_reason") == "max_tokens":
                raise ValueError("工具调用被截断")
            result["tool_calls"] = calls
        if reasoning:
            result["reasoning_content"] = "\n".join(reasoning)
        raw = response.get("usage") or {}
        uncached = usage_value(raw, "input_tokens")
        cache_read = usage_value(raw, "cache_read_input_tokens")
        cache_write = usage_value(raw, "cache_creation_input_tokens")
        output = usage_value(raw, "output_tokens")
        result["usage"] = {"prompt_tokens": uncached + cache_read + cache_write, "completion_tokens": output,
                           "total_tokens": uncached + cache_read + cache_write + output,
                           "cache_read_tokens": cache_read, "cache_write_tokens": cache_write}
        result["finish_reason"] = {"end_turn": "stop", "stop_sequence": "stop", "tool_use": "tool_calls",
                                   "max_tokens": "length"}.get(response.get("stop_reason"), response.get("stop_reason"))
        result["provider_data"] = {"provider": self.config.provider, "api": self.spec.api, "model": model,
                                   "base_url": (self.config.base_url or self.spec.base_url).rstrip("/"),
                                   "content_blocks": copy.deepcopy(response["content"])}
        return result

    def close(self):
        if self._owns_client:
            self.client.close()
