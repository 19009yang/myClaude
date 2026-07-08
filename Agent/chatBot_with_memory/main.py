from __future__ import annotations
from typing import Any,Dict,Tuple
from tools import get_tools, ToolExecutor, SafetyGuard
from core import call_llm_chat, call_llm, Node, Flow, shared, Memory

"""
带有长期记忆和工具调用的对话机器人工作流程
"""

SYSTEM_PROMPT = """
你是一个具备工具调用和长期记忆能力的智能助手。

## 可用工具
- search: 搜索最新信息、新闻、产品发布时间等
- read / grep / ls: 读取和检索本地文件与代码
- bash: 执行终端命令
- write: 写入或创建文件

## 工具使用策略
- 涉及最新信息、事实核验时，优先调用 search，基于搜索结果回答
- 涉及本地文件/代码时，优先使用 read/grep/ls/bash
- 工具调用失败时，尝试换一种方式或直接告知用户原因

## 安全约束
你不得主动执行以下操作：递归删除大量文件、修改系统目录、提权操作、向外发送用户数据。
如果用户要求执行可能有害的操作，先警告风险并确认意图后再操作。
系统已内置安全策略，危险命令会被自动拦截。

## 长期记忆
你拥有长期记忆，会自动记住用户偏好和重要事实。回答时参考长期记忆中的信息，并在不确定时主动确认。

## 回答风格
- 跟随用户使用的语言（中文/英文）
- 使用 Markdown 格式，代码用代码块包裹
- 引用搜索结果时标注来源
"""

class ChatNode(Node):
    """调用 LLM，打印 assistant content，并按 tool_calls 决定是否继续。"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        memory = shared["memory"]
        tools = shared["tools"]

        # 生成上下文消息列表，包括系统提示、长期记忆和最近的对话历史
        messages = memory.build_context(system_prompt=SYSTEM_PROMPT)
        assistant_message = call_llm(messages=messages, tools=tools)
        memory.add_message(assistant_message)

        if assistant_message.get("tool_calls"):
            return "tool_call", assistant_message

        return "output", assistant_message


class ToolCallNode(Node):
    """执行 LLM 返回的 tool_calls"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        response = payload
        memory = shared["memory"]
        executor = shared["tool_executor"]

        tool_calls = executor.parse_tool_calls(response)
        results = executor.execute_all(tool_calls)

        for tool_call, result in zip(tool_calls, results):
            print(f"  [Tool] 执行: {tool_call.name}({tool_call.arguments})")
            print(f"  [Tool] 结果: {result.content[:100]}...")
            memory.add_message(result.to_message())

        return "chat", None

class OutputNode(Node):
    """输出助手回复"""

    def exec(self, payload: Any) -> Tuple[str, Any]:
        response = payload

        # 显示 reasoning_content 和 content
        resoning_content = response.get("reasoning_content", "")
        if resoning_content != "":
            print(f"\n🤖 Assistant reasoning: \033[1;34m{resoning_content}\033[0m")
        content = response.get("content", "")
        print(f"\n🤖 Assistant: {content}\n")
        return "default", None
    
def run_chat() -> None:
    """运行对话循环"""
    print("=" * 60)
    print("🤖 Chatbot with Memory")
    print("=" * 60)
    print("可用工具: read, search, bash, ls, grep, write等")
    print("记忆管理: 短期上下文 + 长期记忆 (自动压缩)")
    print("安全防护: 危险命令自动拦截，高风险操作需确认")
    print("输入 'quit' 或 'exit' 退出\n")

    shared.clear()

    guard = SafetyGuard()
    shared["memory"] = Memory()
    shared["tools"] = [t.to_llm_format() for t in get_tools()]
    shared["tool_executor"] = ToolExecutor(guard=guard)

    chat = ChatNode()
    tool_call = ToolCallNode()
    output = OutputNode()

    chat - "tool_call" >> tool_call
    tool_call - "chat" >> chat
    chat - "output" >> output

    while True:
        user_input = input("👤 You: ").strip()

        if user_input.lower() in ("quit", "exit", "q"):
            print("\n再见！")
            break

        if not user_input:
            continue

        shared["memory"].add_message({"role": "user", "content": user_input})
        flow = Flow(chat)
        flow.run(None)

if __name__ == "__main__":
    run_chat()