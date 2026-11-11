"""主评测方案 C 的查询来源、指令、三轮复用和全任务接线。"""
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmarks.lamemory.query_ablation import INSTRUCTION
from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.backends import LaMemory
from benchmarks.memory_benchmark.queries import (
    FACTS_FILE, FACTS_INSTRUCTION, FACTS_PREFIX, FACTS_QUERY_STRATEGY, public_fact_queries,
)
from benchmarks.memory_benchmark.retrieval import RetrievalRuntime
from benchmarks.memory_benchmark.run import execute, hash_tree, seed_origin, model_configs
from benchmarks.memory_benchmark.settings import Settings, SUITE, load_json, selected_tasks
from benchmarks.memory_benchmark.smoke import FakeModel


PRICES = Path(__file__).resolve().parents[2] / 'benchmarks/memory_benchmark/prices.deepseek-flash.json'


class FactsQueryModeTests(unittest.TestCase):
    def test_plan_covers_all_public_tasks_and_matches_prior_c_instruction(self):
        _, tasks = selected_tasks(Settings())
        plan = public_fact_queries(tasks)
        self.assertEqual(len(plan['queries']), 8)
        self.assertEqual(FACTS_INSTRUCTION, INSTRUCTION)
        self.assertEqual(plan['queries']['01_parents_birthday'],
                         '父亲和母亲的姓名、生日日期、生日提醒偏好，以及相关礼物待办')
        self.assertNotIn('沈建国', str(plan['queries']))
        self.assertNotIn('1972', str(plan['queries']))
        self.assertNotIn('14天', str(plan['queries']))

    def test_invalid_mode_and_keyword_c_are_rejected(self):
        self.assertEqual(Settings().retrieval_query_mode, 'request')
        with self.assertRaises(ValueError):
            Settings(retrieval_query_mode='unknown')
        with self.assertRaises(ValueError):
            Settings(retrieval_query_mode='facts', retrieval_mode='keyword')

    def test_supplemental_judge_options_do_not_change_agent_reasoning(self):
        configs = model_configs(Settings(judge_reasoning_effort='off'), env={
            'LLM_PROVIDER': 'deepseek', 'LLM_MODEL_NAME': 'deepseek-flash',
            'LLM_API_KEY': 'test-only', 'LLM_CONTEXT_WINDOW': '65536'})
        self.assertEqual(configs['judge'].reasoning_effort, 'off')
        self.assertIsNone(configs['agent'].reasoning_effort)

    def test_changed_public_prompt_invalidates_query_before_model_calls(self):
        data = load_json(FACTS_FILE)
        data['queries']['01_parents_birthday']['prompt_sha256'] = 'wrong'
        _, tasks = selected_tasks(Settings(tasks=('01_parents_birthday',)))
        with patch('benchmarks.memory_benchmark.queries.load_json', return_value=data):
            with self.assertRaisesRegex(ValueError, '公开提示与事实查询不一致'):
                public_fact_queries(tasks)

    def test_c_sends_facts_query_with_one_instruction_and_preserves_public_message(self):
        settings = Settings(groups=('lamemory',), repeats=1, retrieval_query_mode='facts')
        _, tasks = selected_tasks(settings)
        task_query = public_fact_queries(tasks)['queries']['01_parents_birthday']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger = Ledger(root / 'usage.jsonl', Prices(PRICES))
            runtime = RetrievalRuntime(settings, {}, ledger, offline=True)
            self.addCleanup(runtime.close)
            self.assertEqual(runtime.provider.descriptor.query_prefix, FACTS_PREFIX)
            backend = LaMemory(root, 'task', settings, FakeModel('memory', settings),
                               memory_factory=runtime.create, task_query=task_query)
            self.addCleanup(backend.close)
            backend.service.store('父亲生日3月20日')
            backend.service.flush_embeddings(timeout=5)
            inputs = []
            original = runtime.provider.embed_documents

            def documents(texts, *, timeout):
                inputs.extend(texts)
                return original(texts, timeout=timeout)

            runtime.provider.embed_documents = documents
            prompt = (SUITE / 'tasks/01_parents_birthday/prompt.md').read_text()
            reviews = load_json(SUITE / 'judge/01_parents_birthday/follow_ups.json')['messages']
            for text in [prompt, *reviews]:
                backend.append_message({'role': 'user', 'content': text})
                context = backend.build_context()
                self.assertEqual(context.diagnostics['retrieval_query'], task_query)
                self.assertEqual(context.diagnostics['retrieval_query_strategy'], FACTS_QUERY_STRATEGY)
                self.assertEqual(context.messages[-1]['content'], text)
                backend.append_message({'role': 'assistant', 'content': '已保存。'})
            self.assertEqual(inputs, [FACTS_PREFIX + task_query])
            self.assertEqual(sum(e['purpose'] == 'embedding_query' for e in ledger.events), 1)

    def test_missing_task_query_does_not_silently_use_request(self):
        settings = Settings(retrieval_query_mode='facts')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = RetrievalRuntime(settings, {}, Ledger(root / 'usage.jsonl', Prices(PRICES)), offline=True)
            self.addCleanup(runtime.close)
            backend = LaMemory(root, 'task', settings, FakeModel('memory', settings), memory_factory=runtime.create)
            self.addCleanup(backend.close)
            backend.append_message({'role': 'user', 'content': '生日提醒'})
            with self.assertRaisesRegex(ValueError, '不能静默回退'):
                backend.build_context()

    def test_all_eight_tasks_complete_offline_with_facts_plan_in_trace(self):
        settings = Settings(groups=('lamemory',), repeats=1, retrieval_query_mode='facts')
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            report = execute(settings, Prices(PRICES), output, offline=True, progress=lambda *a: None)
            self.assertEqual(report['status'], 'complete', report.get('error'))
            self.assertEqual(report['protocol']['retrieval_query_strategy'], FACTS_QUERY_STRATEGY)
            self.assertEqual(report['retrieval']['embedding']['query_prefix'], FACTS_PREFIX)
            self.assertEqual(len(report['runs']), 8)
            self.assertEqual(report['groups']['lamemory']['complete_runs'], 8)
            plan = load_json(output / 'query_plan.json')
            for run in report['runs']:
                for number in range(1, 4):
                    trace = output / f"runs/{run['task_id']}/repeat-1/lamemory/round-{number}/trace.jsonl"
                    contexts = [d['data'] for line in trace.read_text().splitlines()
                                if (d := json.loads(line))['event'] == 'context']
                    self.assertTrue(contexts)
                    self.assertTrue(all(c['retrieval_query'] == plan['queries'][run['task_id']] for c in contexts))
            # 补跑使用同一份快照，不产生任何建库调用。
            followup = Path(tmp) / 'followup'
            resumed = execute(replace(settings, tasks=('08_buggy_code_review',), seed_source=str(output)),
                              Prices(PRICES), followup, offline=True, progress=lambda *a: None)
            self.assertEqual(resumed['status'], 'complete', resumed.get('error'))
            self.assertEqual(resumed['seed_snapshot_hashes'], report['seed_snapshot_hashes'])
            self.assertEqual(hash_tree(output / 'seeds/lamemory'), report['seed_snapshot_hashes']['lamemory'])
            usage = [json.loads(line) for line in (followup / 'usage.jsonl').read_text().splitlines()]
            self.assertFalse(any(row['stage'] == 'build' for row in usage))
            self.assertEqual(resumed['seed_origin']['source_run'], str(output.resolve()))
            (output / 'seeds/lamemory/unexpected.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, '快照校验失败'):
                seed_origin(replace(settings, seed_source=str(output)), report['task_hashes'])


if __name__ == '__main__':
    unittest.main()
