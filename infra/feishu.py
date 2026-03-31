"""Feishu message sending helpers."""

import json
import logging

import lark_oapi as lark
from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

from config import settings

logger = logging.getLogger(__name__)

_client = (
    lark.Client.builder()
    .app_id(settings.feishu_app_id)
    .app_secret(settings.feishu_app_secret)
    .build()
)


def _send_text(receive_id: str, receive_id_type: str, text: str) -> None:
    request = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = _client.im.v1.message.create(request)
    if not response.success():
        logger.error(
            "[feishu] send failed receive_id_type=%s receive_id=%s code=%s msg=%s",
            receive_id_type,
            receive_id,
            response.code,
            response.msg,
        )


def send_text(chat_id: str, text: str) -> None:
    """Send a text message to a chat."""
    _send_text(chat_id, "chat_id", text)


def send_text_to_user(open_id: str, text: str) -> None:
    """Send a text message to a user by open_id."""
    _send_text(open_id, "open_id", text)
