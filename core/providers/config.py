"""实例级模型配置和显式环境解析；业务模块不修改全局环境。"""
from dataclasses import dataclass, field
import os
from urllib.parse import urlsplit

from .types import Compatibility, ProviderConfigError


def canonical_provider(value):
    return {"dashscope": "qwen", "custom": "openai-compatible"}.get(value, value)


@dataclass(frozen=True)
class ModelConfig:
    model: str
    provider: str = "openai-compatible"
    api_key: str | None = field(default=None, repr=False)
    base_url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    timeout: float = 30
    max_retries: int = 0
    max_tokens: int = 4096
    context_window: int | None = None
    compatibility: Compatibility | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None
    thinking_budget: int | None = None

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderConfigError("请显式配置模型名称，不自动选择模型")
        if not isinstance(self.provider, str) or not self.provider.strip():
            raise ProviderConfigError("provider 必须为非空字符串")
        object.__setattr__(self, "provider", canonical_provider(self.provider))
        if self.api_key is not None and not isinstance(self.api_key, str):
            raise ProviderConfigError("api_key 必须为字符串")
        if not isinstance(self.headers, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in self.headers.items()):
            raise ProviderConfigError("headers 必须为字符串映射")
        object.__setattr__(self, "headers", dict(self.headers))
        if any(key.lower() in {"authorization", "x-api-key", "anthropic-version"} for key in self.headers):
            raise ProviderConfigError("鉴权和协议版本请使用专用配置，不能通过 headers 覆盖")
        if self.compatibility is not None and not isinstance(self.compatibility, Compatibility):
            raise ProviderConfigError("compatibility 必须为 Compatibility")
        if self.base_url is not None:
            url = urlsplit(self.base_url)
            if url.scheme not in {"https", "http"} or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ProviderConfigError("base_url 必须为不带凭据、查询和片段的 HTTP(S) 地址")
        if self.context_window is not None and (not isinstance(self.context_window, int) or self.context_window <= 0):
            raise ProviderConfigError("context_window 必须为正整数")
        from .types import RequestOptions
        RequestOptions(timeout=self.timeout, max_retries=self.max_retries, max_tokens=self.max_tokens,
                       temperature=self.temperature, reasoning_effort=self.reasoning_effort, thinking_budget=self.thinking_budget)

    @classmethod
    def from_env(cls, *, prefix="LLM_", env=None, model=None, provider=None, require_key=True, registry=None):
        """显式环境映射优先于进程环境；只读取选中供应商的专属密钥。"""
        values = os.environ if env is None else env
        name = canonical_provider(provider or values.get(prefix + "PROVIDER", "openai-compatible"))
        from .registry import create_default_registry
        selected_registry = registry if registry is not None else create_default_registry()
        spec = selected_registry.get(name)
        key = values.get(prefix + "API_KEY") or next((values[k] for k in spec.api_key_envs if values.get(k)), None)
        if require_key and not key:
            raise ProviderConfigError(f"{name} 未配置 API key（{prefix}API_KEY 或供应商专属变量）")
        kwargs = {"provider": name, "model": model or values.get(prefix + "MODEL_NAME", ""),
                  "api_key": key, "base_url": values.get(prefix + "BASE_URL") or None}
        numeric = {"TIMEOUT": ("timeout", float), "MAX_RETRIES": ("max_retries", int),
                   "MAX_TOKENS": ("max_tokens", int), "CONTEXT_WINDOW": ("context_window", int),
                   "TEMPERATURE": ("temperature", float), "THINKING_BUDGET": ("thinking_budget", int)}
        if values.get(prefix + "REASONING_EFFORT"):
            kwargs["reasoning_effort"] = values[prefix + "REASONING_EFFORT"]
        try:
            for suffix, (field_name, convert) in numeric.items():
                if values.get(prefix + suffix):
                    kwargs[field_name] = convert(values[prefix + suffix])
        except (ValueError, TypeError):
            raise ProviderConfigError("模型环境配置包含无效数字") from None
        return cls(**kwargs)
