"""固定快照、单变量查询、完整排名及只读证据的隔离测试。"""
from dataclasses import asdict, replace
from contextlib import closing
from pathlib import Path
import sqlite3
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from benchmarks.lamemory.query_ablation import (
    INSTRUCTION, configured_provider, execute, file_hash, load_items, prepare,
)
from benchmarks.memory_benchmark.settings import load_json, save_json
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.embedding.space import EmbeddingDescriptor, digest


class QueryEmbedding:
    def __init__(self):
        self.descriptor = EmbeddingDescriptor('test-query-embedding', 3, digest('https://embedding.test/v1'), 'query-test')
        self.config = SimpleNamespace(provider='openai-compatible', model=self.descriptor.model)
        self.usage_events = []
        self.queries = []
        self.document_calls = 0

    def embed_documents(self, texts, *, timeout):
        self.document_calls += 1
        return [[1., 0., 0.] if '父亲生日' in text else
                [.9, .1, 0.] if '母亲生日' in text else [0., 1., 0.] for text in texts]

    def embed_query(self, text, *, timeout):
        self.queries.append(text)
        self.usage_events.append({'usage': {'prompt_tokens': 10}})
        return ([1., 0., 0.] if text.endswith('父亲生日') else
                [.7, .7, 0.] if text.startswith('Instruct:') else [0., 1., 0.])


class QueryAblationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.run = self.root / 'source-run'
        self.database = self.run / 'seeds/lamemory/lamemory.sqlite3'
        self.database.parent.mkdir(parents=True)
        provider = QueryEmbedding()
        cfg = LaMemoryConfig(database_path=self.database, retrieval_mode='hybrid', top_k=1,
                             semantic_candidate_k=1, keyword_candidate_k=1)
        with LaMemoryService(cfg, user_id='memory-benchmark-user', session_id='seed', embedding_provider=provider) as memory:
            self.father = memory.store('父亲生日3月20日', slot_key='family.father.birthday').item.memory_id
            memory.store('母亲生日8月5日', slot_key='family.mother.birthday')
            memory.store('每周安排固定日程')
            memory.store('private-session-only', scope_kind='session')
            self.assertEqual(memory.flush_embeddings(timeout=5)['status'], 'complete')
        parameters = asdict(cfg)
        parameters.pop('database_path')
        prices = load_json(Path(__file__).resolve().parents[2] / 'benchmarks/memory_benchmark/prices.deepseek-flash.json')
        prices['embedding'] = {'provider': 'openai-compatible', 'model': provider.descriptor.model, 'input': .14}
        save_json(self.run / 'report.json', {'status': 'complete', 'retrieval': {
            'mode': 'hybrid', 'embedding': asdict(provider.descriptor), 'parameters': parameters}, 'pricing': prices})
        prompt = self.run / 'runs/birthday/repeat-1/lamemory/round-1/prompt.md'
        prompt.parent.mkdir(parents=True)
        prompt.write_text('当前评测日期固定为2025-03-26。交付目录是 /work。\n\n'
                          '帮我在日历安排爸妈生日提醒。\n\n输出 `birthday.md`，包含日期和备注。')
        self.output = self.root / 'experiment'
        self.original_hash = file_hash(self.database)

    def prepare(self, **kwargs):
        return prepare(self.run, 'birthday', self.output, '父亲生日',
                       target_slots=['family.father.birthday'], **kwargs)

    def test_prepare_freezes_one_snapshot_and_changes_only_intended_query_inputs(self):
        plan = self.prepare()
        self.assertEqual(plan['item_count'], 3)  # 种子会话私有项对新会话不可见。
        self.assertEqual(file_hash(self.output / 'snapshot.sqlite3'), self.original_hash)
        self.assertEqual(file_hash(self.database), self.original_hash)
        self.assertFalse((self.output / 'results').exists())
        self.assertNotIn('birthday.md', plan['request'])
        a, b, c = [plan['variants'][v] for v in ('raw', 'instruct', 'facts')]
        self.assertEqual(a['keyword_query'], b['keyword_query'])
        self.assertEqual(b['embedding_input'], f'Instruct: {INSTRUCTION}\nQuery: ' + a['embedding_input'])
        self.assertEqual(c['embedding_input'], f'Instruct: {INSTRUCTION}\nQuery: 父亲生日')
        self.assertNotIn('3月20日', c['embedding_input'])  # 目标标注不泄漏到查询。
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_complete_comparison_distinguishes_threshold_candidate_limit_and_success(self):
        self.prepare()
        provider = QueryEmbedding()
        report = execute(self.output, provider=provider)
        self.assertEqual(report['status'], 'complete')
        self.assertTrue(report['simulated'])
        self.assertFalse(report['real_semantic_validation'])
        self.assertEqual(len(provider.queries), 3)
        self.assertEqual(provider.document_calls, 0)
        a, b, c = [r['targets'][0] for r in report['results']]
        self.assertEqual(a['semantic_exclusion'], 'below_similarity_threshold')
        self.assertEqual(b['semantic_exclusion'], 'outside_candidate_limit')
        self.assertIsNone(c['semantic_exclusion'])
        self.assertEqual(c['semantic_rank_all'], 1)
        self.assertTrue(c['selected'])
        detail = load_json(self.output / 'results/facts-repeat-1.json')
        self.assertEqual(len(detail['all_items']), 3)
        self.assertEqual(len(detail['query_vector']), 3)
        self.assertEqual(detail['snapshot_sha256'], self.original_hash)
        self.assertEqual(file_hash(self.database), self.original_hash)
        self.assertEqual(file_hash(self.output / 'snapshot.sqlite3'), self.original_hash)
        self.assertEqual(report['usage']['calls'], 3)
        with self.assertRaises(FileExistsError):
            execute(self.output, provider=provider)

    def test_repeats_rotate_order_without_query_cache(self):
        plan = self.prepare(repeats=3)
        provider = QueryEmbedding()
        report = execute(self.output, provider=provider)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(provider.queries), 9)
        self.assertEqual([r['variant'] for r in report['results']],
                         ['raw', 'instruct', 'facts', 'instruct', 'facts', 'raw', 'facts', 'raw', 'instruct'])
        self.assertEqual(provider.queries.count(plan['variants']['raw']['embedding_input']), 3)

    def test_raw_ranking_matches_runtime_on_an_isolated_copy(self):
        plan = self.prepare()
        report = execute(self.output, provider=QueryEmbedding())
        self.assertEqual(report['status'], 'complete')
        clone = self.root / 'runtime-copy.sqlite3'
        shutil.copyfile(self.output / 'snapshot.sqlite3', clone)
        cfg = LaMemoryConfig(database_path=clone, **plan['parameters'])
        with LaMemoryService(cfg, user_id=plan['identity']['user'], session_id=plan['identity']['session'],
                             embedding_provider=QueryEmbedding()) as memory:
            actual = memory.retrieve(plan['request'], kinds=('item',))
        recorded = load_json(self.output / 'results/raw-repeat-1.json')
        self.assertEqual(recorded['selected_ids'], [h.item.memory_id for h in actual.items])
        row = next(r for r in recorded['all_items'] if r['selected'])
        self.assertEqual(row['fusion'], {k: v for k, v in actual.items[0].retrieval.items() if k != 'score_kind'})

    def test_plan_or_snapshot_change_is_rejected_before_requests(self):
        self.prepare()
        provider = QueryEmbedding()
        with (self.output / 'snapshot.sqlite3').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, '快照校验失败'):
            execute(self.output, provider=provider)
        self.assertEqual(provider.queries, [])
        self.assertEqual(file_hash(self.database), self.original_hash)

    def test_nonempty_wal_and_missing_projection_fail_before_requests(self):
        wal = Path(str(self.database) + '-wal')
        wal.write_bytes(b'uncheckpointed')
        with self.assertRaisesRegex(ValueError, '非空 WAL'):
            self.prepare()
        self.assertEqual(file_hash(self.database), self.original_hash)

    def test_corrupt_vector_is_not_silently_dropped(self):
        self.prepare()
        path = self.output / 'snapshot.sqlite3'
        # 只改临时测试副本，不触碰源快照。
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('PRAGMA journal_mode=DELETE')
            db.execute("DELETE FROM lamemory_embeddings WHERE entity_id=?", (self.father,))
        plan = load_json(self.output / 'plan.json')
        with self.assertRaisesRegex(ValueError, '有效投影'):
            load_items(path, EmbeddingDescriptor(**plan['embedding']), plan['identity'])

    def test_provider_error_preserves_usage_and_never_claims_complete(self):
        self.prepare()
        provider = QueryEmbedding()

        def fail(text, *, timeout):
            provider.usage_events.append({'usage': {'prompt_tokens': 8}})
            raise TimeoutError('test failure')

        provider.embed_query = fail
        report = execute(self.output, provider=provider)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error_type'], 'TimeoutError')
        self.assertFalse(report['real_semantic_validation'])
        self.assertEqual(report['usage']['calls'], 1)
        self.assertEqual(report['results'], [])
        self.assertTrue((self.output / 'results/report.json').exists())

    def test_budget_guard_prevents_paid_query_before_send(self):
        self.prepare()
        provider = QueryEmbedding()
        provider.close = lambda: None
        with patch('benchmarks.lamemory.query_ablation.configured_provider', return_value=provider):
            report = execute(self.output, env={}, max_cost=1e-12)
        self.assertEqual(report['status'], 'failed')
        self.assertEqual(report['error_type'], 'CostLimit')
        self.assertEqual(provider.queries, [])
        self.assertEqual(report['usage']['calls'], 0)

    def test_prefix_in_provider_is_rejected_to_avoid_double_instructions(self):
        plan = self.prepare()
        provider = QueryEmbedding()
        provider.descriptor = replace(provider.descriptor, query_prefix='implicit prefix: ')
        with self.assertRaisesRegex(ValueError, '隐式追加查询前缀'):
            execute(self.output, provider=provider)
        env = {'LAMEMORY_EMBEDDING_BASE_URL': 'https://embedding.test/v1',
               'LAMEMORY_EMBEDDING_QUERY_PREFIX': 'unwanted environment prefix'}
        with patch('benchmarks.lamemory.query_ablation.OpenAIEmbeddingProvider') as factory:
            configured_provider(plan, env)
            self.assertEqual(factory.call_args.args[0].query_prefix, '')
        env['LAMEMORY_EMBEDDING_BASE_URL'] = 'https://different.test/v1'
        with self.assertRaisesRegex(ValueError, '端点'):
            configured_provider(plan, env)


if __name__ == '__main__':
    unittest.main()
