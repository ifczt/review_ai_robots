"""普通对话 Handler，维护每个用户的对话历史。"""
import logging
from collections import defaultdict

from ai.client import call_once
from ai.prompts import CHAT_PROMPT
from infra.feishu import send_text

logger = logging.getLogger(__name__)

# 按 user_id 隔离的对话历史
_histories: dict[str, list[dict]] = defaultdict(list)
MAX_HISTORY = 20


def handle(user_id: str, text: str, chat_id: str) -> None:
    history = _histories[user_id]
    history.append({"role": "user", "content": text})

    # 滑动窗口：超出上限时移除最旧的消息对
    while len(history) > MAX_HISTORY:
        history.pop(0)
        if history and history[0]["role"] == "assistant":
            history.pop(0)

    messages = [{"role": "system", "content": CHAT_PROMPT}] + history

    try:
        message = call_once(messages)
        reply = message.content or "（AI 返回内容为空）"
    except Exception as e:
        logger.error("[chat] AI 调用失败: %s", e)
        reply = "AI 服务暂时不可用：{}".format(e)

    history.append({"role": "assistant", "content": reply})
    send_text(chat_id, reply)


def clear_history(user_id: str) -> None:
    _histories.pop(user_id, None)
