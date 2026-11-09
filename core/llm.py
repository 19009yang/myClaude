"""旧模型函数兼容入口，供应商差异统一交给 core.providers。"""
from __future__ import annotations

from typing import Any

from dotenv import load_dotenv

from .providers import ChatModel, ModelConfig, ProviderConfigError, ProviderRegistry
from .providers.config import canonical_provider

load_dotenv()


def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
    *,
    client: Any = None,
    model: str | None = None,
    timeout: float | None = None,
    max_retries: int | None = None,
    max_tokens: int | None = None,
    provider: str | ChatModel | None = None,
    config: ModelConfig | None = None,
    registry: ProviderRegistry | None = None,
    temperature: float | None = None,
    reasoning_effort: str | None = None,
    thinking_budget: int | None = None,
) -> dict[str, Any]:
    """返回统一 assistant 字典；旧 LLM_* 配置和 SDK client 注入继续有效。"""
    options = dict(model=model, timeout=timeout, max_retries=max_retries, max_tokens=max_tokens,
                   temperature=temperature, reasoning_effort=reasoning_effort, thinking_budget=thinking_budget)
    if isinstance(provider, ChatModel):
        if config is not None or client is not None or registry is not None:
            raise ProviderConfigError("已有 ChatModel 实例不能与 config/client/registry 混用")
        return provider.complete(messages, tools, system_prompt, **options)
    if provider is not None and not isinstance(provider, str):
        raise ProviderConfigError("provider 必须为注册名或 ChatModel 实例")
    if config is not None and provider is not None and config.provider != canonical_provider(provider):
        raise ProviderConfigError("provider 与显式 config 不一致；请构造独立配置")
    resolved = config if config is not None else ModelConfig.from_env(
        model=model, provider=provider, require_key=client is None, registry=registry)
    with ChatModel(resolved, registry=registry, client=client) as chat:
        return chat.complete(messages, tools, system_prompt, **options)


def call_llm_chat(prompt: str, **kwargs) -> str:
    """文本便捷接口，使用与聊天/工具/记忆相同的适配层。"""
    system_prompt = kwargs.pop("system_prompt", "You are a helpful assistant")
    return call_llm([{"role": "user", "content": prompt}], system_prompt=system_prompt, **kwargs)["content"]
