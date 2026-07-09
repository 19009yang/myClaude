"""内置工具模块 - 简化版

使用示例:
    from tools.builtins import get_builtin_tools

    # 获取所有工具
    tools = get_builtin_tools()

    # 直接执行
    result = tools[0].execute(path="some/file.py")

    # 转换为 LLM 格式
    llm_tools = [t.to_llm_format() for t in tools]
"""

from .read import read_file
from .search import search
from .tool_def import Tool, get_builtin_tools, activate_skill
from .bash import bash
from .ls import ls
from .edit import edit_file
from .grep import grep
from .write import write_file
from .paper_search import search_papers, download_papers
from .latex_render import render_latex


__all__ = [
    # 工具函数
    "read_file",
    "search",
    "bash",
    "ls",
    "edit_file",
    "grep",
    "write_file",
    "activate_skill",
    "search_papers",
    "download_papers",
    "render_latex",
    # 工具定义
    "Tool",
    "get_builtin_tools",
]