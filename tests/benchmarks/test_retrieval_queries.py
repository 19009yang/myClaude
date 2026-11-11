"""交付要求不进入检索，但完整任务与用户补充仍交给执行模型。"""
from pathlib import Path
import tempfile
import unittest

from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.backends import LaMemory
from benchmarks.memory_benchmark.queries import FIXED_REVIEWS, QUERY_STRATEGY, retrieval_text
from benchmarks.memory_benchmark.retrieval import RetrievalRuntime
from benchmarks.memory_benchmark.settings import Settings, SUITE, load_json
from benchmarks.memory_benchmark.smoke import FakeModel


PRICES = Path(__file__).resolve().parents[2] / "benchmarks/memory_benchmark/prices.deepseek-flash.json"


class RetrievalQueryTests(unittest.TestCase):
    def birthday_prompt(self):
        return (SUITE / "tasks/01_parents_birthday/prompt.md").read_text().replace(
            "{{output_dir}}", "/isolated/work")

    def test_public_memory_tasks_exclude_delivery_paragraph(self):
        tasks = load_json(SUITE / "manifest.json")["tasks"]
        for task in tasks[:6]:
            with self.subTest(task=task["task_id"]):
                prompt = (SUITE / task["prompt"]).read_text()
                query = retrieval_text(prompt)
                self.assertEqual(query, prompt.split("\n\n")[1])
                self.assertNotIn("{{output_dir}}", query)
                self.assertNotIn(".md", query)
                self.assertEqual(retrieval_text(prompt.replace("\n", "\r\n")), query)

    def test_unstructured_requests_and_unrecognized_formats_preserve_content(self):
        request = "帮我比较JSON配置。\n\n输出 `diff_report.json`，保留类型变化。"
        self.assertEqual(retrieval_text(request), request)
        header = "当前评测日期固定为2025-03-26。交付目录是 /isolated/work。"
        self.assertEqual(retrieval_text(header), header)
        # 未识别的格式不猜测段落含义，具体代码任务仍保留。
        self.assertEqual(retrieval_text(header + "\n\n请编写代码。\n\n数组整体替换。"),
                         "请编写代码。\n\n数组整体替换。")
        self.assertEqual(retrieval_text("输出分析报告是我的工作目标"), "输出分析报告是我的工作目标")

    def test_only_exact_fixed_reviews_are_omitted(self):
        for review in FIXED_REVIEWS:
            self.assertEqual(retrieval_text(review), "")
            correction = review + "\n爸爸生日更正为3月21日。"
            self.assertEqual(retrieval_text(correction), correction)

    def test_three_rounds_reuse_task_query_and_preserve_full_model_messages(self):
        settings = Settings(retrieval_mode="keyword")
        with tempfile.TemporaryDirectory() as tmp:
            backend = LaMemory(Path(tmp), "query-test", settings, FakeModel("memory", settings))
            self.addCleanup(backend.close)
            backend.service.store("父亲沈建国的生日是1972年3月20日")
            backend.service.store("母亲王芳的生日是1974年8月5日")
            noise = backend.service.store("用户目标是独立跑数并输出报告")
            prompt = self.birthday_prompt()
            reviews = load_json(SUITE / "judge/01_parents_birthday/follow_ups.json")["messages"]
            originals = [prompt, *reviews]
            for text in originals:
                backend.append_message({"role": "user", "content": text})
                context = backend.build_context()
                self.assertEqual(context.diagnostics["retrieval_query"], retrieval_text(prompt))
                self.assertEqual(context.diagnostics["retrieval_query_strategy"], QUERY_STRATEGY)
                self.assertNotIn(noise.item.memory_id,
                                 [h["item"]["memory_id"] for h in context.diagnostics["retrieved"]])
                self.assertEqual(context.messages[-1]["content"], text)
                backend.append_message({"role": "assistant", "content": "已保存。"})
            self.assertEqual([r["payload"]["content"] for r in backend.service.history()
                              if r["payload"]["role"] == "user"], originals)
            followup = "再查一下父亲的礼物待办，生日更正为3月21日。"
            backend.append_message({"role": "user", "content": followup})
            self.assertIn(followup, backend.build_context().diagnostics["retrieval_query"])
            explicit = backend.build_context(retrieval_query="父亲生日")
            self.assertEqual(explicit.diagnostics["retrieval_query"], "父亲生日")
            self.assertEqual(explicit.diagnostics["retrieval_query_strategy"], "explicit")

    def test_hybrid_embedding_receives_clean_query_and_reuses_it_for_reviews(self):
        settings = Settings(groups=("lamemory",), repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            prices = Prices(PRICES)
            prices.data["embedding"] = {"provider": "openai-compatible", "model": "offline-fake-embedding", "input": 0.1}
            runtime = RetrievalRuntime(settings, {}, Ledger(root / "usage.jsonl", prices), offline=True)
            self.addCleanup(runtime.close)
            queries = []
            original = runtime.provider.embed_query

            def embed_query(text, *, timeout):
                queries.append(text)
                return original(text, timeout=timeout)

            runtime.provider.embed_query = embed_query
            backend = LaMemory(root, "hybrid-query-test", settings, FakeModel("memory", settings),
                               memory_factory=runtime.create)
            self.addCleanup(backend.close)
            backend.service.store("父亲沈建国的生日是1972年3月20日")
            self.assertEqual(backend.service.flush_embeddings(timeout=30)["status"], "complete")
            prompt = self.birthday_prompt()
            reviews = load_json(SUITE / "judge/01_parents_birthday/follow_ups.json")["messages"]
            for text in [prompt, *reviews]:
                backend.append_message({"role": "user", "content": text})
                backend.build_context()
                backend.append_message({"role": "assistant", "content": "已保存。"})
            self.assertEqual(queries, [retrieval_text(prompt)])


if __name__ == "__main__":
    unittest.main()
