"""公开配置；定价是可审阅的快照，不从模型名猜测价格。"""
from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUITE = ROOT / "benchmarks/memory_task"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode()).hexdigest()


def save_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                          encoding="utf-8")


@dataclass(frozen=True)
class Settings:
    groups: tuple[str, ...] = ("none", "legacy", "lamemory")
    repeats: int = 3
    tasks: tuple[str, ...] = ()
    context_window: int = 32768
    max_output_tokens: int = 4096
    memory_token_budget: int = 2048
    max_model_calls: int = 8
    max_tool_calls: int = 24
    memory_timeout: float = 60
    judge_timeout: float = 120
    job_attempts: int = 3
    hierarchy: bool = True
    sandbox: str = "seatbelt"
    docker_image: str = "python:3.13-slim"
    expected_calls_per_round: int = 3
    expected_output_tokens: int = 900
    expected_artifact_tokens: int = 2400
    seed_memory_tokens_per_message: int = 80
    max_cost: float | None = None

    def __post_init__(self):
        if not self.groups or len(set(self.groups)) != len(self.groups) or set(self.groups) - {"none", "legacy", "lamemory"}:
            raise ValueError("groups 必须是 none/legacy/lamemory 的非空无重复子集")
        for key in ("repeats", "context_window", "max_output_tokens", "max_model_calls", "max_tool_calls",
                    "job_attempts", "expected_calls_per_round", "expected_output_tokens",
                    "expected_artifact_tokens", "seed_memory_tokens_per_message"):
            v = getattr(self, key)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                raise ValueError(f"{key} 必须为正整数")
        if self.memory_token_budget < 0 or self.input_budget <= 0:
            raise ValueError("上下文预算无效")
        for v in (self.memory_timeout, self.judge_timeout):
            if not math.isfinite(v) or v <= 0:
                raise ValueError("超时必须为有限正数")
        if self.max_cost is not None and (not math.isfinite(self.max_cost) or self.max_cost <= 0):
            raise ValueError("max_cost 必须为有限正数")
        if self.sandbox not in {"seatbelt", "docker"}:
            raise ValueError("sandbox 仅支持 seatbelt 或 docker，没有非隔离执行模式")
        if self.expected_calls_per_round > self.max_model_calls or self.expected_output_tokens > self.max_output_tokens:
            raise ValueError("期望调用/输出不能超过上限")

    @property
    def input_budget(self):
        return self.context_window - self.max_output_tokens - 512

    def public(self):
        return asdict(self)


def read_settings(path=None, **overrides):
    data = load_json(path) if path else {}
    for k, v in overrides.items():
        if v is not None:
            data[k] = v
    for k in ("groups", "tasks"):
        if k in data:
            data[k] = tuple(data[k])
    return Settings(**data)


def selected_tasks(settings):
    manifest = load_json(SUITE / "manifest.json")
    tasks = [t for t in manifest["tasks"] if not settings.tasks or t["task_id"] in settings.tasks]
    if len(tasks) != (len(settings.tasks) if settings.tasks else manifest["task_count"]):
        raise ValueError("未知或重复的 task ID")
    return manifest, tasks
