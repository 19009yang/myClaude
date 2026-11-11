"""与终端和全局 shared 分离的聊天入口，用于嵌入和隔离 smoke。"""
import time
from concurrent.futures import CancelledError

from core.llm import call_llm
from core.providers import ProviderError
from core.lamemory.integration.tools import get_memory_tools
from tools.executor import ToolExecutor, ToolResult


class ChatRunner:
    def __init__(self, memory, *, chat=call_llm, tools=None, system_prompt="你是一个具备长期记忆的助手。",
                 memory_tools=True):
        self.memory = memory
        self.chat = chat
        self.system_prompt = system_prompt
        self.executor = ToolExecutor(tools=[*(tools or []), *(get_memory_tools(memory) if memory_tools else [])],
                                     interactive=False)
        self.tools = [tool.to_llm_format() for tool in self.executor.tools]
        self.usage_events = []

    def send(self, content, *, max_rounds=8, timeout=60, on_event=None, cancelled=None,
             max_tool_calls=None, response_retries=1):
        if max_rounds < 1 or timeout <= 0:
            raise ValueError("轮数和超时必须为正数")
        if max_tool_calls is not None and max_tool_calls < 1:
            raise ValueError("工具调用上限必须为正数")
        if type(response_retries) is not int or not 0 <= response_retries <= 3:
            raise ValueError("response_retries 必须为 0 到 3")
        invalid_responses = 0
        deadline = time.monotonic() + timeout
        tool_count = 0
        emit = on_event or (lambda event, data: None)

        def check_cancelled():
            if cancelled is not None and cancelled():
                raise CancelledError("本轮已停止；已保存的原文和工具结果保留")

        check_cancelled()
        self.memory.append_message({"role": "user", "content": content})
        emit("message", {"role": "user", "content": content})
        for call_index in range(max_rounds):
            check_cancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("聊天轮次达到时间上限")
            calls_left = max_rounds - call_index
            closing = (calls_left == 1 or (max_tool_calls is not None and tool_count >= max_tool_calls)
                       or remaining <= min(10, timeout * 0.1))
            budget_instruction = f"\n本轮模型调用还剩 {calls_left} 次（含本次），已执行 {tool_count} 次工具调用。"
            if closing:
                budget_instruction += ("这是本轮收尾回复，不再提供工具。依据已保存的产物和已有证据给简短总结；"
                                       "未知事实保留待确认，尚未完成或未保存的部分明确说明，不声称已经完成。")
            elif calls_left == 2:
                budget_instruction += ("这是本轮最后一次可用工具的机会。完成必要的文件写入；"
                                       "不要重复已经通过的校验，下一次只做最终总结。")
            active_tools = [] if closing else self.tools
            if callable(getattr(self.memory, 'set_context_deadline', None)):
                self.memory.set_context_deadline(deadline)
            context = self.memory.build_context(self.system_prompt + budget_instruction, tools=active_tools)
            emit("context", context.diagnostics)
            emit("status", {"text": "正在调用模型"})
            emit("call_budget", {"model_calls_left": calls_left, "tool_calls_used": tool_count, "closing": closing})
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("构造上下文后已达到聊天时间上限")
            try:
                response = self.chat(messages=context.messages, tools=active_tools, timeout=remaining,
                                     max_tokens=self.memory.config.reserved_output or None)
            except ProviderError as exc:
                retry = (exc.kind == "invalid_response" and exc.retryable and invalid_responses < response_retries
                         and call_index + 1 < max_rounds)
                emit("provider_error", {"kind": exc.kind, "diagnostics": exc.diagnostics, "retry": retry})
                if not retry:
                    raise
                invalid_responses += 1
                # No assistant message or tool side effect from a rejected response is committed.
                continue
            self.usage_events.append({"purpose": "agent", "usage": response.get("usage", {})})
            if time.monotonic() > deadline:
                raise TimeoutError("模型响应超过聊天时间上限")
            if closing and (response.get("tool_calls") or not (response.get("content") or "").strip()):
                emit("message", response)
                raise RuntimeError("收尾回复必须为非空文本且不能调用工具；未执行额外工具")
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
                return {"message": response, "errors": [], "memory_diagnostics": context.diagnostics,
                        "completion_reason": "budget_closeout" if closing else "natural"}
            calls = self.executor.parse_tool_calls(response)
            for call in calls:
                check_cancelled()
                if max_tool_calls is not None and tool_count >= max_tool_calls:
                    result = ToolResult(call.id, "工具预算已用尽，本次调用未执行；请基于已保存产物收尾。", is_error=True)
                    self.memory.append_message(result.to_message())
                    emit("tool_end", {"id": call.id, "name": call.name, "content": result.content,
                                      "is_error": True, "executed": False, "reason": "tool_budget"})
                    continue
                if time.monotonic() >= deadline:
                    raise TimeoutError("执行工具前已达到聊天时间上限")
                tool_count += 1
                emit("tool_start", {"id": call.id, "name": call.name, "arguments": call.arguments})
                result = self.executor.execute(call)
                self.memory.append_message(result.to_message())
                emit("tool_end", {"id": call.id, "name": call.name, "content": result.content,
                                  "is_error": result.is_error})
        raise RuntimeError("工具调用达到轮数上限；原始消息仍已留档")

    def close(self):
        self.memory.close()
