# -*- coding: utf-8 -*-
"""
Test ChatGPT API connection (Responses API format).
Run: python test_chatgpt.py

需要在 .env 中填入有效的 CHATGPT_API_KEY（和可选 CHATGPT_BASE_URL）。
使用 /v1/responses 端点 + input 数组格式。
"""
import sys
import os

os.chdir(os.path.dirname(os.path.abspath(__file__)))

# Windows 控制台强制 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(".env")

import httpx

# 读取配置
api_key = os.getenv("CHATGPT_API_KEY", "")
base_url = os.getenv("CHATGPT_BASE_URL", "")
model = os.getenv("CHATGPT_MODEL", "gpt-5.2")

if not api_key:
    print("❌ 错误：CHATGPT_API_KEY 未配置，请在 .env 中填入有效的 API Key")
    sys.exit(1)

# 处理 base_url
if base_url:
    base_url = base_url.rstrip("/")
    if not base_url.endswith("/v1"):
        base_url += "/v1"
else:
    base_url = "https://api.openai.com/v1"

api_url = f"{base_url}/responses"

print(f"📡 ChatGPT 连接测试 (Responses API)")
print(f"   Endpoint: {api_url}")
print(f"   Model:    {model}")
print(f"   API Key:  {api_key[:8]}...{api_key[-4:]}")
print()

# 构建 Responses API 格式的请求
payload = {
    "model": model,
    "input": [
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": "Say 'Hello! ChatGPT is working.' in one short sentence."
                }
            ]
        }
    ]
}

# 使用系统代理（trust_env=True）
client = httpx.Client(
    trust_env=True,
    timeout=120.0,
    headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    },
)

try:
    print("⏳ 正在发送请求...")
    response = client.post(api_url, json=payload)

    if response.status_code != 200:
        print(f"❌ HTTP {response.status_code}")
        # 如果是 HTML（Cloudflare 拦截），只打印前 200 字符
        content_type = response.headers.get("content-type", "")
        if "html" in content_type:
            print(f"   看起来是 Cloudflare 拦截，请检查代理/VPN 设置")
            print(f"   响应片段: {response.text[:200]}")
        else:
            print(f"   响应: {response.text[:500]}")
        sys.exit(1)

    data = response.json()
    print(f"✅ 调用成功！")

    # 解析 Responses API 返回
    output = data.get("output", [])
    for item in output:
        if item.get("type") == "message":
            content = item.get("content", [])
            for block in content:
                if block.get("type") == "output_text":
                    print(f"   模型返回: {block.get('text', '')}")

    # 打印 usage 信息
    usage = data.get("usage", {})
    if usage:
        print(f"   Usage:   input_tokens={usage.get('input_tokens', '?')}, "
              f"output_tokens={usage.get('output_tokens', '?')}")

    print(f"\n   完整响应 ID: {data.get('id', '?')}")
    print(f"   模型: {data.get('model', '?')}")

except httpx.ConnectError as e:
    print(f"❌ 连接失败: {e}")
    print(f"   请检查网络连接和代理设置")
    sys.exit(1)
except Exception as e:
    print(f"❌ 调用失败: {type(e).__name__}: {e}")
    sys.exit(1)
finally:
    client.close()
