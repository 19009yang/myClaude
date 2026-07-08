"""工具解析和执行，LLM工具调用"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
sys.path.insert(0, str(Path(__file__).parent.parent))
from tools.builtins import get_builtin_tools
from tools.guard import SafetyGuard, GuardResult

@dataclass(slots=True)
class ToolCall:
    """从LLM消息解析出的标准化调用"""

    id: str
    name: str
    arguments: dict[str, Any]

    @classmethod
    #从外部数据构造实例
    def from_openai_item(cls, item: dict[str, Any]) -> ToolCall:
        """从一个OpenAI格式的tool_call项构造实例"""

        function = item.get("function", {})
        arguments = function.get("arguments", {})

        if isinstance(arguments, str):
            arguments = _safe_json_loads(arguments)

        #解析后仍然不是字典，置空
        if not isinstance(arguments, dict):
            arguments = {}

        return cls(
            id=item.get("id", ""),
            name=function.get("name", ""),
            arguments=arguments,
        )
    

# def from_openai_item(item: dict[str, Any]) -> ToolCall:
#     function = item.get("function", {})
#     arguments = function.get("arguments", {})
#     if isinstance(arguments, str):
#         arguments = _safe_json_loads(arguments)
#     #解析后仍然不是字典，置空
#     if not isinstance(arguments, dict):
#         arguments = {}
#     return ToolCall(
#         id=item.get("id", ""),
#         name=function.get("name", ""),
#         arguments=arguments,
#     )


@dataclass(slots=True)
class ToolResult:
    """一次工具执行的结果."""

    tool_call_id: str
    content: str
    #是否执行错误
    is_error: bool = False

    #转化为tool message格式
    def to_message(self) -> dict[str, str]:
        """转换为用于对话历史记录的标准工具消息."""
        return {
            "role": "tool",
            "tool_call_id": self.tool_call_id,
            "content": self.content,
        }

    
class ToolExecutor:
    """解析LLM消息并执行引用的工具."""

    def __init__(self, guard: Optional[SafetyGuard] = None) -> None:
        self.tools = get_builtin_tools()
        self.tool_map = {tool.name: tool for tool in self.tools}
        self.guard = guard or SafetyGuard()

    def parse_tool_calls(self, assistant_message: dict[str, Any]) -> list[ToolCall]:
        """
        从LLM消息提取工具调用.
        只支持OpenAI格式
        Supported format:
        - OpenAI: message.tool_calls
        """

        openai_calls = assistant_message.get("tool_calls")
        #openai格式的"tool_calls"只有list与None两种情况
        if isinstance(openai_calls, list):
            return [ToolCall.from_openai_item(item) for item in openai_calls]
        return []

    def execute(self, tool_call: ToolCall) -> ToolResult:
        """执行一次工具调用并标准化输出"""

        tool = self.tool_map.get(tool_call.name)
        #工具不存在
        if not tool:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Tool '{tool_call.name}' not found",
                is_error=True,
            )

        # ── 安全校验 ──
        guard_result: GuardResult = self.guard.check(tool_call.name, tool_call.arguments)

        if guard_result.blocked:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"⚠️ 操作被安全策略拦截: {guard_result.reason}",
                is_error=True,
            )

        if guard_result.needs_confirm:
            print(f"\n⚠️ 高风险操作: {guard_result.reason}")
            print(f"   工具: {tool_call.name}, 参数: {tool_call.arguments}")
            confirm = input("   是否允许执行？(y/N): ").strip().lower()
            if confirm != "y":
                return ToolResult(
                    tool_call_id=tool_call.id,
                    content=f"用户拒绝执行: {guard_result.reason}",
                    is_error=True,
                )

        try:
            raw_result = tool.execute(**tool_call.arguments)

        except Exception as exc:
            return ToolResult(
                tool_call_id=tool_call.id,
                content=f"Error: {exc}",
                is_error=True,
            )

        return ToolResult(
            tool_call_id=tool_call.id,
            content=_stringify_result(raw_result),
            is_error=False,
        )

    def execute_all(self, tool_calls: list[ToolCall]) -> list[ToolResult]:
        """按顺序执行所有工具"""
        return [self.execute(tool_call) for tool_call in tool_calls]


def _safe_json_loads(value: str) -> Any:
    """安全加载JSON，若失败则返回空字典."""
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return {}


def _stringify_result(value: Any) -> str:
    """将工具的原始返回值转为字符串"""
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def demo() -> None:
    """演示解析和执行"""
    print("=" * 60)
    print("Tool 执行器演示")
    print("=" * 60)

    executor = ToolExecutor()

    # 模拟 LLM 返回的 assistant 消息
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": "我来查看目录",
        "tool_calls": [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "read",
                    "arguments": {"path": "markdown\codex-memory-system.md"}
                }
            },
            {
                "id": "call_def456",
                "type": "function",
                "function": {
                    "name": "search",
                    "arguments": '{"query": "python programming", "max_results": 5}'
                }
            }
        ]
    }

    print("\n1. LLM 返回的消息:")
    print(f"   Content: {assistant_message['content']}")
    print(f"   Tool calls: {len(assistant_message['tool_calls'])}")

    # 解析 tool calls
    print("\n2. 解析 tool calls:")
    tool_calls = executor.parse_tool_calls(assistant_message)
    for tc in tool_calls:
        print(f"   - {tc.name}({tc.arguments})")

    # 执行工具
    print("\n3. 执行工具:")
    for tc in tool_calls:
        result = executor.execute(tc)
        print(f"   {tc.name} -> {result.content[:60]}...")

    # 转换为 messages
    print("\n4. 转换为 messages 追加到 context:")
    results = executor.execute_all(tool_calls)
    for r in results:
        msg = r.to_message()
        print(f"   {msg}")


if __name__ == "__main__":
    demo()