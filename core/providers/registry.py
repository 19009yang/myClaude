"""可扩展供应商注册中心；预设仅包含端点和协议，不硬编码模型目录。"""
from .anthropic import AnthropicMessagesProvider
from .config import canonical_provider
from .openai_chat import OpenAIChatProvider
from .types import Compatibility, ProviderConfigError, ProviderSpec


class ProviderRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, spec, factory, *, replace=False):
        if not isinstance(spec, ProviderSpec) or not spec.id or not callable(factory):
            raise ProviderConfigError("注册需要 ProviderSpec 和 adapter factory")
        if spec.id in self._entries and not replace:
            raise ProviderConfigError(f"供应商已注册：{spec.id}")
        self._entries[spec.id] = (spec, factory)

    def get(self, provider):
        name = canonical_provider(provider)
        if name not in self._entries:
            raise ProviderConfigError(f"未知供应商：{name}")
        return self._entries[name][0]

    def list_providers(self):
        return list(self._entries)

    def create(self, config, *, client=None):
        spec = self.get(config.provider)
        return self._entries[spec.id][1](config, spec, client=client)


def create_default_registry():
    registry = ProviderRegistry()
    common = dict(max_tokens_field="max_tokens", supports_temperature=True,
                  supports_tools=True, system_role="system", reasoning_format="none", replay_reasoning=False)
    specs = [
        ProviderSpec("openai", "openai-chat", "https://api.openai.com/v1", ("OPENAI_API_KEY",),
                     Compatibility(**{**common, "max_tokens_field": "max_completion_tokens", "reasoning_format": "openai"})),
        ProviderSpec("openai-compatible", "openai-chat", "https://api.openai.com/v1", (), Compatibility(**common)),
        ProviderSpec("deepseek", "openai-chat", "https://api.deepseek.com", ("DEEPSEEK_API_KEY",),
                     Compatibility(**{**common, "reasoning_format": "deepseek", "replay_reasoning": True})),
        ProviderSpec("qwen", "openai-chat", "https://dashscope.aliyuncs.com/compatible-mode/v1", ("DASHSCOPE_API_KEY", "QWEN_API_KEY"),
                     Compatibility(**{**common, "reasoning_format": "qwen"})),
        ProviderSpec("anthropic", "anthropic-messages", "https://api.anthropic.com", ("ANTHROPIC_API_KEY",),
                     Compatibility(**{**common, "reasoning_format": "anthropic"})),
    ]
    for spec in specs:
        factory = AnthropicMessagesProvider if spec.api == "anthropic-messages" else OpenAIChatProvider
        registry.register(spec, factory)
    return registry
