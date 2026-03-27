"""Anthropic 原生 SDK 后端（推荐）。

适用场景：
- Anthropic 官方 API
- 支持 base_url 的中转代理（如 api.cooker.club）

配置项：ANTHROPIC_API_KEY、ANTHROPIC_BASE_URL
"""
import json
import logging

import anthropic
import httpx

from ai._types import AIMessage
from config import settings

logger = logging.getLogger(__name__)

# 有自定义 base_url（中转代理）时禁用系统代理，避免二次转发；
# 无 base_url 时保留系统代理，确保能通过本机代理访问 api.anthropic.com
_use_custom_base = bool(settings.anthropic_base_url)
_http_client = httpx.Client(trust_env=not _use_custom_base)

_client = anthropic.Anthropic(
    auth_token=settings.anthropic_api_key,
    base_url="https://api.cooker.club"
)

# ── 内部工具函数 ─────────────────────────────────────────────────────────────

def _extract_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """从消息列表中提取 system prompt，返回 (system_text, 剩余消息)。

    Anthropic API 要求 system 作为独立参数，不能混入 messages 列表。
    """
    system = ""
    msgs = []
    for m in messages:
        if m.get("role") == "system":
            system = m.get("content", "")
        else:
            msgs.append(m)
    return system, msgs


def _to_anthropic_tools(openai_tools: list[dict]) -> list[dict]:
    """将 OpenAI function calling schema 转换为 Anthropic tools schema。

    OpenAI 格式：
      {"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}}}
    Anthropic 格式：
      {"name": ..., "description": ..., "input_schema": {...}}
    """
    result = []
    for t in openai_tools:
        fn = t.get("function", t)  # 兼容已是 function dict 的情况
        result.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


# ── 公共接口 ─────────────────────────────────────────────────────────────────

def call_once(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AIMessage:
    """单次调用，返回 AIMessage。"""
    system, msgs = _extract_system(messages)

    kwargs: dict = {
        "model": settings.claude_model,
        "max_tokens": settings.claude_max_tokens,
        "messages": msgs,
    }
    if system:
        kwargs["system"] = system
    if tools:
        kwargs["tools"] = _to_anthropic_tools(tools)
        kwargs["tool_choice"] = {"type": "auto"}

    response = _client.messages.create(**kwargs)

    text = next((b.text for b in response.content if b.type == "text"), None)
    logger.debug("[anthropic] call_once 完成，content 长度=%d", len(text or ""))
    return AIMessage(content=text)


def call_with_tools(
    messages: list[dict],
    tools: list[dict],
    call_tool_fn,
    max_rounds: int = 5,
) -> str:
    """带工具调用循环的 AI 调用。

    工具格式：传入 OpenAI schema，内部自动转换为 Anthropic 格式。
    call_tool_fn(name: str, arguments: str) -> str，arguments 为 JSON 字符串。
    返回 AI 最终文本回复。
    """
    system, msgs = _extract_system(messages)
    anthropic_tools = _to_anthropic_tools(tools)

    for round_num in range(max_rounds):
        response = _client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.claude_max_tokens,
            system=system,
            messages=msgs,
            tools=anthropic_tools,
            tool_choice={"type": "auto"},
        )

        tool_uses = [b for b in response.content if b.type == "tool_use"]
        text_blocks = [b for b in response.content if b.type == "text"]

        if tool_uses:
            logger.info("[anthropic] 第 %d 轮，调用 %d 个工具", round_num + 1, len(tool_uses))

            # 将 assistant 的 tool_use 内容追加到对话
            msgs.append({
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": tu.id, "name": tu.name, "input": tu.input}
                    for tu in tool_uses
                ],
            })

            # 执行工具，将所有结果打包为单条 user 消息（Anthropic 规范）
            tool_results = []
            for tu in tool_uses:
                logger.info("[anthropic] 工具: %s 参数: %s", tu.name, tu.input)
                result = call_tool_fn(tu.name, json.dumps(tu.input, ensure_ascii=False))
                logger.info("[anthropic] 结果: %s", result[:200])
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": result,
                })
            msgs.append({"role": "user", "content": tool_results})
            continue

        logger.info("[anthropic] 第 %d 轮，AI 给出最终结论", round_num + 1)
        return text_blocks[0].text if text_blocks else ""

    return "[REJECT]\nAI 工具调用轮次超限，请人工处理。"
