"""验收工具自身使用合成临时快照，不调用真实模型。"""
import unittest
from benchmarks.lamemory.hybrid_retrieval import evaluate, metrics


class BenchmarkTests(unittest.TestCase):
    def test_missing_real_config_is_blocked(self):
        results = evaluate(env={})
        self.assertEqual(results['keyword']['status'], 'complete')
        self.assertEqual(len(results['keyword']['queries']), 40)
        self.assertEqual(sum(not r['expected'] for r in results['keyword']['queries']), 8)
        self.assertEqual(results['hybrid']['status'], 'blocked')
        self.assertEqual(results['hybrid-rerank']['status'], 'blocked')
        self.assertFalse(results['hybrid']['real_semantic_validation'])
    def test_hand_calculated_metrics(self):
        rows = [{'expected': ['a', 'b'], 'selected': ['c', 'a'], 'latency_ms': 1, 'diagnostics': {'degraded': False}},
                {'expected': [], 'selected': ['c'], 'latency_ms': 2, 'diagnostics': {'degraded': False}}]
        result = metrics(rows)
        self.assertEqual(result['recall_at_5'], .5)
        self.assertEqual(result['mrr_at_5'], .5)
        self.assertAlmostEqual(result['ndcg_at_5'], (1 / 1.584962500721156)/(1+1/1.584962500721156))
        self.assertEqual(result['no_answer_false_recall_rate'], 1)

    def test_fixture_id_tie_break_is_repeatable(self):
        first = evaluate('keyword', env={})['keyword']
        second = evaluate('keyword', env={})['keyword']
        self.assertEqual([r['selected'] for r in first['queries']], [r['selected'] for r in second['queries']])
        for split in ('calibration', 'acceptance'):
            for name in ('recall_at_5', 'mrr_at_5', 'ndcg_at_5', 'no_answer_false_recall_rate'):
                self.assertEqual(first['metrics_by_split'][split][name], second['metrics_by_split'][split][name])
