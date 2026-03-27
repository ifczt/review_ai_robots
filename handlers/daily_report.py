"""
每日代码审查工作日报。

每天 21:30 汇总当天所有 PR 审查记录，通过 AI 生成员工工作总结并发送到指定群。
目标群由 settings.daily_report_chat_id 配置；未配置则跳过。

日综合评分（0-100，用于绩效考核）：
  工作饱和度（0-40）+ 代码质量（0-40）+ 技术难度（0-20）
  重点：一天只提交 1 个微小 PR 的员工，工作饱和度极低，整体日分会偏低。
"""
import logging
from datetime import date

from ai import client as ai_client
from ai.prompts import DAILY_REPORT_PROMPT
from config import settings
from infra import stats
from infra.feishu import send_text

logger = logging.getLogger(__name__)

_VERDICT_EMOJI = {
    "APPROVE": "✅",
    "REQUEST_CHANGES": "⚠️",
    "REJECT": "❌",
}


def send_daily_report() -> None:
    """生成并发送今日代码审查日报（21:30 由调度线程调用）。"""
    if not settings.daily_report_chat_id:
        logger.info("[daily_report] daily_report_chat_id 未配置，跳过日报")
        return

    records = stats.get_daily_records()
    today_str = date.today().strftime("%Y-%m-%d")

    if not records:
        send_text(
            settings.daily_report_chat_id,
            f"📊 代码审查日报 · {today_str}\n\n今日暂无 PR 审查记录。",
        )
        logger.info("[daily_report] 今日无记录，已发送空日报")
        return

    # ── 构建给 AI 的结构化输入（按员工分组，含完整统计）──────────
    by_author: dict[str, list[dict]] = {}
    for r in records:
        by_author.setdefault(r["pr_author"], []).append(r)

    sections: list[str] = [f"日期：{today_str}\n"]
    for author, rs in sorted(by_author.items()):
        total_added   = sum(r.get("lines_added", 0)   for r in rs)
        total_removed = sum(r.get("lines_removed", 0) for r in rs)
        total_files   = sum(r.get("files_changed", 0) for r in rs)
        sections.append(
            f"【{author}】{len(rs)} 个PR  "
            f"总变更：+{total_added}/-{total_removed} 行  {total_files} 个文件"
        )
        for r in rs:
            sections.append(
                f"  PR #{r['pr_index']} [{r['verdict']}] 质量分:{r['score']} "
                f"+{r.get('lines_added',0)}/-{r.get('lines_removed',0)}行 "
                f"{r.get('files_changed',0)}文件  "
                f"标题:{r['pr_title']}  "
                f"摘要:{r['review_summary'][:120]}"
            )
        sections.append("")

    ai_input = "\n".join(sections)

    try:
        messages = [
            {"role": "system", "content": DAILY_REPORT_PROMPT},
            {"role": "user", "content": ai_input},
        ]
        msg = ai_client.call_once(messages)
        report_text = (msg.content or "").strip()
        if not report_text:
            raise ValueError("AI 返回空内容")
    except Exception as e:
        logger.warning("[daily_report] AI 生成失败，使用兜底模板: %s", e)
        report_text = _fallback_report(by_author, today_str)

    send_text(settings.daily_report_chat_id, report_text)
    logger.info("[daily_report] 日报已发送，共 %d 条记录", len(records))


def _fallback_report(by_author: dict[str, list[dict]], today_str: str) -> str:
    """AI 不可用时的纯文本兜底日报（含工作量数据）。"""
    lines = [f"📊 代码审查日报 · {today_str}\n"]

    for author, rs in sorted(by_author.items()):
        total_added   = sum(r.get("lines_added", 0)   for r in rs)
        total_removed = sum(r.get("lines_removed", 0) for r in rs)
        total_files   = sum(r.get("files_changed", 0) for r in rs)
        avg_score     = int(sum(r["score"] for r in rs) / len(rs))
        approves  = sum(1 for r in rs if r["verdict"] == "APPROVE")
        changes   = sum(1 for r in rs if r["verdict"] == "REQUEST_CHANGES")
        rejects   = sum(1 for r in rs if r["verdict"] == "REJECT")

        lines.append(
            f"👤 **{author}** — {len(rs)} 个 PR  "
            f"+{total_added}/-{total_removed} 行  {total_files} 文件  "
            f"质量均分 {avg_score} 分"
        )
        lines.append(f"   ✅通过 {approves}  ⚠️需修改 {changes}  ❌拒绝 {rejects}")
        for r in rs:
            emoji = _VERDICT_EMOJI.get(r["verdict"], "•")
            added   = r.get("lines_added", 0)
            removed = r.get("lines_removed", 0)
            lines.append(
                f"   {emoji} PR #{r['pr_index']}（质量{r['score']}分 +{added}/-{removed}行）"
                f"：{r['pr_title']}"
            )
        lines.append("")

    return "\n".join(lines)
