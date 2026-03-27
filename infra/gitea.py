"""Gitea HTTP API 封装（同步，与项目其他 infra 保持一致）。"""
import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)

# diff 文本超出此长度时截断，防止超出 AI 上下文窗口
_DIFF_MAX_CHARS = 6000

# httpx 请求超时（秒）
_TIMEOUT = 20


def _headers() -> dict:
    return {"Authorization": f"token {settings.gitea_token}"}


def _api(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/api/v1{path}"


def get_pr(base_url: str, owner: str, repo: str, index: int) -> dict:
    """获取 PR 基本信息（标题、描述、状态、作者等）。"""
    url = _api(base_url, f"/repos/{owner}/{repo}/pulls/{index}")
    resp = httpx.get(url, headers=_headers(), timeout=_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def get_pr_diff(base_url: str, owner: str, repo: str, index: int) -> str:
    """获取 PR 的 unified diff 文本，超长则截断。"""
    # 使用 API 端点获取 diff，带 token 认证，避免私有仓库 303 重定向到登录页
    url = _api(base_url, f"/repos/{owner}/{repo}/pulls/{index}.diff")
    resp = httpx.get(url, headers=_headers(), timeout=30, follow_redirects=False)
    resp.raise_for_status()
    diff = resp.text
    # 防御：内容应以 diff 头或 commit 信息开头，否则可能是错误页面
    if diff and not diff.lstrip().startswith(("diff --git", "commit ", "From ")):
        logger.warning("[gitea] get_pr_diff 返回内容疑似非 diff，前50字符: %r", diff[:50])
        raise ValueError("获取到的内容不是有效的 diff，请确认 GITEA_TOKEN 配置正确且有仓库读权限")
    if len(diff) > _DIFF_MAX_CHARS:
        diff = diff[:_DIFF_MAX_CHARS] + f"\n\n... [diff 超过 {_DIFF_MAX_CHARS} 字符，已截断，请在 Gitea 查看完整内容]"
    return diff


def post_comment(base_url: str, owner: str, repo: str, index: int, body: str) -> None:
    """在 PR（Issue）下发表评论。"""
    # Gitea 中 PR 和 Issue 共用同一套评论 API
    url = _api(base_url, f"/repos/{owner}/{repo}/issues/{index}/comments")
    resp = httpx.post(url, headers=_headers(), json={"body": body}, timeout=_TIMEOUT)
    resp.raise_for_status()


def merge_pr(base_url: str, owner: str, repo: str, index: int) -> None:
    """合并 PR，使用配置的合并方式。"""
    url = _api(base_url, f"/repos/{owner}/{repo}/pulls/{index}/merge")
    resp = httpx.post(url, headers=_headers(), json={
        "Do": settings.gitea_auto_merge_method,
        "merge_message_field": "Auto merged by AI code review bot",
    }, timeout=_TIMEOUT)
    resp.raise_for_status()
