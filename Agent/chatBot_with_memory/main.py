from __future__ import annotations
from typing import Any,Dict,Tuple
from tools import get_tools, ToolExecutor, SafetyGuard
from tools.skill_loader import get_default_registry
from core import  call_llm, Node, Flow, shared, Memory
from core.lamemory import LaMemoryConfig
from core.lamemory.tools import get_memory_tools
from core.providers import ModelConfig
from core.lamemory.runtime import maintain_embeddings

"""
带有长期记忆和工具调用的对话机器人工作流程(带skill激活功能)
"""

_registry = get_default_registry()
_skill_summaries = _registry.skill_summaries_text()

SYSTEM_PROMPT = """
你是一个具备工具调用、SKill加载和长期记忆能力的智能助手。

## 可用工具
- search: 搜索最新信息、新闻、产品发布时间等
- read / grep / ls: 读取和检索本地文件与代码
- bash: 执行终端命令
- write: 写入或创建文件
- activate_skill: 激活一个 Skill，获取其完整操作指南

## 工具使用策略
- 涉及最新信息、事实核验时，优先调用 search，基于搜索结果回答
- 涉及本地文件/代码时，优先使用 read/grep/ls/bash
- 工具调用失败时，尝试换一种方式或直接告知用户原因

## Skill 系统
当用户的需求匹配某个 Skill 的功能时，你应该调用 activate_skill(name) 激活该 Skill。
激活后你会收到该 Skill 的完整操作指南（包含任务目标、操作步骤、注意事项等），请严格按照指南中的步骤执行。
如果不确定用户需求是否匹配某个 Skill，可以先查看下方 Skill 摘要列表再做判断。

""" + (_skill_summaries + "\n" if _skill_summaries else "") + """
## 安全约束
你不得主动执行以下操作：递归删除大量文件、修改系统目录、提权操作、向外发送用户数据。
如果用户要求执行可能有害的操作，先警告风险并确认意图后再操作。
系统已内置安全策略，危险命令会被自动拦截。

## 长期记忆
你拥有长期记忆，会自动记住用户偏好和重要事实。回答时参考长期记忆中的信息，并在不确定时主动确认。
用户要求记住、更正或忘记时，使用 lamemory_store/update/forget，并核实工具返回状态后说明结果。
可用 lamemory_search 查询记忆和原始来源 ID；conflict 表示待裁决，不代表新事实已经生效。
记忆可限定 user/session/project 范围，project 需要启动时绑定项目。可使用 lamemory_experience_search
查询已验证经验，按适用条件复用并核实当前结果；错误经验可按用户要求撤销。

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
        messages = memory.build_context(system_prompt=SYSTEM_PROMPT, tools=tools)
        chat = shared.get("chat", call_llm)
        assistant_message = chat(messages=messages, tools=tools,
                                 max_tokens=memory.service.config.reserved_output or None)
        try:
            memory.add_message(assistant_message)
        except Exception as exc:
            # Storage failures must never trigger regeneration of an already generated reply.
            print(f"记忆保存失败，本轮未完整保存：{exc}")
            if assistant_message.get("tool_calls"):
                return "output", {"content": "工具调用消息未能保存，本轮工具未执行。", "role": "assistant"}

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
            try:
                memory.add_message(result.to_message())
            except Exception as exc:
                return "output", {"role": "assistant", "content": f"工具已执行，但结果留档失败，本轮停止：{exc}"}

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
        memory = shared["memory"]
        if memory.service.config.auto_process_jobs:
            try:
                for job in memory.process_pending(max_jobs=1):
                    if job["status"] != "completed":
                        print(f"记忆提取任务 {job['status']}，可稍后重试：{job['last_error']}")
            except Exception as exc:
                print(f"记忆任务处理失败，可稍后重试：{exc}")
        try:
            status = maintain_embeddings(memory.service)
            if status and (status["remaining"] or status["missing"]):
                print(f"语义索引尚有积压：{status}")
        except Exception as exc:
            print(f"语义索引维护失败，可稍后重试：{type(exc).__name__}")
        return "default", None
    
def run_chat(*, database_path=None, user_id="local-user", session_id=None, config=None, provider=None,
             model_config=None, embedding_provider=None, reranker=None, env=None,
             retrieval_mode=None, rerank=None) -> None:
    """运行对话循环"""
    print("=" * 60)
    print("🤖 Chatbot")
    print("=" * 60)
    print("工具调用: 可调用 search/read/grep/ls/bash/write等工具")
    print("技能激活: 可调用 activate_skill(name) 激活 Skill，获取完整操作指南")
    print("记忆管理: LaMemory 检索 + 原文留档 + 主动保存/更正/遗忘")
    print("安全防护: 危险命令自动拦截，高风险操作需确认")
    print("输入 'quit' 或 'exit' 退出\n")

    shared.clear()
    from functools import partial
    shared["chat"] = partial(call_llm, config=model_config) if model_config is not None else call_llm

    guard = SafetyGuard()
    shared["memory"] = Memory(database_path=database_path, user_id=user_id, session_id=session_id,
                               config=config, provider=provider, env=env, embedding_provider=embedding_provider,
                               reranker=reranker, retrieval_mode=retrieval_mode, rerank=rerank)
    print(f"当前会话: {shared['memory'].service.session_id}")
    instance_tools = [*get_tools(), *get_memory_tools(shared["memory"].service)]
    shared["tool_executor"] = ToolExecutor(guard=guard, tools=instance_tools)
    shared["tools"] = [t.to_llm_format() for t in shared["tool_executor"].tools]

    chat = ChatNode()
    tool_call = ToolCallNode()
    output = OutputNode()

    chat - "tool_call" >> tool_call
    tool_call - "chat" >> chat
    tool_call - "output" >> output
    chat - "output" >> output

    try:
        while True:
            try:
                user_input = input("👤 You: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if user_input.lower() in ("quit", "exit", "q"):
                print("\n再见！")
                break
            if not user_input:
                continue
            try:
                shared["memory"].add_message({"role": "user", "content": user_input})
                Flow(chat).run(None)
            except Exception as exc:
                print(f"本轮未完成：{exc}")
    finally:
        shared["memory"].close()

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="LaMemory 对话助手")
    parser.add_argument("--database")
    parser.add_argument("--user", default="local-user")
    parser.add_argument("--session", help="留空创建新会话，指定已有 ID 恢复会话")
    parser.add_argument("--project", help="绑定固定项目标签")
    parser.add_argument("--retrieval-mode", choices=["keyword", "hybrid"], default=None)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--learn-experiences", action="store_true", help="完成轮次后投递经验候选提取任务")
    parser.add_argument("--provider", help="openai / anthropic / deepseek / qwen / openai-compatible")
    parser.add_argument("--model", help="覆盖 LLM_MODEL_NAME")
    parser.add_argument("--base-url", help="覆盖供应商默认端点")
    parser.add_argument("--context-window", type=int)
    parser.add_argument("--reserved-output", type=int)
    args = parser.parse_args()
    from dataclasses import replace
    model_config = ModelConfig.from_env(model=args.model, provider=args.provider, require_key=False)
    if args.base_url:
        model_config = replace(model_config, base_url=args.base_url)
    run_chat(database_path=args.database, user_id=args.user, session_id=args.session, model_config=model_config,
             retrieval_mode=args.retrieval_mode, rerank=args.rerank,
             config=LaMemoryConfig(context_window=args.context_window or model_config.context_window or 32768,
                                   project_id=args.project, auto_learn_experiences=args.learn_experiences,
                                   reserved_output=args.reserved_output if args.reserved_output is not None else model_config.max_tokens))
