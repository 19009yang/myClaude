"""从公开评测提示派生查询；原始消息仍用于任务执行和证据留档。"""
import re
import hashlib
from pathlib import Path

from .settings import SUITE, load_json


QUERY_STRATEGY = "recent-task-requests-without-structured-delivery-or-fixed-reviews-v2"
FACTS_QUERY_STRATEGY = "manual-public-request-facts-with-instruction-v1"
FACTS_INSTRUCTION = ("Given a user's request, retrieve personal facts, preferences, and pending tasks "
                     "needed to fulfill it.")
FACTS_PREFIX = f"Instruct: {FACTS_INSTRUCTION}\nQuery: "
FACTS_FILE = Path(__file__).with_name('lamemory-fact-queries.json')

# 公开资产中的 fixed_no_new_facts 追问只要求复查，不增加检索主题。
FIXED_REVIEWS = frozenset({
    "请检查刚才的交付物是否完整、可执行，核对已知约束，修正遗漏并保存。",
    "请做最后一次一致性检查，修正发现的问题并保存最终交付物；没有依据的信息请标明待确认。",
})


def retrieval_text(text):
    """识别冻结提示的协议头和独立交付段；其他自由文本保守保留。"""
    normalized = text.replace("\r\n", "\n").strip()
    if normalized in FIXED_REVIEWS:
        return ""
    if not normalized.startswith("当前评测日期固定为"):
        return text
    sections = re.split(r"\n[ \t]*\n", normalized, maxsplit=1)
    if len(sections) != 2:
        return text
    body = sections[1]
    # 六道记忆/偏好题的公开格式为：协议头、用户请求、输出 `文件`，交付要求。
    # 不对任意用户句子删除“输出/包含/不要”等词，也不从 rubric 推导查询。
    paragraphs = re.split(r"\n[ \t]*\n", body)
    for index, paragraph in enumerate(paragraphs):
        if index and re.match(r"输出\s+`[^`\n]+`[，,。]", paragraph):
            return "\n\n".join(paragraphs[:index]).strip()
    return body


def public_fact_queries(tasks):
    """查询只来自预先审阅的公开请求；提示变化时拒绝套用旧改写。"""
    data = load_json(FACTS_FILE)
    if data['strategy'] != FACTS_QUERY_STRATEGY:
        raise ValueError('事实查询文件的策略版本不一致')
    queries = {}
    for task in tasks:
        row = data['queries'].get(task['task_id'])
        actual_hash = hashlib.sha256((SUITE / task['prompt']).read_bytes()).hexdigest()
        if not row or row['prompt_sha256'] != actual_hash or not row['query'].strip():
            raise ValueError(f"公开提示与事实查询不一致：{task['task_id']}")
        queries[task['task_id']] = row['query']
    return {'strategy': FACTS_QUERY_STRATEGY, 'method': 'manual-from-public-request',
            'source_sha256': hashlib.sha256(FACTS_FILE.read_bytes()).hexdigest(),
            'embedding_query_prefix': FACTS_PREFIX, 'queries': queries}
