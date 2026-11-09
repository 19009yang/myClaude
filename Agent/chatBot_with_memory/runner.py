"""与终端和全局 shared 分离的聊天入口，用于嵌入和隔离 smoke。"""
import time
from concurrent.futures import CancelledError

from core.llm import call_llm
from core.lamemory.tools import get_memory_tools
from tools.executor import ToolExecutor


class ChatRunner:
    def __init__(self, memory, *, chat=call_llm, tools=None, system_prompt="你是一个具备长期记忆的助手。"):
        self.memory = memory
        self.chat = chat
        self.system_prompt = system_prompt
        self.executor = ToolExecutor(tools=[*(tools or []), *get_memory_tools(memory)], interactive=False)
        self.tools = [tool.to_llm_format() for tool in self.executor.tools]
        self.usage_events = []

    def send(self, content, *, max_rounds=8, timeout=60, on_event=None, cancelled=None):
        if max_rounds < 1 or timeout <= 0:
            raise ValueError("轮数和超时必须为正数")
        deadline = time.monotonic() + timeout
        emit = on_event or (lambda event, data: None)

        def check_cancelled():
            if cancelled is not None and cancelled():
                raise CancelledError("本轮已停止；已保存的原文和工具结果保留")

        check_cancelled()
        self.memory.append_message({"role": "user", "content": content})
        emit("message", {"role": "user", "content": content})
        for _ in range(max_rounds):
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("聊天轮次达到时间上限")
            context = self.memory.build_context(self.system_prompt, tools=self.tools)
            emit("context", context.diagnostics)
            emit("status", {"text": "正在调用模型"})
            response = self.chat(messages=context.messages, tools=self.tools, timeout=remaining,
                                  max_tokens=self.memory.config.reserved_output or None)
            self.usage_events.append({"purpose": "agent", "usage": response.get("usage", {})})
            try:
                self.memory.append_message(response)
                if not response.get("tool_calls"):
                    self.memory.commit_turn()
            except Exception as exc:
                emit("message", response)
                return {"message": response, "errors": [f"本轮未完整保存：{exc}"],
                        "memory_diagnostics": context.diagnostics}
            emit("message", response)
            if not response.get("tool_calls"):
                return {"message": response, "errors": [], "memory_diagnostics": context.diagnostics}
            calls = self.executor.parse_tool_calls(response)
            for call in calls:
                check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("执行工具前已达到聊天时间上限")
                emit("tool_start", {"id": call.id, "name": call.name, "arguments": call.arguments})
                result = self.executor.execute(call)
                self.memory.append_message(result.to_message())
                emit("tool_end", {"id": call.id, "name": call.name, "content": result.content,
                                  "is_error": result.is_error})
        raise RuntimeError("工具调用达到轮数上限；原始消息仍已留档")

    def close(self):
        self.memory.close()
