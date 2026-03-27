"""
SQLite-based PR review statistics storage.
DB file: f:/AI/bot/data/stats.db (auto-created on first use)

Table: reviews
  id              INTEGER PRIMARY KEY
  date            TEXT    YYYY-MM-DD
  user_id         TEXT    飞书 open_id（触发审查的人）
  pr_author       TEXT    Gitea 用户名（PR 提交者）
  pr_index        INTEGER
  pr_title        TEXT
  pr_url          TEXT
  verdict         TEXT    APPROVE / REQUEST_CHANGES / REJECT
  score           INTEGER AI 综合评分 0-100（质量+难度+单PR工作量）
  lines_added     INTEGER diff 新增行数
  lines_removed   INTEGER diff 删除行数
  files_changed   INTEGER diff 变更文件数
  review_summary  TEXT    AI 审查摘要（前 400 字）
  created_at      TEXT    ISO 时间戳
"""
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from app_paths import app_path

logger = logging.getLogger(__name__)

_DB_PATH = app_path("data", "stats.db")


def _get_conn() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table() -> None:
    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS reviews (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                date            TEXT    NOT NULL,
                user_id         TEXT    NOT NULL,
                pr_author       TEXT    NOT NULL,
                pr_index        INTEGER NOT NULL,
                pr_title        TEXT    NOT NULL,
                pr_url          TEXT    NOT NULL,
                verdict         TEXT    NOT NULL,
                score           INTEGER NOT NULL,
                lines_added     INTEGER NOT NULL DEFAULT 0,
                lines_removed   INTEGER NOT NULL DEFAULT 0,
                files_changed   INTEGER NOT NULL DEFAULT 0,
                review_summary  TEXT    NOT NULL,
                created_at      TEXT    NOT NULL
            )
        """)
        conn.commit()


def _migrate() -> None:
    """安全地给旧版数据库补充新列，列已存在时跳过。"""
    new_cols = [
        ("lines_added",   "INTEGER NOT NULL DEFAULT 0"),
        ("lines_removed", "INTEGER NOT NULL DEFAULT 0"),
        ("files_changed", "INTEGER NOT NULL DEFAULT 0"),
    ]
    with _get_conn() as conn:
        existing = {row[1] for row in conn.execute("PRAGMA table_info(reviews)")}
        for col, typedef in new_cols:
            if col not in existing:
                conn.execute(f"ALTER TABLE reviews ADD COLUMN {col} {typedef}")
                logger.info("[stats] 迁移：新增列 %s", col)
        conn.commit()


_ensure_table()
_migrate()


def record_review(
    *,
    user_id: str,
    pr_author: str,
    pr_index: int,
    pr_title: str,
    pr_url: str,
    verdict: str,          # APPROVE / REQUEST_CHANGES / REJECT
    score: int,            # AI 综合评分 0-100
    lines_added: int,      # diff 新增行数
    lines_removed: int,    # diff 删除行数
    files_changed: int,    # diff 变更文件数
    review_summary: str,   # AI 审查全文，自动截取前 400 字
) -> None:
    """将一次 PR 审查结果写入统计数据库。"""
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="seconds")
    try:
        with _get_conn() as conn:
            conn.execute(
                """INSERT INTO reviews
                   (date, user_id, pr_author, pr_index, pr_title, pr_url,
                    verdict, score, lines_added, lines_removed, files_changed,
                    review_summary, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    today, user_id, pr_author, pr_index, pr_title, pr_url,
                    verdict, score, lines_added, lines_removed, files_changed,
                    review_summary[:400], now,
                ),
            )
            conn.commit()
        logger.info(
            "[stats] 记录 PR #%d author=%s verdict=%s score=%d +%d/-%d lines %d files",
            pr_index, pr_author, verdict, score, lines_added, lines_removed, files_changed,
        )
    except Exception:
        logger.exception("[stats] 写入失败 PR #%d", pr_index)


def get_daily_records(target_date: date | None = None) -> list[dict]:
    """返回指定日期（默认今天）的所有审查记录，按时间升序。"""
    d = (target_date or date.today()).isoformat()
    try:
        with _get_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM reviews WHERE date = ? ORDER BY created_at",
                (d,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        logger.exception("[stats] 查询失败 date=%s", d)
        return []
