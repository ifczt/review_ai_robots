"""Git PR 代码审查 Handler。

流程：
  /gitreview <PR_URL>
    → 获取 PR 元信息 + diff
    → AI 审查（GIT_REVIEW_PROMPT）
    → [APPROVE]          → 调用 Gitea merge API → 飞书通知已合并
    → [REQUEST_CHANGES]  → Gitea PR 留评论 → 飞书通知审查意见
    → [REJECT]           → Gitea PR 留评论 → 飞书通知拒绝原因
"""
import logging
import re
import threading

from ai import client as ai_client
from ai.prompts import GIT_REVIEW_PROMPT
from config import settings
from handlers import test_plan
from infra import freeze, gitea, stats
from infra.feishu import send_text

logger = logging.getLogger(__name__)

# 禁止自动合并的目标分支（生产 / 主干分支），PR 指向这些分支时直接拒绝
_PROTECTED_BRANCHES = {"master", "main", "production", "release"}

# 匹配 Gitea PR URL，兼容 /pulls/ 和 /pull/ 两种路径
_PR_URL_RE = re.compile(
    r"^(https?://[^/\s]+)/([^/\s]+)/([^/\s]+)/pulls?/(\d+)\s*$",
    re.IGNORECASE,
)

# 匹配 AI 在审查结尾输出的 SCORE 行，例如 "SCORE: 78"
_SCORE_RE = re.compile(r"\nSCORE:\s*(\d{1,3})\s*$")

# verdict → 兜底分（AI 未输出 SCORE 时使用）
_FALLBACK_SCORE = {"APPROVE": 75, "REQUEST_CHANGES": 50, "REJECT": 20}


def _parse_diff_stats(diff: str) -> tuple[int, int, int]:
    """从 unified diff 中统计 (lines_added, lines_removed, files_changed)。
    只计算实际代码行，跳过 diff 头部元信息行（+++ / ---）。
    """
    lines_added = len(re.findall(r"^\+(?!\+\+)", diff, re.MULTILINE))
    lines_removed = len(re.findall(r"^-(?!--)", diff, re.MULTILINE))
    files_changed = len(re.findall(r"^diff --git ", diff, re.MULTILINE))
    return lines_added, lines_removed, files_changed


def handle(chat_id: str, user_id: str, pr_url: str) -> None:
    """处理 /gitreview 命令的入口函数。"""
    # ── 配置校验 ────────────────────────────────────────────────
    if not settings.gitea_base_url or not settings.gitea_token:
        send_text(chat_id, "❌ Gitea 未配置，请联系管理员在 .env 中填写 GITEA_BASE_URL 和 GITEA_TOKEN。")
        return

    # ── 解析 PR URL ─────────────────────────────────────────────
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        send_text(
            chat_id,
            "❌ PR 链接格式错误，示例：\n"
            "/gitreview https://gitea.example.com/owner/repo/pulls/42",
        )
        return

    base_url = m.group(1)
    owner = m.group(2)
    repo = m.group(3)
    index = int(m.group(4))
    project = f"{owner}/{repo}"

    # ── 获取 PR 元信息 ───────────────────────────────────────────
    try:
        pr = gitea.get_pr(base_url, owner, repo, index)
    except Exception as e:
        logger.exception("[git_review] 获取 PR 失败 url=%s", pr_url)
        send_text(chat_id, f"❌ 获取 PR #{index} 失败：{e}")
        return

    state = pr.get("state", "")
    if state != "open":
        send_text(chat_id, f"❌ PR #{index} 当前状态为「{state}」，只能审查 open 状态的 PR。")
        return

    title = pr.get("title") or "（无标题）"
    pr_body = pr.get("body") or ""
    base_branch = (pr.get("base") or {}).get("ref") or ""

    # ── 生产分支保护 ──────────────────────────────────────────────
    if base_branch.lower() in _PROTECTED_BRANCHES:
        send_text(
            chat_id,
            f"🚫 PR #{index} 的目标分支为「{base_branch}」（生产/主干分支），"
            f"禁止通过机器人自动合并，请走正式发布流程。",
        )
        return

    # ── 封版检查 ─────────────────────────────────────────────────
    active_freeze = freeze.get_active_freeze(project)
    if active_freeze and not freeze.is_bugfix_pr(title, pr_body):
        scope_text = (
            f"项目「{project}」"
            if active_freeze["scope"] == "project"
            else "全局"
        )
        reason = active_freeze.get("reason", "")
        reason_part = f"\n原因：{reason}" if reason else ""
        send_text(
            chat_id,
            f"🔒 当前{scope_text}处于封版状态，PR #{index}「{title}」不属于 BUG 修复，已拒绝合并。{reason_part}",
        )
        return

    send_text(chat_id, f"🔍 正在审查 PR #{index}: {title} ...")

    # ── 获取 diff ────────────────────────────────────────────────
    try:
        diff = gitea.get_pr_diff(base_url, owner, repo, index)
    except Exception as e:
        logger.exception("[git_review] 获取 diff 失败 url=%s", pr_url)
        send_text(chat_id, f"❌ 获取 PR #{index} diff 失败：{e}")
        return

    if not diff.strip():
        send_text(chat_id, f"⚠️ PR #{index} diff 为空，可能没有文件变更。")
        return

    # ── 调用 AI 审查 ─────────────────────────────────────────────
    pr_body = pr_body or "（无描述）"
    user_content = (
        f"PR #{index}: {title}\n"
        f"描述: {pr_body}\n\n"
        f"--- diff ---\n{diff}"
    )
    messages = [
        {"role": "system", "content": GIT_REVIEW_PROMPT},
        {"role": "user", "content": user_content},
    ]

    try:
        msg = ai_client.call_once(messages)
        review_text = (msg.content or "").strip()
    except Exception as e:
        logger.exception("[git_review] AI 审查失败")
        send_text(chat_id, f"❌ AI 审查失败：{e}")
        return

    if not review_text:
        send_text(chat_id, "❌ AI 返回空结果，请重试。")
        return

    first_line = review_text.splitlines()[0]
    logger.info("[git_review] PR #%d 审查结论: %s", index, first_line)

    # ── 解析 verdict ─────────────────────────────────────────────
    if first_line.strip().startswith("[APPROVE]"):
        verdict = "APPROVE"
    elif first_line.strip().startswith("[REQUEST_CHANGES]"):
        verdict = "REQUEST_CHANGES"
    else:
        verdict = "REJECT"

    # ── 解析 AI 综合评分，并剥离 SCORE 行（不展示给用户）─────────
    score_match = _SCORE_RE.search(review_text)
    if score_match:
        score = max(0, min(100, int(score_match.group(1))))
        clean_review = _SCORE_RE.sub("", review_text).strip()
    else:
        score = _FALLBACK_SCORE[verdict]
        clean_review = review_text
    logger.info("[git_review] PR #%d score=%d", index, score)

    # ── 记录统计数据 ─────────────────────────────────────────────
    lines_added, lines_removed, files_changed = _parse_diff_stats(diff)
    stats.record_review(
        user_id=user_id,
        pr_author=pr.get("user", {}).get("login", "unknown"),
        pr_index=index,
        pr_title=title,
        pr_url=pr_url,
        verdict=verdict,
        score=score,
        lines_added=lines_added,
        lines_removed=lines_removed,
        files_changed=files_changed,
        review_summary=clean_review,
    )

    # ── 根据审查结论执行操作 ─────────────────────────────────────
    if verdict == "APPROVE":
        try:
            gitea.merge_pr(base_url, owner, repo, index)
            send_text(chat_id, f"✅ PR #{index} 已自动合并\n\n{clean_review}")
            if settings.bw_chat_id:
                pr_author = pr.get("user", {}).get("login", "unknown")
                threading.Thread(
                    target=test_plan.create_session,
                    args=(index, project, title, diff, chat_id, pr_author, user_id),
                    daemon=True,
                ).start()
        except Exception as e:
            logger.exception("[git_review] 合并 PR 失败")
            send_text(chat_id, f"✅ AI 审查通过，但合并失败：{e}\n\n{clean_review}")
    else:
        # 无论 REQUEST_CHANGES 还是 REJECT，都在 Gitea 留评论
        try:
            gitea.post_comment(base_url, owner, repo, index, clean_review)
        except Exception as e:
            logger.warning("[git_review] Gitea 留评论失败: %s", e)

        emoji = "⚠️" if verdict == "REQUEST_CHANGES" else "❌"
        send_text(chat_id, f"{emoji} PR #{index} 审查未通过，已在 Gitea PR 留评论\n\n{clean_review}")
