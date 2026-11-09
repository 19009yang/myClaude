"""用当前 .env 验证真实文本、工具回传与隔离记忆流程，不输出凭据。"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from urllib.parse import urlsplit

from dotenv import dotenv_values

from benchmarks.lamemory.smoke import run_smoke
from core.providers import ChatModel, ModelConfig, create_default_registry


def run():
    root = Path(__file__).resolve().parents[2]
    config = ModelConfig.from_env(env=dotenv_values(root / ".env"))
    base = config.base_url or create_default_registry().get(config.provider).base_url
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "provider": config.provider,
              "model": config.model, "endpoint_host": urlsplit(base).hostname,
              "checks": {}, "usage_events": [], "status": "running"}
    started = time.monotonic()

    def record(purpose, response):
        report["usage_events"].append({"purpose": purpose, "usage": response.get("usage", {})})

    with ChatModel(config) as model:
        try:
            response = model.complete([{"role": "user", "content": "这是接口连通性测试。请只回复 MODEL_OK。"}],
                                      timeout=min(config.timeout, 30), max_tokens=1024, max_retries=0)
            record("text", response)
            report["checks"]["text"] = {"passed": response["content"].strip() == "MODEL_OK",
                                           "answer": response["content"], "finish_reason": response.get("finish_reason")}
        except Exception as exc:
            report["checks"]["text"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
        print(json.dumps({"check": "text", **report["checks"]["text"]}, ensure_ascii=False), flush=True)
        if report["checks"]["text"]["passed"]:
            try:
                tools = [{"type": "function", "function": {"name": "test_add", "description": "返回两个整数的和，用于工具调用测试。",
                    "parameters": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                                   "required": ["a", "b"], "additionalProperties": False}}}]
                messages = [{"role": "user", "content": "工具协议测试：必须先调用 test_add(a=17,b=25)，再根据工具结果仅回复 TOOL_OK=42。不要自行跳过工具计算。"}]
                response = model.complete(messages, tools=tools, timeout=min(config.timeout, 30), max_tokens=1024, max_retries=0)
                record("tool_request", response)
                calls = response.get("tool_calls") or []
                if len(calls) != 1 or calls[0]["function"]["name"] != "test_add":
                    raise ValueError("模型没有生成预期的单个 test_add 调用")
                arguments = json.loads(calls[0]["function"]["arguments"])
                if arguments != {"a": 17, "b": 25}:
                    raise ValueError("工具调用参数与测试请求不一致")
                messages.extend([response, {"role": "tool", "tool_call_id": calls[0]["id"], "content": "42"}])
                final = model.complete(messages, tools=tools, timeout=min(config.timeout, 30), max_tokens=1024, max_retries=0)
                record("tool_response", final)
                report["checks"]["tool_roundtrip"] = {"passed": final["content"].strip() == "TOOL_OK=42" and not final.get("tool_calls"),
                                                          "tool_name": "test_add", "arguments": arguments, "answer": final["content"]}
            except Exception as exc:
                report["checks"]["tool_roundtrip"] = {"passed": False, "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps({"check": "tool_roundtrip", **report["checks"]["tool_roundtrip"]}, ensure_ascii=False), flush=True)
    if report["checks"]["text"]["passed"]:
        memory = run_smoke(real=True)
        report["memory_smoke"] = memory
        report["checks"]["memory"] = {"passed": memory["status"] == "passed", "status": memory["status"],
                                        "details": memory.get("checks", {}), "error": memory.get("error")}
        report["usage_events"].extend(memory.get("usage_events", []))
        (root / "benchmarks/lamemory/results/real-smoke.json").write_text(
            json.dumps(memory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"check": "memory", **report["checks"]["memory"]}, ensure_ascii=False), flush=True)
    report["status"] = "passed" if len(report["checks"]) == 3 and all(c["passed"] for c in report["checks"].values()) else "failed"
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["total_tokens"] = sum(e.get("usage", {}).get("total_tokens", 0) for e in report["usage_events"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/providers/results/live-smoke.json"))
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "model": report["model"], "total_tokens": report["total_tokens"],
                      "elapsed_seconds": report["elapsed_seconds"], "report": str(args.output)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
