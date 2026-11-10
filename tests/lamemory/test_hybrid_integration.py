"""临时数据库验收：权限、一致性、宿主工厂和无网络路径。"""
import contextlib
from dataclasses import replace
import io
import json
import time
from unittest.mock import patch
from types import SimpleNamespace as NS
import test_embedding_index as index_cases
from core.lamemory import LaMemoryService
from core.lamemory.embedding_space import EmbeddingDescriptor
from core.lamemory.runtime import create_memory
from core.lamemory.tools import get_memory_tools
from core.lamemory.__main__ import main
from Agent.chatBot_with_memory.runner import ChatRunner
from Agent.chatBot_with_memory.web import WebApp


class IntegrationTests(index_cases.IndexFixture):
    def hybrid(self, **kwargs):
        self.s.config = replace(self.cfg, retrieval_mode='hybrid', **kwargs)
        return self.s

    def test_spaces_partial_and_mixed(self):
        self.hybrid()
        self.item('alpha')
        self.s.flush_embeddings()
        with self.s.db.transaction():
            self.s.db.execute("INSERT INTO lamemory_experiences VALUES ('exp','u','user','u','alpha','condition','[\"step\"]','passed','hash','traj','active',1,'line',NULL,2,0,'now','now')")
        bundle = self.s.retrieve('alpha')
        self.assertEqual(bundle.diagnostics['effective_mode'], 'mixed')
        self.assertEqual(bundle.diagnostics['per_kind']['experience']['indexed'], 0)
        other = index_cases.FakeEmbedding()
        other.descriptor = replace(other.descriptor, namespace='v2')
        with LaMemoryService(self.s.config, user_id='u', session_id='s', embedding_provider=other) as changed:
            self.assertEqual(changed.embedding_status()['indexed'], 0)
            self.assertTrue(changed.retrieve('alpha').diagnostics['degraded'])
            self.assertEqual(other.queries, 0)
        self.item('beta')
        bundle = self.s.retrieve('beta')
        self.assertTrue(bundle.diagnostics['per_kind']['item']['partial_index'])
        self.assertTrue(bundle.items)

    def test_repair_corrupt_atomic_batch_and_metadata_reuse(self):
        self.hybrid()
        item = self.item('alpha')
        self.s.flush_embeddings()
        before = self.fake.documents
        with self.s.db.transaction():
            self.s.db.execute('UPDATE lamemory_items SET revision=revision+1 WHERE memory_id=?', (item.memory_id,))
        self.s.enqueue_embeddings()
        self.assertEqual(self.s.embedding_status()['indexed'], 1)
        self.assertEqual(self.fake.documents, before)
        with self.s.db.transaction():
            self.s.db.execute("UPDATE lamemory_embeddings SET vector_blob=?", (b'\0'*8,))
        self.assertEqual(self.s.retrieve('alpha').diagnostics['invalid_vectors'], 1)
        repair = self.s.enqueue_embeddings()
        self.assertEqual(repair['repaired'] + repair['queued'], 1)
        self.s.flush_embeddings()
        self.assertEqual(self.s.embedding_status()['indexed'], 1)
        self.item('beta')
        self.item('gamma')
        self.fake.embed_documents = lambda texts, timeout: [[1, 0], [float('nan'), 1]]
        result = self.s.process_embedding_jobs()
        self.assertEqual(len(result), 2)
        self.assertEqual(self.s.embedding_status()['indexed'], 1)

    def test_failed_forget_and_external_connection(self):
        self.hybrid()
        item = self.item('alpha')
        self.fake.fail = True
        self.assertEqual(self.s.flush_embeddings()['status'], 'failed')
        self.s.forget(item.memory_id)
        self.assertEqual(self.s.flush_embeddings()['status'], 'complete')
        self.fake.fail = False
        item = self.item('beta')
        self.s.flush_embeddings()
        self.assertTrue(self.s.search('paraphrase'))
        with LaMemoryService(self.cfg, user_id='u', session_id='s') as other:
            other.forget(item.memory_id)
        self.assertEqual(self.s.search('paraphrase'), [])
        self.assertEqual(self.s.db.rows('PRAGMA foreign_key_check'), [])

    def test_history_events_profile_and_filter_contract(self):
        self.hybrid()
        for _ in range(2):
            self.item('travel', type='event')
        self.assertEqual(len(self.s.search('travel')), 2)
        self.s.hierarchy.define('profile', 'profile')
        self.s.process_pending(10)
        before = self.fake.queries
        only_profiles = self.s.retrieve('x', kinds=('summary',))
        self.assertEqual(only_profiles.diagnostics['effective_mode'], 'empty')
        self.assertEqual(self.fake.queries, before)
        first = self.s.search('travel')[0].item
        self.s.forget(first.memory_id)
        old = self.s.retrieve('travel', kinds=('item',), filters={'status': 'deleted'})
        self.assertEqual(old.items[0].mode, 'keyword')
        self.assertEqual(old.diagnostics['per_kind']['item']['semantic_skipped_reason'], 'historical_status')
        with self.assertRaises(ValueError):
            self.s.retrieve('x', kinds=('summary',), filters={'type': 'fact'})
        with self.assertRaises(ValueError):
            self.s.search('x', filters={'unknown': 1})

    def test_tool_context_runner_and_index_failure(self):
        self.hybrid()
        self.s.append_message({'role': 'user', 'content': 'remember alpha'})
        tools = {t.name: t for t in get_memory_tools(self.s)}
        saved = tools['lamemory_store'].fn('alpha')
        self.assertEqual(saved['semantic_index_status'], 'pending')
        self.s.flush_embeddings()
        result = tools['lamemory_search'].fn('paraphrase')
        self.assertEqual(result['mode'], 'hybrid')
        self.assertTrue(result['results'][0]['item']['source_message_ids'])
        before = self.fake.documents
        runner = ChatRunner(self.s, chat=lambda **kw: {'role': 'assistant', 'content': 'reply'})
        self.assertEqual(runner.send('paraphrase')['message']['content'], 'reply')
        self.assertEqual(self.fake.documents, before)
        self.assertEqual(self.s.last_context.diagnostics['retrieval_mode'], 'hybrid')

    def test_web_dependencies_and_demo(self):
        app = WebApp(config=replace(self.cfg, retrieval_mode='hybrid'), user_id='u', env={}, embedding_provider=self.fake)
        with app.memory('web') as memory:
            memory.store('alpha')
            memory.flush_embeddings()
        result = app.search_details('web', 'paraphrase')
        self.assertEqual(result['mode'], 'hybrid')
        self.assertEqual(result['index']['indexed'], 1)
        self.assertNotIn('api_key', json.dumps(result))
        app.model = NS(api_key='chat-key')
        self.assertIs(app.embedding_provider, self.fake)
        demo = WebApp(config=replace(self.cfg, retrieval_mode='hybrid'), user_id='u', env={}, demo=True, embedding_provider=self.fake)
        calls = self.fake.queries
        self.assertEqual(demo.search_details('web', 'alpha')['mode'], 'keyword')
        self.assertEqual(self.fake.queries, calls)

    def test_cli_index_and_offline(self):
        self.item('alpha')
        def factory(config, **kwargs):
            kwargs.pop('select_space', None)
            return create_memory(config, env={}, embedding_provider=self.fake, **kwargs)
        def command(*args):
            output = io.StringIO()
            with patch('sys.argv', ['lamemory', '--database', str(self.cfg.database_path), '--user', 'u', '--session', 's', *args]), patch('core.lamemory.__main__.create_memory', side_effect=factory), contextlib.redirect_stdout(output):
                code = main()
            return code, json.loads(output.getvalue())
        self.assertEqual(command('--retrieval-mode', 'hybrid', 'index-enqueue')[1]['queued'], 1)
        self.assertEqual(command('--retrieval-mode', 'hybrid', 'index-flush')[0], 0)
        self.assertEqual(command('--retrieval-mode', 'hybrid', 'search', 'paraphrase')[1]['mode'], 'hybrid')
        before = self.fake.queries
        self.assertEqual(command('--retrieval-mode', 'hybrid', '--offline', 'search', 'alpha')[1]['mode'], 'keyword')
        self.assertEqual(self.fake.queries, before)

    def test_rerank_mutation_and_user_scope(self):
        self.hybrid(rerank_enabled=False)
        item = self.item('alpha')
        self.s.flush_embeddings()
        with LaMemoryService(self.cfg, user_id='hidden', session_id='s', embedding_provider=self.fake) as other:
            other.store('secret')
            other.flush_embeddings()
        class Rerank:
            fingerprint = 'test'
            def rerank(inner, query, documents, *, timeout):
                self.assertEqual(len(documents), 1)
                self.assertNotIn('secret', str(documents))
                self.s.forget(item.memory_id)
                return [{'id': d['id'], 'score': .9} for d in documents]
        self.s.reranker = Rerank()
        self.s.config = replace(self.s.config, rerank_enabled=True)
        self.assertEqual(self.s.search('paraphrase'), [])
        self.assertEqual(self.s.embedding_status()['eligible'], 0)
        self.assertEqual(len(self.s.embedding_index.jobs()), 1)

    def test_sdk_http_transport(self):
        import httpx
        from openai import OpenAI
        from core.lamemory.embeddings import EmbeddingConfig, OpenAIEmbeddingProvider
        requests = []
        def transport(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1, 0]}],
                'model': 'fixture', 'object': 'list', 'usage': {'prompt_tokens': 1, 'total_tokens': 1}})
        with httpx.Client(transport=httpx.MockTransport(transport)) as http:
            with OpenAI(api_key='synthetic', base_url='http://fixture.invalid/v1', http_client=http) as client:
                provider = OpenAIEmbeddingProvider(EmbeddingConfig('fixture', 'http://fixture.invalid/v1', 2, 'v1', send_dimensions=True), client=client)
                self.assertEqual(provider.embed_query('q', timeout=1), [1, 0])
                self.assertEqual(requests[0]['dimensions'], 2)
                self.assertEqual(provider.usage_events[0]['usage']['total_tokens'], 1)

    def test_v2_v3_upgrade_and_ledger(self):
        import sqlite3
        from pathlib import Path
        from core.lamemory.store import SQLiteStore
        directory = Path(__file__).parents[2]/'core/lamemory/migrations'
        for version in (2, 3):
            path = self.cfg.database_path.parent/('old-v'+str(version)+'.db')
            with sqlite3.connect(path) as db:
                for migration in sorted(directory.glob('[0-9]*.sql')):
                    number = int(migration.name.split('_')[0])
                    if number > version:
                        break
                    db.executescript(migration.read_text())
                    db.execute('INSERT INTO lamemory_migrations VALUES (?,?)', (number, 'old'))
                    db.commit()
                db.execute("INSERT INTO lamemory_items VALUES ('old','u','user','u','fact','old text','hash',NULL,NULL,'active',1,NULL,'explicit','old','old')")
            db.close()
            store = SQLiteStore(path)
            try:
                self.assertEqual(store.one('SELECT generation FROM lamemory_embedding_entities')['generation'], 1)
                self.assertEqual(store.one('SELECT content FROM lamemory_items')['content'], 'old text')
                self.assertEqual(store.rows('PRAGMA foreign_key_check'), [])
            finally:
                store.close()

    def test_generation_after_publish_relations_and_deadline(self):
        self.hybrid()
        item = self.item('alpha')
        old = self.s.retrieve('alpha').diagnostics
        self.s.flush_embeddings()
        new = self.s.retrieve('alpha').diagnostics
        self.assertGreater(new['retrieval_generation'], old['retrieval_generation'])
        self.assertEqual(old['data_version'], new['data_version'])  # same-connection commits
        source = self.s.append_message({'role': 'user', 'content': 'new evidence'})
        with self.s.db.transaction():
            self.s.db.execute('INSERT INTO lamemory_sources VALUES (?,?,?)', ('u', item.memory_id, source))
        hit = self.s.search('alpha')[0]
        self.assertEqual(hit.item.source_message_ids, [source])
        self.assertGreater(self.s.retrieve('alpha').diagnostics['retrieval_generation'], new['retrieval_generation'])
        before = self.fake.documents
        self.item('beta')
        self.assertEqual(self.s.process_embedding_jobs(deadline=0), [])
        self.assertEqual(self.s.process_embedding_jobs(deadline=time.monotonic()-1), [])
        self.assertEqual(self.fake.documents, before)

    def test_pending_experience_and_summary_cas(self):
        self.hybrid()
        item = self.item('alpha')
        topic = self.s.define_topic('topic', [item.memory_id])
        self.s.process_pending(10)
        with self.s.db.transaction():
            self.s.db.execute("INSERT INTO lamemory_experiences VALUES ('candidate','u','user','u','alpha','cond','[\"step\"]','basis','hash','traj','candidate',1,'line',NULL,0,0,'now','now')")
        self.assertEqual(self.s.embedding_status()['eligible'], 2)
        def change():
            self.assertFalse(self.s.db.connection.in_transaction)
            self.s.update(item.memory_id, 'new beta')
        self.fake.hook = change
        self.assertEqual({r['status'] for r in self.s.process_embedding_jobs()}, {'obsolete'})
        self.assertEqual(self.s.embedding_status()['indexed'], 0)
        self.assertEqual(self.s.search_summaries('alpha'), [])
        self.assertEqual(self.s.search_experiences('alpha'), [])

    def test_status_without_configuration_and_offline_descriptor(self):
        with create_memory(self.cfg, env={}, retrieval_mode='hybrid', select_space=True, user_id='u', session_id='s') as memory:
            self.assertEqual(memory.embedding_status()['status'], 'unselected')
        env = {'LAMEMORY_RETRIEVAL_MODE': 'hybrid', 'LAMEMORY_EMBEDDING_MODEL': 'test',
               'LAMEMORY_EMBEDDING_BASE_URL': 'http://fixture.invalid/v1', 'LAMEMORY_EMBEDDING_DIMENSIONS': '2',
               'LAMEMORY_EMBEDDING_NAMESPACE': 'v1'}
        with patch('core.lamemory.runtime.OpenAIEmbeddingProvider', side_effect=AssertionError('no SDK')):
            with create_memory(self.cfg, env=env, offline=True, select_space=True, user_id='u', session_id='s') as memory:
                self.assertIsNotNone(memory.embedding_status()['space_id'])
            with create_memory(self.cfg, env=env, offline=True, user_id='u', session_id='s') as memory:
                self.assertIsNone(memory.embedding_provider)

    def test_shared_clients_not_closed_and_disabled_provider(self):
        from core.memory import Memory
        from unittest.mock import Mock
        fake = index_cases.FakeEmbedding()
        fake.close = Mock()
        with create_memory(self.cfg, env={}, retrieval_mode='hybrid', embedding_provider=fake, user_id='u', session_id='s'):
            pass
        fake.close.assert_not_called()
        with patch('core.lamemory.runtime.OpenAIEmbeddingProvider', side_effect=AssertionError('keyword must not construct SDK')):
            facade = Memory(config=self.cfg, user_id='u', session_id='s', env={})
            facade.close()

    def test_later_kind_rerank_cannot_return_stale_earlier_item(self):
        self.hybrid()
        item = self.item('alpha')
        self.s.define_topic('topic', [item.memory_id])
        self.s.process_pending(10)
        self.s.flush_embeddings()
        class Rerank:
            fingerprint = 'cross-kind'
            def rerank(inner, query, documents, *, timeout):
                if documents[0]['id'].startswith('summary:'):
                    self.s.forget(item.memory_id)
                return [{'id': d['id'], 'score': .5} for d in documents]
        self.s.reranker = Rerank()
        self.s.config = replace(self.s.config, rerank_enabled=True)
        result = self.s.retrieve('paraphrase')
        self.assertEqual(result.items, [])
        self.assertEqual(result.summaries, [])
        self.assertGreater(result.diagnostics['sources_changed'], 0)

    def test_earlier_rerank_cannot_send_newly_dirty_topic(self):
        self.hybrid()
        item = self.item('alpha')
        self.s.define_topic('topic', [item.memory_id])
        self.s.process_pending(10)
        self.s.flush_embeddings()
        class Rerank:
            fingerprint = 'cross-kind'
            calls = 0
            def rerank(inner, query, documents, *, timeout):
                inner.calls += 1
                self.assertTrue(all(d['id'].startswith('item:') for d in documents))
                self.s.forget(item.memory_id)
                return [{'id': d['id'], 'score': .5} for d in documents]
        self.s.reranker = Rerank()
        self.s.config = replace(self.s.config, rerank_enabled=True)
        self.s.retrieve('paraphrase')
        self.assertEqual(self.s.reranker.calls, 1)
