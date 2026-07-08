from __future__ import annotations
from core import Node,call_llm,Flow
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from typing import Any, Tuple
from tools.builtins.search import search as search_ddgs

"""
演示如何使用 Node 和 Flow 构建一个简单的工作流程：
搜索和摘要工作流程:query -> search -> summarize -> output
"""

class QueryNode(Node):
    def exec(self, payload:Any)->Tuple[str, Any]:
        return "search", payload
    

class SearchNode(Node):
    def exec(self, payload: Any) -> Tuple[str, Any]:
        results = search_ddgs(str(payload), max_results=3)
        print("result:",results)
        titles = [r.get("title") or r.get("body") or "" for r in results]
        summary_input = " | ".join([t for t in titles if t])
        return "summarize", summary_input
    
class SumarizeNode(Node):
    def exec(self, payload: Any) -> Tuple[str, Any]:
        summary_input = payload
        messages = [
            {"role": "system", "content": "你是一个信息摘要助手。"},
            {"role": "user", "content": f"请将以下内容进行总结，提取关键信息: {summary_input}"}
        ]
        summary_message = call_llm(messages=messages)
        summary_content = summary_message.get("content", "")
        return "output", summary_content

class OutputNode(Node):
    def exec(self, payload: Any) -> Tuple[str, Any]:
        summary_content = payload
        print(f"\n🤖 Summary: {summary_content}\n")
        return "done", None

def run_workflow() -> None:
    print("=" * 60)
    print("🤖 Search and Summarize Workflow")
    print("=" * 60)
    print("输入 'quit' 或 'exit' 退出\n")

    query_node = QueryNode()
    search_node = SearchNode()
    summarize_node = SumarizeNode()
    output_node = OutputNode()

    query_node - "search" >> search_node
    search_node - "summarize" >> summarize_node
    summarize_node - "output" >> output_node

    while True:
        user_input = input("👤 You: ").strip()

        if user_input.lower() in ("quit", "exit", "q"):
            print("\n再见！")
            break

        if not user_input:
            continue

        flow=Flow(query_node)
        flow.run(user_input)
    

if __name__ == "__main__":
    run_workflow()