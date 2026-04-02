"""
TestPlan 工作流。

PR 合并后自动触发，AI 从 diff 提取测试要点，
通知 BW 包网项目群成员逐项确认，汇总结果后
向原始群反馈「可以发布生产环境」或失败报告。

会话状态持久化到 data/stats.db（test_plan_sessions 表），
服务重启后自动恢复未完成的测试任务。

公开接口：
  create_session(pr_index, pr_title, diff, source_chat_id,
                 pr_author="", initiator_id="") -> None
  handle_reply(text, chat_id, user_id) -> None
  has_active_session() -> bool
"""
import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from ai import client as ai_client
from ai.prompts import TEST_PLAN_PROMPT
from app_paths import app_path
from config import settings
from infra.feishu import send_text

logger = logging.getLogger(__name__)

_DB_PATH = app_path("data", "stats.db")

# 解析 AI 输出的编号列表，兼容 "1." 和 "1、" 两种格式
_POINT_RE = re.compile(r"^\s*(\d+)[.、]\s*(.+)$", re.MULTILINE)

# 解析 BW 群成员回复，支持可选的 #<PR编号> 前缀（多任务并发时用于指定目标）
_ACCEPT_ALL_RE = re.compile(r"^通过\s*(?:#(\d+)\s+)?全部$")
_ACCEPT_RE = re.compile(r"^通过\s+(?:#(\d+)\s+)?(\d[\d\s]*)$")
_REJECT_RE = re.compile(r"^拒绝\s+(?:#(\d+)\s+)?(\d+)\s*(.*)$")


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


@dataclass
class TestPlanSession:
    session_id: str
    source_chat_id: str      # /gitreview 所在群，最终报告目标
    pr_index: int
    pr_title: str
    pr_author: str           # Gitea PR 作者
    initiator_id: str        # 触发审查的飞书用户 open_id
    points: list[str]        # AI 提取的测试要点，0-based 存储
    accepted: set[int] = field(default_factory=set)  # 已通过的编号（1-based）
    rejected: bool = False   # 任一点被拒绝后置 True
    created_at: float = field(default_factory=time.time)
    last_handler_id: str = ""   # 最后操作的 BW 群用户 open_id
    finished_at: float = 0.0


# 模块级状态（单 worker 进程安全）
# key: session_id，value: TestPlanSession
_sessions: dict[str, TestPlanSession] = {}


# ── 持久化层 ────────────────────────────────────────────────────────────────


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS test_plan_sessions (
                session_id      TEXT    PRIMARY KEY,
                source_chat_id  TEXT    NOT NULL,
                pr_index        INTEGER NOT NULL,
                pr_title        TEXT    NOT NULL,
                pr_author       TEXT    NOT NULL DEFAULT '',
                initiator_id    TEXT    NOT NULL DEFAULT '',
                points          TEXT    NOT NULL,
                accepted        TEXT    NOT NULL DEFAULT '[]',
                rejected        INTEGER NOT NULL DEFAULT 0,
                created_at      REAL    NOT NULL,
                last_handler_id TEXT    NOT NULL DEFAULT '',
                finished_at     REAL    NOT NULL DEFAULT 0.0
            )
        """)
        conn.commit()


def _save_session(session: TestPlanSession) -> None:
    """将会话状态写入数据库（INSERT OR REPLACE）。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO test_plan_sessions
                   (session_id, source_chat_id, pr_index, pr_title, pr_author,
                    initiator_id, points, accepted, rejected,
                    created_at, last_handler_id, finished_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.source_chat_id,
                    session.pr_index,
                    session.pr_title,
                    session.pr_author,
                    session.initiator_id,
                    json.dumps(session.points, ensure_ascii=False),
                    json.dumps(sorted(session.accepted)),
                    int(session.rejected),
                    session.created_at,
                    session.last_handler_id,
                    session.finished_at,
                ),
            )
            conn.commit()
    except Exception:
        logger.exception("[test_plan] 持久化写入失败 session_id=%s", session.session_id)


def _delete_session_db(session_id: str) -> None:
    """从数据库删除已完成/过期的会话。"""
    try:
        with _get_conn() as conn:
            conn.execute(
                "DELETE FROM test_plan_sessions WHERE session_id = ?",
                (session_id,),
            )
            conn.commit()
    except Exception:
        logger.exception("[test_plan] 持久化删除失败 session_id=%s", session_id)


def _load_sessions() -> None:
    """启动时从数据库恢复未完成的会话到内存。过期会话直接丢弃。"""
    try:
        now = time.time()
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM test_plan_sessions WHERE rejected = 0"
            ).fetchall()

        for row in rows:
            r = dict(row)
            session = TestPlanSession(
                session_id=r["session_id"],
                source_chat_id=r["source_chat_id"],
                pr_index=r["pr_index"],
                pr_title=r["pr_title"],
                pr_author=r["pr_author"],
                initiator_id=r["initiator_id"],
                points=json.loads(r["points"]),
                accepted=set(json.loads(r["accepted"])),
                rejected=bool(r["rejected"]),
                created_at=r["created_at"],
                last_handler_id=r["last_handler_id"],
                finished_at=r["finished_at"],
            )
            _sessions[session.session_id] = session

        logger.info(
            "[test_plan] 启动加载完成：恢复 %d 个活跃会话",
            len(_sessions),
        )
    except Exception:
        logger.exception("[test_plan] 启动时加载会话失败")


# 模块加载时初始化表结构并恢复会话
_ensure_table()
_load_sessions()


# ── 公开接口 ────────────────────────────────────────────────────────────────


def has_active_session() -> bool:
    """是否有活跃的测试计划会话（供 router 判断是否处理 BW 群无 @ 消息）。"""
    return bool(_sessions)


def get_pending_summary() -> str:
    """返回当前所有待测试任务的汇总文本，供 /testlist 命令使用。"""
    active = [s for s in _sessions.values() if not s.rejected]
    if not active:
        return "当前没有待测试的任务，所有 PR 均已完成验收或尚未提交测试。"

    lines = [f"📋 待测试任务（共 {len(active)} 个）\n"]
    for s in sorted(active, key=lambda x: x.created_at):
        done = len(s.accepted)
        total = len(s.points)
        remaining = [
            f"  {i + 1}. {p}"
            for i, p in enumerate(s.points)
            if (i + 1) not in s.accepted
        ]
        status = f"{done}/{total} 已通过" if done > 0 else "待开始"
        lines.append(
            f"▸ PR #{s.pr_index}  {s.pr_title}\n"
            f"  发起人：{s.pr_author}　发起时间：{_fmt_time(s.created_at)}\n"
            f"  进度：{status}\n"
            + ("  待验收要点：\n" + "\n".join(remaining) if remaining else "  所有要点已通过，等待确认")
        )
    return "\n\n".join(lines)


def create_session(
    pr_index: int,
    pr_title: str,
    diff: str,
    source_chat_id: str,
    pr_author: str = "",
    initiator_id: str = "",
) -> None:
    """PR 合并后调用：提取测试要点，创建会话，持久化，发送 BW 群通知。"""

    if not settings.bw_chat_id:
        logger.warning("[test_plan] BW_CHAT_ID 未配置，跳过测试通知")
        return

    points = _extract_test_points(pr_title, diff)
    if not points:
        logger.warning("[test_plan] AI 未提取到测试要点，PR #%d", pr_index)
        send_text(
            source_chat_id,
            f"⚠️ PR #{pr_index} 合并成功，但 AI 未能提取测试要点，请人工安排测试。",
        )
        return

    session_id = f"{pr_index}_{int(time.time())}"
    session = TestPlanSession(
        session_id=session_id,
        source_chat_id=source_chat_id,
        pr_index=pr_index,
        pr_title=pr_title,
        pr_author=pr_author or "unknown",
        initiator_id=initiator_id,
        points=points,
    )
    _sessions[session_id] = session
    _save_session(session)

    _send_bw_notification(session)
    logger.info(
        "[test_plan] 会话已创建 session_id=%s pr=%d points=%d",
        session_id, pr_index, len(points),
    )


def handle_reply(text: str, chat_id: str, user_id: str) -> None:
    """处理来自 BW 群的回复，驱动测试确认状态机。"""
    if not _sessions:
        logger.info("[test_plan] handle_reply 收到回复但无活跃会话，忽略 text=%r", text)
        return

    stripped = text.strip()

    # 通过全部 / 通过 #123 全部
    m = _ACCEPT_ALL_RE.match(stripped)
    if m:
        pr_hint = int(m.group(1)) if m.group(1) else None
        session = _resolve_session(pr_hint, chat_id)
        if session is None:
            return
        session.accepted = set(range(1, len(session.points) + 1))
        session.last_handler_id = user_id
        _on_all_accepted(session)
        return

    # 通过 1 2 3 / 通过 #123 1 2
    m = _ACCEPT_RE.match(stripped)
    if m:
        pr_hint = int(m.group(1)) if m.group(1) else None
        nums_str = m.group(2)
        session = _resolve_session(pr_hint, chat_id)
        if session is None:
            return
        new_nums = {
            int(n) for n in nums_str.split()
            if n.isdigit() and 1 <= int(n) <= len(session.points)
        }
        session.accepted.update(new_nums)
        session.last_handler_id = user_id
        _save_session(session)  # 持久化部分进度
        if len(session.accepted) >= len(session.points):
            _on_all_accepted(session)
        else:
            accepted_count = len(session.accepted)
            total = len(session.points)
            nums_display = " ".join(str(n) for n in sorted(new_nums))
            send_text(
                chat_id,
                f"已记录：PR #{session.pr_index} 要点 {nums_display} 通过"
                f"（{accepted_count}/{total} 完成）",
            )
        return

    # 拒绝 2 原因 / 拒绝 #123 2 原因
    m = _REJECT_RE.match(stripped)
    if m:
        pr_hint = int(m.group(1)) if m.group(1) else None
        point_num = int(m.group(2))
        reason = m.group(3).strip()
        session = _resolve_session(pr_hint, chat_id)
        if session is None:
            return
        if 1 <= point_num <= len(session.points):
            session.last_handler_id = user_id
            _on_rejected(session, point_num, reason)
        else:
            send_text(chat_id, f"要点编号 {point_num} 不存在，PR #{session.pr_index} 共 {len(session.points)} 条要点。")
        return

    # 无匹配：BW 群普通聊天，静默忽略


# ── 私有函数 ────────────────────────────────────────────────────────────────


def _resolve_session(pr_hint: int | None, chat_id: str) -> "TestPlanSession | None":
    """
    根据可选的 PR 编号提示解析目标会话。
    - 单任务且无提示 → 直接返回该任务
    - 多任务且有提示 → 按 PR 编号查找
    - 多任务且无提示 → 发提示消息，返回 None
    - 找不到 → 发错误消息，返回 None
    """
    active = {s.pr_index: s for s in _sessions.values() if not s.rejected}

    if not active:
        return None

    if pr_hint is not None:
        s = active.get(pr_hint)
        if s is None:
            send_text(chat_id, f"没有 PR #{pr_hint} 的活跃测试任务。")
        return s

    if len(active) == 1:
        return next(iter(active.values()))

    # 多任务，未指定 PR 编号
    items = "\n".join(f"  PR #{idx}: {s.pr_title}" for idx, s in sorted(active.items()))
    send_text(
        chat_id,
        f"当前有 {len(active)} 个并发测试任务，请在回复中指定 PR 编号，例如：\n"
        f"  通过 #123 全部\n"
        f"  拒绝 #123 2 原因\n\n"
        f"活跃任务：\n{items}",
    )
    return None


def _extract_test_points(pr_title: str, diff: str) -> list[str]:
    """调用 AI 从 PR 信息提取测试要点，返回要点文本列表（可能为空）。"""
    user_content = f"PR 标题：{pr_title}\n\n--- diff ---\n{diff}"
    messages = [
        {"role": "system", "content": TEST_PLAN_PROMPT},
        {"role": "user", "content": user_content},
    ]
    try:
        msg = ai_client.call_once(messages)
        raw = (msg.content or "").strip()
    except Exception:
        logger.exception("[test_plan] AI 提取测试要点失败")
        return []

    matches = _POINT_RE.findall(raw)
    return [text.strip() for _, text in matches]


def _send_bw_notification(session: TestPlanSession) -> None:
    """向 BW 群发送测试通知。"""
    points_text = "\n".join(
        f"{i + 1}. {p}" for i, p in enumerate(session.points)
    )
    active_count = sum(1 for s in _sessions.values() if not s.rejected)
    if active_count > 1:
        reply_hint = (
            f"  通过 #{session.pr_index} 1 2  —— 标记指定要点通过\n"
            f"  通过 #{session.pr_index} 全部 —— 全部通过\n"
            f"  拒绝 #{session.pr_index} 2 页面404 —— 拒绝要点，可附原因"
        )
    else:
        reply_hint = (
            f"  通过 1 2 3        —— 标记指定要点通过\n"
            f"  通过全部           —— 全部通过\n"
            f"  拒绝 2 页面404    —— 拒绝要点，可附原因"
        )

    msg = (
        f"【测试通知】PR #{session.pr_index} 已合并，请进行测试验证\n\n"
        f"PR 标题：{session.pr_title}\n"
        f"发起人：{session.pr_author}\n"
        f"发起时间：{_fmt_time(session.created_at)}\n\n"
        f"测试要点：\n{points_text}\n\n"
        f"回复格式：\n{reply_hint}"
    )
    send_text(settings.bw_chat_id, msg)


def _on_all_accepted(session: TestPlanSession) -> None:
    """所有测试要点通过：发成功报告，持久化删除，清理内存。"""
    session.finished_at = time.time()
    total = len(session.points)
    points_summary = "\n".join(
        f"{i + 1}. {p} ✓" for i, p in enumerate(session.points)
    )
    send_text(
        session.source_chat_id,
        f"✅ PR #{session.pr_index} 测试通过，可以发布生产环境\n\n"
        f"测试要点（{total}/{total} 通过）：\n{points_summary}\n\n"
        f"处理人：{session.last_handler_id or 'BW群成员'}\n"
        f"处理时间：{_fmt_time(session.finished_at)}",
    )
    send_text(settings.bw_chat_id, f"PR #{session.pr_index} 全部测试要点已通过，已通知发布方。")
    _sessions.pop(session.session_id, None)
    _delete_session_db(session.session_id)
    logger.info("[test_plan] PR #%d 测试全部通过", session.pr_index)


def _on_rejected(session: TestPlanSession, point_num: int, reason: str) -> None:
    """某测试要点被拒绝：发失败报告，持久化删除，终止计划。"""
    session.rejected = True
    session.finished_at = time.time()
    point_text = session.points[point_num - 1]
    reason_display = reason if reason else "（未说明）"
    send_text(
        session.source_chat_id,
        f"❌ PR #{session.pr_index} 测试未通过，暂不可发布\n\n"
        f"失败要点：#{point_num} {point_text}\n"
        f"拒绝原因：{reason_display}\n\n"
        f"处理人：{session.last_handler_id or 'BW群成员'}\n"
        f"处理时间：{_fmt_time(session.finished_at)}\n\n"
        f"请修复后重新提交 PR。",
    )
    send_text(
        settings.bw_chat_id,
        f"PR #{session.pr_index} 测试中止：要点 #{point_num} 被拒绝，已通知发布方。",
    )
    _sessions.pop(session.session_id, None)
    _delete_session_db(session.session_id)
    logger.info(
        "[test_plan] PR #%d 测试被拒绝 point=%d reason=%r",
        session.pr_index, point_num, reason,
    )



