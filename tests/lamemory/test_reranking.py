from dataclasses import replace
import test_hybrid_retrieval as hybrid_cases
from core.lamemory.retrieval.reranking import ChatReranker, validate_scores


class RerankTests(hybrid_cases.index_cases.IndexFixture):
    def setUp(self):
        super().setUp()
        self.s.config = replace(self.cfg, retrieval_mode='hybrid')
        self.cfg = self.s.config
    def test_rerank_protocol_and_fallback(self):
        self.item('alpha')
        self.item('beta')
        self.s.flush_embeddings()
        class Rerank:
            fingerprint = 'test'
            calls = 0
            bad = False
            def rerank(self, query, documents, *, timeout):
                self.calls += 1
                self.documents = documents
                return [{'id': d['id'], 'score': n} for n, d in enumerate(documents)] if not self.bad else []
        r = Rerank()
        self.s.reranker = r
        original = self.s.retrieve('something')
        self.assertEqual(r.calls, 0)
        self.s.config = replace(self.cfg, rerank_enabled=True)
        ranked = self.s.retrieve('something')
        self.assertEqual([h.item.memory_id for h in ranked.items], list(reversed([h.item.memory_id for h in original.items])))
        self.assertEqual(ranked.items[0].retrieval['score_kind'], 'rerank')
        r.bad = True
        fallback = self.s.retrieve('something')
        self.assertEqual([h.item.memory_id for h in fallback.items], [h.item.memory_id for h in original.items])
        self.assertEqual(fallback.diagnostics['per_kind']['item']['rerank_status'], 'fallback')
    def test_invalid_scores(self):
        docs = [{'id': 'a'}, {'id': 'b'}]
        for result in ([], [{'id': 'a', 'score': .5}]*2, [{'id': 'x', 'score': 1}, {'id': 'b', 'score': .5}],
                       [{'id': 'a', 'score': True}, {'id': 'b', 'score': 1}],
                       [{'id': 'a', 'score': float('nan')}, {'id': 'b', 'score': 1}]):
            with self.assertRaises(ValueError):
                validate_scores(result, docs)
        adapter = ChatReranker(chat=lambda **kw: {'content': '[{"id":"a","score":0.5}]'})
        self.assertEqual(adapter.rerank('q', [{'id': 'a', 'text': 'data'}], timeout=1)[0]['score'], .5)
