"""飞书消息发送封装。"""
import asyncio
import json

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from config import settings

# 单例客户端，内置 access_token 自动刷新（有效期 2 小时）
_client = (
    lark.Client.builder()
    .app_id(settings.feishu_app_id)
    .app_secret(settings.feishu_app_secret)
    .build()
)


def send_text(chat_id: str, text: str) -> None:
    """向指定群发送文本消息。"""
    request = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = _client.im.v1.message.create(request)
    if not response.success():
        import logging
        logging.getLogger(__name__).error(
            "消息发送失败: code=%s msg=%s", response.code, response.msg
        )
