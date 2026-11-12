"""四组公平协议：完整历史、相同会话边界、配对样本及成本分离。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.memory_benchmark.accounting import Prices
from benchmarks.memory_benchmark.backends import BACKENDS, build_seed
from benchmarks.memory_benchmark.estimate import estimate
from benchmarks.memory_benchmark.reporting import summarize_runs, paired_comparisons, cost_breakdown
from benchmarks.memory_benchmark.run import execute
from benchmarks.memory_benchmark.settings import Settings, load_json
from benchmarks.memory_benchmark.smoke import FakeModel
from core.lamemory.models import BudgetError

PRICES = Path(__file__).resolve().parents[2] / 'benchmarks/memory_benchmark/prices.deepseek-flash.json'
SESSIONS = [dict(session_id='a', date='2025-01-01', kind='history_message',
                 messages=[dict(message_id='H1', content='端口是1234。')]),
            dict(session_id='b', date='2025-02-01', kind='synthetic_supplement',
                 messages=[dict(message_id='H2', content='更正，端口改成5678，旧端口停用。')])]


def scored(group, task, score, *, repeat=1):
    grade = dict(status='complete', score=score, passed=score >= 70,
                 dimensions={'memory': dict(score=score, max_score=100)})
    return dict(group=group, task_id=task, repeat=repeat,
                rounds=[dict(round=n, status='complete', grade=copy.deepcopy(grade)) for n in (1, 2, 3)])


class FairComparisonTests(unittest.TestCase):
    def test_full_history_preserves_order_dates_sources_and_never_calls_a_model(self):
        settings = Settings(groups=('full_history',), repeats=1)
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'seed'
            def forbidden(**kwargs):
                self.fail('完整历史基线不能调用记忆模型')
            build_seed('full_history', directory, SESSIONS, settings, forbidden)
            history = load_json(directory / 'history.json')
            self.assertEqual([r['message_id'] for r in history], ['H1', 'H2'])
            self.assertEqual([r['date'] for r in history], ['2025-01-01', '2025-02-01'])
            frozen = (directory / 'history.json').read_bytes()
            backend = BACKENDS['full_history'](directory, 'new', settings, forbidden)
            for message in [dict(role='user', content='当前端口？'), dict(role='assistant', content='5678。')]:
                backend.append_message(message)
            backend.commit_turn()
            backend.flush()
            context = backend.build_context('公共规则')
            self.assertIn('1234', context.messages[0]['content'])
            self.assertIn('5678', context.messages[0]['content'])
            self.assertNotIn('H1', context.messages[0]['content'])
            self.assertNotIn('H2', context.messages[0]['content'])
            self.assertEqual(context.messages[1:], backend.messages)
            self.assertEqual((directory / 'history.json').read_bytes(), frozen)
            self.assertFalse((directory / 'lamemory.sqlite3').exists())
            self.assertFalse((directory / 'MEMORY.md').exists())

    def test_all_four_groups_preserve_current_transcript_and_reject_overflow(self):
        settings = Settings(context_window=5000, max_output_tokens=1024, retrieval_mode='keyword')
        for group in settings.groups:
            with self.subTest(group=group), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                if group == 'full_history':
                    (root / 'history.json').write_text('[]')
                model = FakeModel('memory', settings)
                backend = BACKENDS[group](root, 'task', settings, model)
                try:
                    messages = [dict(role='user', content='首轮请求'), dict(role='assistant', content='首轮回答'),
                                dict(role='user', content='当前请求')]
                    for message in messages:
                        backend.append_message(message)
                    self.assertEqual(backend.build_context('规则').messages[1:], messages)
                    backend.append_message(dict(role='assistant', content='x' * 20000))
                    backend.append_message(dict(role='user', content='复查'))
                    with self.assertRaises(BudgetError):
                        backend.build_context('规则')
                    self.assertEqual(model.serial, 0)  # 不调用摘要来改变任一组的会话。
                    if group == 'lamemory':
                        self.assertEqual(len(backend.service.history()), 5)
                    elif group == 'legacy':
                        self.assertEqual(len(backend.legacy.messages), 5)
                    else:
                        self.assertEqual(len(backend.messages), 5)
                finally:
                    backend.close()

    def test_both_memory_backends_receive_same_dated_records_and_keep_original_hash(self):
        import hashlib
        settings = Settings(retrieval_mode='keyword', hierarchy=False)
        delivered = []
        for group in ('legacy', 'lamemory'):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / 'seed'
                fake = FakeModel('memory', settings)
                seen = []
                def recorded(**kwargs):
                    seen.append(copy.deepcopy(kwargs['messages']))
                    return fake(**kwargs)
                build_seed(group, root, SESSIONS, settings, recorded)
                mapping = load_json(root / 'seed_evidence.json')
                self.assertEqual(mapping['H1']['content_sha256'], hashlib.sha256('端口是1234。'.encode()).hexdigest())
                self.assertNotEqual(mapping['H1']['content_sha256'], mapping['H1']['replay_sha256'])
                text = json.dumps(seen, ensure_ascii=False)
                self.assertIn('2025-01-01', text)
                self.assertIn('2025-02-01', text)
                self.assertNotIn('H1', text)
                self.assertNotIn('H2', text)
                self.assertIn('H1', mapping)
                self.assertIn('H2', mapping)
                delivered.append([mapping[k]['replay_sha256'] for k in ('H1', 'H2')])
        self.assertEqual(delivered[0], delivered[1])

    def test_full_history_too_large_fails_without_truncating_it(self):
        settings = Settings(context_window=3000, max_output_tokens=1024)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            history = [{'content': '事实' * 6000}]
            (root / 'history.json').write_text(json.dumps(history))
            backend = BACKENDS['full_history'](root, 'task', settings)
            backend.append_message(dict(role='user', content='请求'))
            with self.assertRaises(BudgetError):
                backend.build_context('规则')
            self.assertEqual(load_json(root / 'history.json'), history)

    def test_capacity_judge_failure_and_unstarted_samples_are_separate(self):
        a, b = scored('legacy', 'a', 100), scored('legacy', 'b', 90)
        a['rounds'][1]['grade'] = dict(status='error')
        b['rounds'][2].update(status='error', error_type='BudgetError')
        planned = [dict(group='legacy', task_id=t, repeat=1, category='memory') for t in ('a', 'b', 'c')]
        row = summarize_runs([a, b], planned)['legacy']
        self.assertEqual(row['planned_runs'], 3)
        self.assertEqual(row['not_started_runs'], 1)
        self.assertEqual(row['execution_complete_runs'], 1)
        self.assertEqual(row['execution_failed_runs'], 1)
        self.assertEqual(row['judge_failed_runs'], 1)
        self.assertEqual(row['failure_causes'], {'judge_error': 1, 'context_capacity': 1})
        self.assertEqual(row['scored_final_runs'], 1)
        self.assertEqual(row['final_mean_score'], 100)
        self.assertEqual(row['final_pass_rate'], 0)

    def test_pairing_uses_same_task_repeat_and_excludes_invalid_artifact_scores(self):
        runs = [scored('legacy', 'a', 100), scored('lamemory', 'a', 80),
                scored('legacy', 'b', 100), scored('lamemory', 'b', 10)]
        runs[-1]['rounds'][-1]['grade']['artifact_only'] = True
        planned = [dict(group=g, task_id=t, repeat=1, category='memory')
                   for t in ('a', 'b', 'c') for g in ('legacy', 'lamemory')]
        result = next(r for r in paired_comparisons(runs, planned)
                      if r['category'] == 'all' and r['stage'] == 'final')
        self.assertEqual((result['planned_pairs'], result['valid_pairs']), (3, 1))
        self.assertEqual(result['mean_delta'], 20)
        self.assertEqual(len(result['excluded']), 2)

    def test_paired_costs_exclude_unmatched_and_failed_task_runs(self):
        runs = [scored(g, t, 90) for t in ('a', 'b') for g in ('legacy', 'lamemory')]
        runs[-1]['rounds'][-1]['status'] = 'error'
        planned = [dict(group=g, task_id=t, repeat=1, category='memory')
                   for t in ('a', 'b') for g in ('legacy', 'lamemory')]
        events = [dict(group=g, task_id=t, repeat=1, stage='online', cost=cost,
                       usage_mode='provider_usage', tokens=dict(input=5, output=2))
                  for t, cost in [('a', 2), ('b', 100)] for g in ('legacy', 'lamemory')]
        costs = cost_breakdown(events, planned, runs)
        comparison = next(r for r in paired_comparisons(runs, planned, costs)
                          if r['stage'] == 'final' and r['category'] == 'all')
        self.assertEqual(comparison['cost_pairs'], 1)
        self.assertEqual(comparison['left_mean_online_cost'], 2)
        self.assertEqual(comparison['right_mean_online_cost'], 2)

    def test_estimate_and_costs_separate_full_history_build_online_and_judge(self):
        settings = Settings(groups=('full_history',), repeats=1, tasks=('01_parents_birthday',))
        report = estimate(settings, Prices(PRICES))
        for scenario in report['scenarios'].values():
            self.assertFalse(any('extraction' in k or 'summary' in k or k.startswith('build/')
                                 for k in scenario['buckets']))
        planned = [dict(group='full_history', task_id='a', repeat=1, category='memory')]
        events = [dict(group='full_history', stage=stage, task_id='a', repeat=1, cost=cost,
                       usage_mode='provider_usage', tokens=dict(input=5, cache_read=10, output=2))
                  for stage, cost in [('online', 2), ('judge', 5)]]
        costs = cost_breakdown(events, planned, reused_seed=True)
        self.assertEqual(costs['groups']['full_history']['build_plus_workload_cost'], 2)
        self.assertEqual(costs['groups']['full_history']['stages']['judge']['cost'], 5)
        self.assertFalse(costs['groups']['full_history']['seed_cost_included'])
        self.assertEqual(costs['by_run'][0]['online_input_tokens'], 15)

    def test_pipeline_rotates_order_and_records_four_group_protocol(self):
        settings = Settings(groups=('none', 'full_history'), repeats=2,
                            tasks=('01_parents_birthday', '02_therapy_schedule'))
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'run'
            report = execute(settings, Prices(PRICES), output, offline=True, progress=lambda *a: None)
            self.assertEqual(report['status'], 'complete', report.get('error'))
            self.assertEqual([r['group'] for r in report['runs']],
                             ['none', 'full_history', 'full_history', 'none',
                              'full_history', 'none', 'none', 'full_history'])
            self.assertEqual(report['protocol']['context_policy'], 'shared_strict')
            self.assertTrue(report['cost_breakdown']['simulated'])
            self.assertEqual(report['usage']['cost'], 0)
            self.assertTrue(report['paired_comparisons'])
            for path in (output / 'runs').rglob('trace.jsonl'):
                contexts = [r['data'] for line in path.read_text().splitlines()
                            if (r := json.loads(line))['event'] == 'context']
                self.assertTrue(all(r['context_policy'] == 'shared_strict' for r in contexts))


if __name__ == '__main__':
    unittest.main()
