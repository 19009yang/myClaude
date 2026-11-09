"""服务返回对象和可处理错误。"""
from dataclasses import asdict, dataclass, field
from typing import Any


class BudgetError(ValueError):
    """不可裁剪的输入超过模型预算。"""


@dataclass
class MemoryItem:
    memory_id: str
    user_id: str
    scope_kind: str
    scope_key: str
    type: str
    content: str
    content_hash: str
    slot_key: str | None
    occurred_at: str | None
    status: str
    revision: int
    superseded_by: str | None
    origin: str
    created_at: str
    updated_at: str
    source_message_ids: list[str] = field(default_factory=list)

    def to_dict(self):
        return asdict(self)


@dataclass
class WriteResult:
    status: str
    item: MemoryItem | None

    def to_dict(self):
        return asdict(self)


@dataclass
class SearchHit:
    item: MemoryItem
    score: float
    reason: str
    mode: str = "keyword"

    def to_dict(self):
        return asdict(self)


@dataclass
class ContextResult:
    messages: list[dict[str, Any]]
    diagnostics: dict[str, Any]


@dataclass
class FlushResult:
    status: str
    completed: int
    failed: int
    remaining: int
