"""
服务器状态监控处理器。

负责：
  1. 巡检播报 — 采集 → AI 汇总 → 发送到监控群
  2. 告警发送 — 单独格式化超阈值告警消息
  3. 每日汇报 — 聚合当天所有巡检数据 → AI 汇总 → 发送
  4. 指标历史记录 — 保留当天所有巡检快照供每日汇报使用

对外接口：
  - send_patrol_report()          — 执行一次巡检，汇报结果并在必要时发送告警
  - send_daily_server_report()    — 发送当天服务器健康汇报（定时调用）
  - query_status(region=None)     — 返回最近一次巡检的文本摘要（供 /status 命令）
"""
from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from ai import client as ai_client
from ai.prompts import SERVER_MONITOR_PATROL_PROMPT, SERVER_MONITOR_DAILY_REPORT_PROMPT
from config import settings
from infra import server_monitor as _mon
from infra import ssh as _ssh
from infra.feishu import send_text

logger = logging.getLogger(__name__)

# ── 当天巡检数据历史（用于每日汇报）───────────────────────────────────────────
_history_lock = threading.Lock()
_history: list[_mon.ServerMetrics] = []      # 当天所有巡检快照（扁平存储）
_history_date: Optional[date] = None         # 数据所属日期，跨天时自动清空

# 最近一次完整巡检结果（供 /status 快速查询）
_last_patrol: list[_mon.ServerMetrics] = []
_last_patrol_lock = threading.Lock()


def _get_monitor_chat_id() -> str:
    return settings.server_monitor_chat_id


def _store_metrics(metrics_list: list[_mon.ServerMetrics]) -> None:
    """将本次巡检结果存入历史（跨天自动清空）。"""
    global _history, _history_date
    today = date.today()
    with _history_lock:
        if _history_date != today:
            _history = []
            _history_date = today
        _history.extend(metrics_list)


def _build_patrol_input(metrics_list: list[_mon.ServerMetrics]) -> str:
    """将采集结果序列化为纯文字，作为 AI 的输入。"""
    lines = [f"采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", ""]
    cpu_t = settings.server_monitor_cpu_threshold
    mem_t = settings.server_monitor_mem_threshold
    disk_t = settings.server_monitor_disk_threshold

    for m in metrics_list:
        if not m.ok:
            lines.append(f"[{m.region}]（{m.label}）采集失败：{m.error}")
            continue

        def flag(val, threshold):
            return "⚠️" if val is not None and val >= threshold else ""

        cpu_str = f"{m.cpu_percent:.1f}%{flag(m.cpu_percent, cpu_t)}" if m.cpu_percent is not None else "N/A"
        mem_str = f"{m.mem_percent:.1f}%{flag(m.mem_percent, mem_t)}" if m.mem_percent is not None else "N/A"
        disk_str = f"{m.disk_percent:.1f}%{flag(m.disk_percent, disk_t)}" if m.disk_percent is not None else "N/A"
        load_str = (
            f"{m.load_1}/{m.load_5}/{m.load_15}"
            if m.load_1 is not None else "N/A"
        )
        sup_str = (
            f"{m.supervisor_running}/{m.supervisor_total}"
            if m.supervisor_running is not None else "N/A"
        )
        lines.append(
            f"[{m.region}]（{m.label}）"
            f"CPU {cpu_str}  内存 {mem_str}  磁盘 {disk_str}  "
            f"负载 {load_str}  Supervisor {sup_str}"
        )
        # 附加高 CPU 进程信息
        if m.process_high_cpu:
            proc_t = settings.server_monitor_process_cpu_threshold
            proc_lines = "  |  ".join(
                f"{p['name']}({p['pid']}) {p['cpu']:.1f}%"
                for p in m.process_high_cpu[:5]  # 最多展示 5 个
            )
            lines.append(
                f"  ⚠️ 高CPU进程 [{proc_t}%+]: {proc_lines}"
            )
    return "\n".join(lines)


def _ai_patrol_report(patrol_input: str) -> str:
    """调用 AI 生成巡检播报，失败则返回 fallback 文本。"""
    try:
        messages = [
            {"role": "system", "content": SERVER_MONITOR_PATROL_PROMPT},
            {"role": "user", "content": patrol_input},
        ]
        result = ai_client.call_once(messages)
        text = (result.content or "").strip()
        if text:
            return text
    except Exception as e:
        logger.warning("[server_status] AI 巡检播报失败，使用原始数据: %s", e)
    # Fallback：直接返回原始数据
    return f"🖥️ 服务器巡检播报 · {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n{patrol_input}"


def _build_alert_text(m: _mon.ServerMetrics, new_alerts: list[str]) -> str:
    """格式化告警消息。"""
    cpu_t = settings.server_monitor_cpu_threshold
    mem_t = settings.server_monitor_mem_threshold
    disk_t = settings.server_monitor_disk_threshold

    cfg = _ssh._get_config().get(m.region, {})
    host = f"{cfg.get('host', '?')}:{cfg.get('port', '?')}"

    lines = [
        f"🚨 服务器告警 · {m.region}（{m.label}）",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        "",
    ]
    label_map = {
        "cpu": ("CPU 使用率", m.cpu_percent, cpu_t, "%"),
        "mem": ("内存使用率", m.mem_percent, mem_t, "%"),
        "disk": ("磁盘 / 使用率", m.disk_percent, disk_t, "%"),
    }
    for key in new_alerts:
        if key == "proc_cpu":
            # 展示具体高 CPU 进程列表
            proc_t = settings.server_monitor_process_cpu_threshold
            lines.append(f"⚠️ 单进程 CPU 占用过高（阈值 {proc_t}%）")
            for p in m.process_high_cpu[:8]:  # 最多展示 8 个
                lines.append(f"   • {p['name']} (PID {p['pid']}) 占用 {p['cpu']:.1f}%")
        else:
            name, val, threshold, unit = label_map[key]
            val_str = f"{val:.1f}" if val is not None else "N/A"
            lines.append(f"⚠️ {name} {val_str}{unit}（阈值 {threshold}{unit}）")

    lines += ["", f"主机：{host}"]
    return "\n".join(lines)


def send_patrol_report() -> None:
    """
    执行一次完整巡检：
      1. 并发采集所有地区指标
      2. AI 汇总 → 发送巡检播报到监控群
      3. 检查告警（带冷却去重）→ 发送独立告警消息
    """
    chat_id = _get_monitor_chat_id()
    if not chat_id:
        logger.debug("[server_status] server_monitor_chat_id 未配置，跳过巡检")
        return

    logger.info("[server_status] 开始巡检...")
    metrics_list = _mon.collect_all()

    # 保存历史
    _store_metrics(metrics_list)
    with _last_patrol_lock:
        _last_patrol.clear()
        _last_patrol.extend(metrics_list)

    # AI 汇总播报
    patrol_input = _build_patrol_input(metrics_list)
    report_text = _ai_patrol_report(patrol_input)
    send_text(chat_id, report_text)

    # 逐地区检查告警
    for m in metrics_list:
        if not m.ok:
            continue
        new_alerts = _mon.get_new_alerts(m)
        if new_alerts:
            alert_text = _build_alert_text(m, new_alerts)
            send_text(chat_id, alert_text)
            logger.info("[server_status] %s 告警已发送: %s", m.region, new_alerts)


def send_daily_server_report() -> None:
    """
    发送当天服务器健康汇报（聚合全天所有巡检数据，通过 AI 生成）。
    """
    chat_id = _get_monitor_chat_id()
    if not chat_id:
        return

    today = date.today()
    with _history_lock:
        snapshot = list(_history) if _history_date == today else []

    if not snapshot:
        send_text(chat_id, f"📋 服务器每日状态汇报 · {today.isoformat()}\n\n今日暂无有效巡检数据。")
        return

    # 按地区聚合：计算均值、峰值、告警次数
    aggregated = _aggregate_history(snapshot)
    ai_input = _build_daily_ai_input(aggregated, today)

    try:
        messages = [
            {"role": "system", "content": SERVER_MONITOR_DAILY_REPORT_PROMPT},
            {"role": "user", "content": ai_input},
        ]
        result = ai_client.call_once(messages)
        text = (result.content or "").strip()
        if not text:
            raise ValueError("empty AI daily server report")
        send_text(chat_id, text)
    except Exception as e:
        logger.warning("[server_status] AI 每日汇报失败，使用 fallback: %s", e)
        send_text(chat_id, _fallback_daily_report(aggregated, today))

    logger.info("[server_status] 每日服务器汇报已发送，覆盖巡检次数=%d", len(snapshot))


def query_status(region: Optional[str] = None) -> str:
    """
    返回最近一次巡检的文本摘要（供 /status 命令使用）。
    如果 region 为 None，返回所有地区；否则只返回指定地区。
    触发一次新的实时采集以确保数据最新。
    """
    # 实时采集（无论是否配置了 chat_id，/status 命令都要能用）
    if region:
        if not _ssh.is_valid_region(region):
            return f"未知地区：{region}\n\n{_ssh.format_available()}"
        metrics_list = [_mon.collect(region)]
    else:
        metrics_list = _mon.collect_all()

    # 更新最近巡检记录
    if not region:
        _store_metrics(metrics_list)
        with _last_patrol_lock:
            _last_patrol.clear()
            _last_patrol.extend(metrics_list)

    patrol_input = _build_patrol_input(metrics_list)
    return _ai_patrol_report(patrol_input)


# ── 聚合与格式化辅助 ──────────────────────────────────────────────────────────

def _aggregate_history(snapshot: list[_mon.ServerMetrics]) -> dict[str, dict]:
    """按地区聚合当天巡检历史，统计均值/峰值/告警次数。"""
    cpu_t = settings.server_monitor_cpu_threshold
    mem_t = settings.server_monitor_mem_threshold
    disk_t = settings.server_monitor_disk_threshold

    region_data: dict[str, dict] = defaultdict(lambda: {
        "label": "",
        "cpu_vals": [], "mem_vals": [], "disk_vals": [],
        "load1_vals": [],
        "cpu_alerts": 0, "mem_alerts": 0, "disk_alerts": 0,
        "fail_count": 0, "total_count": 0,
    })

    for m in snapshot:
        d = region_data[m.region]
        d["label"] = m.label
        d["total_count"] += 1
        if not m.ok:
            d["fail_count"] += 1
            continue
        if m.cpu_percent is not None:
            d["cpu_vals"].append(m.cpu_percent)
            if m.cpu_percent >= cpu_t:
                d["cpu_alerts"] += 1
        if m.mem_percent is not None:
            d["mem_vals"].append(m.mem_percent)
            if m.mem_percent >= mem_t:
                d["mem_alerts"] += 1
        if m.disk_percent is not None:
            d["disk_vals"].append(m.disk_percent)
            if m.disk_percent >= disk_t:
                d["disk_alerts"] += 1
        if m.load_1 is not None:
            d["load1_vals"].append(m.load_1)

    return dict(region_data)


def _avg(vals: list) -> str:
    if not vals:
        return "N/A"
    return f"{sum(vals)/len(vals):.1f}"


def _max_val(vals: list) -> str:
    if not vals:
        return "N/A"
    return f"{max(vals):.1f}"


def _build_daily_ai_input(aggregated: dict[str, dict], today: date) -> str:
    lines = [f"日期：{today.isoformat()}", f"统计地区数：{len(aggregated)}", ""]
    for region, d in aggregated.items():
        lines.append(
            f"[{region}]（{d['label']}）巡检 {d['total_count']} 次，"
            f"失败 {d['fail_count']} 次"
        )
        lines.append(
            f"  CPU 均值 {_avg(d['cpu_vals'])}%，峰值 {_max_val(d['cpu_vals'])}%，"
            f"告警 {d['cpu_alerts']} 次"
        )
        lines.append(
            f"  内存 均值 {_avg(d['mem_vals'])}%，峰值 {_max_val(d['mem_vals'])}%，"
            f"告警 {d['mem_alerts']} 次"
        )
        lines.append(
            f"  磁盘 均值 {_avg(d['disk_vals'])}%，峰值 {_max_val(d['disk_vals'])}%，"
            f"告警 {d['disk_alerts']} 次"
        )
        if d["load1_vals"]:
            lines.append(
                f"  负载(1m) 均值 {_avg(d['load1_vals'])}，峰值 {_max_val(d['load1_vals'])}"
            )
        lines.append("")
    return "\n".join(lines)


def _fallback_daily_report(aggregated: dict[str, dict], today: date) -> str:
    lines = [f"📋 服务器每日状态汇报 · {today.isoformat()}", ""]
    has_alert = False
    for region, d in aggregated.items():
        alert_total = d["cpu_alerts"] + d["mem_alerts"] + d["disk_alerts"]
        if alert_total > 0:
            has_alert = True
        status = "⚠️" if alert_total > 0 else "✅"
        lines.append(
            f"{status} {region}（{d['label']}）"
            f"CPU均值 {_avg(d['cpu_vals'])}%  内存均值 {_avg(d['mem_vals'])}%  "
            f"磁盘均值 {_avg(d['disk_vals'])}%  "
            f"告警 {alert_total} 次"
        )
    lines.append("")
    if has_alert:
        lines.append("📌 请关注触发告警的地区，检查进程和资源占用情况。")
    else:
        lines.append("今日各地区运行正常，无告警事件。")
    return "\n".join(lines)
