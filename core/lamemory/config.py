"""LaMemory 单机配置，不读取模型密钥。"""
from dataclasses import dataclass
from pathlib import Path
import math

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / ".myclaude/lamemory/lamemory.sqlite3"


@dataclass(frozen=True)
class LaMemoryConfig:
    database_path: Path | str = DEFAULT_DATABASE
    retrieval_mode: str = "keyword"
    keyword_candidate_k: int = 30
    semantic_candidate_k: int = 30
    rrf_k: int = 60
    keyword_weight: float = 1.0
    semantic_weight: float = 1.0
    semantic_min_similarity: float = 0.3
    retrieval_timeout_seconds: float = 10
    allow_keyword_fallback: bool = True
    rerank_enabled: bool = False
    rerank_top_n: int = 20
    rerank_timeout_seconds: float = 5
    embedding_batch_size: int = 16
    max_embedding_chars: int = 12000
    top_k: int = 5
    memory_token_budget: int = 2048
    context_window: int = 32768
    reserved_output: int = 4096
    safety_margin: int = 512
    max_extracted_items: int = 8
    max_content_chars: int = 4000
    provider_timeout_seconds: float = 30
    job_max_attempts: int = 3
    retry_delay_seconds: float = 0.1
    auto_process_jobs: bool = True
    project_id: str | None = None
    auto_learn_experiences: bool = False
    experience_min_successes: int = 2
    max_experience_candidates: int = 3
    max_summary_chars: int = 1200

    def __post_init__(self):
        object.__setattr__(self, "database_path", Path(self.database_path).expanduser().resolve())
        if self.retrieval_mode not in {"keyword", "hybrid"}:
            raise ValueError("retrieval_mode 必须为 keyword/hybrid")
        if self.rerank_enabled and self.retrieval_mode != "hybrid":
            raise ValueError("rerank 需要 hybrid")
        for name in ("keyword_candidate_k", "semantic_candidate_k", "rerank_top_n", "embedding_batch_size"):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= 100:
                raise ValueError(name + " 必须为 1–100 整数")
        for name in ("rrf_k", "max_embedding_chars"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(name + " 必须为正整数")
        for name in ("keyword_weight", "semantic_weight", "retrieval_timeout_seconds", "rerank_timeout_seconds"):
            if isinstance(getattr(self, name), bool) or not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError(name + " 必须为正有限数")
        if isinstance(self.semantic_min_similarity, bool) or not math.isfinite(self.semantic_min_similarity) or not -1 <= self.semantic_min_similarity <= 1:
            raise ValueError("无效 cosine 阈值")
        for key in ("top_k", "context_window", "max_extracted_items", "max_content_chars", "job_max_attempts",
                    "experience_min_successes", "max_experience_candidates", "max_summary_chars"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} 必须为正数")
        if min(self.memory_token_budget, self.reserved_output, self.safety_margin, self.retry_delay_seconds) < 0:
            raise ValueError("预算与重试间隔不能为负数")
        if self.provider_timeout_seconds <= 0 or self.input_budget <= 0:
            raise ValueError("无效的模型超时或输入预算")
        if self.project_id is not None and (not isinstance(self.project_id, str) or not self.project_id.strip() or len(self.project_id) > 200):
            raise ValueError("project_id 必须为非空短字符串")

    @property
    def input_budget(self):
        return self.context_window - self.reserved_output - self.safety_margin
