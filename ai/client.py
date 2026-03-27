"""AI 客户端门面层，根据 AI_BACKEND 配置自动切换后端。

后端选项：
  anthropic  - Anthropic 原生 SDK（默认，推荐）
  openai     - OpenAI 兼容格式（中转/备用）
  chatgpt    - ChatGPT（OpenAI GPT 系列模型）

所有上层调用方（handlers）只需 from ai.client import call_once / call_with_tools，
无需关心底层使用哪个 SDK。
"""
from config import settings

if settings.ai_backend == "openai":
    from ai.openai_client import call_once, call_with_tools
elif settings.ai_backend == "chatgpt":
    from ai.chatgpt_client import call_once, call_with_tools
else:
    from ai.anthropic_client import call_once, call_with_tools

__all__ = ["call_once", "call_with_tools"]
