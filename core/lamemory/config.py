"""LaMemory 单机配置，不读取模型密钥。"""
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DATABASE = Path(__file__).resolve().parents[2] / ".myclaude/lamemory/lamemory.sqlite3"


@dataclass(frozen=True)
class LaMemoryConfig:
    database_path: Path | str = DEFAULT_DATABASE
    retrieval_mode: str = "keyword"
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

    def __post_init__(self):
        object.__setattr__(self, "database_path", Path(self.database_path).expanduser().resolve())
        if self.retrieval_mode != "keyword":
            raise ValueError("初版仅支持 keyword 检索；尚未接入 embedding")
        for key in ("top_k", "context_window", "max_extracted_items", "max_content_chars", "job_max_attempts"):
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} 必须为正数")
        if min(self.memory_token_budget, self.reserved_output, self.safety_margin, self.retry_delay_seconds) < 0:
            raise ValueError("预算与重试间隔不能为负数")
        if self.provider_timeout_seconds <= 0 or self.input_budget <= 0:
            raise ValueError("无效的模型超时或输入预算")

    @property
    def input_budget(self):
        return self.context_window - self.reserved_output - self.safety_margin
