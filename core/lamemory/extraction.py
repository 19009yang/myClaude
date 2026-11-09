"""不信任模型字段，先验证整个批次，再原子写入。"""
import re
from datetime import date, datetime, timezone


def normalize_time(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("occurred_at 必须为 ISO 日期/时间或 null")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value).isoformat()
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("事件时间需要时区；不能推断发生时间")
    return parsed.astimezone(timezone.utc).isoformat()


def validate_candidate(data, config, *, allowed_sources=None):
    if not isinstance(data, dict):
        raise ValueError("记忆项必须为对象")
    allowed = {"content", "type", "scope_kind", "slot_key", "occurred_at", "source_message_ids", "correction"}
    if set(data) - allowed:
        raise ValueError("提取结果含不受信任或未知字段")
    content = data.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > config.max_content_chars:
        raise ValueError("记忆内容为空或过长")
    kind = data.get("type", "fact")
    scope = data.get("scope_kind", "user")
    if kind not in {"fact", "preference", "event"} or scope not in {"user", "session"}:
        raise ValueError("无效的记忆类型或作用范围")
    sources = data.get("source_message_ids", [])
    if not isinstance(sources, list) or any(not isinstance(s, str) for s in sources):
        raise ValueError("来源必须为消息 ID 数组")
    if allowed_sources is not None and (not sources or not set(sources) <= allowed_sources):
        raise ValueError("自动提取来源必须属于本轮用户消息")
    slot = data.get("slot_key")
    if slot is not None and (not isinstance(slot, str) or not slot.strip() or len(slot) > 200):
        raise ValueError("无效的 slot_key")
    if not isinstance(data.get("correction", False), bool):
        raise ValueError("correction 必须为布尔值")
    return {"content": content.strip(), "type": kind, "scope_kind": scope,
            "slot_key": slot, "occurred_at": normalize_time(data.get("occurred_at")),
            "source_message_ids": list(dict.fromkeys(sources)), "correction": data.get("correction", False)}


def validate_batch(result, messages, config):
    if not isinstance(result, list) or len(result) > config.max_extracted_items:
        raise ValueError("提取结果必须是限长数组")
    sources = {m["message_id"] for m in messages if m["payload"]["role"] == "user"}
    return [validate_candidate(item, config, allowed_sources=sources) for item in result]
