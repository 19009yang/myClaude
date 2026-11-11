import copy
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

import httpx
from openai import OpenAI

from Agent.chatBot_with_memory.runner import ChatRunner
from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.backends import LaMemory, NoMemory
from benchmarks.memory_benchmark.judging import grade
from benchmarks.memory_benchmark.run import summarize_runs
from benchmarks.memory_benchmark.sandbox import common_tools
from benchmarks.memory_benchmark.settings import Settings, SUITE
from benchmarks.memory_benchmark.smoke import FakeModel, OfflineSandbox
from core.providers import ChatModel, ModelConfig, ProviderError
from core.lamemory.integration.context import estimate_tokens

PRICES = Path(__file__).resolve().parents[2] / "benchmarks/memory_benchmark/prices.deepseek-flash.json"


def completion(message, finish="stop"):
    return {"id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "message": message, "finish_reason": finish}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def tool_response(arguments, finish="tool_calls"):
    return completion({"role": "assistant", "content": "", "tool_calls": [
        {"id": "write_one", "type": "function", "function": {"name": "write", "arguments": arguments}}]}, finish)


class TrialRegressions(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings()
        self.ledger = Ledger(self.root / "usage.jsonl", Prices(PRICES))

    def runner(self, responses):
        self.requests = []
        def handle(request):
            self.requests.append(json.loads(request.content))
            return httpx.Response(200, json=responses[min(len(self.requests) - 1, len(responses) - 1)])
        sdk = OpenAI(api_key="private-test-key", http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        self.addCleanup(sdk.close)
        chat = ChatModel(ModelConfig(provider="deepseek", model="test", api_key="private-test-key"), client=sdk)
        memory = NoMemory(self.root, "trial", self.settings)
        return ChatRunner(memory, chat=self.ledger.wrap(chat, "agent"),
                          tools=common_tools(self.root, OfflineSandbox()), memory_tools=False)

    def test_invalid_tool_response_retries_without_duplicate_side_effects_and_preserves_usage(self):
        runner = self.runner([tool_response('{"private-test-key":'),
                              tool_response('{"path":"saved.md","content":"saved once"}'),
                              completion({"role": "assistant", "content": "done"})])
        events = []
        result = runner.send("write a file", max_rounds=3, on_event=lambda name, data: events.append((name, data)))
        self.assertEqual(result["errors"], [])
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(sum(name == "tool_start" for name, _ in events), 1)
        self.assertEqual((self.root / "saved.md").read_text(), "saved once")
        self.assertEqual(self.requests[0]["messages"][1:], self.requests[1]["messages"][1:])
        self.assertEqual(self.ledger.events[0]["diagnostics"]["reason"], "invalid_tool_arguments")
        self.assertEqual(self.ledger.events[0]["usage_mode"], "provider_usage")
        self.assertEqual(self.ledger.events[0]["tokens"]["output"], 20)
        evidence = (self.root / self.ledger.events[0]["response_evidence"]).read_text()
        self.assertNotIn("private-test-key", evidence)
        self.assertIn("REDACTED", evidence)
        self.assertEqual(sum(name == "provider_error" for name, _ in events), 1)

    def test_repeated_invalid_response_is_bounded_and_never_executes_tools(self):
        runner = self.runner([tool_response('{"path":')])
        with self.assertRaises(ProviderError) as error:
            runner.send("write", max_rounds=8, response_retries=1)
        self.assertEqual(error.exception.kind, "invalid_response")
        self.assertEqual(len(self.requests), 2)
        self.assertFalse((self.root / "saved.md").exists())
        self.assertEqual(len(runner.memory.messages), 1)

    def test_truncated_response_has_specific_error_and_billable_usage(self):
        runner = self.runner([tool_response('{"path":"saved.md"', finish="length")])
        with self.assertRaises(ProviderError) as error:
            runner.send("write", max_rounds=8)
        self.assertEqual(error.exception.kind, "output_truncated")
        self.assertEqual(error.exception.diagnostics["finish_reason"], "length")
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.ledger.events[0]["tokens"], {"input": 100, "output": 20, "cache_read": 0, "cache_write": 0})
        self.assertEqual(json.loads((self.root / self.ledger.events[0]["response_evidence"]).read_text())["finish_reason"], "length")

    def test_judge_deduplicates_parameters_but_retains_artifacts_execution_and_unexecuted_calls(self):
        snapshot = self.root / "trial/round-1/artifacts"
        snapshot.mkdir(parents=True)
        content = "birthday information " * 240
        (snapshot / "birthday_reminders.md").write_text(content)
        call = {"id": "write1", "function": {"name": "write", "arguments": {"path": "birthday_reminders.md", "content": content}}}
        pending = {"id": "unexecuted", "function": {"name": "read", "arguments": {"path": "missing.md"}}}
        trace = [{"event": "message", "data": {"role": "assistant", "content": "", "reasoning_content": "private reasoning " * 300,
                                                  "provider_data": {"provider": "deepseek"}, "tool_calls": [call, pending]}},
                 {"event": "tool_start", "data": {"id": "write1", "name": "write", "arguments": call["function"]["arguments"]}},
                 {"event": "tool_end", "data": {"id": "write1", "name": "write", "content": "saved", "is_error": False}}]
        original = copy.deepcopy(trace)
        settings = Settings(context_window=10500, max_output_tokens=4096)
        captured = []
        def judge(**kwargs):
            captured.extend(kwargs["messages"])
            return {"content": "{}"}
        task = json.loads((SUITE / "manifest.json").read_text())["tasks"][0]
        grade(task, snapshot, trace, judge, settings, None, input_path=self.root / "judge_input.json")
        self.assertLessEqual(estimate_tokens(captured), settings.judge_input_budget)
        data = json.loads(captured[-1]["content"])
        uncompacted = {**data, "trace": trace}
        self.assertGreater(estimate_tokens([captured[0], {"role": "user", "content": json.dumps(uncompacted, ensure_ascii=False)}]),
                           settings.judge_input_budget)
        self.assertEqual(data["artifacts"]["birthday_reminders.md"], content)
        self.assertEqual(data["trace"][1]["data"]["arguments"]["content"], content)
        self.assertEqual(data["trace"][0]["data"]["tool_calls"][0]["arguments_from_tool_start"], "write1")
        self.assertEqual(data["trace"][0]["data"]["tool_calls"][1]["arguments"], {"path": "missing.md"})
        self.assertNotIn("reasoning_content", str(data))
        self.assertEqual(trace, original)
        self.assertTrue((self.root / "judge_input.json").exists())

    def test_large_judge_payload_uses_its_own_configured_ledger_budget(self):
        settings = Settings(judge_context_window=65536)
        self.ledger.input_budget = settings.input_budget
        self.ledger.role_input_budgets["judge"] = settings.judge_input_budget
        fake = FakeModel("judge", settings)
        def chat(**kwargs):
            return {"content": "{}", "usage": {"prompt_tokens": 10, "completion_tokens": 10}}
        chat.config = fake.config
        messages = [{"role": "user", "content": "x" * 90000}]
        with self.assertRaises(ValueError):
            self.ledger.wrap(chat, "agent")(messages=messages)
        self.ledger.wrap(chat, "judge")(messages=messages)
        self.assertEqual(len(self.ledger.events), 1)

    def test_failed_round_artifact_score_never_enters_means_or_dimension_totals(self):
        score = {"status": "complete", "score": 100, "passed": True, "dimensions": {"memory": {"score": 90, "max_score": 90}}}
        row = {"group": "lamemory", "task_id": "one", "repeat": 1,
               "rounds": [{"round": n, "status": "error", "grade": score} for n in (1, 2, 3)]}
        summary = summarize_runs([row])["lamemory"]
        self.assertIsNone(summary["first_mean_score"])
        self.assertIsNone(summary["final_mean_score"])
        self.assertEqual(summary["tasks"]["one"]["final_dimensions"], {})
        self.assertEqual(summary["final_pass_rate"], 0)

    def test_followups_keep_task_query_and_do_not_embed_protocol_header(self):
        settings = replace(self.settings, retrieval_mode="keyword")
        backend = LaMemory(self.root, "new", settings, FakeModel("memory", settings))
        self.addCleanup(backend.close)
        backend.service.store("父亲生日3月20日")
        text = "当前评测日期固定为2025-03-26。交付目录是 /private/host/path。\n\n帮我整理父亲生日提醒和礼物待办。"
        backend.append_message({"role": "user", "content": text})
        backend.append_message({"role": "assistant", "content": "done"})
        backend.append_message({"role": "user", "content": "请检查交付物，修正遗漏。"})
        context = backend.build_context()
        self.assertIn("父亲生日", context.diagnostics["retrieval_query"])
        self.assertIn("修正遗漏", context.diagnostics["retrieval_query"])
        self.assertNotIn("/private/host/path", context.diagnostics["retrieval_query"])
        self.assertEqual(backend.service.history()[0]["payload"]["content"], text)
        self.assertEqual(backend.config.top_k, 10)
        self.assertTrue(any("父亲生日" in h["item"]["content"] for h in context.diagnostics["retrieved"]))


if __name__ == "__main__":
    unittest.main()
