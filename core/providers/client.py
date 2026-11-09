"""同步模型入口：统一参数校验、生命周期和既有消息返回格式。"""
import copy

from .messages import validate_history, validate_tools
from .registry import create_default_registry
from .types import ProviderConfigError, RequestOptions


class ChatModel:
    def __init__(self, config, *, registry=None, client=None):
        self.config = config
        self.registry = registry if registry is not None else create_default_registry()
        self.spec = self.registry.get(config.provider)
        self.adapter = self.registry.create(config, client=client)
        self._closed = False

    def complete(self, messages, tools=None, system_prompt=None, *, model=None, timeout=None,
                 max_retries=None, max_tokens=None, temperature=None, reasoning_effort=None, thinking_budget=None):
        if self._closed:
            raise ProviderConfigError("模型实例已经关闭")
        history = copy.deepcopy(messages)
        if system_prompt:
            history = [{"role": "system", "content": system_prompt}, *history]
        validate_history(history)
        validate_tools(tools)
        options = RequestOptions(
            model=model, timeout=self.config.timeout if timeout is None else timeout,
            max_retries=self.config.max_retries if max_retries is None else max_retries,
            max_tokens=self.config.max_tokens if max_tokens is None else max_tokens,
            temperature=self.config.temperature if temperature is None else temperature,
            reasoning_effort=self.config.reasoning_effort if reasoning_effort is None else reasoning_effort,
            thinking_budget=self.config.thinking_budget if thinking_budget is None else thinking_budget)
        return self.adapter.complete(history, copy.deepcopy(tools), options)

    __call__ = complete

    def close(self):
        if not self._closed:
            self.adapter.close()
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
