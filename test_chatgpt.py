# -*- coding: utf-8 -*-
"""测试新的 ChatGPT 中转。

当前中转在非流式 `responses` 返回里不会回填 `output`，
因此测试脚本改为用 SSE 流式事件读取最终文本。

运行：
    python test_chatgpt.py
"""
import json
import os
import sys

import httpx
from dotenv import load_dotenv

os.chdir(os.path.dirname(os.path.abspath(__file__)))

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

load_dotenv(".env")


def build_base_url() -> str:
    base_url = os.getenv("CHATGPT_BASE_URL", "").rstrip("/")
    if not base_url:
        base_url = "https://api.openai.com"
    if not base_url.endswith("/v1"):
        base_url += "/v1"
    return base_url


def consume_sse(response: httpx.Response) -> tuple[str, dict]:
    event_name = ""
    data_lines: list[str] = []
    deltas: list[str] = []
    done_text = ""
    completed_response = {}
    last_payload = {}

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
            raise RuntimeError(f"中转返回失败事件: {error}")

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
    return done_text or "".join(deltas), completed_response or last_payload


def main() -> int:
    api_key = os.getenv("CHATGPT_API_KEY", "")
    model = os.getenv("CHATGPT_MODEL", "gpt-4o")
    max_tokens = int(os.getenv("CHATGPT_MAX_TOKENS", "4096") or "4096")
    api_url = f"{build_base_url()}/responses"

    if not api_key:
        print("❌ 错误：CHATGPT_API_KEY 未配置")
        return 1

    payload = {
        "model": model,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "请只回复一句短话：Hello! ChatGPT relay is working.",
                    }
                ],
            }
        ],
        "max_output_tokens": max_tokens,
        "stream": True,
    }

    print("📡 ChatGPT 新中转连接测试")
    print(f"   Endpoint: {api_url}")
    print(f"   Model:    {model}")
    print(f"   API Key:  {api_key[:8]}...{api_key[-4:]}")
    print("   Mode:     Responses API + SSE")
    print()

    with httpx.Client(
        trust_env=True,
        timeout=120.0,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    ) as client:
        try:
            print("⏳ 正在发送流式请求...")
            with client.stream("POST", api_url, json=payload) as response:
                response.raise_for_status()
                text, data = consume_sse(response)

            if not text.strip():
                print("❌ 调用成功，但流式事件里没有正文")
                print(f"   原始 completed 响应: {json.dumps(data, ensure_ascii=False)[:1000]}")
                return 1

            print("✅ 调用成功！")
            print(f"   模型返回: {text}")

            usage = data.get("usage", {})
            if usage:
                print(
                    "   Usage:   input_tokens={}, output_tokens={}, total_tokens={}".format(
                        usage.get("input_tokens", "?"),
                        usage.get("output_tokens", "?"),
                        usage.get("total_tokens", "?"),
                    )
                )

            print(f"   响应 ID:  {data.get('id', '?')}")
            print(f"   状态:     {data.get('status', '?')}")
            print(f"   模型:     {data.get('model', '?')}")
            return 0

        except httpx.HTTPStatusError as exc:
            print(f"❌ HTTP {exc.response.status_code}")
            print(f"   响应: {exc.response.text[:800]}")
            return 1
        except httpx.ConnectError as exc:
            print(f"❌ 连接失败: {exc}")
            print("   请检查网络、代理或中转地址")
            return 1
        except Exception as exc:
            print(f"❌ 调用失败: {type(exc).__name__}: {exc}")
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
