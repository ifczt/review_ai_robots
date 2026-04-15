"""封版开关管理。

封版期间 /gitreview 只允许 BUG 修复类 PR 合并，其余一律拒绝。

状态持久化到 freeze_state.json（与本文件同级的项目根目录），
重启后保持封版状态不变。
"""
import json
import logging
import re

from app_paths import app_path

logger = logging.getLogger(__name__)

_STATE_FILE = app_path("freeze_state.json")
_PROJECT_RE = re.compile(r"^[^/\s]+(?:/[^/\s]+)?$")
_DEFAULT_ENTRY = {"frozen": False, "reason": ""}

# BUG 修复类关键词（PR 标题命中任意一个即视为 bugfix）
# 注意：Python 3 Unicode 模式下 \b 把中文字符也视为 \w，导致 "bug修复" 中 bug\b 无法匹配。
# 改用 ASCII 负向环视：前后不能是英文字母（排除 "debug" 等误匹配），中文无此限制。
_BUGFIX_KEYWORDS = re.compile(
    r"(?<![a-zA-Z])(fix|bug|hotfix|patch|urgent|critical|emergency|revert|rollback|hotpatch)(?![a-zA-Z])"
    r"|修复|缺陷|故障",
    re.IGNORECASE,
)

# 封版状态（内存缓存）
_state: dict = {"global": dict(_DEFAULT_ENTRY), "projects": {}}


def _normalize_entry(raw: object) -> dict:
    """将任意状态项归一化为标准结构。"""
    if not isinstance(raw, dict):
        return dict(_DEFAULT_ENTRY)
    return {
        "frozen": bool(raw.get("frozen")),
        "reason": str(raw.get("reason") or "").strip(),
    }


def _normalize_project(project: str | None) -> str | None:
    """标准化项目标识，支持 repo 或 owner/repo 两种格式。"""
    if not project:
        return None
    normalized = str(project).strip().strip("/").lower()
    if not normalized:
        return None
    if not _PROJECT_RE.fullmatch(normalized):
        raise ValueError("项目格式必须为 repo 或 owner/repo")
    return normalized


def _normalize_state(raw: object) -> dict:
    """兼容旧版全局结构，并规范化新版状态结构。"""
    if not isinstance(raw, dict):
        return {"global": dict(_DEFAULT_ENTRY), "projects": {}}

    if "global" not in raw and "projects" not in raw:
        return {
            "global": _normalize_entry(raw),
            "projects": {},
        }

    global_state = _normalize_entry(raw.get("global"))
    projects_state: dict[str, dict] = {}
    raw_projects = raw.get("projects")
    if isinstance(raw_projects, dict):
        for project, item in raw_projects.items():
            try:
                normalized_project = _normalize_project(str(project))
            except ValueError:
                logger.warning("[freeze] 忽略非法项目名: %r", project)
                continue
            entry = _normalize_entry(item)
            if normalized_project and entry["frozen"]:
                projects_state[normalized_project] = entry

    return {"global": global_state, "projects": projects_state}


def _load() -> None:
    """从文件加载封版状态（启动时调用一次）。"""
    global _state
    if _STATE_FILE.exists():
        try:
            _state = _normalize_state(
                json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            )
        except Exception as e:
            logger.warning("[freeze] 加载状态文件失败，使用默认值: %s", e)


def _save() -> None:
    """将当前状态持久化到文件。"""
    try:
        _STATE_FILE.write_text(
            json.dumps(_normalize_state(_state), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("[freeze] 保存状态文件失败: %s", e)


def _project_candidates(project: str | None) -> list[str]:
    """返回项目匹配候选，优先精确匹配 owner/repo，其次匹配 repo。"""
    normalized_project = _normalize_project(project)
    if not normalized_project:
        return []
    if "/" in normalized_project:
        owner, repo = normalized_project.split("/", 1)
        return [f"{owner}/{repo}", repo]
    return [normalized_project]


def _project_entry(project: str | None) -> tuple[str | None, dict]:
    """获取指定项目的封版状态，并返回命中的项目标识。"""
    for candidate in _project_candidates(project):
        entry = _state["projects"].get(candidate)
        if entry:
            return candidate, entry
    return None, dict(_DEFAULT_ENTRY)


def _format_single_status(title: str, entry: dict, scope_desc: str) -> str:
    """生成单个范围的状态文本。"""
    if entry.get("frozen"):
        reason = entry.get("reason", "")
        reason_part = f"\n原因：{reason}" if reason else ""
        return f"🔒 【{title}】当前处于{scope_desc}状态，仅允许 BUG 修复 PR 合并。{reason_part}"
    return f"🔓 【{title}】当前未封版，PR 审查正常进行。"


def _render_summary() -> str:
    """渲染全局 + 各项目的汇总状态。"""
    lines = [_format_single_status("全局", _state["global"], "封版")]
    projects = _state.get("projects", {})
    if not projects:
        lines.append("📁 当前没有处于封版中的项目。")
        return "\n".join(lines)

    lines.append("📁 项目封版列表：")
    for project in sorted(projects):
        entry = projects[project]
        reason = entry.get("reason", "")
        suffix = f"（原因：{reason}）" if reason else ""
        lines.append(f"- {project}{suffix}")
    return "\n".join(lines)


# 模块导入时自动加载
_load()


# ── 公开 API ────────────────────────────────────────────────────────────────

def looks_like_project(project: str | None) -> bool:
    """判断字符串是否像 repo 或 owner/repo 项目标识。"""
    if not project:
        return False
    return bool(_PROJECT_RE.fullmatch(project.strip().strip("/")))


def get_active_freeze(project: str | None = None) -> dict | None:
    """返回生效中的封版规则，优先返回项目级规则，其次返回全局规则。"""
    matched_project, project_entry = _project_entry(project)
    if project_entry.get("frozen"):
        return {
            "scope": "project",
            "project": matched_project,
            "reason": project_entry.get("reason", ""),
        }

    global_entry = _state["global"]
    if global_entry.get("frozen"):
        return {
            "scope": "global",
            "project": None,
            "reason": global_entry.get("reason", ""),
        }
    return None


def is_frozen(project: str | None = None) -> bool:
    """返回当前是否处于封版状态。"""
    return get_active_freeze(project) is not None


def get_reason(project: str | None = None) -> str:
    """返回当前生效封版规则的原因（可为空）。"""
    active = get_active_freeze(project)
    if not active:
        return ""
    return active.get("reason", "")


def enable(reason: str = "", project: str | None = None) -> None:
    """开启封版，支持全局或项目维度。"""
    normalized_project = _normalize_project(project)
    entry = {"frozen": True, "reason": reason.strip()}

    if normalized_project:
        _state["projects"][normalized_project] = entry
        logger.info(
            "[freeze] 项目封版已开启 project=%s reason=%r",
            normalized_project,
            entry["reason"],
        )
    else:
        _state["global"] = entry
        logger.info("[freeze] 全局封版已开启，reason=%r", entry["reason"])
    _save()


def disable(project: str | None = None) -> None:
    """解除封版，支持全局或项目维度。"""
    normalized_project = _normalize_project(project)
    if normalized_project:
        _state["projects"].pop(normalized_project, None)
        logger.info("[freeze] 项目封版已解除 project=%s", normalized_project)
    else:
        _state["global"] = dict(_DEFAULT_ENTRY)
        logger.info("[freeze] 全局封版已解除")
    _save()


def is_bugfix_pr(pr_title: str, pr_body: str = "") -> bool:
    """判断 PR 是否属于 BUG 修复类。

    规则：PR 标题或描述的第一行命中 BUG 修复关键词即视为 bugfix。
    """
    first_line_body = (pr_body or "").splitlines()[0] if pr_body else ""
    return bool(
        _BUGFIX_KEYWORDS.search(pr_title) or _BUGFIX_KEYWORDS.search(first_line_body)
    )


def status_text(project: str | None = None) -> str:
    """返回人类可读的当前状态描述。"""
    normalized_project = _normalize_project(project)
    if normalized_project:
        active = get_active_freeze(normalized_project)
        if not active:
            return f"🔓 项目 {normalized_project} 未封版，PR 审查正常进行。"

        reason = active.get("reason", "")
        reason_part = f"\n原因：{reason}" if reason else ""
        if active["scope"] == "project":
            return (
                f"🔒 项目 {active['project']} 处于封版状态，仅允许 BUG 修复 PR 合并。"
                f"{reason_part}"
            )
        return (
            f"🔒 项目 {normalized_project} 当前受全局封版影响，仅允许 BUG 修复 PR 合并。"
            f"{reason_part}"
        )
    return _render_summary()
