import copy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from Agent.chatBot_with_memory.runner import ChatRunner
from benchmarks.memory_benchmark.accounting import CostLimit, Ledger, Prices, normalized_usage
from benchmarks.memory_benchmark.backends import FROZEN, BACKENDS, build_seed
from benchmarks.memory_benchmark.estimate import estimate
from benchmarks.memory_benchmark.judging import grade, validate_scores
from benchmarks.memory_benchmark.run import execute, summarize_runs, validate_bundle
from benchmarks.memory_benchmark.sandbox import Sandbox, common_tools, files_in
from benchmarks.memory_benchmark.settings import Settings, SUITE, load_json
from benchmarks.memory_benchmark.smoke import FakeModel, OfflineSandbox
from benchmarks.memory_benchmark.retrieval import RetrievalRuntime

PRICES = Path(__file__).resolve().parents[2] / "benchmarks/memory_benchmark/prices.deepseek-flash.json"


class AccountingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prices = Prices(PRICES)
        self.prices.data["embedding"] = {"provider": "openai-compatible", "model": "test-embedding", "input": 0.1}
        self.ledger = Ledger(Path(self.tmp.name) / "usage.jsonl", self.prices)

    def model(self, response=None, error=None):
        def call(**kwargs):
            if error:
                raise error
            return response
        call.config = SimpleNamespace(model="fake", provider="fake", max_tokens=100)
        return call

    def test_cache_and_stage_accounting(self):
        self.ledger.tags = {"stage": "build", "group": "lamemory"}
        chat = self.ledger.wrap(self.model({"content": "[]", "usage": {
            "prompt_tokens": 100, "completion_tokens": 20, "cache_read_tokens": 70, "cache_write_tokens": 10}}), "memory")
        chat(messages=[{"role": "system", "content": "提取"}])
        e = self.ledger.events[0]
        self.assertEqual(e["tokens"], {"input": 20, "output": 20, "cache_read": 70, "cache_write": 10})
        self.assertAlmostEqual(e["cost"], (30 * 0.3 + 70 * 0.006 + 20 * 1.2) / 1_000_000)
        self.assertIn("build/lamemory/extraction", self.ledger.summary()["buckets"])
        self.assertEqual(self.ledger.summary()["input_tokens"], 100)
        self.assertEqual(self.ledger.summary()["output_tokens"], 20)

    def test_missing_usage_and_failed_calls_are_reserved(self):
        self.ledger.wrap(self.model({"content": "ok"}), "agent")(messages=[{"role": "user", "content": "hello"}])
        with self.assertRaises(TimeoutError):
            self.ledger.wrap(self.model(error=TimeoutError("failed")), "judge")(
                messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(len(self.ledger.events), 2)
        self.assertTrue(all(e["tokens"]["output"] == 100 and e["cost"] > 0 for e in self.ledger.events))
        self.assertEqual(self.ledger.events[-1]["status"], "error")

    def test_cost_guard_stops_before_call(self):
        called = []
        model = self.model()
        def call(**kwargs):
            called.append(True)
            return model(**kwargs)
        call.config = model.config
        self.ledger.max_cost = 0.000001
        with self.assertRaises(CostLimit):
            self.ledger.wrap(call, "agent")(messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(called, [])
        self.assertEqual(self.ledger.events, [])
        self.assertTrue(self.ledger.cost_exhausted)

    def test_input_guard_and_bad_usage(self):
        self.ledger.input_budget = 10
        with self.assertRaises(ValueError):
            self.ledger.wrap(self.model(), "agent")(messages=[{"role": "user", "content": "x" * 500}])
        with self.assertRaises(ValueError):
            normalized_usage({"prompt_tokens": 10, "cache_read_tokens": 11})

    def test_estimate_counts_and_scaling(self):
        one = estimate(Settings(repeats=1), self.prices)
        three = estimate(Settings(), self.prices)
        self.assertEqual(one["task_runs"], 24)
        self.assertEqual(three["task_runs"], 72)
        self.assertEqual(one["seed_user_messages_per_backend"], 44)
        for name in one["scenarios"]:
            a, b = one["scenarios"][name], three["scenarios"][name]
            build = sum(v["cost"] for k, v in a["buckets"].items() if k.startswith("build/"))
            self.assertAlmostEqual(b["cost"], (a["cost"] - build) * 3 + build)
        self.assertGreater(three["scenarios"]["configured_ceiling"]["cost"], three["scenarios"]["expected"]["cost"])


class ProtocolTests(unittest.TestCase):
    def test_frozen_assets_and_legacy_source(self):
        self.assertEqual(validate_bundle()["tasks"], 8)
        self.assertEqual(hashlib.sha256(FROZEN.read_bytes()).hexdigest(),
                         "d6b346be5c8323b0bc73167ff5a93f8322190c5acb1865998dfb7d19773b4a1a")

    def test_backend_cross_session_history_and_sources(self):
        settings = Settings(repeats=1)
        sessions = [{"session_id": "a", "date": "2025-03-01", "kind": "history_message",
                     "messages": [{"message_id": "X1", "content": "我住北京。"}]},
                    {"session_id": "b", "date": "2025-03-02", "kind": "synthetic_supplement",
                     "messages": [{"message_id": "X2", "content": "爸爸生日3月20日。"}]}]
        for group in ("legacy", "lamemory"):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as tmp:
                directory = Path(tmp) / group
                model = FakeModel("memory", settings)
                ledger = Ledger(Path(tmp) / "usage.jsonl", Prices(PRICES))
                retrieval = RetrievalRuntime(settings, {}, ledger, offline=True)
                self.addCleanup(retrieval.close)
                build_seed(group, directory, sessions, settings, model, memory_factory=retrieval.create)
                mapping = load_json(directory / "seed_evidence.json")
                self.assertEqual(set(mapping), {"X1", "X2"})
                self.assertEqual(mapping["X1"]["content_sha256"], hashlib.sha256("我住北京。".encode()).hexdigest())
                kwargs = {"memory_factory": retrieval.create} if group == "lamemory" else {}
                backend = BACKENDS[group](directory, "fresh", settings, model, **kwargs)
                try:
                    backend.append_message({"role": "user", "content": "爸爸生日是什么时候？"})
                    context = backend.build_context("共同规则")
                    self.assertEqual(len([m for m in context.messages if m["role"] == "user"]), 1)
                    self.assertIn("3月20日", context.messages[0]["content"])
                finally:
                    backend.close()

    def test_none_does_not_read_long_term_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "MEMORY.md").write_text("hidden-history", encoding="utf-8")
            backend = BACKENDS["none"](directory, "fresh", Settings())
            backend.append_message({"role": "user", "content": "hello"})
            self.assertNotIn("hidden-history", str(backend.build_context("system").messages))
            backend.append_message({"role": "assistant", "content": "ok"})
            backend.commit_turn()
            self.assertEqual((directory / "MEMORY.md").read_text(), "hidden-history")
            self.assertFalse((directory / "lamemory.sqlite3").exists())

    def test_tools_block_escape_and_symlinks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "public"
            root.mkdir()
            private = Path(tmp) / "private.txt"
            private.write_text("secret", encoding="utf-8")
            tools = {t.name: t for t in common_tools(root, OfflineSandbox())}
            for path in ("../private.txt", str(private)):
                with self.assertRaises(PermissionError):
                    tools["read"].execute(path=path)
            (root / "link").symlink_to(private)
            with self.assertRaises(PermissionError):
                tools["write"].execute(path="link", content="overwritten")
            with self.assertRaises(ValueError):
                files_in(root)
            self.assertEqual(private.read_text(), "secret")

    def test_runner_tool_limit(self):
        settings = Settings(repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            memory = BACKENDS["none"](root, "task", settings)
            fake = FakeModel("agent", settings)
            def double(**kwargs):
                response = fake(**kwargs)
                response["tool_calls"].append(copy.deepcopy(response["tool_calls"][0]))
                response["tool_calls"][-1]["id"] += "b"
                return response
            runner = ChatRunner(memory, chat=double, tools=common_tools(root, OfflineSandbox()), memory_tools=False)
            with self.assertRaisesRegex(RuntimeError, "工具调用达到次数上限"):
                runner.send("write", max_tool_calls=1)
            runner.close()

    def test_judge_schema_and_blind_paths(self):
        task = load_json(SUITE / "manifest.json")["tasks"][0]
        rubric = load_json(SUITE / task["rubric"])
        scores = {"criteria": [{"item_id": c["item_id"], "score": 0, "reason": "missing"} for c in rubric["criteria"]]}
        self.assertEqual(validate_scores(scores, rubric)["score"], 0)
        for value in (float("nan"), 1000, -1, True):
            bad = copy.deepcopy(scores)
            bad["criteria"][0]["score"] = value
            with self.assertRaises(ValueError):
                validate_scores(bad, rubric)
        bad = copy.deepcopy(scores)
        bad["criteria"][-1] = bad["criteria"][0]
        with self.assertRaises(ValueError):
            validate_scores(bad, rubric)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "lamemory"
            snapshot = root / "round-1/artifacts"
            snapshot.mkdir(parents=True)
            seen = []
            def judge(**kwargs):
                seen.extend(kwargs["messages"])
                return {"content": json.dumps(scores)}
            grade(task, snapshot, [{"event": "message", "data": {"content": str(root / "work")}},
                                   {"event": "context", "data": {"retrieval_mode": "lamemory"}}],
                  judge, Settings(), OfflineSandbox())
            self.assertNotIn(str(root), str(seen))
            self.assertNotIn("retrieval_mode", str(seen))

    def test_failures_cannot_pass_even_with_high_scores(self):
        grade_result = {"status": "complete", "score": 100, "passed": True, "dimensions": {}}
        run = {"group": "none", "task_id": "task", "repeat": 1,
               "rounds": [{"round": n, "status": "error", "grade": grade_result} for n in (1, 2, 3)]}
        group = summarize_runs([run])["none"]
        self.assertEqual(group["complete_runs"], 0)
        self.assertEqual(group["failed_runs"], 1)
        self.assertEqual(group["final_pass_rate"], 0)
        self.assertIsNone(group["earliest_pass_rounds"][0]["round"])

    def test_full_offline_pipeline_and_snapshot_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            report = execute(Settings(repeats=1), Prices(PRICES), output, offline=True, progress=lambda *a: None)
            self.assertEqual(report["status"], "complete", report.get("error"))
            self.assertEqual(len(report["runs"]), 24)
            self.assertEqual(sum(len(r["rounds"]) for r in report["runs"]), 72)
            self.assertEqual(report["mode"], "offline_smoke")
            self.assertEqual(report["retrieval"]["mode"], "hybrid")
            self.assertTrue(report["retrieval"]["simulated"])
            usage = [json.loads(line) for line in (output / "usage.jsonl").read_text().splitlines()]
            embedding = [e for e in usage if e["role"] == "embedding"]
            self.assertTrue(any(e["purpose"] == "embedding_documents" and e["stage"] == "build" for e in embedding))
            self.assertTrue(any(e["purpose"] == "embedding_query" and e["stage"] == "online" for e in embedding))
            self.assertTrue(all(e["group"] == "lamemory" and e["simulated"] for e in embedding))
            for group in report["groups"].values():
                self.assertEqual(group["complete_runs"], 8)
                self.assertEqual(group["final_mean_score"], 0)
            from benchmarks.memory_benchmark.run import hash_tree
            for group, hashes in report["seed_snapshot_hashes"].items():
                self.assertEqual(hash_tree(output / "seeds" / group), hashes)
            self.assertFalse(any((output / "runs").rglob("**/none/private/lamemory.sqlite3")))
            with self.assertRaises(ValueError):
                execute(Settings(), Prices(PRICES), output, offline=True)

    def test_seed_cost_failure_preserves_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "run"
            settings = Settings(groups=("lamemory",), repeats=1, tasks=("01_parents_birthday",), max_cost=0.000001)
            report = execute(settings, Prices(PRICES), output, offline=True, progress=lambda *a: None)
            self.assertEqual(report["status"], "cost_limit")
            self.assertEqual(report["usage"]["cost"], 0)
            self.assertEqual(report["runs"], [])
            self.assertTrue((output / "seeds/lamemory/seed_jobs.json").is_file())
            self.assertFalse((output / "seeds/lamemory/snapshot.json").exists())


class SandboxTests(unittest.TestCase):
    def test_actual_isolation_and_code_execution(self):
        import sys
        if sys.platform != "darwin":
            self.skipTest("macOS seatbelt 测试；Docker 按部署环境另验")
        sandbox = Sandbox(Settings())
        try:
            sandbox.preflight()
        except RuntimeError as exc:
            if "sandbox_apply: Operation not permitted" in str(exc):
                self.skipTest("宿主沙箱禁止嵌套；须在外层运行此测试")
            raise
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "module.py").write_text("def f(): return 42\n", encoding="utf-8")
            (directory / "test_module.py").write_text("import unittest\nfrom module import f\n"
                "class T(unittest.TestCase):\n def test_f(self): self.assertEqual(f(),42)\n", encoding="utf-8")
            result = sandbox.run(directory, ["-m", "unittest", "discover", "-s", ".", "-p", "test_module.py"])
            self.assertEqual(result["returncode"], 0, result)
            self.assertIn("Ran 1 test", result["output"])


if __name__ == "__main__":
    unittest.main()
