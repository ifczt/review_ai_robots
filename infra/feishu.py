"""Feishu message sending helpers."""

import json
import logging
import shutil
import subprocess
import tempfile
from typing import Any
from pathlib import Path

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


def _send_message(
    receive_id: str,
    receive_id_type: str,
    msg_type: str,
    content: dict[str, Any],
) -> bool:
    request = (
        CreateMessageRequest.builder()
        .receive_id_type(receive_id_type)
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(receive_id)
            .msg_type(msg_type)
            .content(json.dumps(content, ensure_ascii=False))
            .build()
        )
        .build()
    )
    response = _client.im.v1.message.create(request)
    if response.success():
        return True

    logger.error(
        "[feishu] send failed receive_id_type=%s receive_id=%s msg_type=%s code=%s msg=%s",
        receive_id_type,
        receive_id,
        msg_type,
        response.code,
        response.msg,
    )
    return False


def send_text(chat_id: str, text: str) -> bool:
    """Send a text message to a chat."""
    return _send_message(chat_id, "chat_id", "text", {"text": text})


def send_text_to_user(open_id: str, text: str) -> bool:
    """Send a text message to a user by open_id."""
    return _send_message(open_id, "open_id", "text", {"text": text})


def send_card(chat_id: str, card: dict[str, Any]) -> bool:
    """Send an interactive card message to a chat."""
    return _send_message(chat_id, "chat_id", "interactive", card)


def _curl_path() -> str | None:
    return shutil.which("curl.exe") or shutil.which("curl")


def _run_curl(args: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _get_token_via_curl(retries: int = 4) -> str:
    curl = _curl_path()
    if not curl:
        raise RuntimeError("curl is not available")

    payload = json.dumps(
        {
            "app_id": settings.feishu_app_id,
            "app_secret": settings.feishu_app_secret,
        },
        ensure_ascii=False,
    )
    last_error = ""
    for _ in range(retries):
        result = _run_curl(
            [
                curl,
                "--tlsv1.2",
                "--http1.1",
                "-sS",
                "-X",
                "POST",
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                "-H",
                "Content-Type: application/json",
                "-d",
                payload,
            ],
            timeout=20,
        )
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = f"invalid auth response: {result.stdout}"
                continue
            token = data.get("tenant_access_token")
            if token:
                return token
            last_error = result.stdout or result.stderr
        else:
            last_error = result.stderr or f"curl auth exited with {result.returncode}"
    raise RuntimeError(f"failed to fetch tenant_access_token via curl: {last_error}")


def _send_message_via_curl(
    receive_id: str,
    receive_id_type: str,
    msg_type: str,
    content: dict[str, Any],
    retries: int = 4,
) -> bool:
    curl = _curl_path()
    if not curl:
        return False

    try:
        token = _get_token_via_curl()
    except Exception:
        logger.exception("[feishu] curl auth failed for msg_type=%s", msg_type)
        return False

    payload = {
        "receive_id": receive_id,
        "msg_type": msg_type,
        "content": json.dumps(content, ensure_ascii=False),
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as f:
        tmp_path = Path(f.name)
        json.dump(payload, f, ensure_ascii=False)

    try:
        last_error = ""
        for _ in range(retries):
            result = _run_curl(
                [
                    curl,
                    "--tlsv1.2",
                    "--http1.1",
                    "-sS",
                    "-X",
                    "POST",
                    f"https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
                    "-H",
                    f"Authorization: Bearer {token}",
                    "-H",
                    "Content-Type: application/json; charset=utf-8",
                    "--data-binary",
                    f"@{tmp_path}",
                ],
            )
            if result.stdout.strip():
                try:
                    data = json.loads(result.stdout)
                except json.JSONDecodeError:
                    last_error = f"invalid send response: {result.stdout}"
                    continue
                if data.get("code") == 0:
                    return True
                last_error = result.stdout
            else:
                last_error = result.stderr or f"curl send exited with {result.returncode}"
        logger.error(
            "[feishu] curl send failed receive_id_type=%s receive_id=%s msg_type=%s err=%s",
            receive_id_type,
            receive_id,
            msg_type,
            last_error,
        )
        return False
    finally:
        tmp_path.unlink(missing_ok=True)


def _upload_image_via_curl(image_path: str, retries: int = 4) -> str:
    curl = _curl_path()
    if not curl:
        raise RuntimeError("curl is not available")

    token = _get_token_via_curl()
    image = Path(image_path)
    if not image.exists():
        raise FileNotFoundError(image_path)

    last_error = ""
    for _ in range(retries):
        result = _run_curl(
            [
                curl,
                "--tlsv1.2",
                "--http1.1",
                "-sS",
                "-X",
                "POST",
                "https://open.feishu.cn/open-apis/im/v1/images",
                "-H",
                f"Authorization: Bearer {token}",
                "-F",
                "image_type=message",
                "-F",
                f"image=@{image}",
            ],
            timeout=60,
        )
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                last_error = f"invalid upload response: {result.stdout}"
                continue
            image_key = ((data.get("data") or {}).get("image_key"))
            if data.get("code") == 0 and image_key:
                return image_key
            last_error = result.stdout
        else:
            last_error = result.stderr or f"curl image upload exited with {result.returncode}"
    raise RuntimeError(f"failed to upload image via curl: {last_error}")


def send_image(chat_id: str, image_path: str) -> bool:
    """Upload a local image and send it to a chat."""
    try:
        image_key = _upload_image_via_curl(image_path)
    except Exception:
        logger.exception("[feishu] upload image failed path=%s", image_path)
        return False
    return _send_message_via_curl(chat_id, "chat_id", "image", {"image_key": image_key})
