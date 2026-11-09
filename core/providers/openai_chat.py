"""Chat Completions 协议及兼容服务的请求/响应转换。"""
from dataclasses import replace
import json
import time

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from .messages import arguments, as_dict, field, safe_call_id, same_origin, usage_value
from .types import ProviderConfigError, ProviderError


class OpenAIChatProvider:
    def __init__(self, config, spec, *, client=None):
        self.config = config
        self.spec = spec
        self.compat = spec.compatibility.merge(config.compatibility)
        self._owns_client = client is None
        if client is None:
            if not config.api_key:
                raise ProviderConfigError(f"{config.provider} 未配置 API key")
            client = OpenAI(api_key=config.api_key, base_url=config.base_url or spec.base_url,
                            default_headers=config.headers, timeout=config.timeout, max_retries=config.max_retries)
        self.client = client

    def _messages(self, messages, config):
        result = []
        for original in messages:
            role = original["role"]
            message = {"role": role, "content": original.get("content")}
            if role in {"system", "developer"}:
                message["role"] = self.compat.system_role
            if role == "assistant":
                if original.get("tool_calls"):
                    message["tool_calls"] = [
                        {"id": safe_call_id(c["id"]), "type": "function", "function": {
                            "name": c["function"]["name"],
                            "arguments": json.dumps(arguments(c["function"].get("arguments", {})), ensure_ascii=False)}}
                        for c in original["tool_calls"]]
                if self.compat.replay_reasoning:
                    message["reasoning_content"] = original.get("reasoning_content", "") if same_origin(original, config, self.spec.api, self.spec.base_url) else ""
            if role == "tool":
                message["tool_call_id"] = safe_call_id(original["tool_call_id"])
                if original.get("name"):
                    message["name"] = original["name"]
            result.append(message)
        return result

    def build_params(self, messages, tools, options):
        config = replace(self.config, model=options.model) if options.model else self.config
        params = {"model": config.model, "messages": self._messages(messages, config),
                  self.compat.max_tokens_field: options.max_tokens}
        if tools:
            if not self.compat.supports_tools:
                raise ProviderConfigError("该模型未启用工具调用能力")
            params["tools"] = tools
            params["tool_choice"] = "auto"
        if options.temperature is not None:
            if not self.compat.supports_temperature:
                raise ProviderConfigError("该模型不支持 temperature")
            params["temperature"] = options.temperature
        if options.thinking_budget is not None:
            raise ProviderConfigError("thinking_budget 仅用于 Anthropic Messages")
        effort = options.reasoning_effort
        if effort is not None:
            enabled = effort not in {"off", "none"}
            match self.compat.reasoning_format:
                case "openai":
                    params["reasoning_effort"] = effort if enabled else "none"
                case "deepseek":
                    params["extra_body"] = {"thinking": {"type": "enabled" if enabled else "disabled"}}
                    if enabled:
                        params["extra_body"]["reasoning_effort"] = effort
                case "qwen":
                    params["extra_body"] = {"enable_thinking": enabled}
                case _:
                    raise ProviderConfigError("该模型未配置推理参数格式")
        return params

    def complete(self, messages, tools, options):
        params = self.build_params(messages, tools, options)
        deadline = time.monotonic() + options.timeout
        for attempt in range(options.max_retries + 1):
            remaining = options.timeout if attempt == 0 else deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError(self.config.provider, "timeout", retryable=True)
            try:
                # Own the retry budget instead of multiplying it by SDK retries.
                client_options = {"timeout": remaining, "max_retries": 0}
                if self.config.base_url is not None or self.config.provider != "openai-compatible":
                    client_options["base_url"] = self.config.base_url or self.spec.base_url
                if self.config.api_key is not None:
                    client_options["api_key"] = self.config.api_key
                if self.config.headers:
                    client_options["default_headers"] = self.config.headers
                client = self.client.with_options(**client_options)
                response = client.chat.completions.create(**params)
                try:
                    return self._parse(response, options.model or self.config.model)
                except (ValueError, TypeError, KeyError, IndexError, AttributeError):
                    raise ProviderError(self.config.provider, "invalid_response") from None
            except (APITimeoutError, httpx.TimeoutException):
                error = ProviderError(self.config.provider, "timeout", retryable=True)
            except APIStatusError as exc:
                status = exc.status_code
                kind = "authentication" if status in {401, 403} else "rate_limit" if status == 429 else "http"
                error = ProviderError(self.config.provider, kind, status_code=status,
                                      retryable=status in {408, 409, 429} or status >= 500)
            except (APIConnectionError, httpx.TransportError):
                error = ProviderError(self.config.provider, "connection", retryable=True)
            if not error.retryable or attempt == options.max_retries:
                raise error from None
            time.sleep(min(0.1 * 2 ** attempt, max(0, deadline - time.monotonic())))

    def _parse(self, response, model):
        choices = field(response, "choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("没有回复")
        choice = choices[0]
        message = field(choice, "message")
        if message is None:
            raise ValueError("缺少 assistant 消息")
        content = field(message, "content") or field(message, "refusal") or ""
        if not isinstance(content, str):
            raise ValueError("不支持的响应内容")
        raw_usage = field(response, "usage")
        prompt = usage_value(raw_usage, "prompt_tokens")
        completion = usage_value(raw_usage, "completion_tokens")
        result = {"role": "assistant", "content": content,
                  "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                            "total_tokens": usage_value(raw_usage, "total_tokens") or prompt + completion,
                            "cache_read_tokens": usage_value(field(raw_usage, "prompt_tokens_details"), "cached_tokens")},
                  "finish_reason": field(choice, "finish_reason") or "stop",
                  "provider_data": {"provider": self.config.provider, "api": self.spec.api, "model": model,
                                    "base_url": (self.config.base_url or self.spec.base_url).rstrip("/")}}
        reasoning = field(message, "reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            result["reasoning_content"] = reasoning
        calls = field(message, "tool_calls")
        if calls:
            if result["finish_reason"] == "length":
                raise ValueError("工具调用被截断")
            result["tool_calls"] = []
            seen = set()
            for value in calls:
                call = as_dict(value)
                safe_call_id(call.get("id"))
                if call["id"] in seen:
                    raise ValueError("响应的工具 ID 重复")
                seen.add(call["id"])
                function = call["function"]
                if not function.get("name"):
                    raise ValueError("工具缺少名称")
                result["tool_calls"].append({"id": call["id"], "type": "function", "function": {
                    "name": function["name"], "arguments": json.dumps(arguments(function.get("arguments", {})), ensure_ascii=False)}})
        return result

    def close(self):
        if self._owns_client:
            self.client.close()
