"""ChatGPT 后端（使用 OpenAI Responses API 调用 GPT 系列模型）。

适用场景：
- 通过中转代理调用 GPT 系列模型（如 gmn.chuangzuoli.com）
- 使用 /v1/responses 端点 + input 数组格式

配置项：CHATGPT_API_KEY、CHATGPT_BASE_URL、CHATGPT_MODEL、CHATGPT_MAX_TOKENS
"""
import json
import logging
from typing import Any

import httpx

from ai._types import AIMessage
from config import settings

logger = logging.getLogger(__name__)

_base_url = settings.chatgpt_base_url.rstrip("/") if settings.chatgpt_base_url else "https://api.openai.com"
if not _base_url.endswith("/v1"):
    _base_url += "/v1"

_api_url = f"{_base_url}/responses"

# 使用系统代理（trust_env=True），以应对 Cloudflare 等 CDN 拦截
_http_client = httpx.Client(
    trust_env=True,
    timeout=120.0,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.chatgpt_api_key}",
    },
)


# ── 内部工具函数 ─────────────────────────────────────────────────────────────

def _build_input(messages: list[dict]) -> list[dict]:
    """将标准 messages 格式转换为 Responses API 的 input 格式。

    标准格式: [{"role": "system"/"user"/"assistant", "content": "..."}]
    Responses API: [{"type": "message", "role": "developer"/"user"/"assistant",
                     "content": [{"type": "input_text", "text": "..."}]}]
    """
    result = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")

        # system → developer（Responses API 规范）
        if role == "system":
            role = "developer"

        result.append({
            "type": "message",
            "role": role,
            "content": [
                {"type": "input_text", "text": content}
            ],
        })
    return result


def _parse_response_text(data: dict) -> str | None:
    """从 Responses API 返回数据中提取文本内容。"""
    output = data.get("output", [])
    for item in output:
        if item.get("type") == "message":
            content = item.get("content", [])
            for block in content:
                if block.get("type") == "output_text":
                    return block.get("text", "")
    # 兜底：尝试直接取 output_text
    return data.get("output_text", None)


# ── 公共接口 ─────────────────────────────────────────────────────────────────

def call_once(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AIMessage:
    """单次调用，返回 AIMessage。"""
    payload: dict[str, Any] = {
        "model": settings.chatgpt_model,
        "input": _build_input(messages),
    }

    logger.debug("[chatgpt] 请求 %s，model=%s", _api_url, settings.chatgpt_model)
    response = _http_client.post(_api_url, json=payload)
    response.raise_for_status()
    data = response.json()

    text = _parse_response_text(data)
    logger.debug("[chatgpt] call_once 完成，content 长度=%d", len(text or ""))
    return AIMessage(content=text)


def call_with_tools(
    messages: list[dict],
    tools: list[dict],
    call_tool_fn,
    max_rounds: int = 5,
) -> str:
    """带工具调用循环的 AI 调用。

    注意：当前中转代理 Responses API 暂不支持工具调用，
    此函数仅做纯文本对话，忽略 tools 参数。
    如后续代理支持工具调用，可在此扩展。
    """
    msgs = list(messages)

    # 将工具定义追加到 system prompt 中，让 AI 以纯文本方式回复
    if tools:
        tool_desc = json.dumps(tools, ensure_ascii=False, indent=2)
        tool_hint = (
            "\n\n[可用工具（仅供参考，请直接以文本方式回复结论）]:\n"
            f"{tool_desc}"
        )
        # 将工具描述追加到 system 消息
        for m in msgs:
            if m.get("role") == "system":
                m["content"] = m.get("content", "") + tool_hint
                break
        else:
            msgs.insert(0, {"role": "system", "content": tool_hint})

    payload: dict[str, Any] = {
        "model": settings.chatgpt_model,
        "input": _build_input(msgs),
    }

    logger.debug("[chatgpt] call_with_tools 请求 %s", _api_url)
    response = _http_client.post(_api_url, json=payload)
    response.raise_for_status()
    data = response.json()

    text = _parse_response_text(data)
    logger.info("[chatgpt] call_with_tools 完成")
    return text or ""
