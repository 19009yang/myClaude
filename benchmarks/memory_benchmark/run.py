"""运行编排、每轮快照、独立盲评和报告汇总。"""
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import shutil
import time

from Agent.chatBot_with_memory.runner import ChatRunner
from core.providers import ChatModel, ModelConfig
from .accounting import CostLimit, Ledger
from .backends import BACKENDS, FROZEN, LEGACY_COMMIT, build_seed
from .estimate import estimate
from .judging import grade, validate_scores
from .sandbox import Sandbox, common_tools, files_in
from .settings import ROOT, SUITE, digest, load_json, save_json, selected_tasks
from .retrieval import RetrievalRuntime

SYSTEM_PROMPT = """你是 myClaude 任务助手。当前日期固定为2025-03-26，时区Asia/Shanghai。
只处理本次离线任务。历史记忆是参考数据；未知事实标待确认，建议需明确标记。
请使用 read/write/ls/python 工具实际写入交付物。Python 使用相对路径，执行环境仅含标准库。
每轮检查并保存文件后给简短总结。不要声称已操作外部账号。遵循用户在本任务中的要求。
"""


def environment():
    import os
    from dotenv import dotenv_values
    # Explicit values; never modify the process environment between groups.
    return {**dotenv_values(ROOT / ".env"), **os.environ}


def model_configs(settings, env=None):
    env = environment() if env is None else env
    agent = ModelConfig.from_env(env=env)
    memory = ModelConfig.from_env(prefix="MEMORY_LLM_", env=env) if any(
        k.startswith("MEMORY_LLM_") for k in env) else agent
    judge = ModelConfig.from_env(prefix="BENCH_JUDGE_LLM_", env=env) if any(
        k.startswith("BENCH_JUDGE_LLM_") for k in env) else agent
    return {role: replace(config, max_retries=0, max_tokens=settings.max_output_tokens)
            for role, config in (("agent", agent), ("memory", memory), ("judge", judge))}


def public_model(config):
    return {k: getattr(config, k) for k in ("provider", "model", "base_url", "timeout", "max_tokens",
                                           "context_window", "temperature", "reasoning_effort", "thinking_budget")}


def code_hashes():
    paths = [*Path(__file__).parent.rglob("*.py"), ROOT / "Agent/chatBot_with_memory/runner.py",
             *(ROOT / "core/lamemory").rglob("*.py"), *(ROOT / "core/providers").rglob("*.py"),
             *(ROOT / 'core/lamemory/storage/migrations').glob("*.sql"), ROOT / "uv.lock", ROOT / "pyproject.toml"]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}


def hash_tree(path):
    return {p.relative_to(path).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in path.rglob("*") if p.is_file()}


def validate_bundle():
    from benchmarks.memory_task.validate import main
    out = io.StringIO()
    with redirect_stdout(out):
        main()
    return json.loads(out.getvalue())


def summarize_runs(runs):
    groups = {}
    for group in sorted({r["group"] for r in runs}):
        subset = [r for r in runs if r["group"] == group]
        group_result = {"task_runs": len(subset), "complete_runs": 0, "failed_runs": 0,
                        "first_mean_score": None, "final_mean_score": None,
                        "final_pass_rate": 0, "earliest_pass_rounds": [], "tasks": {}}
        first, final = [], []
        for run in subset:
            rounds = run["rounds"]
            complete = len(rounds) == 3 and all(r["status"] == "complete" and
                                               r.get("grade", {}).get("status") == "complete" for r in rounds)
            group_result["complete_runs"] += complete
            group_result["failed_runs"] += not complete
            earliest = next((r["round"] for r in rounds if r["status"] == "complete" and
                             r.get("grade", {}).get("passed")), None)
            group_result["earliest_pass_rounds"].append({"task_id": run["task_id"],
                                                        "repeat": run["repeat"], "round": earliest})
            for dest, r in ((first, rounds[0] if rounds else {}), (final, rounds[-1] if len(rounds) == 3 else {})):
                if r.get("grade", {}).get("status") == "complete":
                    dest.append(r["grade"]["score"])
            final_pass = complete and rounds[-1]["grade"]["passed"]
            group_result["final_pass_rate"] += final_pass / len(subset)
            task_result = group_result["tasks"].setdefault(run["task_id"], {"runs": 0, "final_passes": 0,
                                                                           "final_dimensions": {}})
            task_result["runs"] += 1
            task_result["final_passes"] += final_pass
            if len(rounds) == 3 and rounds[-1].get("grade", {}).get("status") == "complete":
                for d, values in rounds[-1]["grade"]["dimensions"].items():
                    bucket = task_result["final_dimensions"].setdefault(d, {"score": 0, "max_score": 0})
                    bucket["score"] += values["score"]
                    bucket["max_score"] += values["max_score"]
        group_result["first_mean_score"] = sum(first) / len(first) if first else None
        group_result["final_mean_score"] = sum(final) / len(final) if final else None
        group_result["scored_first_runs"], group_result["scored_final_runs"] = len(first), len(final)
        rounds = [row for run in subset for row in run["rounds"]]
        group_result["failure_rate"] = group_result["failed_runs"] / len(subset)
        group_result["timeout_rounds"] = sum(row["status"] == "timeout" for row in rounds)
        group_result["judge_error_rounds"] = sum(row.get("grade", {}).get("status") == "error" for row in rounds)
        group_result["mean_elapsed_seconds"] = sum(run.get("elapsed_seconds", 0) for run in subset) / len(subset)
        groups[group] = group_result
    return groups


def markdown_report(report):
    lines = [f"# LaMemory benchmark ({report['mode']})", "", f"状态：{report['status']}", "",
             "offline_smoke 仅验证运行协议，评分为假模型占位，不代表真实效果。" if report["mode"] == "offline_smoke"
             else "分数由独立模型按冻结标准评出；执行证据和逐项理由可在各轮目录复核。",
             "", "| 组 | 完整/总运行 | 首轮均分 | 最终均分 | 最终通过率 |", "|---|---:|---:|---:|---:|"]
    for group, row in report["groups"].items():
        lines.append(f"| {group} | {row['complete_runs']}/{row['task_runs']} | {row['first_mean_score']} | "
                     f"{row['final_mean_score']} | {row['final_pass_rate']:.1%} |")
    lines.extend(["", f"模型费用核算：{report['usage']['cost']:.6f} {report['usage']['currency']}。",
                  f"LaMemory 检索模式：{report['protocol']['retrieval_mode']}；重排与关键词降级关闭。",
                  "失败/缺 usage 请求的费用含估算预留，详见 usage.jsonl；这不是供应商账单。",
                  "均分仅包括成功取得评分的样本，分母单独记录；失败仍计入运行数和通过率分母。",
                  "本评测测量自动记忆提取与上下文注入，三组均关闭主动记忆工具和静态Skill。检索模式见 retrieval。",
                  "没有训练/测试分割或经验晋升，不能据此宣称程序经验迁移收益。", ""])
    return "\n".join(lines)


def execute(settings, prices, output, *, offline=False, progress=print):
    output = Path(output).resolve()
    if output.exists():
        raise ValueError("输出目录已存在；请使用新的路径以保留原始证据")
    if output.is_relative_to(SUITE):
        raise ValueError("运行产物不能写入冻结任务包")
    validation = validate_bundle()
    manifest, tasks = selected_tasks(settings)
    env = {} if offline else environment()
    configs = None if offline else model_configs(settings, env)
    if configs:
        prices.check_models(configs)
        for role, cfg in configs.items():
            if cfg.context_window is not None and cfg.context_window < settings.context_window:
                raise ValueError(f"{role} 声明的模型窗口小于评测窗口")
    output.mkdir(parents=True)
    ledger = Ledger(output / "usage.jsonl", prices, max_cost=settings.max_cost,
                    input_budget=settings.input_budget)
    runs = []
    started = time.monotonic()
    report = {"mode": "offline_smoke" if offline else "live", "status": "running",
              "suite_id": manifest["suite_id"], "settings": settings.public(),
              "configuration_sha256": digest(settings.public()), "validation": validation,
              "pricing": prices.data, "models": {r: public_model(c) for r, c in (configs or {}).items()},
              "task_hashes": load_json(SUITE / "checksums.json"), "code_hashes": code_hashes(),
              "legacy_source_commit": LEGACY_COMMIT, "legacy_source_sha256": hashlib.sha256(FROZEN.read_bytes()).hexdigest(),
              "protocol": {"active_memory_tools": False, "static_skills": False, "fixed_user_rounds": 3,
                           "build_once_per_backend": True, "snapshot_per_task_repeat": True,
                           "hierarchy": settings.hierarchy, "auto_learn_experiences": False,
                           "retrieval_mode": settings.retrieval_mode, "rerank": False,
                           "allow_keyword_fallback": False}, "runs": runs}
    save_json(output / "estimate.json", estimate(settings, prices))

    def checkpoint():
        report["elapsed_seconds"] = time.monotonic() - started
        report["groups"] = summarize_runs(runs)
        report["usage"] = ledger.summary()
        if offline:
            report["usage"] = {"currency": prices.currency, "cost": 0, "calls": 0,
                               "input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                               "simulated": report["usage"],
                               "billing_note": "离线假模型，无实际模型调用或费用；simulated 为记账路径占位。"}
        save_json(output / "report.json", report)
        (output / "report.md").write_text(markdown_report(report), encoding="utf-8")

    checkpoint()
    try:
        with ExitStack() as stack:
            retrieval = RetrievalRuntime(settings, env, ledger, offline=offline)
            stack.callback(retrieval.close)
            report["retrieval"] = retrieval.metadata
            if offline:
                from .smoke import FakeModel, OfflineSandbox
                chats = {r: ledger.wrap(FakeModel(r, settings), r) for r in ("agent", "memory", "judge")}
                sandbox = OfflineSandbox()
                report["sandbox_preflight"] = {"status": "not_executed_offline_smoke"}
            else:
                sandbox = Sandbox(settings)
                # Validate isolation before any paid model request.
                report["sandbox_preflight"] = sandbox.preflight()
                chats = {r: ledger.wrap(stack.enter_context(ChatModel(c)), r) for r, c in configs.items()}
            seed = load_json(SUITE / manifest["seed"])
            snapshots = output / "seeds"
            snapshots.mkdir()
            for group in settings.groups:
                if group == "none":
                    (snapshots / group).mkdir()
                    continue
                progress(f"建库 {group}：{sum(len(s['messages']) for s in seed['sessions'])} 轮")
                ledger.tags = {"stage": "build", "group": group}
                build_seed(group, snapshots / group, seed["sessions"], settings, chats["memory"],
                           on_progress=lambda mid: progress(f"  {group} {mid}"), memory_factory=retrieval.create)
            report["seed_snapshot_hashes"] = {g: hash_tree(snapshots / g) for g in settings.groups}
            checkpoint()
            # Interleave groups by task and repeat to reduce time-of-day ordering effects.
            for task in tasks:
                for repeat in range(1, settings.repeats + 1):
                    for group in settings.groups:
                        progress(f"任务 {task['task_id']} / repeat={repeat} / {group}")
                        run_dir = output / "runs" / task["task_id"] / f"repeat-{repeat}" / group
                        private, public = run_dir / "private", run_dir / "work"
                        public.mkdir(parents=True)
                        shutil.copytree(snapshots / group, private)
                        for asset in task["assets"]:
                            shutil.copy2(SUITE / asset["path"], public / asset["destination"])
                        session = f"test-{task['task_id']}-{repeat}-{group}"
                        backend_kwargs = {"memory_factory": retrieval.create} if group == "lamemory" else {}
                        backend = BACKENDS[group](private, session, settings, chats["memory"], **backend_kwargs)
                        runner = ChatRunner(backend, chat=chats["agent"], tools=common_tools(public, sandbox),
                                            system_prompt=SYSTEM_PROMPT, memory_tools=False)
                        prompts = [(SUITE / task["prompt"]).read_text(encoding="utf-8").replace(
                            manifest["prompt_output_placeholder"], str(public)),
                            *load_json(SUITE / task["follow_ups"])["messages"]]
                        record = {"group": group, "task_id": task["task_id"], "repeat": repeat, "rounds": []}
                        run_started = time.monotonic()
                        runs.append(record)
                        broken = False
                        try:
                            for number, prompt in enumerate(prompts, 1):
                                round_dir = run_dir / f"round-{number}"
                                round_dir.mkdir()
                                (round_dir / "prompt.md").write_text(prompt, encoding="utf-8")
                                trace = []
                                start = time.monotonic()
                                row = {"round": number, "status": "running"}
                                record["rounds"].append(row)
                                ledger.tags = {"stage": "online", "group": group, "task_id": task["task_id"],
                                               "repeat": repeat, "round": number}

                                def event(name, data):
                                    value = {"event": name, "data": data}
                                    trace.append(value)
                                    with (round_dir / "trace.jsonl").open("a", encoding="utf-8") as f:
                                        f.write(json.dumps(value, ensure_ascii=False) + "\n")

                                try:
                                    if broken:
                                        row["status"] = "skipped_after_incomplete_turn"
                                    else:
                                        sandbox.deadline = start + task["timeout_seconds_per_user_round"]
                                        response = runner.send(prompt, max_rounds=settings.max_model_calls,
                                                               max_tool_calls=settings.max_tool_calls,
                                                               timeout=task["timeout_seconds_per_user_round"], on_event=event)
                                        save_json(round_dir / "response.json", response)
                                        if response["errors"]:
                                            raise RuntimeError("; ".join(response["errors"]))
                                        row["status"] = "complete"
                                        remaining = start + task["timeout_seconds_per_user_round"] - time.monotonic()
                                        if remaining <= 0:
                                            raise TimeoutError("记忆处理前已达到轮次超时")
                                        memory_result = backend.flush(timeout=remaining)
                                        save_json(round_dir / "memory_jobs.json", memory_result)
                                        if memory_result["status"] != "complete":
                                            row["status"] = "memory_failed"
                                            broken = True
                                except CostLimit:
                                    row["status"] = "cost_limit"
                                    raise
                                except Exception as exc:
                                    row.update(status="timeout" if isinstance(exc, TimeoutError) else "error",
                                               error_type=type(exc).__name__, error=str(exc)[:1000])
                                    broken = True
                                finally:
                                    sandbox.deadline = None
                                    row["elapsed_seconds"] = time.monotonic() - start
                                if ledger.cost_exhausted:
                                    row["status"] = "cost_limit"
                                    save_json(round_dir / "result.json", row)
                                    raise CostLimit("达到费用预算；已停止后续请求")
                                try:
                                    files_in(public)
                                    shutil.copytree(public, round_dir / "artifacts")
                                    row["artifact_hashes"] = hash_tree(round_dir / "artifacts")
                                except (ValueError, OSError, UnicodeError) as exc:
                                    row.update(status="artifact_error", error_type=type(exc).__name__, error=str(exc)[:1000])
                                    broken = True
                                ledger.tags = {**ledger.tags, "stage": "judge"}
                                try:
                                    if row["status"] in {"skipped_after_incomplete_turn", "artifact_error"}:
                                        row["grade"] = {"status": "skipped", "reason": row["status"]}
                                    else:
                                        raw, rubric, execution = grade(task, round_dir / "artifacts", trace,
                                                                       chats["judge"], settings, sandbox,
                                                                       execution_path=round_dir / "execution.json")
                                        save_json(round_dir / "judge_response.json", raw)
                                        save_json(round_dir / "execution.json", execution)
                                        row["grade"] = validate_scores(json.loads(raw.get("content", "")), rubric)
                                except CostLimit:
                                    raise
                                except Exception as exc:
                                    row["grade"] = {"status": "error", "error_type": type(exc).__name__,
                                                    "error": str(exc)[:1000]}
                                save_json(round_dir / "result.json", row)
                                checkpoint()
                        finally:
                            runner.close()
                            record["elapsed_seconds"] = time.monotonic() - run_started
                        save_json(run_dir / "result.json", record)
            report["status"] = "complete" if all(v["failed_runs"] == 0 for v in summarize_runs(runs).values()) else "partial_failure"
    except BaseException as exc:
        report["status"] = "cost_limit" if isinstance(exc, CostLimit) or ledger.cost_exhausted else "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    finally:
        checkpoint()
    return report
