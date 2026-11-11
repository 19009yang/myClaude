"""评测专用的 hybrid 依赖与客户端生命周期；不读取真实记忆库。"""
from dataclasses import asdict

from core.lamemory.embedding.provider import EmbeddingConfig, OpenAIEmbeddingProvider
from core.lamemory.integration.runtime import boolean, create_memory


class RetrievalRuntime:
    def __init__(self, settings, env, ledger, *, offline=False):
        self.settings = settings
        self.env = {} if offline else dict(env)
        # 评测不允许由环境变量开启降级或未计费的重排。
        self.env.pop("LAMEMORY_ALLOW_KEYWORD_FALLBACK", None)
        self.provider = None
        self.embedding = None
        self.metadata = {"mode": settings.retrieval_mode, "rerank": False,
                         "allow_keyword_fallback": False, "simulated": offline,
                         "enabled": "lamemory" in settings.groups}
        if settings.retrieval_mode != "hybrid" or "lamemory" not in settings.groups:
            return
        if offline:
            from .smoke import FakeEmbedding
            self.provider = FakeEmbedding()
        else:
            p = "LAMEMORY_EMBEDDING_"
            if any(not self.env.get(p + key) for key in ("MODEL", "BASE_URL", "DIMENSIONS", "NAMESPACE")):
                raise ValueError("hybrid 缺少 LAMEMORY_EMBEDDING_MODEL/BASE_URL/DIMENSIONS/NAMESPACE")
            config = EmbeddingConfig(
                model=self.env[p + "MODEL"], base_url=self.env[p + "BASE_URL"],
                dimensions=int(self.env[p + "DIMENSIONS"]), namespace=self.env[p + "NAMESPACE"],
                provider=self.env.get(p + "PROVIDER", "openai-compatible"), api_key=self.env.get(p + "API_KEY") or None,
                send_dimensions=boolean(self.env.get(p + "SEND_DIMENSIONS", "false")),
                query_prefix=self.env.get(p + "QUERY_PREFIX", ""), document_prefix=self.env.get(p + "DOCUMENT_PREFIX", ""),
                timeout=float(self.env.get(p + "TIMEOUT", "10")), max_retries=int(self.env.get(p + "MAX_RETRIES", "0")),
                max_embedding_chars=int(self.env.get("LAMEMORY_MAX_EMBEDDING_CHARS", "12000")))
            ledger.prices.check_embedding(config)
            self.provider = OpenAIEmbeddingProvider(config)
        self.embedding = ledger.wrap_embedding(self.provider, simulated=offline)
        self.metadata["embedding"] = asdict(self.provider.descriptor)

    def create(self, config, **kwargs):
        service = create_memory(config, env=self.env, retrieval_mode=self.settings.retrieval_mode,
                                rerank=False, embedding_provider=self.embedding, **kwargs)
        parameters = asdict(service.config)
        parameters.pop("database_path")
        self.metadata["parameters"] = parameters
        return service

    def close(self):
        if self.provider is not None:
            self.provider.close()
