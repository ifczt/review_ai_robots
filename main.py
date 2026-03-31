import ctypes
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime

# 必须在所有业务模块导入前初始化，否则模块级日志（如 test_plan._load_sessions）会丢失
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 单实例锁（防止同时运行多个 EXE 实例）──────────────────────────────────
def _acquire_single_instance_lock() -> None:
    """创建全局 Windows Mutex，若已存在则说明另一实例在运行，直接退出。
    Mutex 句柄由 OS 持有，进程退出前不会释放，无需在 Python 层保存引用。
    """
    if sys.platform != "win32":
        logger.info("[bot] 当前系统不是 Windows，跳过单实例 Mutex 检查")
        return

    ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\FeiShuBotSingleInstance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("[bot] 检测到另一个实例已在运行，请勿重复启动。按回车退出...")
        input()
        sys.exit(1)

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1

from config import settings
from handlers import daily_report, router

# 已处理的消息 ID 集合（幂等去重，防止重复投递）
_processed_message_ids: set[str] = set()


def on_message_receive(data: P2ImMessageReceiveV1) -> None:
    """
    飞书长连接消息接收回调。
    必须立即返回，不能做任何耗时操作（AI 调用、DB 查询等会卡住 WebSocket 心跳导致断连）。
    实际处理放到独立线程中执行。
    """
    try:
        message = data.event.message
        message_id = message.message_id

        # 幂等去重
        if message_id in _processed_message_ids:
            return
        _processed_message_ids.add(message_id)
        if len(_processed_message_ids) > 1000:
            _processed_message_ids.clear()

        # 只处理群聊消息
        if message.chat_type != "group":
            return

        # 提取文本内容
        content_str = message.content or "{}"
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            return

        text = content.get("text", "").strip()
        if not text:
            return

        chat_id = message.chat_id
        user_id = data.event.sender.sender_id.open_id or "unknown"
        is_bw_group = bool(settings.bw_chat_id and chat_id == settings.bw_chat_id)

        # 有 @机器人：去掉 @ 前缀后处理
        if message.mentions:
            text = re.sub(r"@[^\s]+\s*", "", text).strip()
            if not text:
                return
        else:
            # 没有 @机器人：仅在以下情况处理：
            #   1. 该用户有待确认 SQL
            #   2. 来自 BW 群且有活跃测试计划
            if not router.has_pending(user_id) and not is_bw_group:
                return
            if is_bw_group and not router.has_pending_bw():
                return

        # 放入后台线程，避免阻塞 WebSocket 心跳
        threading.Thread(
            target=_process,
            args=(text, chat_id, user_id, is_bw_group),
            daemon=True,
        ).start()

    except Exception as e:
        logger.exception("[on_message_receive] 异常: %s", e)


def _process(text: str, chat_id: str, user_id: str, is_bw_group: bool = False) -> None:
    """在独立线程中执行耗时的消息处理。"""
    try:
        logger.info("[_process] 线程开始处理 user=%s text=%r", user_id, text[:100])
        if is_bw_group:
            router.dispatch_bw(text=text, chat_id=chat_id, user_id=user_id)
        else:
            router.dispatch(text=text, chat_id=chat_id, user_id=user_id)
        logger.info("[_process] 线程处理完成 user=%s", user_id)
    except Exception as e:
        logger.exception("[_process] 异常: %s", e)


def _daily_report_scheduler() -> None:
    """后台线程：定时发送群日报和个人日报。"""
    group_fired_on: date | None = None
    private_fired_on: date | None = None
    while True:
        now = datetime.now()
        if now.hour == 21 and now.minute == 30 and group_fired_on != now.date():
            group_fired_on = now.date()
            try:
                daily_report.send_daily_report(target_date=now.date())
            except Exception as e:
                logger.exception("[scheduler] 群日报发送失败: %s", e)
        if (
            now.hour == settings.daily_report_send_hour
            and now.minute == settings.daily_report_send_minute
            and private_fired_on != now.date()
        ):
            private_fired_on = now.date()
            try:
                daily_report.send_default_private_daily_reports(base_date=now.date())
            except Exception as e:
                logger.exception("[scheduler] 个人日报发送失败: %s", e)
        time.sleep(60)  # 每分钟检查一次，保证同一分钟只触发一次


def main():
    _acquire_single_instance_lock()

    threading.Thread(target=_daily_report_scheduler, daemon=True, name="daily-report").start()
    logger.info(
        "[bot] 日报调度器已启动（群日报 21:30；个人日报 %02d:%02d，回看 %d 天）",
        settings.daily_report_send_hour,
        settings.daily_report_send_minute,
        settings.daily_report_lookback_days,
    )

    handler = (
        lark.EventDispatcherHandler.builder("", "", lark.LogLevel.DEBUG)
        .register_p2_im_message_receive_v1(on_message_receive)
        .build()
    )

    cli = lark.ws.Client(
        settings.feishu_app_id,
        settings.feishu_app_secret,
        event_handler=handler,
        log_level=lark.LogLevel.DEBUG,
        domain="https://open.larksuite.com",
    )

    print("[bot] 飞书机器人启动，使用长连接模式...")
    cli.start()


if __name__ == "__main__":
    main()
