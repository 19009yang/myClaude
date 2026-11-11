"""离线协议验证替身；不产生可用于比较记忆效果的分数。"""
import json
import hashlib
from types import SimpleNamespace
from core.lamemory.embedding.space import EmbeddingDescriptor


class FakeEmbedding:
    """确定性假向量，只覆盖索引/查询协议，不表示真实语义质量。"""
    def __init__(self, query_prefix=''):
        self.descriptor = EmbeddingDescriptor("offline-fake-embedding", 8, "offline", "protocol-smoke", query_prefix=query_prefix)
        self.usage_events = []

    def embed_documents(self, texts, *, timeout):
        self.usage_events.append({"usage": {"prompt_tokens": len(texts) * 10}})
        return [[b + 1 for b in hashlib.sha256(t.encode()).digest()[:8]] for t in texts]

    def embed_query(self, text, *, timeout):
        return self.embed_documents([self.descriptor.query_prefix + text], timeout=timeout)[0]

    def close(self):
        pass


class FakeModel:
    def __init__(self, role, settings):
        self.role = role
        self.config = SimpleNamespace(model="offline-fake", provider="fake", max_tokens=settings.max_output_tokens)
        self.serial = 0

    def __call__(self, messages, tools=None, **kwargs):
        self.serial += 1
        response = {"role": "assistant", "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}
        if self.role == "agent":
            if messages[-1]["role"] == "tool":
                return {**response, "content": "离线 smoke 占位产物已写入。"}
            return {**response, "content": "", "tool_calls": [{"id": f"call_{self.serial}", "type": "function",
                    "function": {"name": "write", "arguments": {"path": "smoke.md", "content": "离线协议验证占位。"}}}]}
        if self.role == "judge":
            data = json.loads(messages[-1]["content"])
            content = {"criteria": [{"item_id": c["item_id"], "score": 0,
                                       "reason": "离线 smoke 假模型占位，不是实际效果评分。"}
                                      for c in data["rubric"]["criteria"]]}
        elif '请压缩以上对话历史' in messages[-1].get('content', ''):
            content = {'summary': '历史任务、文件操作及待确认事项的离线摘要占位。', 'memory_update': []}
        elif "memory_update" in messages[-1].get("content", ""):
            content = {"memory_update": [m["content"] for m in messages[:-1] if m["role"] == "user"]}
        elif "sources" in messages[-1].get("content", "") and "跨会话摘要" in messages[0]["content"]:
            data = json.loads(messages[-1]["content"])
            content = {"summary": "\n".join(s["content"] for s in data["sources"])[:1000] or "占位摘要"}
        else:
            data = json.loads(messages[-1]["content"])
            content = [{"content": m["content"][:3900], "type": "fact", "scope_kind": "user",
                        "source_message_ids": [m["message_id"]]} for m in data]
        return {**response, "content": json.dumps(content, ensure_ascii=False)}


class OfflineSandbox:
    deadline = None

    def run(self, directory, args, *, timeout=30):
        return {"status": "not_executed_offline_smoke", "returncode": None, "args": args}
