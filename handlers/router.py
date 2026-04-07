"""
消息路由器。
纯路由表，无业务逻辑，只负责把消息分发到对应 handler。
"""
import logging
import re

from handlers import chat, daily_report
from handlers import test_plan as _test_plan
from handlers import server_status as _server_status
from handlers.reviews import git as git_review
from handlers.reviews.sql import sql_review
from infra import freeze as _freeze, ssh as _ssh
from infra.db import format_available, is_valid
from infra.feishu import send_text
from tools import command_executor, remote_executor

logger = logging.getLogger(__name__)

HELP_TEXT = """可用命令：
/sql <地区>.<库名> <SQL>          - 提交 SQL 审核并执行（生产环境）
/gitreview <PR链接>               - AI 审查 Gitea PR，通过则自动合并
/freeze on [原因]                 - 开启封版（仅 BUG 修复 PR 可合并）
/freeze off                       - 解除封版
/freeze                           - 查看封版状态
/testlist                         - 查询当前所有待测试任务（上线前确认）
/svc <地区> <子命令> [服务名]     - 远程管理 supervisor 服务进程
/svc <地区> nginx <操作>          - 远程管理 nginx（操作：status/start/stop/restart/reload/test）
/status                           - 查看所有地区服务器实时状态（AI 汇总）
/status <地区>                    - 查看指定地区服务器实时状态
/run <命令>                       - 执行本地系统命令（仅限只读/诊断类）
/report                           - 立即发送今日代码审查日报
/preport                          - 立即发送个人日报（默认发送昨天）
/clear                            - 清除对话历史
/help                             - 显示此帮助

示例：
  /sql sa.user SELECT * FROM users WHERE id=1
  /gitreview https://gitea.example.com/owner/repo/pulls/42
  /svc sa status
  /svc sa restart grpc_notice_hook
  /svc sa tail grpc_notice_hook
  /svc sa nginx status
  /svc sa nginx reload
  /svc sa nginx test

直接发送 SQL 语句时，需包含地区前缀，例如：
  sa.user: SELECT * FROM users LIMIT 10

AI 提示优化后，直接回复「确认执行」即可（无需 @机器人）。"""

# 匹配 /sql <region>.<db> <SQL> 格式
_SQL_CMD_RE = re.compile(r"^/sql\s+(\w+)\.(\w+)\s+(.+)$", re.DOTALL | re.IGNORECASE)

# 从自然语言文本中提取 <region>.<db>: <SQL> 格式
# 例如：sa.user: SELECT * FROM users
_SQL_WITH_TARGET_RE = re.compile(
    r"(\w+)\.(\w+)\s*[:：]\s*(SELECT\b.+|INSERT\b.+|UPDATE\b.+|DELETE\b.+|SHOW\b.+|EXPLAIN\b.+|DESC(?:RIBE)?\b.+|ALTER\s+TABLE\b.+|CREATE\s+TABLE\b.+)",
    re.IGNORECASE | re.DOTALL,
)

# 纯 SQL 提取（无地区前缀，用于给出引导提示）
_SQL_BARE_RE = re.compile(
    r"(SELECT\b.+|INSERT\b.+|UPDATE\b.+|DELETE\b.+|SHOW\b.+|EXPLAIN\b.+|DESC(?:RIBE)?\b.+|ALTER\s+TABLE\b.+|CREATE\s+TABLE\b.+)",
    re.IGNORECASE | re.DOTALL,
)


def has_pending(user_id: str) -> bool:
    """是否有待确认的 SQL（供 main.py 判断是否处理无 @ 的消息）。"""
    return sql_review.has_pending(user_id)


def has_pending_bw() -> bool:
    """BW 群是否有活跃测试计划（供 main.py 判断是否处理 BW 群无 @ 消息）。"""
    return _test_plan.has_active_session()


def dispatch_bw(text: str, chat_id: str, user_id: str) -> None:
    """处理来自 BW 群的消息。
    / 开头的命令走普通 dispatch；其余消息交给测试计划确认流程。
    """
    logger.info("[router] BW群消息 user=%s text=%r", user_id, text[:80])
    if text.strip().startswith("/"):
        dispatch(text, chat_id, user_id)
    else:
        _test_plan.handle_reply(text, chat_id, user_id)


def dispatch(text: str, chat_id: str, user_id: str) -> None:
    """消息路由主入口，按优先级匹配并分发。"""
    stripped = text.strip()
    logger.info("[router] user=%s text=%r", user_id, stripped[:100])

    # ── 有待确认的 SQL ──────────────────────────────────────────
    if sql_review.has_pending(user_id):
        logger.info("[router] user=%s 有待确认 SQL，转交 sql_review 处理回复", user_id)
        sql_review.handle_pending_reply(stripped, chat_id, user_id)
        return

    # ── /sql <region>.<db> <SQL> 命令 ───────────────────────────
    m = _SQL_CMD_RE.match(stripped)
    if m:
        region, database, sql = m.group(1), m.group(2), m.group(3).strip()
        if not is_valid(region, database):
            send_text(chat_id, "未知地区或数据库：{}.{}\n\n{}".format(
                region, database, format_available()
            ))
            return
        logger.info("[router] /sql 命令 target=%s.%s user=%s", region, database, user_id)
        sql_review.handle(sql, chat_id, user_id, region, database)
        return

    if stripped.startswith("/sql"):
        send_text(chat_id, (
            "用法：/sql <地区>.<库名> <SQL语句>\n\n"
            + format_available()
        ))
        return

    # ── /gitreview 命令 ─────────────────────────────────────────
    if stripped.startswith("/gitreview "):
        pr_url = stripped[len("/gitreview "):].strip()
        # 如果pr_url 不是http 开头 添加 http
        if not pr_url.startswith(("http://", "https://")):
            pr_url = "https://" + pr_url
        logger.info("[router] /gitreview user=%s url=%r", user_id, pr_url)
        git_review.handle(chat_id, user_id, pr_url)
        return

    if stripped == "/gitreview":
        send_text(chat_id, "用法：/gitreview <PR链接>\n示例：/gitreview https://gitea.example.com/owner/repo/pulls/42")
        return

    # ── /freeze 命令（封版开关）────────────────────────────────────
    if stripped.startswith("/freeze"):
        args = stripped[7:].strip().split(None, 1)
        sub = args[0].lower() if args else "status"
        if sub == "on":
            reason = args[1] if len(args) > 1 else ""
            _freeze.enable(reason)
            send_text(chat_id, _freeze.status_text())
        elif sub == "off":
            _freeze.disable()
            send_text(chat_id, _freeze.status_text())
        else:
            send_text(chat_id, _freeze.status_text())
        return

    # ── /svc 命令（远程 supervisorctl / nginx）─────────────────
    if stripped.startswith("/svc "):
        args = stripped[5:].strip().split(None, 1)
        if len(args) < 2:
            send_text(chat_id, (
                "用法：\n"
                "  /svc <地区> <子命令> [服务名]     - supervisorctl\n"
                "  /svc <地区> nginx <操作>           - nginx 管理\n\n"
                "nginx 操作：status / start / stop / restart / reload / test\n\n"
                "示例：\n"
                "  /svc sa status\n"
                "  /svc sa restart grpc_notice_hook\n"
                "  /svc sa nginx status\n"
                "  /svc sa nginx reload\n\n"
                + _ssh.format_available()
            ))
            return
        region, subcmd = args[0], args[1]
        if not _ssh.is_valid_region(region):
            send_text(chat_id, f"未知地区：{region}\n\n" + _ssh.format_available())
            return
        logger.info("[router] /svc region=%s subcmd=%r user=%s", region, subcmd, user_id)
        # 判断是 nginx 还是 supervisorctl
        svc_parts = subcmd.split(None, 1)
        if svc_parts[0].lower() == "nginx":
            nginx_action = svc_parts[1] if len(svc_parts) > 1 else "status"
            result = remote_executor.execute(f"nginx {nginx_action}", region)
        else:
            result = remote_executor.execute("supervisorctl " + subcmd, region)
        send_text(chat_id, "```\n{}\n```".format(result))
        return

    if stripped == "/svc":
        send_text(chat_id, (
            "用法：\n"
            "  /svc <地区> <子命令> [服务名]     - supervisorctl\n"
            "  /svc <地区> nginx <操作>           - nginx 管理\n\n"
            + _ssh.format_available()
        ))
        return

    # ── /run 命令 ───────────────────────────────────────────────
    if stripped.startswith("/run "):
        cmd = stripped[5:].strip()
        logger.info("[router] /run 命令: %r", cmd)
        result = command_executor.execute(cmd)
        send_text(chat_id, "```\n{}\n```".format(result))
        return

    if stripped == "/run":
        send_text(chat_id, "用法：/run <命令>")
        return

    # ── /chatid 命令（用于获取当前群 chat_id，方便配置）────────────
    if stripped == "/chatid":
        send_text(chat_id, f"当前群 chat_id：\n{chat_id}")
        return

    # ── /testlist 命令（查询待测试任务，上线前确认）────────────────
    if stripped == "/testlist":
        logger.info("[router] /testlist 命令 user=%s", user_id)
        send_text(chat_id, _test_plan.get_pending_summary())
        return

    # ── /status 命令（实时服务器状态）──────────────────────────────
    if stripped.startswith("/status"):
        arg = stripped[7:].strip()  # 地区参数（可选）
        logger.info("[router] /status 命令 region=%r user=%s", arg or "all", user_id)
        send_text(chat_id, "⏳ 正在采集服务器状态，请稍候...")
        result = _server_status.query_status(region=arg if arg else None)
        send_text(chat_id, result)
        return

    # ── /report 命令（立即发送今日日报，用于测试或按需查看）────────
    if stripped == "/report":
        logger.info("[router] /report 命令 user=%s", user_id)
        daily_report.send_daily_report()
        return

    # ── /preport 命令（立即发送个人日报）──────────────────────────
    if stripped == "/preport":
        logger.info("[router] /preport 命令 user=%s", user_id)
        target_date = daily_report.send_default_private_daily_reports()
        send_text(chat_id, f"个人日报已触发发送，目标日期：{target_date.isoformat()}")
        return

    # ── /clear 命令 ─────────────────────────────────────────────
    if stripped == "/clear":
        chat.clear_history(user_id)
        logger.info("[router] user=%s 清除对话历史", user_id)
        send_text(chat_id, "对话历史已清除。")
        return

    # ── /help 命令 ──────────────────────────────────────────────
    if stripped == "/help":
        send_text(chat_id, HELP_TEXT)
        return

    # ── 自动识别带目标的 SQL（格式：sa.user: SELECT ...）──────────
    m = _SQL_WITH_TARGET_RE.search(stripped)
    if m:
        region, database, sql = m.group(1), m.group(2), m.group(3).strip()
        if not is_valid(region, database):
            send_text(chat_id, "未知地区或数据库：{}.{}\n\n{}".format(
                region, database, format_available()
            ))
            return
        logger.info("[router] 提取到带目标 SQL target=%s.%s user=%s", region, database, user_id)
        sql_review.handle(sql, chat_id, user_id, region, database)
        return

    # ── 识别到裸 SQL（无地区前缀），引导用户指定目标 ──────────────
    if _SQL_BARE_RE.search(stripped):
        send_text(chat_id, (
            "检测到 SQL 语句，请指定目标数据库。\n\n"
            "格式：/sql <地区>.<库名> <SQL>\n\n"
            + format_available()
        ))
        return

    # ── 普通对话 ────────────────────────────────────────────────
    # logger.info("[router] 普通对话，user=%s", user_id)
    # chat.handle(user_id=user_id, text=stripped, chat_id=chat_id)
