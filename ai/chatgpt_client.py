"""ChatGPT 后端。

当前中转会在非流式 Responses API 返回里丢失 output 内容，
但在 SSE 事件流里仍会完整返回文本，因此这里统一走 stream 模式聚合结果。

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

_http_client = httpx.Client(
    trust_env=True,
    timeout=120.0,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {settings.chatgpt_api_key}",
    },
)


def _extract_instructions(messages: list[dict]) -> tuple[str, list[dict]]:
    """提取 system prompt，映射到 Responses API 的 instructions。"""
    instructions: list[str] = []
    rest_messages: list[dict] = []

    for message in messages:
        if message.get("role") == "system":
            content = message.get("content", "")
            if content:
                instructions.append(content)
            continue
        rest_messages.append(message)

    return "\n\n".join(instructions), rest_messages


def _build_input(messages: list[dict]) -> list[dict]:
    """将非 system 消息转成 Responses API 的 input。"""
    result = []
    for message in messages:
        role = message.get("role", "user")

        result.append(
            {
                "type": "message",
                "role": role,
                "content": [{"type": "input_text", "text": message.get("content", "")}],
            }
        )
    return result


def _parse_response_text(data: dict[str, Any]) -> str | None:
    """从最终 completed 响应里提取文本，兼容标准 Responses API 格式。"""
    output = data.get("output", [])
    for item in output:
        if item.get("type") != "message":
            continue
        for block in item.get("content", []):
            if block.get("type") == "output_text":
                return block.get("text", "")
    return data.get("output_text")


def _raise_if_empty_response(data: dict[str, Any], streamed_text: str = "") -> None:
    """响应没有可用文本时记录关键信息。"""
    response_id = data.get("id", "?")
    model = data.get("model", "?")
    status = data.get("status", "?")
    usage = data.get("usage", {})
    output_len = len(data.get("output", []))
    logger.error(
        "[chatgpt] empty response id=%s model=%s status=%s output_len=%s usage=%s streamed_len=%s raw=%s",
        response_id,
        model,
        status,
        output_len,
        usage,
        len(streamed_text),
        json.dumps(data, ensure_ascii=False)[:1000],
    )
    raise ValueError(
        "ChatGPT 响应为空（id={}, model={}, status={}）".format(
            response_id, model, status
        )
    )


def _consume_stream(response: httpx.Response) -> tuple[str, dict[str, Any]]:
    """解析 SSE 响应，聚合最终文本和 completed 响应体。"""
    event_name = ""
    data_lines: list[str] = []
    deltas: list[str] = []
    done_text = ""
    completed_response: dict[str, Any] = {}
    last_payload: dict[str, Any] = {}

    def flush_event() -> None:
        nonlocal event_name, data_lines, done_text, completed_response, last_payload

        if not data_lines:
            event_name = ""
            return

        raw_data = "\n".join(data_lines)
        data_lines = []

        if raw_data == "[DONE]":
            event_name = ""
            return

        payload = json.loads(raw_data)
        last_payload = payload
        event_type = payload.get("type") or event_name

        if event_type == "response.output_text.delta":
            delta = payload.get("delta", "")
            if delta:
                deltas.append(delta)
        elif event_type == "response.output_text.done":
            done_text = payload.get("text", "") or ""
        elif event_type == "response.completed":
            completed_response = payload.get("response", {}) or {}
        elif event_type == "response.failed":
            error = payload.get("response", {}).get("error") or payload.get("error") or payload
            raise RuntimeError(f"ChatGPT 流式调用失败: {error}")

        event_name = ""

    for raw_line in response.iter_lines():
        if raw_line is None:
            continue

        line = raw_line.strip()
        if not line:
            flush_event()
            continue

        if line.startswith("event:"):
            event_name = line[6:].strip()
            continue

        if line.startswith("data:"):
            data_lines.append(line[5:].strip())

    flush_event()

    final_text = done_text or "".join(deltas)
    final_response = completed_response or last_payload

    if not final_text and final_response:
        final_text = _parse_response_text(final_response) or ""

    return final_text, final_response


def _request_text(messages: list[dict]) -> tuple[str, dict[str, Any]]:
    instructions, input_messages = _extract_instructions(messages)
    payload: dict[str, Any] = {
        "model": settings.chatgpt_model,
        "input": _build_input(input_messages),
        "stream": True,
    }
    if instructions:
        payload["instructions"] = instructions
    if settings.chatgpt_max_tokens > 0:
        payload["max_output_tokens"] = settings.chatgpt_max_tokens

    logger.debug("[chatgpt] 请求 %s，model=%s", _api_url, settings.chatgpt_model)
    with _http_client.stream("POST", _api_url, json=payload) as response:
        response.raise_for_status()
        text, data = _consume_stream(response)

    if not text or not text.strip():
        _raise_if_empty_response(data, streamed_text=text)
    return text, data


def call_once(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> AIMessage:
    """单次调用，返回 AIMessage。"""
    text, _ = _request_text(messages)
    logger.debug("[chatgpt] call_once 完成，content 长度=%d", len(text))
    return AIMessage(content=text)


def call_with_tools(
    messages: list[dict],
    tools: list[dict],
    call_tool_fn,
    max_rounds: int = 5,
) -> str:
    """带工具调用循环的 AI 调用。

    当前中转未稳定支持工具调用，仍采用纯文本模式。
    """
    msgs = list(messages)

    if tools:
        tool_desc = json.dumps(tools, ensure_ascii=False, indent=2)
        tool_hint = (
            "\n\n[可用工具（仅供参考，请直接以文本方式回复结论）]:\n"
            f"{tool_desc}"
        )
        for message in msgs:
            if message.get("role") == "system":
                message["content"] = message.get("content", "") + tool_hint
                break
        else:
            msgs.insert(0, {"role": "system", "content": tool_hint})

    text, _ = _request_text(msgs)
    logger.info("[chatgpt] call_with_tools 完成")
    return text
