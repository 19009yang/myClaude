from typing import Any, Dict, Tuple
from pathlib import Path
from core.llm import call_llm 
import json

MEMORY_FILEPATH = Path(r"core\chat_memory\session.jsonl")                                # 对话记忆文件存储路径（jsonl格式）
LONG_TERM_MEMORY_FILEPATH = Path(r"core\chat_memory\MEMORY.md")                          # 长期记忆文件存储路径（md格式）
MAX_CONTEXT_LENGTH = 128_000                                                          # 大模型最大上下文窗口大小（按token计算）
COMPRESS_THRESHOLD = 0.9                                                              # 摘要压缩阈值（达到阈值后自动摘要压缩）
KEEP_MESSAGES_ON_COMPRESS = 4                                                         # 摘要压缩对话之后保留的最近消息条数
LONG_TERM_MEMORY_HEADER = "# 长期记忆：包括用户偏好、重要事件、运行环境等等\n\n"            # MEMORY.md文件的标题
MESSAGE_KEYS = {"role", "content", "tool_calls", "tool_call_id", "reasoning_content"} # message字典中可出现的所有key值

COMPRESS_PROMPT = """
  {long_term_part}请压缩以上对话历史为一段摘要，并判断是否有值得长期记住的新信息（用户偏好、关键事实、运行
  环境等）。
  注意排除已在长期记忆中的内容，避免重复。
  对话中可能包含工具调用过程，摘要时只需保留调用目的和结果，忽略调用细节。
  摘要用中文撰写，控制在200字以内，保留关键信息和意图，忽略闲聊。

  直接返回纯 JSON 文本，不要用 json 代码块包裹，不要添加任何额外文字说明。
  JSON 格式示例：
  {{\"summary\": \"对话摘要内容\", \"memory_update\": [\"新增记忆项1\", \"新增记忆项2\"]}}
  若无新的长期记忆信息，memory_update 返回空数组 []。
  """

MEMORY_EXTRACT_PROMPT = """
  根据以下这轮对话，判断是否有值得长期记住的新信息（用户偏好、关键事实、重要决策等）。
  排除已在长期记忆中的内容。

  已有长期记忆：
  {long_term_memory}

  直接返回纯 JSON，不要用代码块包裹：
  {{\"memory_update\": [\"记忆项1\", \"记忆项2\"]}}
  若无新信息，返回 {{\"memory_update\": []}}
  """

class Memory:
    def __init__(self, memory_type: str = "memory", memory_path: str = None):
        """初始化记忆文件，并把已有的 session.jsonl 读回内存。"""
        MEMORY_FILEPATH.parent.mkdir(parents=True, exist_ok=True)
        LONG_TERM_MEMORY_FILEPATH.parent.mkdir(parents=True, exist_ok=True)

        #如果长期记忆文件不存在，就创建它并写入初始头部内容
        if not LONG_TERM_MEMORY_FILEPATH.exists():
            LONG_TERM_MEMORY_FILEPATH.write_text(LONG_TERM_MEMORY_HEADER, encoding="utf-8")
        
        self.messages: list[dict[str, Any]] = []

        #从记忆文件中逐行读取 JSON 数据，加载到内存列表中
        if MEMORY_FILEPATH.exists():
            for line in MEMORY_FILEPATH.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self.messages.append(json.loads(line))  #将每一行 JSON 字符串解析为 Python 对象（字典、列表等）
                except json.JSONDecodeError:  #如果某一行不是合法的 JSON，跳过它，不中断整个加载过程
                    pass

        # 上次崩溃如果停在 tool 调用中间，就丢掉这轮未完成消息
        # user（提问）
        # → assistant（决定调用工具，带 tool_calls）
        #   → tool（工具返回结果 1）
        #   → tool（工具返回结果 2）
        #     → assistant（根据工具结果，给出最终回复）  ← 关键：必须有这条！

        need_rewrite = False

        for index in range(len(self.messages) - 1, -1, -1): #从最后一条消息开始，向前遍历消息列表
            message = self.messages[index] 
            #如果最后一天消息是LLM消息，并且没有工具调用，就继续向前找，直到找到最后一条LLM消息，并检查它之后的消息是否都是工具调用，如果不是，就保留这些消息         
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue

            # 找到最后一条LLM消息，并检查它之后的消息是否都是工具调用，如果不是，就保留这些消息
            tail = self.messages[index + 1:]
            if tail and not all(item.get("role") == "tool" for item in tail):
                break
            
            # 如果前一条消息是用户消息，就把它也删除掉，因为这条用户消息可能是触发工具调用的
            start = index - 1 if index > 0 and self.messages[index - 1].get("role") == "user" else index

            # 删除从 start 到最后的消息，保留前面的消息
            del self.messages[start:]
            need_rewrite = True
            break
        
        #当消息被清理后，把修正后的消息列表重新写入记忆文件
        if need_rewrite:
            with MEMORY_FILEPATH.open("w", encoding="utf-8") as f:
                for message in self.messages:
                    f.write(json.dumps(message, ensure_ascii=False) + "\n")
    def get_last_turn_messages(self) -> list[dict]:
        """从 self.messages 中回溯提取最近一轮完整对话的所有消息。
        一轮对话的边界是：从最后一条 user 消息开始，到当前最后一条消息为止。
        包括中间的 tool_calls / tool 交互过程。
        """
        # 从末尾向前找到最近一条user消息的位置
        for i in range(len(self.messages) - 1, -1, -1):
            if self.messages[i].get("role") == "user":
                return self.messages[i:]
        return self.messages[-1:] # 如果没有找到 user 消息，就返回整个消息列表

    def extract_memory_update(self, messages: list[dict]) :
        """每轮对话后，尝试提取值得长期记住的信息。"""

        # 检查长期记忆文件是否有实质内容，如果只有头部标识就视为空
        long_term = LONG_TERM_MEMORY_FILEPATH.read_text(encoding="utf-8").strip()
        if long_term == LONG_TERM_MEMORY_HEADER.strip():
          long_term = "无"

        # 只传最近的一轮消息给LLM，避免上下文过长导致 token 超限
        response = call_llm(messages=[
            *messages,
            {"role": "user", "content": MEMORY_EXTRACT_PROMPT.format(
                long_term_memory=long_term,
            )}
        ])
        
        # 写入长期记忆文件
        try:
            result = json.loads(response.get("content", ""))
            memory_update = result.get("memory_update", [])
        except json.JSONDecodeError:
            return
        
        if memory_update:
            with LONG_TERM_MEMORY_FILEPATH.open("a", encoding="utf-8") as f:
                for item in memory_update:
                    f.write(f"\n- {item}")

    def add_message(self, message: dict[str, Any]):
            """添加一条 message，写入 session.jsonl，并在最终助手回复后按需压缩。"""
            total_tokens = message.get("usage", {}).get("total_tokens", 0)

            # 如果 total_tokens 大于 0 且 message 中没有工具调用，则需要压缩
            should_compress = total_tokens > 0 and not message.get("tool_calls")

            # 只保留 message 字典中指定的 key，过滤掉其他不需要的 key
            message = {key: value for key, value in message.items() if key in MESSAGE_KEYS}
            
            self.messages.append(message)
            with MEMORY_FILEPATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

            # 一轮对话结束（assistant 最终回复无 tool_calls）先提取长期记忆，再判断是否需要压缩
            if should_compress:
                self.extract_memory_update(self.get_last_turn_messages())
                self.compress(total_tokens)

    def build_context(self, system_prompt: str = "") -> list[dict[str, Any]]:
        """组装传给 LLM 的 messages，必要时把长期记忆放进 system prompt。"""
        if not system_prompt: #如果没有提供 system_prompt，就直接返回当前的消息列表
            return list(self.messages)

        # 判断长期记忆文件是否有实质内容，如果只有头部标识就视为空
        long_term_memory = LONG_TERM_MEMORY_FILEPATH.read_text(encoding="utf-8").strip()
        if long_term_memory == LONG_TERM_MEMORY_HEADER.strip():
            long_term_memory = ""

        # 组装system_message字典，包含角色和内容，如果有长期记忆，就把它加到内容中
        system_message = {"role": "system", "content": system_prompt}
        if long_term_memory:
            system_message["content"] += f"\n\n长期记忆：\n{long_term_memory}"

        # 如果消息列表中第一条消息是系统消息，就把它的内容加到system_message中，并返回system_message和剩余的消息列表
        if self.messages and self.messages[0].get("role") == "system":
            system_message["content"] += f"\n\n{self.messages[0]['content']}"
            return [system_message, *self.messages[1:]]

        # 否则，就返回system_message和整个消息列表
        return [system_message, *self.messages]

    def compress(self, total_tokens: int):
        """当上下文接近上限时，把较早消息压缩成摘要，并保留最近几条消息。"""
        if total_tokens < MAX_CONTEXT_LENGTH * COMPRESS_THRESHOLD:
            return  # 如果当前消息的 token 数量没有达到压缩阈值，就不进行压缩
        if len(self.messages) <= KEEP_MESSAGES_ON_COMPRESS:
            return  # 如果消息数量已经小于等于保留的消息条数，就不进行压缩
        
        # 计算需要保留的消息索引，确保不会出现负数索引
        split_index = max(0, len(self.messages) - KEEP_MESSAGES_ON_COMPRESS)

        # 避免把 assistant tool_calls 和后续 tool 结果拆到摘要边界两边。
        while split_index > 0 and self.messages[split_index].get("role") == "tool":
            split_index -= 1

        # 避免把 assistant tool_calls 和前一条 user 消息拆到摘要边界两边。
        if (
            split_index > 0
            and self.messages[split_index].get("role") == "assistant"
            and self.messages[split_index].get("tool_calls")
            and self.messages[split_index - 1].get("role") == "user"
        ):
            split_index -= 1

        # 生成摘要消息
        old_messages = self.messages[:split_index]
        recent_messages = self.messages[split_index:]
        if not old_messages:
            return
        
        # 判断是否有长期记忆内容，如果有，就把它加到摘要消息中
        long_term_memory = LONG_TERM_MEMORY_FILEPATH.read_text(encoding="utf-8").strip()
        if long_term_memory == LONG_TERM_MEMORY_HEADER.strip():
            long_term_memory = "无"
        
        # 生成摘要消息的提示语，包含长期记忆内容（如果有的话）
        long_term_part = f"已有长期记忆：\n{long_term_memory}\n\n" if long_term_memory != "无" else ""
        response = call_llm(messages=[
            *old_messages, #把old_messages拆开成单独的字典，作为消息列表传给LLM
            {
                "role": "user",
                "content": COMPRESS_PROMPT.format(long_term_part=long_term_part)
                ,
            },
        ])
        
        try:
            result = json.loads(response.get("content", ""))
            summary = result.get("summary", "")
            memory_update = result.get("memory_update", "")
        except json.JSONDecodeError:
            summary = response.get("content", "")
            memory_update = ""
        
        # 生成新的消息列表，包含摘要消息和最近的消息
        self.messages = [{"role": "system", "content": f"对话历史摘要：\n{summary}"}, *recent_messages]
        # 将新的消息列表写入记忆文件
        with MEMORY_FILEPATH.open("w", encoding="utf-8") as f:
            for message in self.messages:
                f.write(json.dumps(message, ensure_ascii=False) + "\n")

        # 如果有新的长期记忆内容，就把它写入长期记忆文件
        if memory_update:
            with LONG_TERM_MEMORY_FILEPATH.open("a", encoding="utf-8") as f:
                f.write("\n" + memory_update)





if __name__ == "__main__":
    memory = Memory()
