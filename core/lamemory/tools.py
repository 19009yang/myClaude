"""每个聊天实例绑定的主动工具；模型参数不包含身份和来源选择。"""
from tools.builtins.tool_def import Tool


def get_memory_tools(service):
    def sources():
        rows = service.db.messages(service.user_id, service.session_id, service.current_turn_id)
        return [r["message_id"] for r in rows if r["role"] == "user"]

    def store(content, scope_kind="user"):
        return service.store(content, sources(), scope_kind=scope_kind).to_dict()

    def search(query):
        return {"mode": "keyword", "results": [h.to_dict() for h in service.search(query)],
                "summaries": service.search_summaries(query), "experiences": service.search_experiences(query)}

    def update(memory_id, content):
        return service.update(memory_id, content, sources()).to_dict()

    definitions = [
        ("lamemory_store", "用户明确要求记住时使用；立即持久化并返回状态和 ID。", {"content": {"type": "string"},
         "scope_kind": {"type": "string", "enum": ["user", "session", "project"]}}, store),
        ("lamemory_search", "关键词查找有效记忆，返回 ID、来源和命中原因。", {"query": {"type": "string"}}, search),
        ("lamemory_update", "按 ID 更正当前有效记忆，保留旧版本和更正依据。",
         {"memory_id": {"type": "string"}, "content": {"type": "string"}}, update),
        ("lamemory_forget", "停止使用指定记忆并抑制自动重提取；原始对话仍留档。",
         {"memory_id": {"type": "string"}}, service.forget),
        ("lamemory_experience_search", "查找已验证经验及其适用条件、步骤和结果依据。",
         {"query": {"type": "string"}}, lambda query: {"results": service.search_experiences(query)}),
        ("lamemory_experience_inspect", "查看经验状态、反馈和原始来源。",
         {"experience_id": {"type": "string"}}, service.inspect_experience),
        ("lamemory_experience_revoke", "用户要求停止使用错误经验时撤销指定经验。",
         {"experience_id": {"type": "string"}}, service.revoke_experience),
    ]
    return [Tool(name, description, {"type": "object", "properties": properties,
                                    "required": [key for key in properties if key != "scope_kind"], "additionalProperties": False}, fn)
            for name, description, properties, fn in definitions]
