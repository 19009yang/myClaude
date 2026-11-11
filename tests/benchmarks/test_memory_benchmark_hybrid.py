from contextlib import nullcontext
from dataclasses import replace
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.memory_benchmark.accounting import CostLimit, Ledger, Prices
from benchmarks.memory_benchmark.backends import LaMemory, build_seed
from benchmarks.memory_benchmark.estimate import estimate
from benchmarks.memory_benchmark.retrieval import RetrievalRuntime
from benchmarks.memory_benchmark.run import execute, hash_tree
from benchmarks.memory_benchmark.settings import Settings, load_json
from benchmarks.memory_benchmark.smoke import FakeEmbedding, FakeModel, OfflineSandbox
from core.lamemory.retrieval.types import RetrievalUnavailableError

PRICES = Path(__file__).resolve().parents[2] / "benchmarks/memory_benchmark/prices.deepseek-flash.json"


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings(groups=("lamemory",), repeats=1, tasks=("01_parents_birthday",))
        self.prices = Prices(PRICES)
        self.prices.data["embedding"] = {
            "provider": "openai-compatible", "model": "offline-fake-embedding", "input": 0.1}
        self.ledger = Ledger(self.root / "usage.jsonl", self.prices)
        self.sessions = [{"session_id": "seed", "date": "2025-03-01", "kind": "history_message",
                          "messages": [{"message_id": "X1", "content": "爸爸生日3月20日。"}]}]

    def runtime(self):
        runtime = RetrievalRuntime(self.settings, {}, self.ledger, offline=True)
        self.addCleanup(runtime.close)
        return runtime

    def test_seed_clone_semantic_channel_cache_and_online_index_refresh(self):
        runtime = self.runtime()
        # 人工同向向量只证明语义通道被执行，不作为真实语义效果证据。
        original = runtime.provider.embed_documents
        def constant(texts, *, timeout):
            original(texts, timeout=timeout)
            return [[1.0] + [0.0] * 7 for _ in texts]
        runtime.provider.embed_documents = constant
        seed = self.root / "seed"
        self.ledger.tags = {"stage": "build", "group": "lamemory"}
        build_seed("lamemory", seed, self.sessions, self.settings, FakeModel("memory", self.settings),
                   memory_factory=runtime.create)
        maintenance = load_json(seed / "maintenance_jobs.json")
        self.assertEqual(maintenance["embedding"]["status"], "complete")
        self.assertGreater(maintenance["embedding"]["indexed"], 0)
        frozen_hashes = hash_tree(seed)
        clone = self.root / "clone"
        shutil.copytree(seed, clone)
        before = len(self.ledger.events)
        backend = LaMemory(clone, "fresh", self.settings, FakeModel("memory", self.settings),
                           memory_factory=runtime.create)
        try:
            self.ledger.tags = {"stage": "online", "group": "lamemory"}
            result = backend.flush()
            self.assertEqual(result["embedding"]["missing"], 0)
            self.assertEqual(len(self.ledger.events), before)  # 克隆复用向量，不重建种子。
            bundle = backend.service.retrieve("paternal celebration", kinds=("item",))
            self.assertEqual(bundle.diagnostics["effective_mode"], "hybrid")
            self.assertIsNone(bundle.items[0].retrieval["keyword_rank"])
            self.assertEqual(bundle.items[0].retrieval["semantic_rank"], 1)
            after = len(self.ledger.events)
            again = backend.service.retrieve("paternal celebration", kinds=("item",))
            self.assertTrue(again.diagnostics["cache"]["query_embedding_hit"])
            self.assertEqual(len(self.ledger.events), after)
            backend.append_message({"role": "user", "content": "妈妈生日4月10日。"})
            backend.append_message({"role": "assistant", "content": "已收到。"})
            backend.commit_turn()
            self.assertEqual(backend.flush(index=False)["status"], "complete")
            with self.assertRaisesRegex(RuntimeError, "索引不完整"):
                backend.build_context()
            refreshed = backend.flush()
            self.assertEqual(refreshed["embedding"]["status"], "complete")
            self.assertGreater(refreshed["embedding"]["indexed"], maintenance["embedding"]["indexed"])
            self.assertTrue(any(e["purpose"] == "embedding_documents" for e in self.ledger.events[after:]))
        finally:
            backend.close()
        self.assertEqual(hash_tree(seed), frozen_hashes)

    def test_index_failure_retains_jobs_and_never_freezes_snapshot(self):
        runtime = self.runtime()
        def fail(*args, **kwargs):
            raise TimeoutError("offline injected failure")
        runtime.provider.embed_documents = fail
        seed = self.root / "failed-seed"
        with self.assertRaisesRegex(RuntimeError, "向量索引未完成"):
            build_seed("lamemory", seed, self.sessions, self.settings, FakeModel("memory", self.settings),
                       memory_factory=runtime.create)
        self.assertFalse((seed / "snapshot.json").exists())
        evidence = load_json(seed / "maintenance_jobs.json")
        self.assertEqual(evidence["embedding"]["status"], "failed")
        self.assertTrue(evidence["embedding_jobs"])
        self.assertEqual(len(self.ledger.events), self.settings.job_attempts)
        self.assertTrue(all(e["status"] == "error" for e in self.ledger.events))

    def test_query_failure_cannot_fall_back_to_keyword(self):
        runtime = self.runtime()
        seed = self.root / "seed"
        build_seed("lamemory", seed, self.sessions, self.settings, FakeModel("memory", self.settings),
                   memory_factory=runtime.create)
        backend = LaMemory(seed, "query", self.settings, FakeModel("memory", self.settings),
                           memory_factory=runtime.create)
        self.addCleanup(backend.close)
        def fail(*args, **kwargs):
            raise TimeoutError("query failed")
        runtime.provider.embed_query = fail
        backend.append_message({"role": "user", "content": "爸爸生日"})
        with self.assertRaises(RetrievalUnavailableError):
            backend.build_context()
        self.assertFalse(backend.config.allow_keyword_fallback)
        self.assertEqual(self.ledger.events[-1]["purpose"], "embedding_query")
        self.assertEqual(self.ledger.events[-1]["status"], "error")

    def test_embedding_usage_failures_and_budget_reservation(self):
        provider = FakeEmbedding()
        provider.config = SimpleNamespace(provider="openai-compatible")
        counted = self.ledger.wrap_embedding(provider)
        self.ledger.tags = {"stage": "online", "group": "lamemory"}
        counted.embed_query("爸爸生日", timeout=1)
        event = self.ledger.events[-1]
        self.assertEqual(event["tokens"], {"input": 10})
        self.assertAlmostEqual(event["cost"], 10 * 0.1 / 1_000_000)
        def invalid(*args, **kwargs):
            provider.usage_events.append({"usage": {"prompt_tokens": 23}})
            raise ValueError("invalid vector after HTTP response")
        provider.embed_query = invalid
        with self.assertRaises(ValueError):
            counted.embed_query("query", timeout=1)
        self.assertEqual(self.ledger.events[-1]["tokens"]["input"], 23)
        self.assertEqual(self.ledger.events[-1]["status"], "error")
        self.ledger.max_cost = self.ledger.total() + 0.000000001
        before = len(provider.usage_events)
        with self.assertRaises(CostLimit):
            counted.embed_documents(["new document"], timeout=1)
        self.assertEqual(len(provider.usage_events), before)
        self.assertTrue(self.ledger.cost_exhausted)

    def test_missing_usage_and_transport_error_are_not_zero_cost(self):
        provider = FakeEmbedding()
        provider.config = SimpleNamespace(provider="openai-compatible")
        provider.embed_query = lambda *a, **k: [1.0] + [0.0] * 7
        counted = self.ledger.wrap_embedding(provider)
        counted.embed_query("some input", timeout=1)
        self.assertEqual(self.ledger.events[-1]["usage_mode"], "reserved_estimate_missing_usage")
        def fail(*args, **kwargs):
            raise TimeoutError("request failed")
        provider.embed_query = fail
        with self.assertRaises(TimeoutError):
            counted.embed_query("some input", timeout=1)
        self.assertTrue(all(e["cost"] > 0 for e in self.ledger.events))
        self.assertEqual(self.ledger.events[-1]["usage_mode"], "reserved_estimate_failed_request")

    def test_estimate_missing_price_is_explicit_and_keyword_needs_no_embedding(self):
        result = estimate(self.settings, Prices(PRICES))
        self.assertEqual(result["unpriced_roles"], ["embedding"])
        self.assertIsNone(result["scenarios"]["expected"]["cost"])
        self.assertGreater(result["scenarios"]["expected"]["priced_cost"], 0)
        keyword = estimate(replace(self.settings, retrieval_mode="keyword"), Prices(PRICES))
        self.assertEqual(keyword["unpriced_roles"], [])
        self.assertIsNotNone(keyword["scenarios"]["expected"]["cost"])

    def test_live_path_uses_explicit_embedding_settings_and_counts_usage(self):
        env = {"LLM_PROVIDER": "deepseek", "LLM_MODEL_NAME": "deepseek-flash", "LLM_API_KEY": "test-secret",
               "LAMEMORY_RETRIEVAL_MODE": "keyword", "LAMEMORY_ALLOW_KEYWORD_FALLBACK": "true",
               "LAMEMORY_RERANK_ENABLED": "true", "LAMEMORY_SEMANTIC_MIN_SIMILARITY": "0.2",
               "LAMEMORY_EMBEDDING_MODEL": "offline-fake-embedding", "LAMEMORY_EMBEDDING_DIMENSIONS": "8",
               "LAMEMORY_EMBEDDING_BASE_URL": "https://embedding.example/v1",
               "LAMEMORY_EMBEDDING_NAMESPACE": "benchmark", "LAMEMORY_EMBEDDING_API_KEY": "embedding-secret"}
        class StubEmbedding(FakeEmbedding):
            def __init__(self, config):
                super().__init__()
                self.config, self.descriptor = config, config.descriptor
        class StubSandbox(OfflineSandbox):
            def __init__(self, settings):
                pass

            def preflight(self):
                return {"status": "test_stub"}
        chats = [nullcontext(FakeModel(role, self.settings)) for role in ("agent", "memory", "judge")]
        with patch("benchmarks.memory_benchmark.run.environment", return_value=env), \
             patch("benchmarks.memory_benchmark.run.ChatModel", side_effect=chats), \
             patch("benchmarks.memory_benchmark.run.Sandbox", StubSandbox), \
             patch("benchmarks.memory_benchmark.retrieval.OpenAIEmbeddingProvider", StubEmbedding):
            output = self.root / "live-stub"
            report = execute(self.settings, self.prices, output, progress=lambda *a: None)
        self.assertEqual(report["status"], "complete", report.get("error"))
        self.assertFalse(report["retrieval"]["simulated"])
        parameters = report["retrieval"]["parameters"]
        self.assertEqual(parameters["retrieval_mode"], "hybrid")
        self.assertFalse(parameters["allow_keyword_fallback"])
        self.assertFalse(parameters["rerank_enabled"])
        self.assertEqual(parameters["semantic_min_similarity"], 0.2)
        usage = [json.loads(line) for line in (output / "usage.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["role"] == "embedding" and e["cost"] > 0 for e in usage))
        text = (output / "report.json").read_text() + (output / "usage.jsonl").read_text()
        self.assertNotIn("test-secret", text)
        self.assertNotIn("embedding-secret", text)

    def test_missing_embedding_or_price_stops_before_any_paid_call(self):
        for env in ({}, {"LAMEMORY_EMBEDDING_MODEL": "offline-fake-embedding",
                         "LAMEMORY_EMBEDDING_BASE_URL": "https://embedding.example/v1",
                         "LAMEMORY_EMBEDDING_DIMENSIONS": "8", "LAMEMORY_EMBEDDING_NAMESPACE": "test"}):
            with patch("benchmarks.memory_benchmark.retrieval.OpenAIEmbeddingProvider") as client:
                with self.assertRaises(ValueError):
                    RetrievalRuntime(self.settings, env, Ledger(self.root / "missing.jsonl", Prices(PRICES)))
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
