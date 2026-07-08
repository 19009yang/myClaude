"""安全校验器 - 在工具执行前拦截危险操作"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


POLICY_PATH = Path(__file__).parent / "safety_policy.yaml"


@dataclass(slots=True)
class GuardResult:
    """安全校验结果"""

    blocked: bool        # 是否直接拦截
    needs_confirm: bool  # 是否需要用户确认
    reason: str          # 拦截/确认的原因说明


class SafetyGuard:
    """集中式安全校验器：在 ToolExecutor.execute() 之前拦截危险操作。

    支持两种拦截等级：
      - blocked：绝对拦截，不可执行（如 rm -rf /）
      - confirm：需用户确认后才可执行（如 rm file.txt）

    策略从 YAML 文件加载，用户可自定义规则。
    """

    def __init__(self, policy_path: str | None = None):
        """加载策略文件并构建正则匹配器。

        Args:
            policy_path: 自定义策略文件路径，默认使用内置 safety_policy.yaml
        """
        path = Path(policy_path) if policy_path else POLICY_PATH
        self._policy: dict[str, Any] = self._load_policy(path)

        # 预编译正则，避免每次 check 时重复编译
        self._bash_blocked: list[tuple[re.Pattern, str]] = self._compile_patterns(
            self._policy.get("bash", {}).get("blocked_patterns", [])
        )
        self._bash_confirm: list[tuple[re.Pattern, str]] = self._compile_patterns(
            self._policy.get("bash", {}).get("confirm_patterns", [])
        )

        self._file_blocked_paths: list[str] = self._policy.get("file_paths", {}).get(
            "blocked_paths", []
        )
        self._file_blocked_reason: str = self._policy.get("file_paths", {}).get(
            "blocked_reason", "禁止写入系统目录"
        )
        self._file_confirm_paths: list[str] = self._policy.get("file_paths", {}).get(
            "confirm_paths", []
        )
        self._file_confirm_reason: str = self._policy.get("file_paths", {}).get(
            "confirm_reason", "敏感文件修改需确认"
        )

        # 注册各工具的校验函数
        self._checkers: dict[str, callable] = {
            "bash": self._check_bash,
            "write": self._check_file_path,
            "edit": self._check_file_path,
        }

    # ── 公共接口 ──

    def check(self, tool_name: str, arguments: dict[str, Any]) -> GuardResult:
        """校验工具调用是否安全。

        Args:
            tool_name: 工具名称（如 "bash", "write", "edit"）
            arguments: 工具调用参数

        Returns:
            GuardResult：blocked / needs_confirm / 放行
        """
        checker = self._checkers.get(tool_name)
        if not checker:
            return GuardResult(blocked=False, needs_confirm=False, reason="")

        return checker(arguments)

    # ── bash 校验 ──

    def _check_bash(self, args: dict[str, Any]) -> GuardResult:
        """校验 bash 命令是否包含危险操作。

        优先检查 blocked（绝对拦截），再检查 confirm（需确认）。
        """
        command = args.get("command", "")
        if not command:
            return GuardResult(blocked=False, needs_confirm=False, reason="")

        # 1) 检查 blocked 黑名单
        for pattern, reason in self._bash_blocked:
            if pattern.search(command):
                return GuardResult(blocked=True, needs_confirm=False, reason=reason)

        # 2) 检查 confirm 确认名单
        for pattern, reason in self._bash_confirm:
            if pattern.search(command):
                return GuardResult(blocked=False, needs_confirm=True, reason=reason)

        # 3) 无匹配 → 放行
        return GuardResult(blocked=False, needs_confirm=False, reason="")

    # ── 文件路径校验 ──

    def _check_file_path(self, args: dict[str, Any]) -> GuardResult:
        """校验 write/edit 操作的目标路径是否安全。

        优先检查 blocked（系统目录），再检查 confirm（敏感文件）。
        同时检查原始路径和 resolve 后的路径，以覆盖 Linux 风格路径。
        """
        path = args.get("path", "")
        if not path:
            return GuardResult(blocked=False, needs_confirm=False, reason="")

        # 同时保留原始路径和解析后的绝对路径
        raw = path.lower()
        resolved = str(Path(path).resolve()).lower()

        # 1) 检查 blocked 路径（系统目录）—— 对两条路径都做匹配
        for blocked in self._file_blocked_paths:
            blocked_lower = blocked.lower()
            if resolved.startswith(blocked_lower) or raw.startswith(blocked_lower):
                return GuardResult(
                    blocked=True, needs_confirm=False, reason=self._file_blocked_reason
                )

        # 2) 检查 confirm 路径（敏感文件名）
        for confirm_keyword in self._file_confirm_paths:
            if confirm_keyword.lower() in raw:
                return GuardResult(
                    blocked=False, needs_confirm=True, reason=self._file_confirm_reason
                )

        # 3) 无匹配 → 放行
        return GuardResult(blocked=False, needs_confirm=False, reason="")

    # ── 内部辅助 ──

    @staticmethod
    def _load_policy(path: Path) -> dict[str, Any]:
        """从 YAML 文件加载策略，文件不存在则返回空策略。"""
        if not path.exists():
            print(f"[SafetyGuard] 策略文件不存在: {path}，使用空策略（所有操作放行）")
            return {}
        with path.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    @staticmethod
    def _compile_patterns(entries: list[dict[str, str]]) -> list[tuple[re.Pattern, str]]:
        """将 YAML 中的 pattern+reason 条目预编译为 (re.Pattern, reason) 列表。"""
        compiled = []
        for entry in entries:
            pattern_str = entry.get("pattern", "")
            reason = entry.get("reason", "")
            if pattern_str:
                try:
                    compiled.append((re.compile(pattern_str, re.IGNORECASE), reason))
                except re.error as e:
                    print(f"[SafetyGuard] 正则编译失败: {pattern_str} → {e}")
        return compiled


# ── 快速演示 ──

def demo() -> None:
    """演示 SafetyGuard 的拦截/确认/放行行为"""
    guard = SafetyGuard()

    test_cases = [
        ("bash", {"command": "rm -rf /"}),
        ("bash", {"command": "rm -rf ~"}),
        ("bash", {"command": "shutdown now"}),
        ("bash", {"command": "rm -rf ./tmp"}),
        ("bash", {"command": "rm file.txt"}),
        ("bash", {"command": "sudo apt install nginx"}),
        ("bash", {"command": "ls -la"}),
        ("bash", {"command": "git push origin main"}),
        ("bash", {"command": "pip install requests"}),
        ("write", {"path": "C:\\Windows\\System32\\hack.dll", "content": "evil"}),
        ("write", {"path": "/etc/passwd", "content": "evil"}),
        ("write", {"path": ".env", "content": "DB_PASSWORD=x"}),
        ("write", {"path": "output.txt", "content": "normal"}),
        ("edit", {"path": "C:\\Program Files\\app\\config.ini", "old_text": "a", "new_text": "b"}),
        ("edit", {"path": "my_project/config.yaml", "old_text": "a", "new_text": "b"}),
        ("read", {"path": "safe_file.txt"}),
    ]

    print("=" * 60)
    print("SafetyGuard 演示")
    print("=" * 60)

    for tool, args in test_cases:
        result = guard.check(tool, args)
        status = (
            "[BLOCKED]" if result.blocked
            else "[CONFIRM]" if result.needs_confirm
            else "[PASS]"
        )
        detail = f" | {result.reason}" if result.reason else ""
        args_str = args.get("command", "") or args.get("path", "")
        print(f"  {status}{detail}: {tool}({args_str})")


if __name__ == "__main__":
    demo()
