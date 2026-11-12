"""来源编号歧义及失败响应留档的离线回归；不读取真实会话或调用模型。"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest

from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.backends import build_seed, seed_input, SEED_INPUT_POLICY
from benchmarks.memory_benchmark.settings import Settings, SUITE, load_json

PRICES = Path(__file__).resolve().parents[2] / 'benchmarks/memory_benchmark/prices.deepseek-flash.json'


class SeedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(groups=('lamemory',), retrieval_mode='keyword', hierarchy=False,
                                 job_attempts=2, memory_timeout=5)
        sessions = load_json(SUITE / 'seed/sessions.json')['sessions']
        session = next(s for s in sessions if any(m['message_id'] == 'H034' for m in s['messages']))
        self.message = next(m for m in session['messages'] if m['message_id'] == 'H034')
        self.session = {**session, 'messages': [self.message]}
        self.ledger = Ledger(self.root / 'usage.jsonl', Prices(PRICES))
        self.ledger.tags = dict(stage='build', group='lamemory')

    def model(self, output):
        def call(messages, **kwargs):
            return dict(role='assistant', content=output(messages), reasoning_content='不应存入证据',
                        usage=dict(prompt_tokens=100, completion_tokens=20, total_tokens=120))
        call.config = SimpleNamespace(model='fake', provider='fake', max_tokens=8192, api_key='test-memory-secret')
        return self.ledger.wrap(call, 'memory')

    def seed(self, model):
        build_seed('lamemory', self.root / 'seed', [self.session], self.settings, model)

    def items(self):
        path = (self.root / 'seed/lamemory.sqlite3').resolve()
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            return db.execute('SELECT content FROM lamemory_items').fetchall()

    def test_h034_replay_has_one_source_id_namespace_and_keeps_original_evidence(self):
        def output(messages):
            self.assertIn('最外层', messages[0]['content'])
            data = json.loads(messages[-1]['content'])
            record = json.loads(data[0]['content'].split('\n', 1)[1])
            self.assertNotIn('message_id', record)
            self.assertNotIn('session_id', record)
            self.assertEqual(record['content'], self.message['content'])
            self.assertEqual(record['date'], self.session['date'])
            source = data[0]['message_id']
            self.assertTrue(source.startswith('msg_'))
            return json.dumps([dict(content='回答先给结论再给理由', type='preference', source_message_ids=[source])])

        self.seed(self.model(output))
        self.assertEqual(len(self.ledger.events), 1)
        mapping = load_json(self.root / 'seed/seed_evidence.json')['H034']
        self.assertEqual(mapping['seed_input_policy'], SEED_INPUT_POLICY)
        self.assertEqual(mapping['content_sha256'], hashlib.sha256(self.message['content'].encode()).hexdigest())
        self.assertEqual(len(self.items()), 1)
        self.assertEqual(load_json(self.root / 'seed/seed_jobs.json')[-1]['status'], 'complete')

    def test_wrong_historical_id_still_rejects_entire_batch_and_audits_each_attempt(self):
        def output(messages):
            source = json.loads(messages[-1]['content'])[0]['message_id']
            return json.dumps([dict(content='合法但也不应部分提交', source_message_ids=[source]),
                               dict(content='非法历史编号', source_message_ids=['H034'])])

        with self.assertRaisesRegex(RuntimeError, 'H034'):
            self.seed(self.model(output))
        self.assertEqual(self.items(), [])
        self.assertFalse((self.root / 'seed/snapshot.json').exists())
        job = load_json(self.root / 'seed/seed_jobs.json')[-1]['jobs'][-1]
        self.assertEqual(job['attempts'], 2)
        self.assertIn('来源必须属于本轮', job['last_error'])
        self.assertEqual(len(self.ledger.events), 2)
        for event in self.ledger.events:
            evidence = load_json(self.root / event['memory_response'])
            response = json.loads(evidence['response']['content'])
            self.assertEqual(response[-1]['source_message_ids'], ['H034'])
            self.assertIn('msg_', evidence['request']['messages'][-1]['content'])
            self.assertEqual(event['usage_mode'], 'provider_usage')
            self.assertGreater(event['cost'], 0)

    def test_json_parse_failure_retains_response_and_redacts_credential(self):
        raw = 'bad JSON test-memory-secret'
        self.session = {**self.session, 'messages': [{**self.message, 'content': '用户文本 test-memory-secret'}]}
        with self.assertRaisesRegex(RuntimeError, 'H034'):
            self.seed(self.model(lambda messages: raw))
        self.assertEqual(len(self.ledger.events), 2)
        for event in self.ledger.events:
            text = (self.root / event['memory_response']).read_text()
            self.assertNotIn('test-memory-secret', text)
            self.assertNotIn('reasoning_content', text)
            evidence = json.loads(text)
            self.assertEqual(evidence['response']['content'], 'bad JSON [REDACTED]')
            self.assertEqual(event['tokens']['output'], 20)
        self.assertIn('JSONDecodeError', load_json(self.root / 'seed/seed_jobs.json')[-1]['jobs'][-1]['last_error'])

    def test_invalid_then_valid_retry_keeps_both_responses_and_only_one_item(self):
        responses = []
        def output(messages):
            source = json.loads(messages[-1]['content'])[0]['message_id']
            responses.append('H034' if not responses else source)
            return json.dumps([dict(content='先给结论', source_message_ids=[responses[-1]])])
        self.seed(self.model(output))
        self.assertEqual(len(self.ledger.events), 2)
        self.assertEqual(len(self.items()), 1)
        mapping = load_json(self.root / 'seed/seed_evidence.json')['H034']
        self.assertEqual(responses, ['H034', mapping['runtime_message_id']])
        self.assertNotEqual(self.ledger.events[0]['memory_response'], self.ledger.events[1]['memory_response'])

    def test_actual_user_text_containing_ids_is_not_sanitized_or_rewritten(self):
        message = {**self.message, 'content': '请检查 message_id=H034 的记录，业务端口仍是5678。'}
        replay = seed_input(self.session, message)
        record = json.loads(replay.split('\n', 1)[1])
        self.assertEqual(record['content'], message['content'])
        self.assertNotIn('message_id', record)


if __name__ == '__main__':
    unittest.main()
