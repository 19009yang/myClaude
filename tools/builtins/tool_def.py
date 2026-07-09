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
    from .paper_search import search_papers, download_papers
    from .latex_render import render_latex

    return [
        Tool(
            name="read",
            description="读取文件内容。对于大文件，使用 offset/limit 参数。",
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
            description="使用 DuckDuckGo 搜索查询，返回搜索结果列表。",
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
            description="列出指定目录下的文件和子目录。",
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
            description="执行 Bash 命令。",
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
            description="编辑文件内容，替换指定文本。",
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
            description="搜索文件内容，支持正则表达式和上下文行。",
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
            description="向指定文件写入内容，支持覆盖或追加模式。",
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
        Tool(
            name="search_papers",
            description="在 arxiv 检索论文，返回搜索结果列表。支持标题、作者、分类、摘要等搜索语法。",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，支持 arxiv 查询语法（ti:/au:/cat:/abs: 及 AND/OR 组合）"},
                    "max_results": {"type": "integer", "description": "最大返回条数（默认 10）"},
                    "sort_by": {"type": "string", "description": "排序依据: relevance / lastUpdatedDate / submittedDate"},
                    "sort_order": {"type": "string", "description": "排序方向: ascending / descending"},
                    "start": {"type": "integer", "description": "结果偏移量，用于分页（默认 0）"},
                },
                "required": ["query"],
            },
            fn=search_papers,
        ),
        Tool(
            name="download_papers",
            description="下载指定论文的 PDF，支持单个或批量下载。",
            parameters={
                "type": "object",
                "properties": {
                    "arxiv_ids": {
                        "oneOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
                        "description": "单个 arxiv ID 或 ID 列表，如 '2301.00123' 或 ['2301.00123', '2301.00456']",
                    },
                    "save_dir": {"type": "string", "description": "PDF 保存目录，默认为当前目录下的 papers/"},
                },
                "required": ["arxiv_ids"],
            },
            fn=download_papers,
        ),
        Tool(
            name="render_latex",
            description="将 LaTeX 源码编译为 PDF。输入完整的 LaTeX 代码，输出编译结果和 PDF 文件路径。",
            parameters={
                "type": "object",
                "properties": {
                    "tex_content": {"type": "string", "description": "完整的 LaTeX 源码（须包含 \\documentclass 到 \\end{document}）"},
                    "output_dir": {"type": "string", "description": "输出目录，默认为当前目录下的 output/"},
                    "filename": {"type": "string", "description": "输出文件名（不含扩展名），默认 paper"},
                },
                "required": ["tex_content"],
            },
            fn=render_latex,
        ),
    ]