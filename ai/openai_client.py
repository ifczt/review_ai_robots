"""OpenAI 兼容格式后端（支持中转代理）。

适用场景：
- OpenAI 官方 API
- 任何 OpenAI 兼容的中转服务
- 通过中转调用 Claude（如 api_base_url 指向兼容层）

配置项：OPENAI_API_KEY、OPENAI_BASE_URL
"""
import logging
from typing import Any

from openai import OpenAI

from ai._types import AIMessage
from config import settings

logger = logging.getLogger(__name__)

_base_url = settings.openai_base_url.rstrip("/")
if _base_url and not _base_url.endswith("/v1"):
    _base_url += "/v1"

_client = OpenAI(
    api_key=settings.openai_api_key,
    base_url=_base_url or None,
)


def call_once(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AIMessage:
    """单次调用，返回 AIMessage。"""
    kwargs: dict[str, Any] = {
        "model": settings.claude_model,
        "max_tokens": settings.claude_max_tokens,
        "messages": messages,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"

    response = _client.chat.completions.create(**kwargs)
    msg = response.choices[0].message
    logger.debug("[openai] call_once 完成，content 长度=%d", len(msg.content or ""))
    return AIMessage(content=msg.content)


def call_with_tools(
    messages: list[dict],
    tools: list[dict],
    call_tool_fn,
    max_rounds: int = 5,
) -> str:
    """带工具调用循环的 AI 调用。

    工具格式：OpenAI function calling schema。
    call_tool_fn(name: str, arguments: str) -> str，arguments 为 JSON 字符串。
    返回 AI 最终文本回复。
    """
    msgs = list(messages)

    for round_num in range(max_rounds):
        kwargs: dict[str, Any] = {
            "model": settings.claude_model,
            "max_tokens": settings.claude_max_tokens,
            "messages": msgs,
            "tools": tools,
            "tool_choice": "auto",
        }
        response = _client.chat.completions.create(**kwargs)
        message = response.choices[0].message

        if message.tool_calls:
            logger.info("[openai] 第 %d 轮，调用 %d 个工具", round_num + 1, len(message.tool_calls))
            msgs.append(message)
            for tc in message.tool_calls:
                logger.info("[openai] 工具: %s 参数: %s", tc.function.name, tc.function.arguments)
                result = call_tool_fn(tc.function.name, tc.function.arguments)
                logger.info("[openai] 结果: %s", result[:200])
                msgs.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            continue

        logger.info("[openai] 第 %d 轮，AI 给出最终结论", round_num + 1)
        return message.content or ""

    return "[REJECT]\nAI 工具调用轮次超限，请人工处理。"
