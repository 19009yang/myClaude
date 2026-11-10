"""合成快照上的可重复评测；只使用临时库，真实模式需要显式模型配置。"""
import argparse
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import statistics
import tempfile
import time

from core.lamemory import LaMemoryConfig, LaMemoryService
from core.lamemory.runtime import create_memory

FIXTURE = Path(__file__).resolve().parents[2] / 'tests/lamemory/fixtures/hybrid_retrieval.json'
USER, SESSION, PROJECT = 'benchmark-user', 'benchmark-session', 'demo-project'


def fixed_item_id(memory, old_id, key):
    """Only synthetic fixtures: deterministic IDs make tie-breaks reproducible."""
    ident = 'lam_fixture_' + key
    with memory.db.transaction():
        memory.db.execute('PRAGMA defer_foreign_keys=ON')
        memory.db.execute('UPDATE lamemory_items SET memory_id=? WHERE memory_id=? AND user_id=?', (ident, old_id, USER))
        memory.db.execute('UPDATE lamemory_sources SET memory_id=? WHERE memory_id=? AND user_id=?', (ident, old_id, USER))
        memory.db.execute('UPDATE lamemory_items SET superseded_by=? WHERE superseded_by=? AND user_id=?', (ident, old_id, USER))
    return ident


def seed(path, fixture):
    cfg = LaMemoryConfig(database_path=path, project_id=PROJECT)
    identities = {}
    with LaMemoryService(cfg, user_id=USER, session_id=SESSION) as memory:
        for item in fixture['items']:
            source = memory.append_message({'role': 'user', 'content': item['content']})
            memory.append_message({'role': 'assistant', 'content': '合成数据已记录'})
            saved = memory.store(item['content'], [source], type=item['type'], scope_kind=item.get('scope_kind', 'user'), occurred_at=item.get('occurred_at'))
            identities[item['key']] = fixed_item_id(memory, saved.item.memory_id, item['key'])
        forgotten = memory.store('forgotten-never-return').item
        forgotten_id = fixed_item_id(memory, forgotten.memory_id, 'forgotten')
        memory.forget(forgotten_id)
        outdated = memory.store('superseded-old-region').item
        outdated_id = fixed_item_id(memory, outdated.memory_id, 'outdated')
        updated = memory.update(outdated_id, 'current-safe-region').item
        fixed_item_id(memory, updated.memory_id, 'current')
        for topic in fixture['topics']:
            ident = 'sum_fixture_' + topic['key']
            with memory.db.transaction():
                memory.db.execute("INSERT INTO lamemory_summaries(summary_id,user_id,kind,topic_key,updated_at) VALUES (?,?,'topic',?,'synthetic')", (ident, USER, topic['topic_key']))
                for key in topic['members']:
                    memory.db.execute('INSERT INTO lamemory_topic_items VALUES (?,?,?)', (USER, ident, identities[key]))
                memory.hierarchy.enqueue_dirty()
            identities[topic['key']] = ident
        memory.hierarchy.define('profile', 'profile')
        memory.process_pending(100)
        if memory.db.rows('PRAGMA foreign_key_check'):
            raise ValueError('synthetic fixture foreign key violation')
        for exp in fixture['experiences']:
            ident = exp['key']
            with memory.db.transaction():
                memory.db.execute('INSERT INTO lamemory_experiences VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ident, USER, 'user', USER, exp['task_type'], exp['conditions'], json.dumps(exp['steps'], ensure_ascii=False),
                     exp['result_basis'], ident, ident, 'active', 1, ident, None, 2, 0, 'synthetic', 'synthetic'))
            identities[ident] = ident
    for user, project, content in [('other-user', PROJECT, 'private-user-vault'), (USER, 'private-project', 'private-project-vault')]:
        with LaMemoryService(LaMemoryConfig(database_path=path, project_id=project), user_id=user, session_id='private-session') as memory:
            memory.store(content, scope_kind='project' if project != PROJECT else 'user')
    return cfg, identities


def percentile(values, p):
    values = sorted(values)
    return values[min(len(values)-1, max(0, math.ceil(len(values)*p)-1))] if values else 0


def metrics(rows):
    positive = [r for r in rows if r['expected']]
    negative = [r for r in rows if not r['expected']]
    def recall(r):
        return len(set(r['selected']) & set(r['expected'])) / len(r['expected'])
    def mrr(r):
        return next((1/n for n, ident in enumerate(r['selected'], 1) if ident in r['expected']), 0)
    def ndcg(r):
        dcg = sum(1/math.log2(n+1) for n, ident in enumerate(r['selected'], 1) if ident in r['expected'])
        ideal = sum(1/math.log2(n+1) for n in range(1, min(5, len(r['expected']))+1))
        return dcg/ideal
    return {'queries': len(rows), 'answerable': len(positive), 'no_answer': len(negative),
            'recall_at_5': statistics.mean(map(recall, positive)) if positive else None,
            'mrr_at_5': statistics.mean(map(mrr, positive)) if positive else None,
            'ndcg_at_5': statistics.mean(map(ndcg, positive)) if positive else None,
            'no_answer_false_recall_rate': sum(bool(r['selected']) for r in negative)/len(negative) if negative else None,
            'p50_ms': percentile([r['latency_ms'] for r in rows], .5),
            'p95_ms': percentile([r['latency_ms'] for r in rows], .95),
            'degraded_queries': sum(r['diagnostics']['degraded'] for r in rows)}


def run_variant(cfg, identities, fixture, mode, env):
    rerank = mode == 'hybrid-rerank'
    requested = 'keyword' if mode == 'keyword' else 'hybrid'
    try:
        memory = create_memory(cfg, env=env, retrieval_mode=requested, rerank=rerank, user_id=USER, session_id=SESSION)
    except ValueError as exc:
        return {'status': 'blocked', 'reason': str(exc), 'mode': mode, 'real_semantic_validation': False}
    with memory:
        build_usage_start = len(getattr(memory.embedding_provider, 'usage_events', []))
        index = memory.flush_embeddings(timeout=60) if requested == 'hybrid' else memory.embedding_status()
        build_usage_end = len(getattr(memory.embedding_provider, 'usage_events', []))
        if requested == 'hybrid' and index['status'] != 'complete':
            return {'status': 'failed', 'reason': 'index_not_complete', 'index': index, 'mode': mode}
        rows = []
        for query in fixture['queries']:
            if (query['user_id'], query['session_id'], query['project_id']) != (USER, SESSION, PROJECT):
                raise ValueError('fixture 身份与基准实例不一致')
            started = time.monotonic()
            bundle = memory.retrieve(query['query'], top_k=5)
            elapsed = (time.monotonic()-started)*1000
            kind = query['kind']
            selected = ([h.item.memory_id for h in bundle.items] if kind == 'item' else
                        [r['summary_id'] for r in bundle.summaries if r['kind'] == 'topic'] if kind == 'summary' else
                        [r['experience_id'] for r in bundle.experiences])
            # Context injection is measured separately; a cached query vector may be reused.
            memory.append_message({'role': 'user', 'content': query['query']})
            context = memory.build_context()
            memory.append_message({'role': 'assistant', 'content': '合成评测占位答复'})
            rows.append({**query, 'expected': [identities[k] for k in query['expected']], 'selected': selected,
                         'latency_ms': elapsed, 'diagnostics': bundle.diagnostics,
                         'selected_counts': {'item': len(bundle.items), 'summary': len(bundle.summaries), 'experience': len(bundle.experiences)},
                         'injected_counts': {k: len(context.diagnostics[k]) for k in ('retrieved', 'summaries', 'experiences')},
                         'candidate_counts': {k: v.get('fused_count', v.get('selected_count', 0)) for k, v in bundle.diagnostics['per_kind'].items()}})
        config = asdict(memory.config)
        config.pop('database_path')
        embedding_usage = getattr(memory.embedding_provider, 'usage_events', [])
        degraded = any(r['diagnostics']['degraded'] for r in rows)
        return {'status': 'degraded' if degraded else 'complete', 'mode': mode,
                'real_semantic_validation': requested == 'hybrid' and not degraded,
                'space_id': index.get('space_id'), 'index': index,
                'parameters': config, 'parameter_fingerprint': hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest(),
                'metrics_by_split': {split: metrics([r for r in rows if r['split'] == split]) for split in ('calibration', 'acceptance')},
                'usage': {'index_build': embedding_usage[build_usage_start:build_usage_end],
                          'embedding_queries': embedding_usage[build_usage_end:],
                          'rerank': getattr(memory.reranker, 'usage_events', []), 'extraction': [], 'main_chat': []},
                'cost_note': '记录独立 token 用量；未配置单价，不估算费用',
                'failures': [r['query_id'] for r in rows if (not set(r['selected']) & set(r['expected'])) if r['expected']] +
                            [r['query_id'] for r in rows if not r['expected'] and r['selected']],
                'queries': rows}


def evaluate(mode='all', env=None):
    fixture = json.loads(FIXTURE.read_text())
    values = dict(os.environ if env is None else env)
    with tempfile.TemporaryDirectory(prefix='lamemory-hybrid-eval-') as directory:
        cfg, identities = seed(Path(directory)/'synthetic.sqlite3', fixture)
        # Each variant has its own instance and output; authority snapshot is unchanged.
        variants = ('keyword', 'hybrid', 'hybrid-rerank') if mode == 'all' else (mode,)
        results = {}
        for variant in variants:
            try:
                result = run_variant(cfg, identities, fixture, variant, values)
            except Exception as exc:
                result = {'status': 'failed', 'mode': variant, 'reason': type(exc).__name__}
            result['fixture_sha256'] = hashlib.sha256(FIXTURE.read_bytes()).hexdigest()
            result['synthetic_only'] = True
            result['identity_map'] = identities
            results[variant] = result
        return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['keyword', 'hybrid', 'hybrid-rerank', 'all'], default='all')
    parser.add_argument('--output', required=True, type=Path, help='新输出目录，不覆盖已有结果')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    results = evaluate(args.mode)
    for mode, result in results.items():
        (args.output/(mode+'.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps({mode: result['status'] for mode, result in results.items()}))
    return 0 if all(r['status'] == 'complete' for r in results.values()) else 2


if __name__ == '__main__':
    raise SystemExit(main())
