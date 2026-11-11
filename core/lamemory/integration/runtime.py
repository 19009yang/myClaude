"""宿主共用的显式环境工厂；不自动选择模型，不改写进程环境。"""
from dataclasses import replace
import os
import time
from ..config import LaMemoryConfig
from ..embedding.provider import EmbeddingConfig, OpenAIEmbeddingProvider
from ..retrieval.reranking import ChatReranker
from ..service import LaMemoryService


def boolean(value):
    if str(value).lower() not in ('true', 'false', '1', '0'):
        raise ValueError('布尔配置必须为 true/false')
    return str(value).lower() in ('true', '1')


def create_memory(config=None, *, env=None, retrieval_mode=None, rerank=None, offline=False,
                  embedding_provider=None, reranker=None, select_space=False, **kwargs):
    values = os.environ if env is None else env
    cfg = config or LaMemoryConfig()
    mode = retrieval_mode if retrieval_mode is not None else values.get('LAMEMORY_RETRIEVAL_MODE', cfg.retrieval_mode)
    enabled = rerank if rerank is not None else boolean(values.get('LAMEMORY_RERANK_ENABLED', str(cfg.rerank_enabled)))
    changes = {}
    for name in ('keyword_candidate_k', 'semantic_candidate_k', 'rrf_k', 'rerank_top_n', 'embedding_batch_size', 'max_embedding_chars'):
        if 'LAMEMORY_' + name.upper() in values:
            changes[name] = int(values['LAMEMORY_' + name.upper()])
    for name in ('keyword_weight', 'semantic_weight', 'semantic_min_similarity', 'retrieval_timeout_seconds', 'rerank_timeout_seconds'):
        if 'LAMEMORY_' + name.upper() in values:
            changes[name] = float(values['LAMEMORY_' + name.upper()])
    if 'LAMEMORY_ALLOW_KEYWORD_FALLBACK' in values:
        changes['allow_keyword_fallback'] = boolean(values['LAMEMORY_ALLOW_KEYWORD_FALLBACK'])
    cfg = replace(cfg, retrieval_mode='keyword' if offline or select_space else mode, rerank_enabled=False if offline or select_space else enabled, **changes)
    owned = []
    try:
        if (cfg.retrieval_mode == 'hybrid' or select_space) and embedding_provider is None:
            p = 'LAMEMORY_EMBEDDING_'
            required = ('MODEL', 'BASE_URL', 'DIMENSIONS', 'NAMESPACE')
            complete = all(values.get(p + name) for name in required)
            if not complete and cfg.retrieval_mode == 'hybrid':
                raise ValueError('hybrid 缺少 LAMEMORY_EMBEDDING_MODEL/BASE_URL/DIMENSIONS/NAMESPACE')
            if complete:
                ec = EmbeddingConfig(provider=values.get(p+'PROVIDER', 'openai-compatible'), model=values[p+'MODEL'],
                    base_url=values[p+'BASE_URL'], dimensions=int(values[p+'DIMENSIONS']), namespace=values[p+'NAMESPACE'],
                    api_key=values.get(p+'API_KEY') or None, send_dimensions=boolean(values.get(p+'SEND_DIMENSIONS', 'false')),
                    query_prefix=values.get(p+'QUERY_PREFIX', ''), document_prefix=values.get(p+'DOCUMENT_PREFIX', ''),
                    timeout=float(values.get(p+'TIMEOUT', '10')), max_retries=int(values.get(p+'MAX_RETRIES', '0')),
                    max_embedding_chars=cfg.max_embedding_chars)
                if select_space:
                    class DescriptorOnly:
                        descriptor = ec.descriptor
                        def embed_documents(self, texts, *, timeout):
                            raise RuntimeError('offline_index_provider_unavailable')
                    embedding_provider = DescriptorOnly()
                else:
                    embedding_provider = OpenAIEmbeddingProvider(ec)
                    owned.append(embedding_provider)
        if cfg.rerank_enabled and reranker is None:
            from core.providers import ModelConfig
            prefix = 'LAMEMORY_RERANK_LLM_'
            if any(not values.get(prefix + name) for name in ('PROVIDER', 'MODEL_NAME', 'BASE_URL')):
                raise ValueError('重排需要独立完整 LAMEMORY_RERANK_LLM_ 配置')
            # Exact independent mapping: never inherit the chat provider's key.
            selected = {k: v for k, v in values.items() if k.startswith(prefix)}
            reranker = ChatReranker(model_config=ModelConfig.from_env(prefix=prefix, env=selected, require_key=False))
        service = LaMemoryService(cfg, embedding_provider=embedding_provider if not offline or select_space else None,
                                  reranker=reranker if not offline else None, **kwargs)
    except Exception:
        for client in owned:
            client.close()
        raise
    service._owned_clients.extend(owned)
    service.requested_retrieval_mode = mode
    service.offline = offline
    return service


def maintain_embeddings(service):
    if service.config.retrieval_mode != 'hybrid':
        return None
    service.process_embedding_jobs(max_jobs=16, deadline=time.monotonic()+10)
    return service.embedding_status()
