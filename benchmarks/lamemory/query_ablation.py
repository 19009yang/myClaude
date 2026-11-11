"""固定已完成评测的 LaMemory 快照，对照查询指令与事实需求改写。"""
import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import time

from core.lamemory.config import LaMemoryConfig
from core.lamemory.embedding.index import unpack
from core.lamemory.embedding.provider import EmbeddingConfig, OpenAIEmbeddingProvider
from core.lamemory.embedding.space import EmbeddingDescriptor, digest
from core.lamemory.models import MemoryItem
from core.lamemory.retrieval.fusion import cosine, rrf, unit_vector
from core.lamemory.retrieval.keyword import rank
from core.lamemory.integration.runtime import boolean
from benchmarks.memory_benchmark.accounting import Ledger, Prices
from benchmarks.memory_benchmark.queries import retrieval_text
from benchmarks.memory_benchmark.settings import load_json, save_json


INSTRUCTION = ("Given a user's request, retrieve personal facts, preferences, and pending tasks "
               "needed to fulfill it.")
VARIANTS = ("raw", "instruct", "facts")
PROTOCOL = "lamemory-frozen-query-ablation-v1"


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require_checkpointed(path):
    # immutable=1 不读取 WAL；只接受已关闭/完成 checkpoint 的离线快照。
    wal = Path(str(path) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("快照存在非空 WAL；请先停止写入并完成 SQLite checkpoint，再创建实验")


def freeze(source, destination):
    require_checkpointed(source)
    before = file_hash(source)
    shutil.copyfile(source, destination)
    require_checkpointed(source)
    if before != file_hash(source) or before != file_hash(destination):
        raise ValueError("复制期间源快照发生变化；不能进行固定快照对照")
    return before


def load_items(path, descriptor, identity):
    """不实例化服务、不迁移数据库；只读取同一空间的当前可见 active 原子记忆。"""
    require_checkpointed(path)
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.row_factory = sqlite3.Row
        space = db.execute("SELECT descriptor_json FROM lamemory_embedding_spaces WHERE space_id=?",
                           (descriptor.space_id,)).fetchone()
        if not space or json.loads(space[0]) != asdict(descriptor):
            raise ValueError("快照中的向量空间与源报告不一致")
        rows = db.execute("SELECT * FROM lamemory_items WHERE user_id=? AND status='active' AND "
                          "((scope_kind='user' AND scope_key=?) OR (scope_kind='session' AND scope_key=?) "
                          "OR (scope_kind='project' AND scope_key=?))",
                          (identity['user'], identity['user'], identity['session'], identity['project'])).fetchall()
        items, vectors = [], {}
        for row in rows:
            item = MemoryItem(**dict(row))
            state = db.execute("SELECT * FROM lamemory_embedding_entities WHERE user_id=? "
                               "AND entity_kind='item' AND entity_id=?", (identity['user'], item.memory_id)).fetchone()
            stored = db.execute("SELECT * FROM lamemory_embeddings WHERE user_id=? "
                                "AND entity_kind='item' AND entity_id=? AND space_id=?",
                                (identity['user'], item.memory_id, descriptor.space_id)).fetchone()
            if (not state or state['is_deleted'] or not stored or
                    stored['entity_generation'] != state['generation'] or
                    stored['dimensions'] != descriptor.dimensions or
                    stored['input_hash'] != digest(descriptor.document_input(item.content))):
                raise ValueError(f"快照缺少有效投影，不能公平比较：{item.memory_id}")
            vectors[item.memory_id] = unpack(stored['vector_blob'], descriptor.dimensions)
            items.append(item)
    if not items:
        raise ValueError("指定身份在快照中没有可检索的 active 记忆")
    return items, vectors


def variants(request, facts_query, instruction):
    prefix = f"Instruct: {instruction}\nQuery: "
    return {
        'raw': {'keyword_query': request, 'embedding_input': request},
        'instruct': {'keyword_query': request, 'embedding_input': prefix + request},
        'facts': {'keyword_query': facts_query, 'embedding_input': prefix + facts_query},
    }


def prepare(run, task, output, facts_query, *, instruction=INSTRUCTION, target_slots=(),
            user='memory-benchmark-user', session=None, repeats=1):
    """事实需求改写由调用方先固定，仅来自公开请求，不让标注/记忆反向影响查询。"""
    run, output = Path(run).resolve(), Path(output).resolve()
    if not facts_query.strip() or not instruction.strip() or not 1 <= repeats <= 30:
        raise ValueError("facts-query/instruction 不能为空；repeats 需要为 1–30")
    report = load_json(run / 'report.json')
    if report['status'] != 'complete' or report['retrieval']['mode'] != 'hybrid':
        raise ValueError("必须使用已完成的 hybrid 评测")
    task_root = run / 'runs' / task
    if task_root.parent != run / 'runs' or not task_root.is_dir():
        raise ValueError("task 必须是源运行中的任务 ID")
    prompt_path = task_root / 'repeat-1/lamemory/round-1/prompt.md'
    prompt = prompt_path.read_text(encoding='utf-8')
    request = retrieval_text(prompt).strip()
    if not request:
        raise ValueError("公开任务请求为空")
    descriptor = EmbeddingDescriptor(**report['retrieval']['embedding'])
    cfg = LaMemoryConfig(**report['retrieval']['parameters'])
    if cfg.rerank_enabled:
        raise ValueError("本对照固定关闭重排")
    identity = {'user': user, 'session': session or f'test-{task}-1-lamemory', 'project': cfg.project_id}
    output.mkdir(parents=True, exist_ok=False)
    snapshot = output / 'snapshot.sqlite3'
    snapshot_hash = freeze(run / 'seeds/lamemory/lamemory.sqlite3', snapshot)
    items, _ = load_items(snapshot, descriptor, identity)
    targets = []
    for slot in target_slots:
        matches = [item for item in items if item.slot_key == slot]
        if not matches:
            raise ValueError(f"标注 slot 在可见快照中不存在：{slot}")
        targets.extend({'memory_id': i.memory_id, 'slot_key': slot, 'content': i.content} for i in matches)
    plan = {
        'protocol': PROTOCOL, 'status': 'prepared', 'source_run': str(run), 'task_id': task,
        'source_report_sha256': file_hash(run / 'report.json'),
        'source_prompt_sha256': file_hash(prompt_path), 'snapshot_sha256': snapshot_hash,
        'scope': 'active-visible-items-only', 'identity': identity, 'item_count': len(items),
        'embedding': asdict(descriptor), 'parameters': asdict(cfg), 'repeats': repeats,
        'request': request, 'facts_query': facts_query.strip(), 'instruction': instruction.strip(),
        'rewrite_method': 'manual-from-public-request-before-retrieval',
        'variants': variants(request, facts_query.strip(), instruction.strip()),
        'targets': targets, 'pricing': report['pricing'],
        'controls': {'same_document_vectors': True, 'rebuild_index': False, 'query_cache': False,
                     'extraction': False, 'summarization': False, 'rerank': False,
                     'context_injection': False,
                     'targets_used_for_query': False, 'rotating_order_per_repeat': True},
        'implementation_sha256': file_hash(__file__),
    }
    # database_path 只用于配置校验；回放始终使用输出目录中的冻结副本。
    plan['parameters'].pop('database_path')
    (output / 'public_prompt.md').write_text(prompt, encoding='utf-8')
    save_json(output / 'plan.json', plan)
    (output / 'plan.sha256').write_text(file_hash(output / 'plan.json') + '\n')
    return plan


def rankings(items, vectors, query_vector, keyword_query, cfg):
    semantic = sorted(((i, cosine(query_vector, vectors[i.memory_id])) for i in items),
                      key=lambda pair: (-pair[1], -pair[0].revision, pair[0].memory_id))
    keyword = rank(keyword_query, items)
    semantic_pool = [(i, s) for i, s in semantic if s >= cfg.semantic_min_similarity][
        :max(cfg.semantic_candidate_k, cfg.top_k)]
    # 与运行时一致：关键词先保留至多 100 条，再截取通道候选。
    keyword_pool = keyword[:100][:max(cfg.keyword_candidate_k, cfg.top_k)]
    revisions = {('item', i.memory_id): i.revision for i in items}
    fused = rrf({'keyword': [(('item', i.memory_id), s) for i, s, _ in keyword_pool],
                 'semantic': [(('item', i.memory_id), s) for i, s in semantic_pool]},
                k=cfg.rrf_k, weights={'keyword': cfg.keyword_weight, 'semantic': cfg.semantic_weight},
                revisions=revisions)
    kw_ranks = {i.memory_id: (n, score) for n, (i, score, _) in enumerate(keyword, 1)}
    sem_ranks = {i.memory_id: n for n, (i, _) in enumerate(semantic_pool, 1)}
    fused_ranks = {key[1]: (n, meta) for n, (key, meta) in enumerate(fused, 1)}
    rows = []
    for n, (item, score) in enumerate(semantic, 1):
        ident = item.memory_id
        final_rank, meta = fused_ranks.get(ident, (None, {}))
        exclusion = ('below_similarity_threshold' if score < cfg.semantic_min_similarity else
                     'outside_candidate_limit' if ident not in sem_ranks else None)
        rows.append({'memory_id': ident, 'content': item.content, 'slot_key': item.slot_key,
                     'semantic_rank_all': n, 'cosine': score, 'semantic_candidate_rank': sem_ranks.get(ident),
                     'semantic_exclusion': exclusion, 'keyword_rank_all': kw_ranks.get(ident, (None,))[0],
                     'fused_rank': final_rank, 'fusion': meta, 'selected': final_rank is not None and final_rank <= cfg.top_k})
    return {'all_items': rows, 'semantic_candidate_ids': [i.memory_id for i, _ in semantic_pool],
            'keyword_candidate_ids': [i.memory_id for i, _, _ in keyword_pool],
            'fused_ids': [key[1] for key, _ in fused], 'selected_ids': [key[1] for key, _ in fused[:cfg.top_k]]}


def configured_provider(plan, env):
    """只请求查询向量；把模板显式放进 input，不让环境再追加一次 prefix。"""
    desc = EmbeddingDescriptor(**plan['embedding'])
    p = 'LAMEMORY_EMBEDDING_'
    base_url = env.get(p + 'BASE_URL') or ''
    if digest(base_url.rstrip('/')) != desc.endpoint_fingerprint:
        raise ValueError("当前 embedding 端点与冻结空间不一致")
    if env.get(p + 'MODEL', desc.model) != desc.model:
        raise ValueError("当前 embedding 模型与冻结空间不一致")
    if int(env.get(p + 'DIMENSIONS', desc.dimensions)) != desc.dimensions:
        raise ValueError("当前 embedding 维度与冻结空间不一致")
    return OpenAIEmbeddingProvider(EmbeddingConfig(
        model=desc.model, base_url=base_url, dimensions=desc.dimensions, namespace=desc.namespace,
        api_key=env.get(p + 'API_KEY') or None, send_dimensions=boolean(env.get(p + 'SEND_DIMENSIONS', 'false')),
        query_prefix='', document_prefix=desc.document_prefix, max_embedding_chars=desc.max_embedding_chars,
        timeout=float(env.get(p + 'TIMEOUT', '30'))))


def markdown(report):
    lines = ['# LaMemory 固定快照查询对照', '', f"状态：{report['status']}；文档数：{report['item_count']}；模拟：{report['simulated']}。",
             '', 'A→B 只改变查询指令；B→C 只改变事实需求表述。三组共用冻结文档向量。', '',
             '| 重复 | 方案 | 目标事实 | 原始语义排名 | cosine | 语义候选排除原因 | 融合排名 | 进入最终结果 |',
             '|---|---|---|---:|---:|---|---:|---|']
    for result in report['results']:
        for row in result.get('targets', []):
            lines.append(f"| {result['repeat']} | {result['variant']} | {row['slot_key']} | "
                         f"{row['semantic_rank_all']} | {row['cosine']:.6f} | {row['semantic_exclusion'] or '—'} | "
                         f"{row['fused_rank'] or '—'} | {'是' if row['selected'] else '否'} |")
    if not report['targets']:
        lines.extend(['', '未提供目标标注；请查看每组 JSON 中的完整语义排名、通道候选和融合结果。'])
    lines.extend(['', f"费用：{report['usage']['cost']:.8f} {report['usage']['currency']}；查询请求：{report['usage']['calls']}。",
                  '不运行主模型或评分模型；目标覆盖率只针对调用方预先标注的事实，不代表完整相关性评测。'])
    if report.get('error_type'):
        lines.extend(['', '错误类型：' + report['error_type']])
    return '\n'.join(lines) + '\n'


def execute(experiment, *, env=None, provider=None, max_cost=0.1):
    directory = Path(experiment).resolve()
    if not math.isfinite(max_cost) or max_cost <= 0:
        raise ValueError("max-cost 必须为正有限数，单位与源报告 pricing 一致")
    if file_hash(directory / 'plan.json') != (directory / 'plan.sha256').read_text().strip():
        raise ValueError("计划已变化；请重新 prepare 并使用新目录")
    plan = load_json(directory / 'plan.json')
    snapshot = directory / 'snapshot.sqlite3'
    if plan['protocol'] != PROTOCOL or file_hash(snapshot) != plan['snapshot_sha256']:
        raise ValueError("协议或冻结快照校验失败")
    if plan['implementation_sha256'] != file_hash(__file__):
        raise ValueError("实现已变化；请重新 prepare 并使用新目录")
    desc = EmbeddingDescriptor(**plan['embedding'])
    cfg = LaMemoryConfig(**plan['parameters'])
    items, vectors = load_items(snapshot, desc, plan['identity'])
    if provider is None:
        from benchmarks.memory_benchmark.run import environment
        provider = configured_provider(plan, environment() if env is None else env)
        owned = True
    else:
        owned = False
    # 查询 prefix 是有意改变的实验变量，其他文档空间属性必须完全相同。
    actual = asdict(provider.descriptor)
    expected = asdict(desc)
    actual.pop('query_prefix')
    expected.pop('query_prefix')
    if actual != expected or provider.descriptor.query_prefix:
        if owned:
            provider.close()
        raise ValueError("provider 文档空间不一致，或 provider 会隐式追加查询前缀")
    results_dir = directory / 'results'
    try:
        results_dir.mkdir(exist_ok=False)
    except BaseException:
        if owned:
            provider.close()
        raise
    try:
        save_json(results_dir / 'prices.json', plan['pricing'])
        prices = Prices(results_dir / 'prices.json')
        prices.check_embedding(provider.config)
    except BaseException:
        if owned:
            provider.close()
        raise
    ledger = Ledger(results_dir / 'usage.jsonl', prices, max_cost=max_cost)
    counted = ledger.wrap_embedding(provider, simulated=not owned)
    output = {'protocol': PROTOCOL, 'status': 'running', 'snapshot_sha256': plan['snapshot_sha256'],
              'item_count': len(items), 'targets': plan['targets'], 'results': [], 'max_cost': max_cost,
              'variants': plan['variants'], 'scope': plan['scope'], 'controls': plan['controls'],
              'simulated': not owned, 'real_semantic_validation': False}
    try:
        for repeat in range(plan['repeats']):
            order = VARIANTS[repeat % 3:] + VARIANTS[:repeat % 3]
            for name in order:
                spec = plan['variants'][name]
                ledger.tags = {'stage': 'query_ablation', 'group': name, 'repeat': repeat + 1}
                started = time.monotonic()
                vector = unit_vector(counted.embed_query(spec['embedding_input'], timeout=cfg.retrieval_timeout_seconds), desc.dimensions)
                ranked = rankings(items, vectors, vector, spec['keyword_query'], cfg)
                target_ids = {t['memory_id'] for t in plan['targets']}
                targets = [r for r in ranked['all_items'] if r['memory_id'] in target_ids]
                coverage = (sum(r['selected'] for r in targets) / len(targets)) if targets else None
                result_file = f'{name}-repeat-{repeat + 1}.json'
                result = {'variant': name, 'repeat': repeat + 1, **spec,
                          'query_vector': vector, 'query_vector_sha256': digest(json.dumps(vector)),
                          'snapshot_sha256': plan['snapshot_sha256'], 'latency_ms': (time.monotonic() - started) * 1000,
                          'targets': targets, 'target_coverage_at_k': coverage, **ranked}
                save_json(results_dir / result_file, result)
                output['results'].append({k: result[k] for k in ('variant', 'repeat', 'targets', 'target_coverage_at_k', 'latency_ms')})
                output['results'][-1]['result_file'] = result_file
        output['status'] = 'complete'
        output['real_semantic_validation'] = owned
    except Exception as exc:
        output.update(status='failed', error_type=type(exc).__name__, failed_variant=name, failed_repeat=repeat + 1)
    finally:
        if owned:
            provider.close()
        if file_hash(snapshot) != plan['snapshot_sha256']:
            output.update(status='failed', error_type='SnapshotChanged')
        output['usage'] = ledger.summary()
        save_json(results_dir / 'report.json', output)
        (results_dir / 'report.md').write_text(markdown(output), encoding='utf-8')
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    command = commands.add_parser('prepare', help='只读复制快照并固定查询，不调用模型')
    command.add_argument('--run', type=Path, required=True)
    command.add_argument('--task', required=True)
    command.add_argument('--output', type=Path, required=True)
    command.add_argument('--facts-query', required=True)
    command.add_argument('--instruction', default=INSTRUCTION)
    command.add_argument('--target-slot', action='append', default=[])
    command.add_argument('--repeats', type=int, default=1)
    command.add_argument('--user', default='memory-benchmark-user')
    command.add_argument('--session')
    command = commands.add_parser('run', help='使用已固定的计划；每次重复只请求三个查询向量')
    command.add_argument('--experiment', type=Path, required=True)
    command.add_argument('--max-cost', type=float, default=0.1)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(args.run, args.task, args.output, args.facts_query,
                             instruction=args.instruction, target_slots=args.target_slot,
                             user=args.user, session=args.session, repeats=args.repeats)
            print(json.dumps({'status': result['status'], 'plan': str(args.output / 'plan.json'),
                              'item_count': result['item_count'], 'query_requests': 3 * args.repeats}, ensure_ascii=False))
        else:
            result = execute(args.experiment, max_cost=args.max_cost)
            print(json.dumps({'status': result['status'], 'report': str(args.experiment / 'results/report.md'),
                              'usage': result['usage']}, ensure_ascii=False))
        return 0 if result['status'] in ('prepared', 'complete') else 2
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__, 'error': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
