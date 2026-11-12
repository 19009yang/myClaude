"""逐次调用记账；无 usage 和调用失败均不冒充零成本。"""
from collections import defaultdict
import json
import math
import time

from core.lamemory.integration.context import estimate_tokens
from .settings import digest, load_json, save_json


class CostLimit(RuntimeError):
    pass


class Prices:
    def __init__(self, path):
        self.data = load_json(path)
        self.currency = self.data["currency"]
        for role in ("agent", "memory", "judge"):
            row = self.data[role]
            for name in ("input", "output", "cache_read", "cache_write"):
                value = row.get(name)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                    raise ValueError(f"{role}.{name}: 需要非负有限单价 / 1M tokens")
        if "embedding" in self.data:
            row = self.data["embedding"]
            value = row.get("input")
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0:
                raise ValueError("embedding.input: 需要非负有限单价 / 1M tokens")

    def check_embedding(self, config):
        row = self.data.get("embedding")
        if not row or (row.get("provider"), row.get("model")) != (config.provider, config.model):
            raise ValueError("hybrid 需要与实际 embedding 模型匹配的价格条目，请通过 --prices 指定")

    def check_models(self, configs):
        for role, cfg in configs.items():
            row = self.data[role]
            if (row["provider"], row["model"]) != (cfg.provider, cfg.model):
                raise ValueError(f"{role} 定价与实际模型不匹配，请更新价格文件")

    def cost(self, role, tokens):
        row = self.data[role]
        if role == "embedding":
            return tokens.get("input", 0) * row["input"] / 1_000_000
        return sum(tokens.get(k, 0) * row[k] / 1_000_000
                   for k in ("input", "output", "cache_read", "cache_write"))


def normalized_usage(usage):
    if not isinstance(usage, dict) or not any(usage.get(k, 0) for k in ("prompt_tokens", "completion_tokens")):
        return None
    for key in ("prompt_tokens", "completion_tokens", "cache_read_tokens", "cache_write_tokens"):
        v = usage.get(key, 0)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError("模型 usage 包含非法 token 数")
    read, write = usage.get("cache_read_tokens", 0), usage.get("cache_write_tokens", 0)
    if read + write > usage.get("prompt_tokens", 0):
        raise ValueError("缓存 token 超过输入 token")
    return {"input": usage.get("prompt_tokens", 0) - read - write,
            "output": usage.get("completion_tokens", 0), "cache_read": read, "cache_write": write}


class Ledger:
    def __init__(self, path, prices, *, max_cost=None, input_budget=None):
        self.path, self.prices, self.max_cost = path, prices, max_cost
        self.input_budget = input_budget
        self.role_input_budgets = {}
        self.cost_exhausted = False
        self.events = []
        self.tags = {}

    def total(self):
        return sum(e["cost"] for e in self.events)

    def reserve(self, cost):
        if self.cost_exhausted or (self.max_cost is not None and self.total() + cost > self.max_cost):
            self.cost_exhausted = True
            raise CostLimit("达到费用预算；新请求未发送")

    def wrap(self, chat, role):
        def call(messages, tools=None, **kwargs):
            purpose = role
            if role == "memory":
                first = str(messages[0].get("content", ""))
                last = str(messages[-1].get("content", ""))
                purpose = "summary" if (messages[0].get("role") == "system" and
                                          ("跨会话摘要" in first or "将历史对话概括为" in first)
                                          or "请压缩以上对话历史" in last) else "extraction"
            prompt_estimate = estimate_tokens(messages) + (estimate_tokens(tools) if tools else 0)
            input_budget = self.role_input_budgets.get(role, self.input_budget)
            if input_budget is not None and prompt_estimate > input_budget:
                raise ValueError("模型请求超过共同输入预算；未发送")
            max_output = kwargs.get("max_tokens") or chat.config.max_tokens
            reservation = self.prices.cost(role, {"input": prompt_estimate, "output": max_output})
            self.reserve(reservation)
            start = time.monotonic()
            event = {**self.tags, "role": role, "purpose": purpose,
                     "request_sha256": digest({"messages": messages, "tools": tools}),
                     "model": chat.config.model, "provider": chat.config.provider}
            response = None
            try:
                response = chat(messages=messages, tools=tools, **kwargs)
                event["raw_usage"] = response.get("usage", {})
                tokens = normalized_usage(event["raw_usage"])
                event["status"] = "complete"
                if tokens is None:
                    tokens = {"input": prompt_estimate, "output": max_output}
                    event["usage_mode"] = "reserved_estimate_missing_usage"
                else:
                    event["usage_mode"] = "provider_usage"
                if role == 'memory':
                    # 在 JSON 解析和来源校验之前保存模型正文，失败的逻辑尝试也可追溯。
                    directory = self.path.parent / 'memory_responses'
                    directory.mkdir(exist_ok=True)
                    evidence_path = directory / f'response-{len(self.events) + 1:06d}.json'
                    secret = getattr(chat.config, 'api_key', None)

                    def redact(value):
                        if isinstance(value, str):
                            return value.replace(secret, '[REDACTED]') if secret else value
                        if isinstance(value, dict):
                            return {redact(k): redact(v) for k, v in value.items()}
                        if isinstance(value, list):
                            return [redact(v) for v in value]
                        return value

                    evidence = redact({'purpose': purpose, 'request': {'messages': messages, 'tools': tools},
                                       'response': {k: response[k] for k in ('role', 'content', 'usage') if k in response}})
                    with evidence_path.open('x', encoding='utf-8') as stream:
                        json.dump(evidence, stream, ensure_ascii=False, indent=2, allow_nan=False)
                        stream.write('\n')
                    event['memory_response'] = str(evidence_path.relative_to(self.path.parent))
                return response
            except BaseException as exc:
                event["status"] = "error"
                event["error_type"] = type(exc).__name__
                event["usage_mode"] = "reserved_estimate_failed_request"
                tokens = {"input": prompt_estimate, "output": max_output}
                if getattr(exc, "diagnostics", None):
                    event["diagnostics"] = exc.diagnostics
                if getattr(exc, "response_evidence", None) is not None:
                    directory = self.path.parent / "provider_errors"
                    directory.mkdir(exist_ok=True)
                    evidence_path = directory / f"response-{len(self.events) + 1:06d}.json"
                    save_json(evidence_path, exc.response_evidence)
                    event["response_evidence"] = str(evidence_path.relative_to(self.path.parent))
                error_usage = getattr(exc, "usage", (response or {}).get('usage', {}))
                try:
                    actual = normalized_usage(error_usage)
                except ValueError:
                    actual = None
                    event["invalid_error_usage"] = True
                if actual is not None:
                    event["raw_usage"] = error_usage
                    tokens = actual
                    event["usage_mode"] = "provider_usage"
                raise
            finally:
                event["tokens"] = tokens
                event["cost"] = self.prices.cost(role, tokens)
                event["elapsed_seconds"] = time.monotonic() - start
                self.events.append(event)
                with self.path.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
        return call

    def wrap_embedding(self, provider, *, simulated=False):
        """在真实请求前预留费用；索引重试与查询失败也逐次记账。"""
        ledger = self

        class CountedEmbedding:
            descriptor = provider.descriptor

            def invoke(self, texts, purpose, timeout):
                materialized = texts if purpose == "embedding_documents" else [
                    self.descriptor.query_prefix + texts[0]]
                estimated = sum(estimate_tokens(t) for t in materialized)
                reservation = 0 if simulated else ledger.prices.cost("embedding", {"input": estimated})
                ledger.reserve(reservation)
                event = {**ledger.tags, "role": "embedding", "purpose": purpose,
                         "provider": "fake" if simulated else provider.config.provider,
                         "model": self.descriptor.model, "space_id": self.descriptor.space_id,
                         "request_sha256": digest(materialized), "documents": len(texts),
                         "simulated": simulated, "raw_usage": {}, "tokens": {"input": estimated},
                         "usage_mode": "reserved_estimate_failed_request", "status": "error"}
                before = len(provider.usage_events)
                start = time.monotonic()
                try:
                    result = (provider.embed_documents(texts, timeout=timeout) if purpose == "embedding_documents"
                              else provider.embed_query(texts[0], timeout=timeout))
                    event["status"] = "complete"
                    event["usage_mode"] = "reserved_estimate_missing_usage"
                    return result
                except BaseException as exc:
                    event["error_type"] = type(exc).__name__
                    raise
                finally:
                    # Even invalid vectors can have billable usage from a completed HTTP request.
                    events = provider.usage_events[before:]
                    if events:
                        usage = events[-1].get("usage", {})
                        event["raw_usage"] = usage
                        count = usage.get("prompt_tokens", usage.get("total_tokens"))
                        if type(count) is int and count >= 0:
                            event["tokens"] = {"input": count}
                            event["usage_mode"] = "provider_usage"
                    event["cost"] = 0 if simulated else ledger.prices.cost("embedding", event["tokens"])
                    event["elapsed_seconds"] = time.monotonic() - start
                    ledger.events.append(event)
                    with ledger.path.open("a", encoding="utf-8") as f:
                        f.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")

            def embed_documents(self, texts, *, timeout):
                return self.invoke(texts, "embedding_documents", timeout)

            def embed_query(self, text, *, timeout):
                return self.invoke([text], "embedding_query", timeout)

        return CountedEmbedding()

    def summary(self):
        buckets = defaultdict(lambda: {"calls": 0, "input": 0, "output": 0, "cache_read": 0,
                                       "cache_write": 0, "cost": 0, "estimated_calls": 0, "error_calls": 0})
        for event in self.events:
            key = "/".join(str(event.get(k, "-")) for k in ("stage", "group", "purpose"))
            bucket = buckets[key]
            bucket["calls"] += 1
            bucket["cost"] += event["cost"]
            bucket["estimated_calls"] += event["usage_mode"] != "provider_usage"
            bucket["error_calls"] += event["status"] == "error"
            for k, v in event["tokens"].items():
                bucket[k] += v
        inputs = sum(e["tokens"].get(k, 0) for e in self.events for k in ("input", "cache_read", "cache_write"))
        outputs = sum(e["tokens"].get("output", 0) for e in self.events)
        return {"currency": self.prices.currency, "cost": self.total(), "calls": len(self.events),
                "input_tokens": inputs, "output_tokens": outputs, "total_tokens": inputs + outputs,
                "estimated_calls": sum(e["usage_mode"] != "provider_usage" for e in self.events),
                "buckets": dict(buckets),
                "billing_note": "费用按配置快照核算；失败/缺 usage 的调用使用输入估算及输出上限预留，非供应商账单。"}
