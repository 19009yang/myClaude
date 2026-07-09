"""工具定义 - 简单的工具描述格式"""

from __future__ import annotations
from typing import Any, Callable, List
from tools.skill_loader import get_default_registry


def activate_skill(name: str) -> str:
    """激活指定 Skill，返回其完整操作指南。

    Args:
        name: 要激活的 Skill 名称

    Returns:
        Skill 的完整操作指南正文；若 Skill 不存在，返回提示信息
    """
    registry = get_default_registry()
    skill = registry.get_skill_full(name)
    if not skill:
        available = registry.skill_summaries_text()
        return f"Skill '{name}' 不存在。\n{available}"
    return skill[1]


class Tool:
    """简单工具定义"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict,
        fn: Callable,
    ):
        self.name = name
        self.description = description
        self.parameters = parameters
        self.fn = fn

    def to_llm_format(self) -> dict:
        """转换为 LLM API 格式（OpenAI/Anthropic 通用）"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, **kwargs) -> Any:
        """执行工具"""
        #将参数传入工具，返回处理结果
        return self.fn(**kwargs)


# 内置工具列表
def get_builtin_tools() -> List[Tool]:
    """获取所有内置工具"""
    from .read import read_file
    from .search import search
    from .bash import bash
    from .ls import ls
    from .edit import edit_file
    from .grep import grep
    from .write import write_file

    return [
        Tool(
            name="read",
            description="Read file contents. Use offset/limit for large files.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "offset": {"type": "integer", "description": "Start line (1-indexed)"},
                    "limit": {"type": "integer", "description": "Max lines to read"},
                },
                "required": ["path"],
            },
            fn=read_file,
        ),
        Tool(
            name="search",
            description="Search the web using DuckDuckGo.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {"type": "integer", "description": "Max results to return"},
                },
                "required": ["query"],
            },
            fn=search,
        ),
        Tool(
            name="ls",
            description="List directory contents.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path"},
                    "limit": {"type": "integer", "description": "Max entries to return"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
                "required": ["path"],
            },
            fn=ls,
        ),
        Tool(
            name="bash",
            description="Execute bash commands in a safe environment.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Bash command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout in seconds"},
                },
                "required": ["command"],
            },
            fn=bash,
        ),
        Tool(
            name="edit",
            description="Edit a file by replacing text.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "old_text": {"type": "string", "description": "Text to replace"},
                    "new_text": {"type": "string", "description": "New text"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            fn=edit_file,
        ),
        Tool(
            name="grep",
            description="Search for a pattern in file contents. Supports regex, literal, glob filtering, and context lines.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Search pattern (regex or literal)"},
                    "path": {"type": "string", "description": "Search path (file or directory, default current directory)"},
                    "glob": {"type": "string", "description": "File glob filter, e.g. '*.py'"},
                    "ignore_case": {"type": "boolean", "description": "Ignore case when searching (default: false)"},
                    "literal": {"type": "boolean", "description": "Treat pattern as literal string, not regex (default: false)"},
                    "context": {"type": "integer", "description": "Number of context lines to show around matches (default: 0)"},
                    "limit": {"type": "integer", "description": "Maximum number of matches to return (default: 100)"},
                    "cwd": {"type": "string", "description": "Working directory for resolving relative paths"},
                },
                "required": ["pattern"],
            },
            fn=grep,
        ),
        Tool(
            name="write",
            description="Write content to a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path"},
                    "content": {"type": "string", "description": "Content to write"},
                    "cwd": {"type": "string", "description": "Working directory"},
                },
                "required": ["path", "content"],
            },
            fn=write_file,
        ),
        Tool(
            name="activate_skill",
            description="激活一个 Skill，获取其完整操作指南。调用后你会收到该 Skill 的详细操作步骤，请严格按照指南执行。",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "要激活的 Skill 名称"},
                },
                "required": ["name"],
            },
            fn=activate_skill,
        ),
    ]