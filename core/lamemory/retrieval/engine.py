"""三类共享一次 query embedding；权限/状态先于候选截断，返回前复核。"""
import time
from ..embedding.space import digest
from .fusion import cosine, rrf, unit_vector
from ..models import SearchHit
from .reranking import validate_scores
from .sources import documents, hydrate
from .types import RetrievalBundle, RetrievalUnavailableError, error_kind


KINDS = ('item', 'summary', 'experience')


def retrieve(service, query, *, kinds=KINDS, filters=None, top_k=None):
    service._validate_query(query)
    if not isinstance(kinds, (list, tuple)) or len(set(kinds)) != len(kinds) or set(kinds)-set(KINDS):
        raise ValueError('无效检索种类')
    if filters and 'item' not in kinds:
        raise ValueError('filters 仅用于 item')
    cfg = service.config
    limit = cfg.top_k if top_k is None else top_k
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError('top_k 必须为 0 到 100')
    diagnostics = {'requested_mode': getattr(service, 'requested_retrieval_mode', cfg.retrieval_mode),
                   'effective_mode': cfg.retrieval_mode, 'degraded': False, 'degraded_reasons': [],
                   'space_id': service.embedding_index.space if cfg.retrieval_mode == 'hybrid' else None,
                   'per_kind': {}, 'timings_ms': {'query_embedding': 0, 'semantic': 0, 'fusion': 0, 'rerank': 0},
                   'cache': {'query_embedding_hit': False, 'retrieval_hit': False}}
    if getattr(service, 'offline', False):
        diagnostics.update(degraded=True, degraded_reasons=['offline'])
    bundle = RetrievalBundle(diagnostics=diagnostics)
    state = service.db.one('SELECT generation FROM lamemory_retrieval_state WHERE user_id=?', (service.user_id,))
    diagnostics['retrieval_generation'] = state['generation'] if state else 0
    diagnostics['data_version'] = service.db.execute('PRAGMA data_version').fetchone()[0]
    if limit == 0 or (not query.strip() and cfg.retrieval_mode == 'hybrid'):
        diagnostics['effective_mode'] = 'empty'
        return bundle
    deadline = time.monotonic() + cfg.retrieval_timeout_seconds
    # Existing keyword routines preserve scores, reasons and history filters.
    keyword = {}
    if 'item' in kinds:
        keyword['item'] = service._keyword_search(query, filters, 100)
    if 'summary' in kinds:
        keyword['summary'] = service.hierarchy.search(query, top_k=101)
    if 'experience' in kinds:
        keyword['experience'] = service.procedural.search(query, 100)
    if cfg.retrieval_mode == 'keyword':
        for kind, rows in keyword.items():
            selected = rows[:limit]
            for row in selected:
                metadata = {'keyword_rank': rows.index(row)+1, 'keyword_score': row.score if kind == 'item' else row['score'],
                            'semantic_rank': None, 'semantic_score': None, 'rrf_score': None, 'rerank_score': None,
                            'score_kind': 'background' if kind == 'summary' and row['kind'] == 'profile' else 'keyword'}
                if metadata['score_kind'] == 'background':
                    metadata['keyword_rank'] = metadata['keyword_score'] = None
                if kind == 'item':
                    row.retrieval = metadata
                else:
                    row['retrieval'] = metadata
            setattr(bundle, {'item': 'items', 'summary': 'summaries', 'experience': 'experiences'}[kind], selected)
            diagnostics['per_kind'][kind] = {'selected_count': len(selected), 'keyword_count': len(rows), 'semantic_count': 0, 'fused_count': len(rows), 'rerank_status': 'disabled', 'effective_mode': 'keyword'}
        return bundle
    if service.db.connection.in_transaction:
        raise RuntimeError('retrieval provider 不允许在写事务中调用')
    index = service.embedding_index
    docs = {(d.entity_kind, d.entity_id): d for d in documents(service, index.descriptor, kinds, filters)}
    vectors, invalid = index.vectors(docs)
    diagnostics['invalid_vectors'] = invalid
    historical = bool(filters and filters.get('status', 'active') != 'active')
    # Do not query the provider when all eligible vector projections are absent.
    query_vector, semantic_error = None, None
    if vectors:
        before = time.monotonic()
        try:
            cache = service._query_embedding_cache
            # Hash exact text: do not alias case-sensitive semantic model inputs.
            cache_key = (service.user_id, service.session_id, cfg.project_id, digest(query), index.space)
            if cache_key in cache:
                query_vector = cache.pop(cache_key)
                cache[cache_key] = query_vector
                diagnostics['cache']['query_embedding_hit'] = True
            else:
                remaining = deadline-time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('query_deadline')
                query_vector = unit_vector(service.embedding_provider.embed_query(query, timeout=remaining), index.descriptor.dimensions)
                if time.monotonic() > deadline:
                    raise TimeoutError('query_deadline')
                cache[cache_key] = query_vector
                while len(cache) > 64:
                    cache.popitem(last=False)
        except Exception as exc:
            semantic_error = 'query_embedding_' + error_kind(exc)
        diagnostics['timings_ms']['query_embedding'] = (time.monotonic()-before)*1000
    # Provider may mutate authoritative sources; re-read before any ranking or reranker input.
    fresh = {(d.entity_kind, d.entity_id): d for d in documents(service, index.descriptor, kinds, filters)}
    changed = {key for key, doc in docs.items() if key not in fresh or fresh[key].entity_generation != doc.entity_generation or fresh[key].input_hash != doc.input_hash}
    if changed:
        diagnostics['sources_changed'] = len(changed)
    for key in changed:
        vectors.pop(key, None)
        docs.pop(key, None)
    for kind in kinds:
        # Earlier kind's reranker can invalidate later kinds before their model input.
        current_kind = {(d.entity_kind, d.entity_id): d for d in documents(service, index.descriptor, (kind,), filters if kind == 'item' else None)}
        for key, doc in list(docs.items()):
            if key[0] == kind and (key not in current_kind or current_kind[key].entity_generation != doc.entity_generation or current_kind[key].input_hash != doc.input_hash):
                docs.pop(key)
                vectors.pop(key, None)
                diagnostics['sources_changed'] = diagnostics.get('sources_changed', 0)+1
        eligible = {key: d for key, d in docs.items() if key[0] == kind}
        available = {key: v for key, v in vectors.items() if key in eligible}
        rows = keyword.get(kind, [])
        def identity(row):
            return (kind, row.item.memory_id if kind == 'item' else row['summary_id' if kind == 'summary' else 'experience_id'])
        rows = [r for r in rows if (kind == 'summary' and r['kind'] == 'profile') or (historical and kind == 'item') or identity(r) in eligible]
        if kind == 'summary':
            rows = [r for r in rows if r['kind'] != 'profile']
        reason = 'historical_status' if historical and kind == 'item' else (semantic_error if eligible else None) or ('index_unavailable' if eligible and not available else None)
        mode = 'keyword' if reason else 'hybrid'
        detail = {'eligible': len(eligible), 'indexed': len(available), 'missing': len(eligible)-len(available),
                  'partial_index': len(available) < len(eligible), 'effective_mode': mode, 'rerank_status': 'disabled',
                  'scanned_vectors': len(available), 'truncated_documents': sum(d.truncated for d in eligible.values())}
        diagnostics['per_kind'][kind] = detail
        if reason:
            detail['semantic_skipped_reason'] = reason
            if reason != 'historical_status':
                if not cfg.allow_keyword_fallback:
                    raise RetrievalUnavailableError(reason)
                diagnostics['degraded'] = True
                diagnostics['degraded_reasons'].append(kind + ':' + reason)
        kw_width = max(cfg.keyword_candidate_k, limit)
        kw = [(identity(r), r.score if kind == 'item' else r['score']) for r in rows[:kw_width]]
        semantic = []
        if not reason and query_vector is not None:
            before = time.monotonic()
            try:
                for n, (key, vector) in enumerate(available.items()):
                    if n % 32 == 0 and time.monotonic() >= deadline:
                        raise TimeoutError('semantic_deadline')
                    score = cosine(query_vector, vector)
                    if score >= cfg.semantic_min_similarity:
                        semantic.append((key, score))
                semantic.sort(key=lambda pair: (-pair[1], -eligible[pair[0]].revision, pair[0][1]))
                semantic = semantic[:max(cfg.semantic_candidate_k, limit)]
            except Exception as exc:
                if not cfg.allow_keyword_fallback:
                    raise RetrievalUnavailableError('semantic_' + type(exc).__name__) from None
                semantic = []
                reason, mode = 'semantic_' + type(exc).__name__, 'keyword'
                diagnostics['degraded'] = True
                diagnostics['degraded_reasons'].append(kind + ':' + reason)
                detail.update(effective_mode=mode, semantic_skipped_reason=reason)
            diagnostics['timings_ms']['semantic'] += (time.monotonic()-before)*1000
        before = time.monotonic()
        ranked = rrf({'keyword': kw, 'semantic': semantic}, k=cfg.rrf_k,
                     weights={'keyword': cfg.keyword_weight, 'semantic': cfg.semantic_weight},
                     revisions={key: d.revision for key, d in eligible.items()})
        diagnostics['timings_ms']['fusion'] += (time.monotonic()-before)*1000
        if reason:
            ranked = [(identity(r), {'keyword_rank': n+1, 'keyword_score': r.score if kind == 'item' else r['score'],
                       'semantic_rank': None, 'semantic_score': None, 'rrf_score': None, 'rerank_score': None}) for n, r in enumerate(rows)]
        elif cfg.rerank_enabled and ranked:
            before = time.monotonic()
            prefix = ranked[:max(cfg.rerank_top_n, limit)]
            inputs = [{'id': key[0]+':'+key[1], 'text': eligible[key].text} for key, _ in prefix]
            remaining = min(cfg.rerank_timeout_seconds, deadline-time.monotonic())
            if remaining <= 0:
                detail.update(rerank_status='skipped', rerank_reason='deadline')
            else:
                try:
                    result = service.reranker.rerank(query, inputs, timeout=remaining)
                    scores = validate_scores(result, inputs)
                    if time.monotonic()-before > remaining:
                        raise TimeoutError('rerank_deadline')
                    for key, meta in prefix:
                        meta['rerank_score'] = scores[key[0]+':'+key[1]]
                    prefix.sort(key=lambda pair: -pair[1]['rerank_score'])
                    ranked = prefix + ranked[len(prefix):]
                    detail['rerank_status'] = 'completed'
                except Exception as exc:
                    detail.update(rerank_status='fallback', rerank_reason=type(exc).__name__)
            diagnostics['timings_ms']['rerank'] += (time.monotonic()-before)*1000
        detail.update(keyword_count=len(kw), semantic_count=len(semantic), fused_count=len(ranked))
        # A reranker may also have changed sources; never hydrate an obsolete candidate.
        latest = {(d.entity_kind, d.entity_id): d for d in documents(service, index.descriptor, (kind,), filters if kind == 'item' else None)}
        selected = []
        old_rows = {identity(r): r for r in rows}
        if historical and kind == 'item':
            old_rows = {(kind, r.item.memory_id): r for r in service._keyword_search(query, filters, 100)}
        for key, meta in ranked:
            doc = eligible.get(key)
            if not (historical and kind == 'item'):
                current = latest.get(key)
                if not doc or not current or current.entity_generation != doc.entity_generation or current.input_hash != doc.input_hash:
                    diagnostics['sources_changed'] = diagnostics.get('sources_changed', 0)+1
                    continue
            score_kind = 'keyword' if reason else ('rerank' if meta['rerank_score'] is not None else 'rrf')
            meta['score_kind'] = score_kind
            score = meta['keyword_score'] if reason else meta['rerank_score'] if score_kind == 'rerank' else meta['rrf_score']
            description = ('降级关键词：' + reason) if reason else ('双路命中' if meta['keyword_rank'] and meta['semantic_rank'] else '语义命中' if meta['semantic_rank'] else '关键词命中')
            if historical and kind == 'item':
                if key not in old_rows:
                    continue
                hit = old_rows[key]
                hit.retrieval = meta
            elif kind == 'item':
                hit = SearchHit(hydrate(service, doc), score, description, mode, meta)
            else:
                hit = {**hydrate(service, doc), 'score': score, 'mode': mode, 'reason': description, 'retrieval': meta}
            selected.append(hit)
            if len(selected) == limit:
                break
        if kind == 'summary' and len(selected) < limit:
            # Fresh profile validity, independent background navigation, fills remaining slots.
            for profile in service.hierarchy.search(query, top_k=101):
                if profile['kind'] == 'profile':
                    profile['retrieval'] = {'keyword_rank': None, 'keyword_score': None, 'semantic_rank': None, 'semantic_score': None, 'rrf_score': None, 'rerank_score': None, 'score_kind': 'background'}
                    selected.append(profile)
                    if len(selected) == limit:
                        break
        detail['selected_count'] = len(selected)
        setattr(bundle, {'item': 'items', 'summary': 'summaries', 'experience': 'experiences'}[kind], selected)
    # One final pass after *all* external calls also protects earlier kinds.
    with service.db.transaction():
        final_docs = {(d.entity_kind, d.entity_id): d for d in documents(service, index.descriptor, kinds, filters)}
        for kind in kinds:
            attribute = {'item': 'items', 'summary': 'summaries', 'experience': 'experiences'}[kind]
            kept = []
            history_rows = {r.item.memory_id: r.item for r in service._keyword_search(query, filters, 100)} if historical and kind == 'item' else {}
            valid_profiles = {r['summary_id']: r for r in service.hierarchy.search(query, top_k=101) if r['kind'] == 'profile'} if kind == 'summary' else {}
            for result in getattr(bundle, attribute):
                ident = result.item.memory_id if kind == 'item' else result['summary_id' if kind == 'summary' else 'experience_id']
                key = kind, ident
                if kind == 'summary' and result['kind'] == 'profile':
                    if ident not in valid_profiles:
                        continue
                    result = {**valid_profiles[ident], 'retrieval': result['retrieval']}
                elif historical and kind == 'item':
                    if ident not in history_rows:
                        continue
                    result.item = history_rows[ident]
                else:
                    original, current = docs.get(key), final_docs.get(key)
                    if not original or not current or original.entity_generation != current.entity_generation or original.input_hash != current.input_hash:
                        diagnostics['sources_changed'] = diagnostics.get('sources_changed', 0)+1
                        continue
                    if kind == 'item':
                        result.item = hydrate(service, current)
                    else:
                        result = {**hydrate(service, current), **{name: result[name] for name in ('score', 'mode', 'reason', 'retrieval')}}
                kept.append(result)
            setattr(bundle, attribute, kept)
            diagnostics['per_kind'][kind]['selected_count'] = len(kept)
    modes = {d['effective_mode'] for d in diagnostics['per_kind'].values() if d['eligible'] or d.get('semantic_skipped_reason') == 'historical_status'}
    diagnostics['effective_mode'] = next(iter(modes)) if len(modes) == 1 else 'mixed' if modes else 'empty'
    return bundle
