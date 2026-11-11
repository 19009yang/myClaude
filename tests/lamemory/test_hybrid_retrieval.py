from dataclasses import replace
import unittest
import test_embedding_index as index_cases
from core.lamemory import LaMemoryService
from core.lamemory.retrieval.types import RetrievalUnavailableError
from core.lamemory.integration.runtime import create_memory


class HybridTests(index_cases.IndexFixture):
    def setUp(self):
        super().setUp()
        self.s.close()
        self.cfg = replace(self.cfg, retrieval_mode='hybrid')
        self.s = LaMemoryService(self.cfg, user_id='u', session_id='s', embedding_provider=self.fake)
    def test_semantic_and_keyword_channels(self):
        item = self.item('使用 uv 安装依赖')
        self.s.flush_embeddings()
        self.assertEqual(self.s._keyword_search('package manager'), [])
        bundle = self.s.retrieve('package manager')
        self.assertEqual(bundle.items[0].item.memory_id, item.memory_id)
        self.assertEqual(bundle.items[0].retrieval['score_kind'], 'rrf')
        self.assertEqual(self.fake.queries, 1)
        self.s.retrieve('package manager')
        self.assertEqual(self.fake.queries, 1)
        both = self.s.search('uv')[0]
        self.assertEqual(both.retrieval['keyword_rank'], 1)
        self.assertEqual(both.retrieval['semantic_rank'], 1)
        self.assertEqual(self.s.search('x', top_k=0), [])
    def test_correction_forget_no_cache_resurrection(self):
        item = self.item('old color blue')
        self.s.flush_embeddings()
        self.assertTrue(self.s.search('paraphrase'))
        self.s.update(item.memory_id, 'new color green')
        b = self.s.retrieve('green')
        self.assertTrue(b.diagnostics['degraded'])
        self.assertEqual(b.items[0].mode, 'keyword')
        self.s.flush_embeddings()
        new = self.s.search('paraphrase')[0].item
        self.s.forget(new.memory_id)
        self.assertEqual(self.s.search('paraphrase'), [])
    def test_fallback_strict_and_recovery(self):
        self.item('hello')
        self.assertTrue(self.s.retrieve('hello').diagnostics['degraded'])
        self.s.flush_embeddings()
        self.fake.fail = True
        self.assertEqual(self.s.search('hello')[0].mode, 'keyword')
        self.fake.fail = False
        self.assertEqual(self.s.search('hello')[0].mode, 'hybrid')
        self.s._query_embedding_cache.clear()
        self.fake.fail = True
        self.s.config = replace(self.cfg, allow_keyword_fallback=False)
        with self.assertRaises(RetrievalUnavailableError):
            self.s.search('hello')
    def test_scope_and_callback_change(self):
        visible = self.item('visible')
        with LaMemoryService(self.cfg, user_id='u', session_id='other', embedding_provider=self.fake) as other:
            other.store('private', scope_kind='session')
            other.flush_embeddings()
        self.s.flush_embeddings()
        self.assertEqual([h.item.memory_id for h in self.s.search('any', top_k=1)], [visible.memory_id])
        self.fake.hook = lambda: self.s.forget(visible.memory_id)
        self.assertEqual(self.s.search('different'), [])
    def test_top100_and_threshold(self):
        for n in range(40):
            self.item('unique ' + str(n))
        self.s.flush_embeddings()
        self.assertEqual(len(self.s.search('paraphrase', top_k=100)), 40)
        self.fake.embed_query = lambda text, timeout: [0, 1]
        self.assertEqual(self.s.search('unmatched'), [])
    def test_three_kinds_shared_query_and_dirty(self):
        item = self.item('build dependency')
        topic = self.s.define_topic('build', [item.memory_id])
        self.s.process_pending(10)
        # Synthetic validated procedure, no real trajectory/provider.
        with self.s.db.transaction():
            self.s.db.execute("INSERT INTO lamemory_experiences VALUES ('exp','u','user','u','build','when building','[\"run uv\"]','passed','hash','traj','active',1,'line',NULL,2,0,'now','now')")
        self.s.flush_embeddings()
        b = self.s.retrieve('paraphrase')
        self.assertEqual(self.fake.queries, 1)
        self.assertEqual(len(b.items), 1)
        self.assertEqual(b.summaries[0]['summary_id'], topic)
        self.assertEqual(b.experiences[0]['experience_id'], 'exp')
        self.s.append_message({'role': 'user', 'content': 'paraphrase'})
        c = self.s.build_context()
        self.assertEqual(c.diagnostics['retrieval_mode'], 'hybrid')
        self.assertNotIn('rrf_score', c.messages[0]['content'])
        with self.s.db.transaction():
            self.s.db.execute("UPDATE lamemory_experiences SET status='revoked'")
            self.s.db.execute('UPDATE lamemory_summaries SET dirty=1')
        b = self.s.retrieve('paraphrase')
        self.assertEqual(b.summaries, [])
        self.assertEqual(b.experiences, [])
    def test_runtime_offline_and_missing(self):
        with self.assertRaises(ValueError):
            create_memory(self.cfg, env={}, user_id='u', session_id='s')
        with create_memory(self.cfg, env={}, offline=True, user_id='u', session_id='s') as offline:
            self.assertIsNone(offline.embedding_provider)
            self.assertIn('offline', offline.retrieve('x').diagnostics['degraded_reasons'])
