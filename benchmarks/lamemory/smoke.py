"""隔离聊天 smoke，默认 fake；--real 显式调用环境中配置的模型。"""
import argparse
from dataclasses import asdict
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import platform
import tempfile
import time
from urllib.parse import urlsplit

from Agent.chatBot_with_memory.runner import ChatRunner
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.providers import ChatMemoryProvider
from core.providers import ModelConfig, ProviderConfigError, create_default_registry


class FakeProvider:
    def __init__(self):
        self.usage_events = []

    def extract(self, messages, *, max_items, timeout):
        self.usage_events.append({"purpose": "extraction", "usage": {"total_tokens": 0}, "fake": True})
        return [{"content": "项目代号是 Maple-Quartz-47。", "type": "fact",
                 "source_message_ids": [messages[0]["message_id"]], "slot_key": "project.codename"}]


def fake_chat(*, messages, **kwargs):
    recalled = "lamemory_data" in messages[0]["content"]
    return {"role": "assistant", "content": "项目代号是 Maple-Quartz-47。" if recalled else "收到。",
            "usage": {"total_tokens": 0}}


def run_smoke(real=False):
    root = Path(__file__).resolve().parents[2]
    paths = [*sorted((root / "core/lamemory").rglob("*.py")),
             *sorted((root / "core/providers").rglob("*.py")),
             *sorted((root / "core/lamemory").rglob("*.sql")),
             root / "core/llm.py", root / "core/memory.py", root / "tools/executor.py",
             root / "Agent/chatBot_with_memory/runner.py", root / "Agent/chatBot_with_memory/main.py",
             root / "tests/lamemory/fixtures/retrieval.json"]
    report = {"mode": "real" if real else "fake", "retrieval_mode": "keyword",
              "python": platform.python_version(), "status": "running", "usage_events": [], "checks": {},
              "source_hashes": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}
    if real:
        # core.llm loads dotenv; record configuration identity, never credentials or URL query strings.
        try:
            model_config = ModelConfig.from_env()
            memory_config = ModelConfig.from_env(prefix="MEMORY_LLM_") if any(k.startswith("MEMORY_LLM_") for k in os.environ) else model_config
        except ProviderConfigError as exc:
            return {**report, "status": "blocked", "error": str(exc)}
        for label, selected in (("agent_model", model_config), ("memory_model", memory_config)):
            base_url = selected.base_url or create_default_registry().get(selected.provider).base_url
            report[label] = {"provider": selected.provider, "model": selected.model, "endpoint_host": urlsplit(base_url).hostname}
    provider = ChatMemoryProvider(model_config=memory_config) if real else FakeProvider()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lamemory-smoke-") as temporary:
        cfg = LaMemoryConfig(database_path=Path(temporary) / "memory.sqlite3", provider_timeout_seconds=15)
        config_data = asdict(cfg)
        config_data.pop("database_path")
        report["config"] = config_data
        report["config_hash"] = hashlib.sha256(json.dumps(config_data, sort_keys=True).encode()).hexdigest()
        first = LaMemoryService(cfg, user_id="smoke", session_id="seed", provider=provider)
        second = LaMemoryService(cfg, user_id="smoke", session_id="recall", provider=provider)
        stranger = LaMemoryService(cfg, user_id="other", session_id="recall", provider=provider)
        from core.llm import call_llm
        chat = partial(call_llm, config=model_config) if real else fake_chat
        runner = ChatRunner(first, chat=chat)
        recall_runner = ChatRunner(second, chat=chat)
        try:
            first_result = runner.send("请长期记住：我的项目代号是 Maple-Quartz-47。", timeout=30)
            report["seed_errors"] = first_result["errors"]
            flushed = first.flush(timeout=30)
            report["flush"] = asdict(flushed)
            if flushed.status != "complete":
                raise RuntimeError("提取任务未完成：" + json.dumps(first.jobs(), ensure_ascii=False))
            result = recall_runner.send("我的项目代号是什么？请根据已保存的记忆回答。", timeout=30)
            hits = second.search("项目代号")
            report["checks"] = {
                "cross_session_retrieval": bool(hits),
                "source_trace": bool(hits and second.inspect(hits[0].item.memory_id)["sources"]),
                "user_isolation": not stranger.search("项目代号"),
                "context_retrieval": bool(result["memory_diagnostics"]["retrieved"]),
                "answer_grounded": "Maple-Quartz-47" in result["message"].get("content", ""),
                "persistence": not first_result["errors"] and not result["errors"],
            }
            report["answer"] = result["message"].get("content", "")
            report["retrieved"] = [h.to_dict() for h in hits]
            report["status"] = "passed" if all(report["checks"].values()) else "failed"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            report["usage_events"] = [*runner.usage_events, *recall_runner.usage_events,
                                       *getattr(provider, "usage_events", [])]
            for service in (first, second, stranger):
                service.close()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_smoke(args.real)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
