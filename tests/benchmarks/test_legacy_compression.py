"""原摘要算法在预算前触发，保留证据和合法工具链；不读取真实会话。"""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.backends import Legacy, FROZEN
from benchmarks.memory_benchmark.legacy_compression import POLICY
from benchmarks.memory_benchmark.settings import Settings
from benchmarks.memory_benchmark.smoke import FakeModel
from core.lamemory.integration.context import valid_chain, estimate_tokens
from core.lamemory.models import BudgetError

PRICES = Path(__file__).resolve().parents[2] / 'benchmarks/memory_benchmark/prices.deepseek-flash.json'


class LegacyCompressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = Settings(context_window=6000, max_output_tokens=1024)
        self.model = FakeModel('memory', self.settings)
        self.ledger = Ledger(self.root / 'usage.jsonl', Prices(PRICES), input_budget=self.settings.input_budget)

    def backend(self, model=None, settings=None):
        memory = Legacy(self.root, 'trial', settings or self.settings, self.ledger.wrap(model or self.model, 'memory'))
        self.addCleanup(memory.close)
        return memory

    def history(self, memory, count=5):
        for n in range(count):
            memory.append_message({'role': 'user', 'content': f'旧请求{n}，保留原始证据。'})
            memory.append_message({'role': 'assistant', 'content': f'旧交付物{n}: ' + 'x' * 6000})
        memory.append_message({'role': 'user', 'content': '当前请求：核对文件并保留待办未完成状态。'})

    def test_usage_zero_still_compresses_before_send_and_raw_archive_is_unchanged(self):
        memory = self.backend()
        self.history(memory)
        (self.root / 'MEMORY.md').write_text('# 长期记忆\n爸爸生日3月20日。')
        raw = memory.raw_path.read_bytes()
        current = copy.deepcopy(memory.legacy.messages[-1])
        before = estimate_tokens(memory.legacy.build_context('共同规则'))
        self.assertGreater(before, memory.config.input_budget)
        context = memory.build_context('共同规则', tools=[])
        self.assertLessEqual(context.diagnostics['estimated_tokens'], memory.config.input_budget)
        self.assertEqual(memory.raw_path.read_bytes(), raw)
        self.assertEqual(memory.legacy.messages[-1], current)
        self.assertIn('爸爸生日3月20日', context.messages[0]['content'])
        self.assertTrue(valid_chain(context.messages))
        self.assertEqual(context.diagnostics['legacy_compression']['policy'], POLICY)
        self.assertTrue(context.diagnostics['legacy_compression']['events'])
        self.assertTrue(all(e['purpose'] == 'summary' for e in self.ledger.events))
        self.assertGreater(self.ledger.total(), 0)
        self.assertTrue(list((self.root / 'compression/trial').glob('*-before.json')))
        self.assertTrue(list((self.root / 'compression/trial').glob('*-after.json')))

    def test_flag_off_reproduces_original_budget_error(self):
        memory = self.backend(settings=replace(self.settings, legacy_preflight_compression=False))
        self.history(memory)
        with self.assertRaises(BudgetError):
            memory.build_context('共同规则')
        self.assertEqual(self.ledger.events, [])

    def test_current_turn_completed_tool_groups_can_be_compressed_without_orphans(self):
        memory = self.backend()
        memory.append_message({'role': 'user', 'content': '当前任务原文不得丢失'})
        for n in range(3):
            memory.append_message({'role': 'assistant', 'content': '', 'tool_calls': [{
                'id': f'call-{n}', 'type': 'function', 'function': {'name': 'write',
                'arguments': json.dumps({'path': 'output.md', 'content': 'x' * 6000})}}]})
            memory.append_message({'role': 'tool', 'tool_call_id': f'call-{n}', 'content': 'saved output.md'})
        context = memory.build_context('共同规则')
        self.assertLessEqual(context.diagnostics['estimated_tokens'], memory.config.input_budget)
        self.assertTrue(valid_chain(context.messages))
        self.assertTrue(any(m.get('content') == '当前任务原文不得丢失' for m in context.messages))
        memory.append_message({'role': 'assistant', 'content': '已核对'})
        memory.commit_turn()
        self.assertTrue(valid_chain(memory.legacy.get_last_turn_messages(), complete=True))
        self.assertEqual(memory.flush()['status'], 'complete')

    def test_incomplete_tool_group_never_goes_to_summary_model(self):
        memory = self.backend()
        self.history(memory)
        memory.append_message({'role': 'assistant', 'tool_calls': [{'id': 'pending'}]})
        with self.assertRaisesRegex(ValueError, '未完成工具链'):
            memory.build_context('共同规则')
        self.assertEqual(self.ledger.events, [])

    def test_old_user_tool_boundary_matches_the_recorded_prefix(self):
        calls = []

        def recorded(**kwargs):
            calls.append(copy.deepcopy(kwargs['messages']))
            return self.model(**kwargs)

        recorded.config = self.model.config
        memory = self.backend(model=recorded)
        for n in range(3):
            memory.append_message({'role': 'user', 'content': f'旧请求{n}'})
            memory.append_message({'role': 'assistant', 'tool_calls': [{
                'id': f'old-{n}', 'function': {'name': 'write', 'arguments': 'x' * 6000}}]})
            memory.append_message({'role': 'tool', 'tool_call_id': f'old-{n}', 'content': 'saved'})
            memory.append_message({'role': 'assistant', 'content': '已保存'})
        memory.append_message({'role': 'user', 'content': '当前请求'})
        context = memory.build_context('共同规则')
        events = context.diagnostics['legacy_compression']['events']
        self.assertTrue(events)
        self.assertEqual([e['prefix_messages'] for e in events], [len(c) - 1 for c in calls])
        self.assertTrue(valid_chain(context.messages))

    def test_invalid_summary_rolls_back_both_session_view_and_memory(self):
        def invalid(**kwargs):
            return {'content': json.dumps({'summary': '', 'memory_update': ['不得写入']})}
        invalid.config = self.model.config
        memory = self.backend(model=invalid)
        self.history(memory)
        source = copy.deepcopy(memory.legacy.messages)
        session = (self.root / 'trial.jsonl').read_bytes()
        facts = (self.root / 'MEMORY.md').read_bytes()
        raw = memory.raw_path.read_bytes()
        with self.assertRaisesRegex(ValueError, '非空字符串'):
            memory.build_context('共同规则')
        self.assertEqual(memory.legacy.messages, source)
        self.assertEqual((self.root / 'trial.jsonl').read_bytes(), session)
        self.assertEqual((self.root / 'MEMORY.md').read_bytes(), facts)
        self.assertEqual(memory.raw_path.read_bytes(), raw)
        event = json.loads((self.root / 'compression/trial/0001.json').read_text())
        self.assertEqual(event['status'], 'failed_rolled_back')

    def test_nonreducing_summary_rolls_back_memory_updates_and_is_bounded(self):
        def bloated(**kwargs):
            return {'content': json.dumps({'summary': 'x' * 70000, 'memory_update': ['不得写入']})}
        bloated.config = self.model.config
        memory = self.backend(model=bloated)
        self.history(memory)
        source = copy.deepcopy(memory.legacy.messages)
        facts = (self.root / 'MEMORY.md').read_bytes()
        with self.assertRaises(BudgetError):
            memory.build_context('共同规则')
        self.assertEqual(memory.legacy.messages, source)
        self.assertEqual((self.root / 'MEMORY.md').read_bytes(), facts)
        self.assertEqual(len(self.ledger.events), 1)

    def test_uncompressible_current_request_fails_without_erasing_it(self):
        memory = self.backend()
        current = {'role': 'user', 'content': 'x' * 20000}
        memory.append_message(current)
        with self.assertRaises(BudgetError):
            memory.build_context('共同规则')
        self.assertEqual(memory.legacy.messages, [current])
        self.assertEqual(self.ledger.events, [])

    def test_expired_runner_deadline_prevents_summary_call(self):
        memory = self.backend()
        self.history(memory)
        source = copy.deepcopy(memory.legacy.messages)
        memory.set_context_deadline(time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            memory.build_context('共同规则')
        self.assertEqual(memory.legacy.messages, source)
        self.assertEqual(self.ledger.events, [])

    def test_frozen_source_unchanged_and_settings_validation(self):
        self.assertEqual(hashlib.sha256(FROZEN.read_bytes()).hexdigest(),
                         'd6b346be5c8323b0bc73167ff5a93f8322190c5acb1865998dfb7d19773b4a1a')
        for ratio in (0, 1.01, True, float('nan')):
            with self.assertRaises(ValueError):
                replace(self.settings, legacy_compress_trigger_ratio=ratio)

    def test_raw_archive_precedes_original_startup_cleanup(self):
        messages = [{'role': 'user', 'content': '已完成请求'}, {'role': 'assistant', 'content': '已完成'},
                    {'role': 'user', 'content': '未完成请求'},
                    {'role': 'assistant', 'tool_calls': [{'id': 'pending'}]},
                    {'role': 'tool', 'tool_call_id': 'pending', 'content': '工具原始证据'}]
        original = ''.join(json.dumps(m, ensure_ascii=False) + '\n' for m in messages).encode()
        (self.root / 'trial.jsonl').write_bytes(original)
        memory = self.backend()
        self.assertEqual(memory.raw_path.read_bytes(), original)
        self.assertEqual(memory.legacy.messages, messages[:2])

    def test_late_summary_is_not_published(self):
        def late(**kwargs):
            time.sleep(.01)
            return {'content': json.dumps({'summary': '逾期摘要', 'memory_update': ['不得发布']})}
        late.config = self.model.config
        memory = self.backend(model=late, settings=replace(self.settings, memory_timeout=.001))
        self.history(memory)
        source = copy.deepcopy(memory.legacy.messages)
        facts = (self.root / 'MEMORY.md').read_bytes()
        with self.assertRaises(TimeoutError):
            memory.build_context('共同规则')
        self.assertEqual(memory.legacy.messages, source)
        self.assertEqual((self.root / 'MEMORY.md').read_bytes(), facts)


if __name__ == '__main__':
    unittest.main()
