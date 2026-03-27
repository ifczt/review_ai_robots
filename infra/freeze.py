"""封版开关管理。

封版期间 /gitreview 只允许 BUG 修复类 PR 合并，其余一律拒绝。

状态持久化到 freeze_state.json（与本文件同级的项目根目录），
重启后保持封版状态不变。
"""
import json
import logging
import re
from pathlib import Path

from app_paths import app_path

logger = logging.getLogger(__name__)

_STATE_FILE = app_path("freeze_state.json")

# BUG 修复类关键词（PR 标题命中任意一个即视为 bugfix）
# 注意：Python 3 Unicode 模式下 \b 把中文字符也视为 \w，导致 "bug修复" 中 bug\b 无法匹配。
# 改用 ASCII 负向环视：前后不能是英文字母（排除 "debug" 等误匹配），中文无此限制。
_BUGFIX_KEYWORDS = re.compile(
    r"(?<![a-zA-Z])(fix|bug|hotfix|patch|urgent|critical|emergency|revert|rollback|hotpatch)(?![a-zA-Z])"
    r"|修复|缺陷|故障",
    re.IGNORECASE,
)

# 封版状态（内存缓存）
_state: dict = {"frozen": False, "reason": ""}


def _load() -> None:
    """从文件加载封版状态（启动时调用一次）。"""
    global _state
    if _STATE_FILE.exists():
        try:
            _state = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[freeze] 加载状态文件失败，使用默认值: %s", e)


def _save() -> None:
    """将当前状态持久化到文件。"""
    try:
        _STATE_FILE.write_text(
            json.dumps(_state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logger.warning("[freeze] 保存状态文件失败: %s", e)


# 模块导入时自动加载
_load()


# ── 公开 API ────────────────────────────────────────────────────────────────

def is_frozen() -> bool:
    """返回当前是否处于封版状态。"""
    return bool(_state.get("frozen"))


def get_reason() -> str:
    """返回封版原因（可为空）。"""
    return _state.get("reason", "")


def enable(reason: str = "") -> None:
    """开启封版。"""
    _state["frozen"] = True
    _state["reason"] = reason.strip()
    _save()
    logger.info("[freeze] 封版已开启，reason=%r", _state["reason"])


def disable() -> None:
    """解除封版。"""
    _state["frozen"] = False
    _state["reason"] = ""
    _save()
    logger.info("[freeze] 封版已解除")


def is_bugfix_pr(pr_title: str, pr_body: str = "") -> bool:
    """判断 PR 是否属于 BUG 修复类。

    规则：PR 标题或描述的第一行命中 BUG 修复关键词即视为 bugfix。
    """
    first_line_body = (pr_body or "").splitlines()[0] if pr_body else ""
    return bool(
        _BUGFIX_KEYWORDS.search(pr_title) or _BUGFIX_KEYWORDS.search(first_line_body)
    )


def status_text() -> str:
    """返回人类可读的当前状态描述。"""
    if is_frozen():
        reason = get_reason()
        reason_part = f"\n原因：{reason}" if reason else ""
        return f"🔒 当前处于封版状态，仅允许 BUG 修复 PR 合并。{reason_part}"
    return "🔓 当前未封版，PR 审查正常进行。"
