"""M2/M3 在线交付 smoke：真实模型、临时库、合成数据及可检查的本地工具证据。"""
import argparse
from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
import time
from urllib.parse import urlsplit

from dotenv import load_dotenv

from Agent.chatBot_with_memory.runner import ChatRunner
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.providers import ChatMemoryProvider
from core.providers import ChatModel, ModelConfig, create_default_registry
from tools.builtins.tool_def import Tool


ROOT = Path(__file__).resolve().parents[2]


def check(stage, name, passed, evidence=None):
    stage["checks"][name] = {"passed": bool(passed), "evidence": evidence}
    print(json.dumps({"check": name, "passed": bool(passed)}, ensure_ascii=False), flush=True)


def flush(memory, stage, label):
    result = memory.flush(timeout=180)
    stage.setdefault("flushes", {})[label] = asdict(result)
    if result.status != "complete":
        raise RuntimeError(f"{label}: {result.status}; inspect recorded jobs")


def run_m2(path, provider, chat, stage):
    cfg = LaMemoryConfig(database_path=path, project_id="lamemory-demo", provider_timeout_seconds=45)
    with ExitStack() as stack:
        def service(session, *, user="live-user", project="lamemory-demo"):
            return stack.enter_context(LaMemoryService(replace(cfg, project_id=project), user_id=user,
                                                      session_id=session, provider=provider))
        memory = service("m2-seed")
        try:
            source = memory.append_message({"role": "user", "content": "开发回答偏好中文；lamemory-demo 项目的服务端口是 43127。"})
            preference = memory.store("开发回答偏好中文", [source], type="preference").item
            port = memory.store("lamemory-demo 项目的服务端口是 43127", [source],
                                scope_kind="project", slot_key="service.port").item
            memory.append_message({"role": "assistant", "content": "已按用户与项目范围保存。"})
            memory.refresh_summaries()
            flush(memory, stage, "initial")
            profile_id = memory.db.one("SELECT summary_id FROM lamemory_summaries WHERE user_id=? AND kind='profile'",
                                       (memory.user_id,))["summary_id"]
            topic_id = memory.db.one("SELECT summary_id FROM lamemory_summaries WHERE user_id=? AND kind='topic'",
                                     (memory.user_id,))["summary_id"]
            initial = {"profile": memory.inspect_summary(profile_id), "topic": memory.inspect_summary(topic_id)}
            stage["initial"] = initial
            check(stage, "m2_real_hierarchy", "中文" in initial["profile"]["content"] and
                  "43127" in initial["topic"]["content"] and "43127" not in initial["profile"]["content"], initial)
            check(stage, "m2_source_versions", any(s["memory_id"] == preference.memory_id for s in initial["profile"]["sources"]) and
                  any(s["memory_id"] == port.memory_id and s["source_revision"] == 1 for s in initial["topic"]["sources"]))

            recall = service("m2-recall")
            def recall_port(label, expected):
                recall.append_message({"role": "user", "content": 'lamemory-demo 项目的服务端口是多少？依据记忆只返回 JSON：{"port":整数}。'})
                context = recall.build_context("只依据提供的记忆回答，不猜测。")
                response = chat(messages=context.messages, timeout=45, max_tokens=2048)
                recall.append_message(response)
                stage[label] = {"response": response, "diagnostics": context.diagnostics}
                check(stage, label, json.loads(response["content"]) == {"port": expected} and
                      any(s["summary_id"] == topic_id for s in context.diagnostics["summaries"]), stage[label])
            recall_port("m2_cross_session_answer", 43127)
            other = service("m2-other-project", project="other-project")
            stranger = service("m2-other-user", user="other-user")
            check(stage, "m2_scope_isolation", not other.search("43127") and not stranger.search("43127") and
                  "43127" not in json.dumps(other.search_summaries("服务端口"), ensure_ascii=False) and
                  not stranger.search_summaries("服务端口"))

            correction = memory.append_message({"role": "user", "content": "更正服务端口，原来的 43127 已废弃，新的端口是 52941。"})
            replacement = memory.update(port.memory_id, "lamemory-demo 项目的服务端口是 52941", [correction]).item
            memory.append_message({"role": "assistant", "content": "已更正端口。"})
            dirty = memory.inspect_summary(topic_id)
            check(stage, "m2_dirty_summary_excluded", dirty["dirty"] == 1 and
                  all(s["summary_id"] != topic_id for s in recall.search_summaries("服务端口")), dirty)
            flush(memory, stage, "correction")
            rebuilt = memory.inspect_summary(topic_id)
            stage["rebuilt"] = rebuilt
            check(stage, "m2_rebuild_new_revision", "52941" in rebuilt["content"] and "43127" not in rebuilt["content"] and
                  any(s["memory_id"] == replacement.memory_id and s["source_revision"] == 2 for s in rebuilt["sources"]), rebuilt)
            recall_port("m2_corrected_answer", 52941)
            memory.forget(replacement.memory_id)
            check(stage, "m2_forget_immediate", not recall.search("52941") and
                  all(s["summary_id"] != topic_id for s in recall.search_summaries("服务端口")))
            flush(memory, stage, "forget")
            forgotten = memory.inspect_summary(topic_id)
            stage["forgotten"] = forgotten
            check(stage, "m2_forget_rebuild_preserves_evidence", forgotten["content"] == "" and not forgotten["sources"] and
                  any(r["message_id"] == source for r in memory.history()) and
                  any(r["message_id"] == correction for r in memory.history()), forgotten)
        finally:
            stage["history"] = memory.history()
            stage["jobs"] = memory.jobs()


def run_m3(path, provider, chat, stage):
    cfg = LaMemoryConfig(database_path=path, provider_timeout_seconds=45)
    executions = []

    def sort_unique(values):
        if not isinstance(values, list) or any(type(v) is not int for v in values):
            raise ValueError("values must be an integer array")
        result = {"values": sorted(set(values))}
        executions.append({"input": values, "output": result})
        return result

    tool = Tool("sort_unique", "将整数列表升序排序并去重，返回 JSON 对象 values。",
                {"type": "object", "properties": {"values": {"type": "array", "items": {"type": "integer"}}},
                 "required": ["values"], "additionalProperties": False}, sort_unique)
    prompt = "你负责整数列表排序去重任务。使用 sort_unique 工具执行，核对结果；可参考已验证经验。最终只返回工具输出的 JSON 对象，不用 Markdown。"
    with ExitStack() as stack:
        def service(session, user="live-user"):
            return stack.enter_context(LaMemoryService(cfg, user_id=user, session_id=session, provider=provider))
        memory = service("m3-training")
        runner = ChatRunner(memory, chat=chat, tools=[tool], system_prompt=prompt)

        def trial(active_runner, values, label):
            before = len(executions)
            result = active_runner.send(f"整数列表排序去重：{json.dumps(values)}。请调用 sort_unique 再给结果。", timeout=120)
            rows = active_runner.memory.db.messages(active_runner.memory.user_id, active_runner.memory.session_id,
                                                    active_runner.memory.current_turn_id)
            expected = {"values": sorted(set(values))}
            actual = executions[before:]
            passed = not result["errors"] and bool(actual) and all(e["input"] == values and e["output"] == expected for e in actual)
            passed = passed and json.loads(result["message"]["content"]) == expected
            evidence = {"input": values, "expected": expected, "executions": actual, "result": result,
                        "history": rows, "turn_id": active_runner.memory.current_turn_id}
            stage.setdefault("trials", {})[label] = evidence
            check(stage, label, passed, evidence)
            if not passed:
                raise RuntimeError(f"{label}: actual tool execution or answer failed")
            return rows[0]["message_id"], evidence

        try:
            training_source, _ = trial(runner, [9, 3, 9, 1], "m3_training_tool_execution")
            job = memory.capture_experience()
            check(stage, "m3_capture_idempotent", job == memory.capture_experience(), job)
            flush(memory, stage, "candidate_extraction")
            candidates = memory.list_experiences("candidate")
            stage["candidates"] = candidates
            check(stage, "m3_real_candidate_extraction", bool(candidates) and
                  all(c["sources"] and c["steps"] and c["conditions"] and c["result_basis"] for c in candidates), candidates)
            if not candidates:
                raise RuntimeError("online extraction returned no reusable candidate")
            # Exercise the first real candidate; do not substitute a hand-written candidate.
            candidate = candidates[0]
            experience_id = candidate["experience_id"]
            # A host-side applicability check prevents unrelated extracted advice receiving success feedback.
            description = json.dumps([candidate["task_type"], candidate["conditions"], candidate["steps"]], ensure_ascii=False)
            check(stage, "m3_candidate_applicable", "sort_unique" in description and "排序" in description and "去重" in description)
            if not stage["checks"]["m3_candidate_applicable"]["passed"]:
                raise RuntimeError("candidate does not describe the executed sorting workflow")
            check(stage, "m3_candidate_not_retrieved", not memory.search_experiences("整数列表排序去重"))
            replay = memory.experience_feedback(experience_id, "success", [training_source], evidence_kind="test",
                                                 evidence="训练任务工具输出与宿主计算一致；这是训练重放，不是独立验证。")
            check(stage, "m3_training_not_counted", replay["successes"] == 0 and replay["status"] == "candidate")
            # Independent trials receive the extracted steps even before promotion, solely for validation.
            # They are explicitly supplied by the trusted test host, not retrieved as active memory.
            runner.system_prompt = prompt + "\n待验证的候选步骤（不是已验证经验）：" + json.dumps(candidate["steps"], ensure_ascii=False)
            for number, values in enumerate(([12, -4, 12, 0], [7, 2, 7, -8, 2]), 1):
                source, evidence = trial(runner, values, f"m3_independent_execution_{number}")
                feedback = memory.experience_feedback(experience_id, "success", [source], evidence_kind="test",
                                                       evidence=json.dumps({"assertion": "tool output and final JSON equal sorted(set(input))", "input": values,
                                                                            "expected": evidence["expected"], "passed": True}, ensure_ascii=False))
                duplicate = memory.experience_feedback(experience_id, "success", [source], evidence_kind="test", evidence="同一实际任务反馈重放")
                check(stage, f"m3_feedback_{number}", feedback["successes"] == number and duplicate["successes"] == number and
                      feedback["status"] == ("active" if number == 2 else "candidate"), feedback)
            active = memory.inspect_experience(experience_id)
            stage["active"] = active
            recall = service("m3-recall")
            recall_runner = ChatRunner(recall, chat=chat, tools=[tool], system_prompt=prompt)
            _, result = trial(recall_runner, [41, 8, 41, -2], "m3_cross_session_execution")
            check(stage, "m3_active_context_recall", any(e["experience_id"] == experience_id
                  for e in result["result"]["memory_diagnostics"]["experiences"]), result["result"]["memory_diagnostics"])
            check(stage, "m3_user_isolation", not service("m3-stranger", user="other-user").search_experiences("整数列表排序去重"))
            revoked = memory.revoke_experience(experience_id)
            check(stage, "m3_revocation", revoked["status"] == "revoked" and not recall.search_experiences("整数列表排序去重") and
                  not recall.build_context(prompt).diagnostics["experiences"], revoked)
            # Reopen after revocation to check persisted state, with a distinct connection.
            resumed = service("m3-restarted")
            check(stage, "m3_restart_preserves_revocation", resumed.inspect_experience(experience_id)["status"] == "revoked" and
                  not resumed.search_experiences("整数列表排序去重"))
        finally:
            stage["history"] = memory.history()
            stage["jobs"] = memory.jobs()
            stage["executions"] = executions


def run():
    load_dotenv(ROOT / ".env")
    started = time.monotonic()
    paths = [Path(__file__), *sorted((ROOT / "core/lamemory").rglob("*.py")),
             *sorted((ROOT / "core/lamemory").rglob("*.sql")), *sorted((ROOT / "core/providers").rglob("*.py")),
             ROOT / "Agent/chatBot_with_memory/runner.py", ROOT / "tools/executor.py"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "mode": "real", "status": "running",
              "retrieval_mode": "keyword", "isolation": "temporary databases and synthetic messages only",
              "limits": ["single synthetic workflow; no task-quality comparison or generalization claim",
                         "M2 memories seeded through trusted host; M3 feedback from actual local deterministic tools",
                         "revocation explicitly requested by test host; no simulated user approval"],
              "source_hashes": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
              "calls": [], "stages": {}}
    try:
        config = ModelConfig.from_env()
        if config.provider != "deepseek":
            raise ValueError("This smoke requires the explicitly configured deepseek provider")
        # Bound requests independently of any default retry or output settings.
        config = replace(config, timeout=45, max_retries=0, max_tokens=4096)
        base = config.base_url or create_default_registry().get(config.provider).base_url
        report["model"] = {"provider": config.provider, "model": config.model, "endpoint_host": urlsplit(base).hostname,
                           "timeout": config.timeout, "max_retries": config.max_retries, "max_tokens": config.max_tokens}
        report["config_hash"] = hashlib.sha256(json.dumps(report["model"], sort_keys=True).encode()).hexdigest()
        with ChatModel(config) as model, tempfile.TemporaryDirectory(prefix="lamemory-live-m2-m3-") as temporary:
            def chat(**kwargs):
                kwargs["timeout"] = min(kwargs.get("timeout", config.timeout), config.timeout)
                kwargs["max_retries"] = 0
                call = {"request": kwargs, "started_at": datetime.now(timezone.utc).isoformat()}
                report["calls"].append(call)
                tick = time.monotonic()
                try:
                    response = model.complete(**kwargs)
                    call["response"] = response
                    return response
                except Exception as exc:
                    call["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    call["elapsed_seconds"] = round(time.monotonic() - tick, 3)
            provider = ChatMemoryProvider(chat=chat)
            for name, action in (("m2", run_m2), ("m3", run_m3)):
                stage = {"status": "running", "checks": {}}
                report["stages"][name] = stage
                try:
                    action(Path(temporary) / f"{name}.sqlite3", provider, chat, stage)
                    stage["status"] = "passed" if stage["checks"] and all(c["passed"] for c in stage["checks"].values()) else "failed"
                except Exception as exc:
                    stage.update(status="failed", error=f"{type(exc).__name__}: {exc}")
                print(json.dumps({"stage": name, "status": stage["status"], "error": stage.get("error")}, ensure_ascii=False), flush=True)
            report["memory_usage_events"] = provider.usage_events
            report["status"] = "passed" if all(s["status"] == "passed" for s in report["stages"].values()) else "failed"
    except Exception as exc:
        report.update(status="blocked", error=f"{type(exc).__name__}: {exc}")
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["usage_events"] = [c["response"].get("usage", {}) for c in report["calls"] if "response" in c]
    report["total_tokens"] = sum(e.get("total_tokens", 0) for e in report["usage_events"])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Use a new path to preserve previous evidence")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; select a new path to preserve original evidence")
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "total_tokens": report["total_tokens"], "calls": len(report["calls"]),
                      "elapsed_seconds": report["elapsed_seconds"], "report": str(args.output)}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
