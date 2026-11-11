import tempfile
import unittest
from pathlib import Path
from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.embedding.space import EmbeddingDescriptor


class FakeEmbedding:
    descriptor = EmbeddingDescriptor('fixture', 2, 'offline', 'v1')
    def __init__(self):
        self.documents = 0
        self.queries = 0
        self.hook = None
        self.fail = False
    def embed_documents(self, texts, *, timeout):
        self.documents += len(texts)
        if self.hook:
            self.hook()
        if self.fail:
            raise TimeoutError('private-key should not persist')
        return [[1, 0] for _ in texts]
    def embed_query(self, text, *, timeout):
        self.queries += 1
        if self.hook:
            self.hook()
        if self.fail:
            raise TimeoutError('secret')
        return [1, 0]


class IndexFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = LaMemoryConfig(database_path=Path(self.tmp.name)/'test.db')
        self.fake = FakeEmbedding()
        self.s = LaMemoryService(self.cfg, user_id='u', session_id='s', embedding_provider=self.fake)
    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()
    def item(self, text='original', **kwargs):
        return self.s.store(text, **kwargs).item


class IndexTests(IndexFixture):
    def test_backfill_repair_restart(self):
        self.item()
        self.assertEqual(self.s.enqueue_embeddings()['queued'], 1)
        self.assertEqual(self.s.enqueue_embeddings()['queued'], 0)
        self.s.close()
        self.s = LaMemoryService(self.cfg, user_id='u', session_id='s', embedding_provider=self.fake)
        self.assertEqual(self.s.flush_embeddings()['indexed'], 1)
        with self.s.db.transaction():
            self.s.db.execute('DELETE FROM lamemory_embeddings')
        self.assertEqual(self.s.enqueue_embeddings()['repaired'], 1)
        self.assertEqual(self.s.flush_embeddings()['status'], 'complete')
    def test_cas_no_transaction_and_forget(self):
        item = self.item()
        def change():
            self.assertFalse(self.s.db.connection.in_transaction)
            self.s.forget(item.memory_id)
        self.fake.hook = change
        self.assertEqual(self.s.process_embedding_jobs()[0]['status'], 'obsolete')
        self.assertEqual(self.s.embedding_status()['indexed'], 0)
        self.assertEqual(self.s.flush_embeddings()['eligible'], 0)
    def test_generation_rollback_and_failure(self):
        item = self.item()
        before = self.s.db.one('SELECT generation FROM lamemory_retrieval_state')['generation']
        with self.assertRaises(RuntimeError):
            with self.s.db.transaction():
                self.s.db.execute("UPDATE lamemory_items SET status='deleted'")
                raise RuntimeError()
        self.assertEqual(before, self.s.db.one('SELECT generation FROM lamemory_retrieval_state')['generation'])
        self.fake.fail = True
        self.assertEqual(self.s.flush_embeddings()['status'], 'failed')
        job = self.s.embedding_index.jobs()[0]
        self.assertNotIn('private', job['last_error'])
        self.fake.fail = False
        self.assertTrue(self.s.retry_embedding_job(job['job_id']))
        self.assertEqual(self.s.flush_embeddings()['indexed'], 1)
        self.s.forget(item.memory_id)
        self.assertEqual(self.s.embedding_status()['indexed'], 0)
    def test_scope(self):
        self.item()
        with LaMemoryService(self.cfg, user_id='other', session_id='s', embedding_provider=self.fake) as other:
            other.store('private')
        self.assertEqual(self.s.flush_embeddings()['eligible'], 1)
        self.assertEqual(len(self.s.embedding_index.jobs()), 1)

    def test_actual_input_prefix_truncation_and_hash(self):
        self.s.close()
        from dataclasses import replace
        from core.lamemory.embedding.space import digest
        self.fake.descriptor = replace(self.fake.descriptor, max_embedding_chars=5, document_prefix='D:')
        self.s = LaMemoryService(replace(self.cfg, max_embedding_chars=5), user_id='u', session_id='s', embedding_provider=self.fake)
        self.item('abcdef')
        doc = next(iter(self.s.embedding_index.snapshot().values()))
        self.assertEqual(doc.text, 'D:abc')
        self.assertEqual(doc.input_hash, digest('D:abc'))
        self.assertTrue(doc.truncated)
        self.assertEqual(self.s.embedding_status()['truncated_documents'], 1)
