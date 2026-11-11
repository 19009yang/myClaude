"""三个适配器；旧算法只访问本次运行目录，绝不实例化真实 Memory 门面。"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import time

from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.integration.context import estimate_tokens, MESSAGE_KEYS, valid_chain
from core.lamemory.models import BudgetError, ContextResult
from core.lamemory.generation.providers import ChatMemoryProvider
from .settings import save_json

FROZEN = Path(__file__).parent / "frozen/legacy.py"
LEGACY_COMMIT = "72fa6ee96492f92ea60887cef6d3681bc549bbd8"


class NoMemory:
    def __init__(self, directory, session, settings, chat=None):
        self.directory, self.session, self.settings = directory, session, settings
        self.config = memory_config(directory, settings)
        self.messages = []
        self.committed = 0

    def append_message(self, message):
        self.messages.append(copy.deepcopy(message))
        mid = f"{self.session}:{len(self.messages)}"
        with (self.directory / "session.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"message_id": mid, "payload": message}, ensure_ascii=False) + "\n")
        return mid

    def build_context(self, system_prompt="", budget=None, *, tools=None):
        messages = [{"role": "system", "content": system_prompt},
                    *[{k: v for k, v in m.items() if k in MESSAGE_KEYS} for m in self.messages]]
        return bounded_context(messages, tools, self.config, "none", budget)

    def commit_turn(self):
        # This group never writes or reads long-term memory.
        self.committed = len(self.messages)

    def flush(self, timeout=None):
        return {"status": "complete"}

    def close(self):
        pass


def bounded_context(messages, tools, config, mode, budget=None):
    tokens = estimate_tokens(messages) + (estimate_tokens(tools) if tools else 0)
    limit = min(config.input_budget, budget or config.input_budget)
    if tokens > limit:
        raise BudgetError(f"{mode} 输入超限：{tokens} > {limit}；没有静默裁剪长期记忆")
    return ContextResult(messages, {"estimated_tokens": tokens, "input_budget": limit,
                                    "token_count_mode": "utf8_estimate", "retrieval_mode": mode})


def memory_config(directory, settings):
    return LaMemoryConfig(database_path=directory / "lamemory.sqlite3",
                          context_window=settings.context_window, reserved_output=settings.max_output_tokens,
                          memory_token_budget=settings.memory_token_budget,
                          retrieval_mode=settings.retrieval_mode, allow_keyword_fallback=False,
                          provider_timeout_seconds=settings.memory_timeout,
                          job_max_attempts=settings.job_attempts, auto_learn_experiences=False)


class LaMemory:
    def __init__(self, directory, session, settings, chat, *, memory_factory=None):
        self.settings = settings
        self.config = memory_config(directory, settings)
        self.provider = ChatMemoryProvider(chat=chat)
        factory = memory_factory or LaMemoryService
        self.service = factory(self.config, user_id="memory-benchmark-user", session_id=session,
                               provider=self.provider)
        self.config = self.service.config

    def append_message(self, message):
        return self.service.append_message(message)

    def build_context(self, *args, **kwargs):
        if self.config.retrieval_mode == "hybrid":
            status = self.service.embedding_status()
            if status["missing"] or status["invalid_vectors"] or status["failed"]:
                raise RuntimeError("LaMemory 向量索引不完整；不能作为 hybrid 任务继续运行")
        return self.service.build_context(*args, **kwargs)

    def commit_turn(self):
        self.service.commit_turn()

    def flush(self, timeout=None, *, index=True):
        deadline = time.monotonic() + (min(self.settings.memory_timeout, timeout)
                                       if timeout is not None else self.settings.memory_timeout)
        result = self.service.flush(max(0, deadline - time.monotonic()))
        evidence = {**vars(result), "jobs": self.service.jobs()}
        if index and result.status == "complete" and self.config.retrieval_mode == "hybrid":
            remaining = deadline - time.monotonic()
            embedding = (self.service.flush_embeddings(timeout=remaining) if remaining > 0 else
                         {"status": "timeout", **self.service.embedding_status()})
            evidence.update(embedding=embedding, embedding_jobs=self.service.embedding_index.jobs())
            if embedding["status"] != "complete":
                evidence["status"] = embedding["status"]
        return evidence

    def close(self):
        self.service.close()


class Legacy(NoMemory):
    """实例级装载冻结算法：注入路径/模型，修复 usage 门控与压缩数组写入。"""
    def __init__(self, directory, session, settings, chat):
        super().__init__(directory, session, settings, chat)
        spec = importlib.util.spec_from_file_location("lamemory_benchmark_frozen_legacy", FROZEN)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.MEMORY_FILEPATH = directory / f"{session}.jsonl"
        module.LONG_TERM_MEMORY_FILEPATH = directory / "MEMORY.md"
        module.MAX_CONTEXT_LENGTH = settings.context_window
        self.flush_deadline = None

        def checked_call(**kwargs):
            remaining = min(settings.memory_timeout, self.flush_deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("legacy 记忆处理超时")
            response = chat(**kwargs, timeout=remaining,
                            max_tokens=settings.max_output_tokens, max_retries=0)
            data = json.loads(response.get("content", ""))
            updates = data.get("memory_update")
            if not isinstance(updates, list) or not all(isinstance(item, str) for item in updates):
                raise ValueError("legacy memory_update 必须为字符串数组")
            # The historical compression path tried to concatenate a list to a string.
            if "summary" in data:
                if not isinstance(data["summary"], str):
                    raise ValueError("legacy summary 必须为字符串")
                data["memory_update"] = "\n".join("- " + s for s in updates)
                response = {**response, "content": json.dumps(data, ensure_ascii=False)}
            return response

        module.call_llm = checked_call
        self.legacy = module.Memory()
        self.last_tokens = 0
        self.pending = False

    def append_message(self, message):
        self.last_tokens = message.get("usage", {}).get("total_tokens", 0)
        # Explicit commit_turn triggers extraction even when usage is absent (seed ACKs).
        self.legacy.add_message({k: copy.deepcopy(v) for k, v in message.items() if k in MESSAGE_KEYS})
        self.messages = self.legacy.messages
        return f"{self.session}:{len(self.messages)}"

    def build_context(self, system_prompt="", budget=None, *, tools=None):
        return bounded_context(self.legacy.build_context(system_prompt), tools, self.config, "legacy", budget)

    def commit_turn(self):
        if not valid_chain(self.legacy.get_last_turn_messages(), complete=True):
            raise ValueError("legacy 提交的轮次不完整")
        self.pending = True

    def flush(self, timeout=None):
        if not self.pending:
            return {"status": "complete", "attempts": 0}
        self.flush_deadline = time.monotonic() + (min(self.settings.memory_timeout, timeout)
                                                 if timeout is not None else self.settings.memory_timeout)
        errors = []
        for attempt in range(self.settings.job_attempts):
            try:
                self.legacy.extract_memory_update(self.legacy.get_last_turn_messages())
                self.pending = False
                # Extraction is not repeated if optional compression fails.
                self.legacy.compress(self.last_tokens)
                return {"status": "complete", "attempts": attempt + 1, "errors": errors}
            except Exception as exc:
                errors.append({"error_type": type(exc).__name__, "message": str(exc)[:500]})
                if not self.pending:
                    break
        return {"status": "failed", "attempts": len(errors), "errors": errors}


BACKENDS = {"none": NoMemory, "legacy": Legacy, "lamemory": LaMemory}


def build_seed(group, directory, sessions, settings, chat, on_progress=None, *, memory_factory=None):
    """Seed each user message as a complete turn, retaining message provenance."""
    directory.mkdir(parents=True)
    mapping, results = {}, []
    started = time.monotonic()
    for session in sessions:
        kwargs = {"memory_factory": memory_factory} if group == "lamemory" else {}
        backend = BACKENDS[group](directory, session["session_id"], settings, chat, **kwargs)
        try:
            for message in session["messages"]:
                mid = backend.append_message({"role": "user", "content": message["content"]})
                mapping[message["message_id"]] = {"runtime_message_id": mid, "session_id": session["session_id"],
                                                   "date": session["date"], "kind": session["kind"],
                                                   "content_sha256": hashlib.sha256(message["content"].encode()).hexdigest()}
                backend.append_message({"role": "assistant", "content": "已收到。"})
                backend.commit_turn()
                result = backend.flush(index=False) if group == "lamemory" else backend.flush()
                results.append({"message_id": message["message_id"], **result})
                save_json(directory / "seed_evidence.json", mapping)
                save_json(directory / "seed_jobs.json", results)
                if result["status"] != "complete":
                    raise RuntimeError(f"{group} 建库失败：{message['message_id']}；见 seed_jobs.json")
                if on_progress:
                    on_progress(message["message_id"])
        finally:
            backend.close()
    if group == "lamemory":
        backend = BACKENDS[group](directory, "seed-maintenance", settings, chat, memory_factory=memory_factory)
        try:
            if settings.hierarchy:
                backend.service.refresh_summaries()
            result = backend.flush()
            save_json(directory / "maintenance_jobs.json", result)
            if settings.hierarchy:
                save_json(directory / "hierarchy_jobs.json", result)
            if result["status"] != "complete":
                raise RuntimeError("LaMemory 分层摘要或向量索引未完成，不能冻结快照")
        finally:
            backend.close()
    save_json(directory / "snapshot.json", {"group": group, "status": "complete",
                                             "retrieval_mode": settings.retrieval_mode if group == "lamemory" else group,
                                             "seed_turns": len(mapping), "elapsed_seconds": time.monotonic() - started})
