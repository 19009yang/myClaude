"""检索契约；诊断与来源随本次调用返回。"""
from dataclasses import dataclass, field


class EmbeddingProviderError(RuntimeError):
    """只包含适配器生成的错误类别，不包含服务响应文本。"""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def error_kind(exc):
    return exc.code if isinstance(exc, EmbeddingProviderError) else type(exc).__name__


class RetrievalUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RetrievalDocument:
    entity_kind: str
    entity_id: str
    user_id: str
    scope_kind: str
    scope_key: str
    entity_generation: int
    revision: int
    text: str
    input_hash: str
    payload: dict
    truncated: bool = False


@dataclass(frozen=True)
class ChannelHit:
    entity_kind: str
    entity_id: str
    channel: str
    raw_score: float
    rank: int


@dataclass
class RetrievalBundle:
    items: list = field(default_factory=list)
    summaries: list = field(default_factory=list)
    experiences: list = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)
