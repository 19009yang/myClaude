"""每个聊天实例绑定的主动工具；模型参数不包含身份和来源选择。"""
from tools.builtins.tool_def import Tool


def get_memory_tools(service):
    def sources():
        rows = service.db.messages(service.user_id, service.session_id, service.current_turn_id)
        return [r["message_id"] for r in rows if r["role"] == "user"]

    def store(content):
        return service.store(content, sources()).to_dict()

    def search(query):
        return {"mode": "keyword", "results": [h.to_dict() for h in service.search(query)]}

    def update(memory_id, content):
        return service.update(memory_id, content, sources()).to_dict()

    definitions = [
        ("lamemory_store", "用户明确要求记住时使用；立即持久化并返回状态和 ID。", {"content": {"type": "string"}}, store),
        ("lamemory_search", "关键词查找有效记忆，返回 ID、来源和命中原因。", {"query": {"type": "string"}}, search),
        ("lamemory_update", "按 ID 更正当前有效记忆，保留旧版本和更正依据。",
         {"memory_id": {"type": "string"}, "content": {"type": "string"}}, update),
        ("lamemory_forget", "停止使用指定记忆并抑制自动重提取；原始对话仍留档。",
         {"memory_id": {"type": "string"}}, service.forget),
    ]
    return [Tool(name, description, {"type": "object", "properties": properties,
                                    "required": list(properties), "additionalProperties": False}, fn)
            for name, description, properties, fn in definitions]
