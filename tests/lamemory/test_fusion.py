import unittest
from core.lamemory.retrieval.fusion import cosine, rrf, unit_vector


class FusionTests(unittest.TestCase):
    def test_rrf(self):
        result = rrf({'keyword': [('A', 1), ('A', 1), ('B', .5), ('C', .2)],
                      'semantic': [('B', .9), ('D', .8), ('A', .7)]})
        self.assertEqual([r[0] for r in result], ['B', 'A', 'D', 'C'])
        self.assertAlmostEqual(result[0][1]['rrf_score'], 1/62 + 1/61)
        self.assertIsNone(result[2][1]['keyword_rank'])
        self.assertEqual(rrf({'keyword': [('b', 1), ('a', 1)], 'semantic': [('a', 1), ('b', 1)]})[0][0], 'a')
        self.assertEqual(rrf({'keyword': [('a', 1)], 'semantic': [('b', 1)]}, weights={'semantic': 2})[0][0], 'b')

    def test_vectors(self):
        self.assertAlmostEqual(cosine([2, 0], [1, 1]), 2**-.5)
        for v in ([], [0, 0], [True, 1], [float('nan')], [float('inf')]):
            with self.assertRaises(ValueError):
                unit_vector(v)
        with self.assertRaises(ValueError):
            cosine([1], [1, 2])
