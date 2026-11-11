"""无模型预估：按任务正文/资产、建库协议、逐轮历史增长计算。"""
from collections import defaultdict
import math
from core.lamemory.integration.context import estimate_tokens
from .judging import JUDGE_PROMPT
from .sandbox import common_tools
from .settings import SUITE, load_json, selected_tasks
from .queries import public_fact_queries


def estimate(settings, prices):
    manifest, tasks = selected_tasks(settings)
    query_plan = public_fact_queries(tasks) if settings.retrieval_query_mode == 'facts' else None
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
            if role not in prices.data:
                b["cost"] = None
            elif b["cost"] is not None:
                b["cost"] += prices.cost(role, {"input": round(input_tokens), "output": round(output_tokens)})

        ceiling = scenario == "configured_ceiling"
        output = settings.max_output_tokens if ceiling else settings.expected_output_tokens
        artifact = settings.expected_artifact_tokens * (0.5 if scenario == "low" else 1)
        calls = settings.max_model_calls if ceiling else (2 if scenario == "low" else settings.expected_calls_per_round)
        calls = min(calls, settings.max_model_calls)
        for group in settings.groups:
            if group != "none" and not settings.seed_source:
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
                if group == "lamemory" and settings.retrieval_mode == "hybrid":
                    # 实体数量取决于提取结果；这里只给出显式情景，最终快照只建一次索引。
                    documents = nseed * (8 if ceiling else (1 if scenario == "low" else 2))
                    if settings.hierarchy:
                        documents += nseed if ceiling else 2
                    document_tokens = 4000 if ceiling else settings.seed_memory_tokens_per_message + 100
                    attempts = settings.job_attempts if ceiling else 1
                    add("build", group, "embedding_documents", "embedding",
                        math.ceil(documents / 16) * attempts, documents * document_tokens * attempts, 0)
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
                    if group == "lamemory" and settings.retrieval_mode == "hybrid":
                        queries = calls if ceiling else 1  # 正常同一用户轮次复用查询向量。
                        embedding_text = (query_plan['embedding_query_prefix'] + query_plan['queries'][task['task_id']]
                                          if query_plan else public)
                        add("online", group, "embedding_query", "embedding", queries * settings.repeats,
                            estimate_tokens(embedding_text) * queries * settings.repeats, 0)
                        documents = (16 if ceiling else (1 if scenario == "low" else 2))
                        attempts = settings.job_attempts if ceiling else 1
                        add("online", group, "embedding_documents", "embedding", attempts * settings.repeats,
                            documents * (4000 if ceiling else 300) * attempts * settings.repeats, 0)
                    if group != "none":
                        retries = settings.job_attempts if ceiling else 1
                        inp = base + history + artifact + output if group == "legacy" else estimate_tokens(public) + 600
                        add("online", group, "extraction", "memory", retries * settings.repeats,
                            inp * retries * settings.repeats,
                            (settings.max_output_tokens if ceiling else 160) * retries * settings.repeats)
                        if group == "legacy":
                            summaries = ((calls * settings.job_attempts if ceiling else (0 if scenario == 'low' else 1))
                                         if settings.legacy_preflight_compression else (1 if ceiling else 0))
                            if summaries:
                                add("online", group, "summary", "memory", summaries * settings.repeats,
                                    settings.input_budget * summaries * settings.repeats,
                                    (settings.max_output_tokens if ceiling else 300) * summaries * settings.repeats)
                        if group == "lamemory" and settings.hierarchy:
                            add("online", group, "summary", "memory", retries * settings.repeats,
                                (nseed * settings.seed_memory_tokens_per_message + 1000 + 3 * artifact)
                                * retries * settings.repeats,
                                (settings.max_output_tokens if ceiling else 600) * retries * settings.repeats)
                    judge_input = settings.judge_input_budget if ceiling else (
                        estimate_tokens(rubric) + estimate_tokens(JUDGE_PROMPT) + 1200 +
                        # Tool writes and file snapshots both appear in judge input.
                        2 * artifact + output + asset)
                    add("judge", group, "judge", "judge", settings.repeats,
                        min(settings.judge_input_budget, judge_input) * settings.repeats,
                        (settings.max_output_tokens if ceiling else 1200) * settings.repeats)
        scenarios[scenario] = {"calls": sum(b["calls"] for b in buckets.values()),
                               "input_tokens": sum(b["input"] for b in buckets.values()),
                               "output_tokens": sum(b["output"] for b in buckets.values()),
                               "cost": (None if any(b["cost"] is None for b in buckets.values()) else
                                        sum(b["cost"] for b in buckets.values())),
                               "priced_cost": sum(b["cost"] or 0 for b in buckets.values()),
                               "buckets": dict(buckets)}
    return {"status": "estimate_only", "suite_id": manifest["suite_id"], "tasks": len(tasks),
            "groups": settings.groups, "repeats": settings.repeats,
            "task_runs": len(tasks) * len(settings.groups) * settings.repeats,
            "seed_user_messages_per_backend": nseed, "seed_sessions": len(sessions),
            "currency": prices.currency, "pricing": prices.data, "settings": settings.public(),
            "unpriced_roles": (["embedding"] if settings.retrieval_mode == "hybrid" and
                               "lamemory" in settings.groups and "embedding" not in prices.data else []),
            "scenarios": scenarios,
            "assumptions": ["UTF-8/3 粗估，非精确 tokenizer；所有输入按缓存未命中计价。",
                            "每组仅建库一次；每题/重复克隆同一冻结快照。固定3轮，每轮独立评分。",
                            "low/expected 使用配置中的产物/输出假设，并计入历史与工具结果重发。",
                            "configured_ceiling 按调用/输出上限和记忆重试估算；摘要源大小随模型输出变化，非账单保证上限。",
                            "分层摘要开启时保守预留每个在线轮次一次刷新；无记忆变化时实际可能不调用。",
                            "hybrid 另计建库/每轮新增文档与查询 embedding；正常每轮1次查询，模型输出决定文档数。",
                            "embedding 情景按默认16条批次、低/常规每条种子1/2个实体估算；上限情景按每条种子8条记忆加主题、每轮16个实体，4000 token/实体，非账单保证。",
                            "缺 embedding 价格时总 cost 为 null，priced_cost 仅为已定价部分；真实 hybrid 运行必须补齐价格。",
                            "固定追问无用户模拟模型费用；重排关闭，无外部账号费用。",
                            "代码执行/容器成本不计入模型费用；推理 token 包含在供应商 output usage 中。"]}
