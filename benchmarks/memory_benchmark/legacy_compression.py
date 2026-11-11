"""旧摘要算法的预算触发适配；冻结源码不变，原始会话独立留档。"""
import copy
import hashlib
import json
import math
from pathlib import Path

from core.lamemory.integration.context import estimate_tokens, valid_chain
from core.lamemory.models import BudgetError
from .settings import save_json


POLICY = 'legacy-input-budget-preflight-v1'


class LegacyCompression:
    def __init__(self, legacy, module, directory, session, config, settings):
        self.legacy, self.module = legacy, module
        self.config, self.settings = config, settings
        self.audit = Path(directory) / 'compression' / session
        self.counter = 0

    def tokens(self, system_prompt, tools):
        return estimate_tokens(self.legacy.build_context(system_prompt)) + (estimate_tokens(tools) if tools else 0)

    def candidate(self, messages):
        """优先保留最近四条；边界只能落在完整工具组之间，当前请求原文始终保留。"""
        users = [n for n, m in enumerate(messages) if m['role'] == 'user']
        if not users:
            return None
        current = users[-1]
        desired = max(0, len(messages) - self.module.KEEP_MESSAGES_ON_COMPRESS)
        # 先尝试保留 >=4 条的最大前缀；不够时才压缩当前轮已完成的工具组。
        cuts = sorted(range(1, len(messages) + 1),
                      key=lambda n: (0, -n) if n <= desired else (1, n))
        memory = self.module.LONG_TERM_MEMORY_FILEPATH.read_text(encoding='utf-8').strip()
        if memory == self.module.LONG_TERM_MEMORY_HEADER.strip():
            memory = ''
        instruction = self.module.COMPRESS_PROMPT.format(
            long_term_part=f'已有长期记忆：\n{memory}\n\n' if memory else '')
        for cut in cuts:
            prefix = messages[:cut]
            removable = [m for n, m in enumerate(prefix) if n != current]
            if (not removable or not any(m['role'] != 'system' for m in removable) or
                    estimate_tokens(removable) < 64 or not valid_chain(prefix)):
                continue
            recent = messages[cut:]
            if current < cut:
                recent = [messages[current], *recent]
            elif (recent and recent[0]['role'] == 'assistant' and recent[0].get('tool_calls') and
                  prefix[-1]['role'] == 'user'):
                # 与冻结 compress() 的 user/tool 边界一致，避免它再次移动已审计的切点。
                continue
            view = [{'role': 'system', 'content': '摘要占位'}, *recent]
            if not valid_chain(view):
                continue
            request = [*prefix, {'role': 'user', 'content': instruction}]
            if estimate_tokens(request) > self.config.input_budget:
                continue
            return cut, [*prefix, *recent], len(recent)
        return None

    def compress(self, system_prompt, tools, before):
        source = copy.deepcopy(self.legacy.messages)
        choice = self.candidate(source)
        if choice is None:
            return None
        cut, working, keep = choice
        self.audit.mkdir(parents=True, exist_ok=True)
        while (self.audit / f'{self.counter + 1:04d}.json').exists():
            self.counter += 1
        self.counter += 1
        name = f'{self.counter:04d}'
        session_file = self.module.MEMORY_FILEPATH
        memory_file = self.module.LONG_TERM_MEMORY_FILEPATH
        session_before = session_file.read_bytes()
        memory_before = memory_file.read_bytes()
        save_json(self.audit / f'{name}-before.json', source)
        (self.audit / f'{name}-memory-before.md').write_bytes(memory_before)
        event = {'policy': POLICY, 'prefix_messages': cut, 'retained_messages': keep,
                 'estimated_tokens_before': before, 'status': 'running',
                 'session_sha256_before': hashlib.sha256(session_before).hexdigest()}
        old_keep = self.module.KEEP_MESSAGES_ON_COMPRESS

        def rollback():
            self.legacy.messages = source
            session_file.write_bytes(session_before)
            memory_file.write_bytes(memory_before)

        try:
            # 只替换本实例模块的边界/触发值；摘要提示词、输出处理与长期记忆追加仍用原函数。
            self.module.KEEP_MESSAGES_ON_COMPRESS = keep
            self.legacy.messages = working
            force = math.ceil(self.module.MAX_CONTEXT_LENGTH * self.module.COMPRESS_THRESHOLD)
            self.legacy.compress(force)
            users_before = [m for m in source if m['role'] == 'user']
            users_after = [m for m in self.legacy.messages if m['role'] == 'user']
            if not valid_chain(self.legacy.messages) or not users_after or users_after[-1] != users_before[-1]:
                raise ValueError('旧摘要破坏了工具链或当前用户请求')
            after = self.tokens(system_prompt, tools)
            event.update(estimated_tokens_after=after,
                         summary=self.legacy.messages[0].get('content', ''))
            save_json(self.audit / f'{name}-after.json', self.legacy.messages)
            (self.audit / f'{name}-memory-after.md').write_bytes(memory_file.read_bytes())
            if after >= before:
                rollback()
                event['status'] = 'no_reduction_rolled_back'
            else:
                event['status'] = 'committed'
        except BaseException as exc:
            rollback()
            event.update(status='failed_rolled_back', error_type=type(exc).__name__)
            raise
        finally:
            self.module.KEEP_MESSAGES_ON_COMPRESS = old_keep
            save_json(self.audit / f'{name}.json', event)
        return event

    def prepare(self, system_prompt, tools, limit):
        if not valid_chain(self.legacy.messages):
            raise ValueError('工具调用结果尚未完整保存，不能压缩未完成工具链')
        before = tokens = self.tokens(system_prompt, tools)
        events = []
        trigger = math.floor(limit * self.settings.legacy_compress_trigger_ratio)
        while tokens > trigger and len(events) < self.settings.job_attempts:
            event = self.compress(system_prompt, tools, tokens)
            if event is None:
                break
            events.append(event)
            if event['status'] != 'committed':
                break
            tokens = event['estimated_tokens_after']
        if tokens > limit:
            raise BudgetError(f'legacy 压缩后仍超出输入预算：{tokens} > {limit}；当前请求和长期记忆未截断')
        return {'policy': POLICY, 'trigger_tokens': trigger, 'estimated_tokens_before': before,
                'estimated_tokens_after': tokens, 'events': events}
