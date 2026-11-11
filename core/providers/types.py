"""统一协议、请求选项和供应商错误，不包含任何网络调用。"""
from dataclasses import dataclass, fields
import math
from typing import Any, Protocol


class ProviderConfigError(ValueError):
    """供应商配置或请求能力不匹配。"""


class ProviderError(RuntimeError):
    """稳定错误字符串；可附带响应诊断、用量及脱敏证据，不附请求或请求头。"""

    def __init__(self, provider, kind, *, status_code=None, retryable=False,
                 diagnostics=None, usage=None, response_evidence=None):
        self.provider = provider
        self.kind = kind
        self.status_code = status_code
        self.retryable = retryable
        self.diagnostics = diagnostics or {}
        self.usage = usage or {}
        # Optional response-only evidence; never included in str(error).
        self.response_evidence = response_evidence
        suffix = f" (HTTP {status_code})" if status_code else ""
        super().__init__(f"{provider}: {kind}{suffix}")


@dataclass(frozen=True)
class Compatibility:
    max_tokens_field: str | None = None
    supports_temperature: bool | None = None
    supports_tools: bool | None = None
    system_role: str | None = None
    reasoning_format: str | None = None
    replay_reasoning: bool | None = None

    def __post_init__(self):
        if self.max_tokens_field not in {None, "max_tokens", "max_completion_tokens"}:
            raise ProviderConfigError("不支持的输出 token 字段")
        if self.system_role not in {None, "system", "developer"}:
            raise ProviderConfigError("不支持的系统消息角色")
        if self.reasoning_format not in {None, "none", "openai", "deepseek", "qwen", "anthropic"}:
            raise ProviderConfigError("不支持的推理参数格式")
        for key in ("supports_temperature", "supports_tools", "replay_reasoning"):
            if getattr(self, key) is not None and not isinstance(getattr(self, key), bool):
                raise ProviderConfigError(f"{key} 必须是 bool")

    def merge(self, overrides):
        if overrides is None:
            return self
        return Compatibility(**{f.name: getattr(overrides, f.name) if getattr(overrides, f.name) is not None
                                else getattr(self, f.name) for f in fields(self)})


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    api: str
    base_url: str
    api_key_envs: tuple[str, ...] = ()
    compatibility: Compatibility = Compatibility(
        max_tokens_field="max_tokens", supports_temperature=True, supports_tools=True,
        system_role="system", reasoning_format="none", replay_reasoning=False)


@dataclass(frozen=True)
class RequestOptions:
    timeout: float = 30
    max_retries: int = 0
    max_tokens: int = 4096
    temperature: float | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None
    model: str | None = None

    def __post_init__(self):
        if (not isinstance(self.timeout, (int, float)) or isinstance(self.timeout, bool) or
                not math.isfinite(self.timeout) or self.timeout <= 0 or
                not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or not 0 <= self.max_retries <= 10):
            raise ProviderConfigError("timeout 必须为正数，max_retries 必须为 0 到 10")
        if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
            raise ProviderConfigError("max_tokens 必须为正整数")
        if self.temperature is not None and (not isinstance(self.temperature, (int, float)) or not 0 <= self.temperature <= 2):
            raise ProviderConfigError("temperature 必须为 0 到 2")
        if self.reasoning_effort is not None and self.reasoning_effort not in {
            "off", "none", "minimal", "low", "medium", "high", "xhigh", "max"}:
            raise ProviderConfigError("不支持的 reasoning_effort")
        if self.thinking_budget is not None and (not isinstance(self.thinking_budget, int) or
                                                 not 1024 <= self.thinking_budget < self.max_tokens):
            raise ProviderConfigError("thinking_budget 至少为 1024 且小于 max_tokens")
        if self.model is not None and (not isinstance(self.model, str) or not self.model.strip()):
            raise ProviderConfigError("覆盖的模型名称必须为非空字符串")


class ChatProvider(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None, options: RequestOptions) -> dict[str, Any]: ...

    def close(self) -> None: ...
