"""旧 Memory 接口的 LaMemory 兼容门面；旧文件只显式导入一次。"""
from dataclasses import replace
from pathlib import Path
import warnings

from .lamemory import LaMemoryConfig, LaMemoryService
from .lamemory.providers import ChatMemoryProvider


class Memory:
    def __init__(self, memory_type="memory", memory_path=None, *, database_path=None,
                 user_id="local-user", session_id=None, provider=None, config=None,
                 embedding_provider=None, reranker=None, env=None, offline=False,
                 retrieval_mode=None, rerank=None):
        if memory_type != "memory":
            raise ValueError("memory_type 仅保留兼容值 'memory'，新配置请使用 LaMemoryConfig")
        if memory_path is not None:
            warnings.warn("memory_path 是旧 JSONL 导入源；新存储使用 database_path", DeprecationWarning, stacklevel=2)
            legacy = Path(memory_path).expanduser().resolve()
            database_path = database_path or legacy.parent / "lamemory.sqlite3"
            session_id = session_id or "legacy-session"
        cfg = config or LaMemoryConfig()
        if database_path is not None:
            cfg = replace(cfg, database_path=database_path)
        from .lamemory.runtime import create_memory
        self.service = create_memory(cfg, user_id=user_id, session_id=session_id,
            provider=provider if provider is not None else (None if offline else ChatMemoryProvider()),
            embedding_provider=embedding_provider, reranker=reranker, env=env, offline=offline,
            retrieval_mode=retrieval_mode, rerank=rerank)
        if memory_path is not None and legacy.is_file():
            from .lamemory.migration import import_legacy
            import_legacy(self.service, session_path=legacy)

    @property
    def messages(self):
        return [row["payload"] for row in self.service.history()]

    @property
    def last_context(self):
        return self.service.last_context

    def add_message(self, message):
        """记录原文；最终助手回复只投递任务，不执行模型提取。"""
        source = self.service.append_message(message)
        if message.get("role") == "assistant" and not message.get("tool_calls"):
            self.service.commit_turn()
        return source

    def build_context(self, system_prompt="", *, budget=None, tools=None):
        return self.service.build_context(system_prompt, budget, tools=tools).messages

    def get_last_turn_messages(self):
        return [r["payload"] for r in self.service.db.messages(self.service.user_id, self.service.session_id,
                                                              self.service.current_turn_id)]

    def extract_memory_update(self, messages=None):
        """兼容入口：原文必须已持久化，使用同一轮次任务，避免第二条提取路径。"""
        self.service.commit_turn()
        return self.service.process_pending(1)

    def compress(self, total_tokens=None):
        if total_tokens is not None and total_tokens < self.service.config.input_budget * 0.9:
            return {"status": "unchanged"}
        return self.service.compress()

    def process_pending(self, max_jobs=1):
        return self.service.process_pending(max_jobs)

    def close(self):
        self.service.close()
