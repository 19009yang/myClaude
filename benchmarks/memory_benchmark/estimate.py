"""无模型预估：按任务正文/资产、建库协议、逐轮历史增长计算。"""
from collections import defaultdict
from core.lamemory.context import estimate_tokens
from .judging import JUDGE_PROMPT
from .sandbox import common_tools
from .settings import SUITE, load_json, selected_tasks


def estimate(settings, prices):
    manifest, tasks = selected_tasks(settings)
    sessions = load_json(SUITE / manifest["seed"])["sessions"]
    seeds = [m for s in sessions for m in s["messages"]]
    nseed = len(seeds)
    tools = [t.to_llm_format() for t in common_tools(SUITE, None)]
    scenarios = {}
    for scenario in ("low", "expected", "configured_ceiling"):
        buckets = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cost": 0})

        def add(stage, group, purpose, role, calls, input_tokens, output_tokens):
            b = buckets[f"{stage}/{group}/{purpose}"]
            b["calls"] += calls
            b["input"] += round(input_tokens)
            b["output"] += round(output_tokens)
            b["cost"] += prices.cost(role, {"input": round(input_tokens), "output": round(output_tokens)})

        ceiling = scenario == "configured_ceiling"
        output = settings.max_output_tokens if ceiling else settings.expected_output_tokens
        artifact = settings.expected_artifact_tokens * (0.5 if scenario == "low" else 1)
        calls = settings.max_model_calls if ceiling else (2 if scenario == "low" else settings.expected_calls_per_round)
        calls = min(calls, settings.max_model_calls)
        for group in settings.groups:
            if group != "none":
                for i, seed in enumerate(seeds):
                    inp = estimate_tokens(seed["content"]) + 500
                    if group == "legacy":
                        inp += i * settings.seed_memory_tokens_per_message
                    add("build", group, "extraction", "memory", settings.job_attempts if ceiling else 1,
                        inp * (settings.job_attempts if ceiling else 1),
                        (settings.max_output_tokens if ceiling else settings.seed_memory_tokens_per_message * 2)
                        * (settings.job_attempts if ceiling else 1))
                if group == "lamemory" and settings.hierarchy:
                    add("build", group, "summary", "memory", settings.job_attempts if ceiling else 1,
                        (nseed * settings.seed_memory_tokens_per_message + 1000) * (settings.job_attempts if ceiling else 1),
                        (settings.max_output_tokens if ceiling else 600) * (settings.job_attempts if ceiling else 1))
            memory = 0 if group == "none" else (nseed * settings.seed_memory_tokens_per_message
                                               if group == "legacy" else settings.memory_token_budget)
            for task in tasks:
                public = (SUITE / task["prompt"]).read_text(encoding="utf-8").replace("{{output_dir}}", "/isolated/output")
                asset = sum(estimate_tokens((SUITE / a["path"]).read_text(encoding="utf-8")) for a in task["assets"])
                base = estimate_tokens(public) + estimate_tokens(tools) + memory + asset + 250
                rubric = load_json(SUITE / task["rubric"])
                for round_index in range(task["max_user_rounds"]):
                    # All calls resend previous rounds and accumulated tool messages.
                    history = round_index * (artifact + output + 500)
                    if ceiling:
                        agent_input = settings.input_budget * calls
                    else:
                        agent_input = min(settings.input_budget, base + history + artifact * 0.5) * calls
                    add("online", group, "agent", "agent", calls * settings.repeats,
                        agent_input * settings.repeats, output * calls * settings.repeats)
                    if group != "none":
                        retries = settings.job_attempts if ceiling else 1
                        inp = base + history + artifact + output if group == "legacy" else estimate_tokens(public) + 600
                        add("online", group, "extraction", "memory", retries * settings.repeats,
                            inp * retries * settings.repeats,
                            (settings.max_output_tokens if ceiling else 160) * retries * settings.repeats)
                        if group == "legacy" and ceiling:
                            add("online", group, "summary", "memory", settings.repeats,
                                settings.input_budget * settings.repeats,
                                settings.max_output_tokens * settings.repeats)
                        if group == "lamemory" and settings.hierarchy:
                            add("online", group, "summary", "memory", retries * settings.repeats,
                                (nseed * settings.seed_memory_tokens_per_message + 1000 + 3 * artifact)
                                * retries * settings.repeats,
                                (settings.max_output_tokens if ceiling else 600) * retries * settings.repeats)
                    judge_input = settings.input_budget if ceiling else (
                        estimate_tokens(rubric) + estimate_tokens(JUDGE_PROMPT) + 1200 +
                        # Tool writes and file snapshots both appear in judge input.
                        2 * artifact + output + asset)
                    add("judge", group, "judge", "judge", settings.repeats,
                        min(settings.input_budget, judge_input) * settings.repeats,
                        (settings.max_output_tokens if ceiling else 1200) * settings.repeats)
        scenarios[scenario] = {"calls": sum(b["calls"] for b in buckets.values()),
                               "input_tokens": sum(b["input"] for b in buckets.values()),
                               "output_tokens": sum(b["output"] for b in buckets.values()),
                               "cost": sum(b["cost"] for b in buckets.values()), "buckets": dict(buckets)}
    return {"status": "estimate_only", "suite_id": manifest["suite_id"], "tasks": len(tasks),
            "groups": settings.groups, "repeats": settings.repeats,
            "task_runs": len(tasks) * len(settings.groups) * settings.repeats,
            "seed_user_messages_per_backend": nseed, "seed_sessions": len(sessions),
            "currency": prices.currency, "pricing": prices.data, "settings": settings.public(),
            "scenarios": scenarios,
            "assumptions": ["UTF-8/3 粗估，非精确 tokenizer；所有输入按缓存未命中计价。",
                            "每组仅建库一次；每题/重复克隆同一冻结快照。固定3轮，每轮独立评分。",
                            "low/expected 使用配置中的产物/输出假设，并计入历史与工具结果重发。",
                            "configured_ceiling 按调用/输出上限和记忆重试估算；摘要源大小随模型输出变化，非账单保证上限。",
                            "分层摘要开启时保守预留每个在线轮次一次刷新；无记忆变化时实际可能不调用。",
                            "固定追问无用户模拟模型费用；无 embedding/rerank/外部账号费用。",
                            "代码执行/容器成本不计入模型费用；推理 token 包含在供应商 output usage 中。"]}
