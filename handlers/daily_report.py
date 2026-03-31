"""Daily report generation for group and personal delivery."""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from ai import client as ai_client
from ai.prompts import DAILY_REPORT_PROMPT, PERSONAL_DAILY_REPORT_PROMPT
from config import settings
from infra import stats
from infra.feishu import send_text, send_text_to_user

logger = logging.getLogger(__name__)

_VERDICT_EMOJI = {
    "APPROVE": "✅",
    "REQUEST_CHANGES": "⚠️",
    "REJECT": "❌",
}


def send_daily_report(target_date: date | None = None) -> None:
    """Send the team daily review report to the configured group."""
    if not settings.daily_report_chat_id:
        logger.info("[daily_report] daily_report_chat_id is empty, skip group report")
        return

    report_date = target_date or date.today()
    records = stats.get_daily_records(report_date)
    report_text = build_group_report(records, report_date)
    send_text(settings.daily_report_chat_id, report_text)
    logger.info(
        "[daily_report] group report sent date=%s records=%d",
        report_date.isoformat(),
        len(records),
    )


def send_private_daily_reports(target_date: date) -> None:
    """Send one personal review report per configured author."""
    recipients = get_private_recipients()
    if not recipients:
        logger.info("[daily_report] private recipients are empty, skip personal reports")
        return

    records = stats.get_daily_records(target_date)
    by_author = _group_records_by_author(records)

    for author, open_id in recipients.items():
        try:
            report_text = build_personal_report(author, by_author.get(author, []), target_date)
            send_text_to_user(open_id, report_text)
            logger.info(
                "[daily_report] personal report sent date=%s author=%s records=%d",
                target_date.isoformat(),
                author,
                len(by_author.get(author, [])),
            )
        except Exception:
            logger.exception(
                "[daily_report] failed to send personal report date=%s author=%s",
                target_date.isoformat(),
                author,
            )


def resolve_private_report_date(base_date: date | None = None) -> date:
    """Resolve the target date for personal reports from settings."""
    current = base_date or date.today()
    return current - timedelta(days=max(settings.daily_report_lookback_days, 0))


def send_default_private_daily_reports(base_date: date | None = None) -> date:
    """Send personal reports using the configured lookback window."""
    target_date = resolve_private_report_date(base_date)
    send_private_daily_reports(target_date)
    return target_date


def get_private_recipients() -> dict[str, str]:
    raw = (settings.daily_report_private_recipients or "").strip()
    if not raw:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        logger.exception("[daily_report] invalid DAILY_REPORT_PRIVATE_RECIPIENTS json")
        return {}

    if not isinstance(data, dict):
        logger.error("[daily_report] DAILY_REPORT_PRIVATE_RECIPIENTS must be a JSON object")
        return {}

    recipients: dict[str, str] = {}
    for author, open_id in data.items():
        author_key = str(author).strip()
        open_id_value = str(open_id).strip()
        if author_key and open_id_value:
            recipients[author_key] = open_id_value
    return recipients


def build_group_report(records: list[dict], report_date: date) -> str:
    today_str = report_date.strftime("%Y-%m-%d")

    if not records:
        return f"📊 代码审查日报 · {today_str}\n\n今日暂无 PR 审查记录。"

    by_author = _group_records_by_author(records)
    ai_input = _build_group_ai_input(by_author, today_str)

    try:
        messages = [
            {"role": "system", "content": DAILY_REPORT_PROMPT},
            {"role": "user", "content": ai_input},
        ]
        msg = ai_client.call_once(messages)
        report_text = (msg.content or "").strip()
        if not report_text:
            raise ValueError("empty AI group report")
        return report_text
    except Exception as exc:
        logger.warning("[daily_report] group AI report failed, fallback to template: %s", exc)
        return _fallback_group_report(by_author, today_str)


def build_personal_report(author: str, records: list[dict], report_date: date) -> str:
    report_date_str = report_date.strftime("%Y-%m-%d")
    if not records:
        return (
            f"📝 个人工作小结 · {report_date_str}\n\n"
            f"{author} 今天暂无代码审查记录。"
        )

    total_added = sum(r.get("lines_added", 0) for r in records)
    total_removed = sum(r.get("lines_removed", 0) for r in records)
    total_files = sum(r.get("files_changed", 0) for r in records)
    avg_score = int(sum(r["score"] for r in records) / len(records))
    approves = sum(1 for r in records if r["verdict"] == "APPROVE")
    changes = sum(1 for r in records if r["verdict"] == "REQUEST_CHANGES")
    rejects = sum(1 for r in records if r["verdict"] == "REJECT")

    lines = [
        f"日期：{report_date_str}",
        f"员工：{author}",
        f"今日审查 PR：{len(records)} 个",
        f"总变更规模：+{total_added}/-{total_removed} 行，{total_files} 个文件",
        f"结论分布：APPROVE {approves} / REQUEST_CHANGES {changes} / REJECT {rejects}",
        f"平均质量分（仅供参考）：{avg_score}",
        "",
        "PR 明细：",
    ]
    for record in records:
        lines.append(
            "PR #{pr_index} [{verdict}] 质量分 {score} "
            "+{lines_added}/-{lines_removed} 行 {files_changed} 文件\n"
            "标题：{pr_title}\n"
            "摘要：{review_summary}".format(
                pr_index=record["pr_index"],
                verdict=record["verdict"],
                score=record["score"],
                lines_added=record.get("lines_added", 0),
                lines_removed=record.get("lines_removed", 0),
                files_changed=record.get("files_changed", 0),
                pr_title=record["pr_title"],
                review_summary=record["review_summary"][:200],
            )
        )

    try:
        messages = [
            {"role": "system", "content": PERSONAL_DAILY_REPORT_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]
        msg = ai_client.call_once(messages)
        report_text = (msg.content or "").strip()
        if not report_text:
            raise ValueError("empty AI personal report")
        return report_text
    except Exception as exc:
        logger.warning(
            "[daily_report] personal AI report failed, fallback to template author=%s err=%s",
            author,
            exc,
        )
        return _fallback_personal_report(author, records, report_date_str)


def _group_records_by_author(records: list[dict]) -> dict[str, list[dict]]:
    by_author: dict[str, list[dict]] = {}
    for record in records:
        by_author.setdefault(record["pr_author"], []).append(record)
    return by_author


def _build_group_ai_input(by_author: dict[str, list[dict]], today_str: str) -> str:
    sections: list[str] = [f"日期：{today_str}", ""]
    for author, author_records in sorted(by_author.items()):
        total_added = sum(r.get("lines_added", 0) for r in author_records)
        total_removed = sum(r.get("lines_removed", 0) for r in author_records)
        total_files = sum(r.get("files_changed", 0) for r in author_records)
        sections.append(
            f"【{author}】{len(author_records)} 个 PR，总变更 +{total_added}/-{total_removed} 行，"
            f"{total_files} 个文件"
        )
        for record in author_records:
            sections.append(
                f"  PR #{record['pr_index']} [{record['verdict']}] 质量分 {record['score']} "
                f"+{record.get('lines_added', 0)}/-{record.get('lines_removed', 0)} 行，"
                f"{record.get('files_changed', 0)} 文件，标题：{record['pr_title']}，"
                f"摘要：{record['review_summary'][:120]}"
            )
        sections.append("")
    return "\n".join(sections)


def _fallback_group_report(by_author: dict[str, list[dict]], today_str: str) -> str:
    lines = [f"📊 代码审查日报 · {today_str}", ""]

    for author, author_records in sorted(by_author.items()):
        total_added = sum(r.get("lines_added", 0) for r in author_records)
        total_removed = sum(r.get("lines_removed", 0) for r in author_records)
        total_files = sum(r.get("files_changed", 0) for r in author_records)
        avg_score = int(sum(r["score"] for r in author_records) / len(author_records))
        approves = sum(1 for r in author_records if r["verdict"] == "APPROVE")
        changes = sum(1 for r in author_records if r["verdict"] == "REQUEST_CHANGES")
        rejects = sum(1 for r in author_records if r["verdict"] == "REJECT")

        lines.append(
            f"👤 {author} · {len(author_records)} 个 PR · +{total_added}/-{total_removed} 行 · "
            f"{total_files} 文件 · 平均质量分 {avg_score}"
        )
        lines.append(f"   ✅ 通过 {approves}  ⚠️ 需修改 {changes}  ❌ 拒绝 {rejects}")

        for record in author_records:
            emoji = _VERDICT_EMOJI.get(record["verdict"], "•")
            lines.append(
                f"   {emoji} PR #{record['pr_index']}（质量分 {record['score']}，"
                f"+{record.get('lines_added', 0)}/-{record.get('lines_removed', 0)} 行）"
                f"：{record['pr_title']}"
            )
        lines.append("")

    return "\n".join(lines)


def _fallback_personal_report(author: str, records: list[dict], report_date_str: str) -> str:
    approves = sum(1 for r in records if r["verdict"] == "APPROVE")
    changes = sum(1 for r in records if r["verdict"] == "REQUEST_CHANGES")
    rejects = sum(1 for r in records if r["verdict"] == "REJECT")
    titles = [r["pr_title"] for r in records[:3]]

    focus_parts: list[str] = []
    if titles:
        focus_parts.append("、".join(titles))
    if len(records) > 3:
        focus_parts.append(f"等 {len(records)} 个 PR")
    focus_text = "".join(focus_parts) if focus_parts else "多项代码审查工作"

    lines = [
        f"📝 个人工作小结 · {report_date_str}",
        "",
        f"**{author} 今天主要完成：**",
        f"- 处理了 {len(records)} 个代码审查任务，主要涉及 {focus_text}。",
        f"- 其中通过 {approves} 个，需要修改 {changes} 个，拒绝 {rejects} 个。",
        "",
        "**后续关注：**",
    ]
    if changes or rejects:
        lines.append("- 有提出修改意见的 PR，建议优先跟进这些反馈是否已处理完毕。")
    else:
        lines.append("- 今日审查项整体已完成，如有后续联调或验证，可继续跟进上线结果。")

    return "\n".join(lines)
