"""Tools 模块 - 内置工具和 LLM 集成

使用示例:
    from tools import get_tools, execute_tool, chat_with_tools

    # 获取工具列表
    tools = get_tools()

    # 执行单个工具
    result = execute_tool("ls", {"path": "."})

    # 完整对话（自动处理 tool calls）
    response = chat_with_tools("列出当前目录", tools)
"""


from .builtins import get_builtin_tools, Tool
from .executor import ToolExecutor, ToolResult, ToolCall
from .guard import SafetyGuard, GuardResult
from .skill_loader import get_default_registry, SkillRegistry


__all__ = [
    "Tool",
    "ToolResult",
    "ToolCall",
    "ToolExecutor",
    "SafetyGuard",
    "GuardResult",
    "get_builtin_tools",
    "get_tools",
    "execute_tool",
    "get_default_registry",
    "SkillRegistry",
]

_guard = SafetyGuard()
_executor = ToolExecutor(guard=_guard)  # 单例，tool_map 和 guard 只构建一次

def get_tools():
    """获取所有内置工具"""
    return get_builtin_tools()


def execute_tool(name: str, arguments: dict) -> str:
    """执行指定工具（委托给 ToolExecutor）"""
    tool_call = ToolCall(id="", name=name, arguments=arguments)
    result = _executor.execute(tool_call)
    return result.content
