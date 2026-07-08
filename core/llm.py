from __future__ import annotations
import os
from typing import Any
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

def call_llm_chat(prompt:str):
    """
    简单文本生成接口：输入 prompt，返回字符串。
    """
    system_prompt="You are a helpful assistant"
    client = OpenAI(
    api_key=os.environ.get('LLM_API_KEY'),
    base_url=os.environ.get('LLM_BASE_URL'))

    response = client.chat.completions.create(
        model=os.environ.get('LLM_MODEL_NAME'),
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]
    )
    message=response.choices[0].message.content
    return message or ''


def call_llm(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
)->dict[str,Any]:
    """
    消息/工具模式接口：返回 assistant message 字典。
    """

    msgs = list(messages)
    if system_prompt:
        msgs=[{"role":"system","content":system_prompt}]+msgs
        #msgs = [{"role": "system", "content": system_prompt}, *msgs]
    
    kwargs: dict[str, Any] = {
        "model": os.environ.get("LLM_MODEL_NAME"),
        "messages": msgs,
    }

    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    client = OpenAI(
    api_key=os.environ.get('LLM_API_KEY'),
    base_url=os.environ.get('LLM_BASE_URL'))

    response = client.chat.completions.create(**kwargs)
    message = response.choices[0].message

    result: dict[str, Any] = {
        "role": "assistant",
        "content": message.content or "",
        "usage": {
            "total_tokens": response.usage.total_tokens,
            "prompt_tokens": response.usage.prompt_tokens,
            "completion_tokens": response.usage.completion_tokens,
        },
    }

    #提取模型思考过程（如有）
    reasoning_content = getattr(message, "reasoning_content", None)

    if reasoning_content:
        result["reasoning_content"] = reasoning_content
    if message.tool_calls:
        # .model_dump() 转成普通字典 — 可以序列化、可以传给 API
        result["tool_calls"] = [tool_call.model_dump() for tool_call in message.tool_calls]
    return result






if __name__ == "__main__":
    print("Basic:", call_llm_chat("hi"))
    print("Full:",call_llm([{"role":"user","content":"hi"}],None,"You are a cat"))