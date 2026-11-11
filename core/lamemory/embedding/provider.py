"""独立 embedding 配置与 OpenAI-compatible SDK 适配器。"""
from dataclasses import dataclass, field
import math
import time
from typing import Protocol
from urllib.parse import urlsplit
from .space import EmbeddingDescriptor, digest
from ..retrieval.fusion import unit_vector
from ..retrieval.types import EmbeddingProviderError


class EmbeddingProvider(Protocol):
    descriptor: EmbeddingDescriptor
    def embed_documents(self, texts: list[str], *, timeout: float) -> list[list[float]]: ...
    def embed_query(self, text: str, *, timeout: float) -> list[float]: ...


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    base_url: str
    dimensions: int
    namespace: str
    provider: str = 'openai-compatible'
    api_key: str | None = field(default=None, repr=False)
    send_dimensions: bool = False
    query_prefix: str = ''
    document_prefix: str = ''
    timeout: float = 10
    max_retries: int = 0
    max_embedding_chars: int = 12000

    def __post_init__(self):
        url = urlsplit(self.base_url)
        if self.provider != 'openai-compatible' or url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('embedding 需要显式、不带凭据的 HTTP(S) base_url')
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_retries != 0:
            raise ValueError('embedding timeout 必须为正有限数；SDK 重试必须为 0')
        self.descriptor

    @property
    def descriptor(self):
        return EmbeddingDescriptor(self.model, self.dimensions, digest(self.base_url.rstrip('/')), self.namespace,
                                   self.query_prefix, self.document_prefix, self.max_embedding_chars)


class OpenAIEmbeddingProvider:
    def __init__(self, config, *, client=None):
        self.config, self.descriptor = config, config.descriptor
        self._owns_client = client is None
        if client is None:
            from openai import OpenAI
            client = OpenAI(base_url=config.base_url, api_key=config.api_key or 'unauthenticated',
                            timeout=config.timeout, max_retries=0)
        self.client = client
        self.usage_events = []

    def _embed(self, texts, timeout, purpose):
        if not texts:
            return []
        if not math.isfinite(timeout) or timeout <= 0:
            raise TimeoutError('embedding_deadline')
        started = time.monotonic()
        args = dict(model=self.config.model, input=texts, encoding_format='float',
                    timeout=min(timeout, self.config.timeout))
        if self.config.send_dimensions:
            args['dimensions'] = self.config.dimensions
        event = {'purpose': purpose, 'documents': len(texts), 'space_id': self.descriptor.space_id, 'usage': {}}
        try:
            client = self.client.with_options(max_retries=0) if callable(getattr(self.client, 'with_options', None)) else self.client
            result = client.embeddings.create(**args)
            usage = getattr(result, 'usage', None)
            event['usage'] = {name: getattr(usage, name) for name in ('prompt_tokens', 'total_tokens')
                              if type(getattr(usage, name, None)) is int}
            rows = result.data
            if len(rows) != len(texts) or any(type(r.index) is not int for r in rows) or {r.index for r in rows} != set(range(len(texts))):
                raise ValueError('invalid_embedding_indices')
            vectors = [unit_vector(r.embedding, self.config.dimensions) for r in sorted(rows, key=lambda r: r.index)]
            if time.monotonic()-started > min(timeout, self.config.timeout):
                raise TimeoutError('embedding_deadline')
        except Exception as exc:
            # Never persist raw provider errors, URLs, headers or request text.
            event['error'] = 'embedding_' + type(exc).__name__
            raise EmbeddingProviderError(event['error']) from None
        finally:
            event['elapsed_ms'] = (time.monotonic()-started)*1000
            self.usage_events.append(event)
        return vectors

    def embed_documents(self, texts, *, timeout):
        # Inputs are already materialized, prefixed, truncated and hashed by the index.
        return self._embed(texts, timeout, 'embedding_documents')

    def embed_query(self, text, *, timeout):
        return self._embed([self.config.query_prefix + text], timeout, 'embedding_query')[0]

    def close(self):
        if self._owns_client:
            self.client.close()
