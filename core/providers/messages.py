"""统一历史的验证、工具 ID 映射和供应商元数据处理。"""
import copy
import hashlib
import json
import re

from .types import ProviderConfigError


def field(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def as_dict(value):
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(exclude_none=True)
    raise ProviderConfigError("响应字段不是对象")


def arguments(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            raise ProviderConfigError("工具参数必须为合法 JSON 对象") from None
    if not isinstance(value, dict):
        raise ProviderConfigError("工具参数必须为对象")
    return value


def safe_call_id(value, limit=40):
    if not isinstance(value, str) or not value:
        raise ProviderConfigError("工具调用缺少 ID")
    if len(value) <= limit and re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return "call_" + hashlib.sha256(value.encode()).hexdigest()[:30]


def validate_history(messages):
    """所有调用必须有对应结果；原始传入列表始终不被修改。"""
    if not isinstance(messages, list) or not messages:
        raise ProviderConfigError("messages 必须为非空数组")
    pending, seen, normalized_ids = set(), set(), set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise ProviderConfigError("不支持的消息角色")
        role = message["role"]
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise ProviderConfigError("当前适配层仅支持文本消息和工具调用")
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ProviderConfigError("工具结果没有对应调用，或结果重复")
            pending.remove(call_id)
            continue
        if pending:
            raise ProviderConfigError("工具结果未全部保存")
        calls = message.get("tool_calls") or []
        if not isinstance(calls, list) or calls and role != "assistant":
            raise ProviderConfigError("无效的 tool_calls")
        for call in calls:
            if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
                raise ProviderConfigError("无效的工具调用结构")
            call_id = call.get("id")
            normalized_id = safe_call_id(call_id)
            if call_id in seen or not isinstance(call["function"].get("name"), str) or not call["function"]["name"]:
                raise ProviderConfigError("工具调用 ID 重复或缺少名称")
            if normalized_id in normalized_ids:
                raise ProviderConfigError("工具调用 ID 规范化后发生冲突")
            arguments(call["function"].get("arguments", {}))
            seen.add(call_id)
            normalized_ids.add(normalized_id)
            pending.add(call_id)
    if pending:
        raise ProviderConfigError("调用模型前必须提供全部工具结果")


def validate_tools(tools):
    if tools is not None and not isinstance(tools, list):
        raise ProviderConfigError("tools 必须为数组")
    for tool in tools or []:
        function = tool.get("function", {}) if isinstance(tool, dict) else {}
        if not isinstance(tool, dict) or not isinstance(function, dict) or tool.get("type") != "function" or not function.get("name") or not isinstance(function.get("parameters"), dict):
            raise ProviderConfigError("工具定义必须为 function 和对象 schema")


def same_origin(message, config, api, default_base_url):
    metadata = message.get("provider_data") or {}
    return (isinstance(metadata, dict) and metadata.get("provider") == config.provider and
            metadata.get("model") == config.model and metadata.get("api") == api and
            metadata.get("base_url") == (config.base_url or default_base_url).rstrip("/"))


def usage_value(usage, key):
    value = field(usage, key, 0)
    return value if isinstance(value, int) and value >= 0 else 0
