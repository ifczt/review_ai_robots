"""
服务器状态监控采集器。

通过 SSH 并发采集各地区运维服务器的关键指标：
  - CPU 使用率（%）—— 读 /proc/stat 两次差值，比 top 稳定
  - 内存使用率（%）—— 读 /proc/meminfo，MemAvailable 模式更准确
  - 磁盘使用率（根分区，%）
  - 系统负载（1/5/15 分钟）—— 读 /proc/loadavg
  - Supervisor RUNNING 进程数
  - 单进程高 CPU 占用列表

每次采集后与配置阈值对比，返回告警项列表。
告警带冷却去重：同一地区同一指标在冷却期内只报一次。
采集失败后自动重试一次（等待 3 秒）。
"""
from __future__ import annotations

import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Optional

from config import settings
from infra import ssh as _ssh

logger = logging.getLogger(__name__)

# ── 数据结构 ──────────────────────────────────────────────────────────────────

class ServerMetrics:
    """单台服务器的采集结果。"""

    def __init__(self, region: str):
        self.region: str = region
        self.label: str = _ssh.REGION_LABELS.get(region, region)
        self.collected_at: datetime = datetime.now()

        self.cpu_percent: Optional[float] = None     # CPU 使用率 %
        self.mem_percent: Optional[float] = None     # 内存使用率 %
        self.disk_percent: Optional[float] = None    # 磁盘 / 使用率 %
        self.load_1: Optional[float] = None          # 1 分钟负载
        self.load_5: Optional[float] = None          # 5 分钟负载
        self.load_15: Optional[float] = None         # 15 分钟负载
        self.supervisor_running: Optional[int] = None   # RUNNING 进程数
        self.supervisor_total: Optional[int] = None     # 全部进程数
        # 列表元素: {"pid": int, "name": str, "cpu": float}
        # 只包含 CPU 超过 server_monitor_process_cpu_threshold 的进程
        self.process_high_cpu: list[dict] = []

        self.error: Optional[str] = None             # 采集失败时的错误信息

    @property
    def ok(self) -> bool:
        return self.error is None

    def alerts(self) -> list[str]:
        """返回当前超阈值的指标名列表（用于告警去重 key）。"""
        items: list[str] = []
        if self.cpu_percent is not None and self.cpu_percent >= settings.server_monitor_cpu_threshold:
            items.append("cpu")
        if self.mem_percent is not None and self.mem_percent >= settings.server_monitor_mem_threshold:
            items.append("mem")
        if self.disk_percent is not None and self.disk_percent >= settings.server_monitor_disk_threshold:
            items.append("disk")
        if self.process_high_cpu:
            items.append("proc_cpu")
        return items


# ── 告警冷却表 ────────────────────────────────────────────────────────────────

_alert_lock = threading.Lock()
# key: "{region}:{metric}"  value: 上次告警时间
_last_alert_time: dict[str, datetime] = {}


def _should_alert(region: str, metric: str) -> bool:
    """判断该地区该指标是否需要发送告警（冷却期外则允许）。"""
    key = f"{region}:{metric}"
    now = datetime.now()
    cooldown_minutes = settings.server_monitor_alert_cooldown_minutes
    with _alert_lock:
        last = _last_alert_time.get(key)
        if last is None or (now - last).total_seconds() >= cooldown_minutes * 60:
            _last_alert_time[key] = now
            return True
    return False


def reset_alert_cooldown(region: str, metric: str) -> None:
    """指标恢复正常时清除冷却记录，以便下次触发可立即告警。"""
    key = f"{region}:{metric}"
    with _alert_lock:
        _last_alert_time.pop(key, None)


def get_new_alerts(metrics: ServerMetrics) -> list[str]:
    """
    返回本次采集中需要实际发出告警的指标列表（未在冷却期内）。
    同时对已恢复正常的指标重置冷却记录。
    """
    all_metrics = ["cpu", "mem", "disk", "proc_cpu"]
    triggered = set(metrics.alerts())
    new_alerts: list[str] = []
    for m in all_metrics:
        if m in triggered:
            if _should_alert(metrics.region, m):
                new_alerts.append(m)
        else:
            reset_alert_cooldown(metrics.region, m)
    return new_alerts


# ── 解析函数 ──────────────────────────────────────────────────────────────────

def _parse_cpu_stat(output: str) -> Optional[float]:
    """
    解析两次 /proc/stat 快照结果，计算 CPU 实际使用率。
    awk 预处理后每行格式：total_jiffies  idle_jiffies
    两行对应 0.5s 间隔的两次采样，取差值计算。
    """
    lines = [l.strip() for l in output.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return None
    try:
        t1, i1 = map(float, lines[0].split())
        t2, i2 = map(float, lines[1].split())
        dt = t2 - t1
        if dt <= 0:
            return None
        return round((1.0 - (i2 - i1) / dt) * 100, 1)
    except (ValueError, IndexError):
        return None


def _parse_mem_proc(output: str) -> Optional[float]:
    """
    解析 /proc/meminfo 输出，返回内存使用率。
    用 MemAvailable 而非 MemFree，包含可回收缓存，更接近实际可用内存。
    """
    total = avail = None
    for line in output.splitlines():
        if line.startswith("MemTotal:"):
            m = re.search(r"(\d+)", line)
            if m:
                total = int(m.group(1))
        elif line.startswith("MemAvailable:"):
            m = re.search(r"(\d+)", line)
            if m:
                avail = int(m.group(1))
    if total and avail is not None and total > 0:
        return round((total - avail) / total * 100, 1)
    return None


def _parse_disk(output: str) -> Optional[float]:
    """解析 df 输出，返回根分区使用率百分比。"""
    m = re.search(r"(\d+)%", output)
    return float(m.group(1)) if m else None


def _parse_load_proc(output: str) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """
    解析 /proc/loadavg 输出。
    格式：1.20 1.15 1.08 2/456 12345
    返回 (load1, load5, load15)。
    """
    parts = output.strip().split()
    if len(parts) >= 3:
        try:
            return float(parts[0]), float(parts[1]), float(parts[2])
        except ValueError:
            pass
    return None, None, None


def _parse_supervisor(output: str) -> tuple[Optional[int], Optional[int]]:
    """
    解析 supervisorctl status 输出，返回 (running_count, total_count)。
    过滤掉 Python 警告行和其他非进程状态行。
    """
    if not output.strip():
        return None, None

    _STATUS_RE = re.compile(
        r"^\S+\s+(RUNNING|STOPPED|STARTING|STOPPING|EXITED|FATAL|UNKNOWN)",
        re.IGNORECASE,
    )
    process_lines = [
        l for l in output.strip().splitlines()
        if _STATUS_RE.match(l.strip())
    ]
    if not process_lines:
        return None, None

    total = len(process_lines)
    running = sum(1 for l in process_lines if "RUNNING" in l.upper())
    return running, total


def _parse_procs(output: str) -> list[dict]:
    """
    解析 ps 输出（字段顺序：PID COMMAND %CPU），返回超过阈值的进程列表。
    每个元素为 {"pid": int, "name": str, "cpu": float}，按 CPU 降序。
    阈值 <= 0 时直接返回空列表（功能关闭）。
    """
    threshold = settings.server_monitor_process_cpu_threshold
    if threshold <= 0 or not output.strip():
        return []

    results: list[dict] = []
    for line in output.strip().splitlines():
        parts = line.strip().split(None, 2)   # PID  COMMAND  %CPU
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            name = parts[1]
            cpu = float(parts[2])
        except (ValueError, IndexError):
            continue   # 跳过标题行（PID COMMAND %CPU）或格式异常行
        if cpu >= threshold:
            results.append({"pid": pid, "name": name, "cpu": cpu})

    results.sort(key=lambda x: x["cpu"], reverse=True)
    return results


# ── 采集脚本 ──────────────────────────────────────────────────────────────────
#
# 改动说明（对比旧版）：
#   CPU  —— 两次读 /proc/stat，awk 计算 total/idle jiffies，0.5s 采样间隔
#            取代 top -bn1：跨发行版输出一致，不受终端宽度影响，更快更可靠
#   内存 —— 读 /proc/meminfo，用 MemAvailable 而非 MemFree
#            取代 free -m：格式固定，无表头差异问题
#   负载 —— 读 /proc/loadavg
#            取代 uptime：直接读内核文件，无需正则解析英文句子
#   Supervisor —— 加 timeout 10 防止卡住整个脚本
#   超时 —— 脚本总超时由 35s 降至 25s（节省 0.5s sleep 后还有余量）

_COLLECT_SCRIPT = r"""
echo "=CPU="
awk 'NR==1{t=0;for(i=2;i<=NF;i++)t+=$i;print t,$5+$6}' /proc/stat
sleep 0.5
awk 'NR==1{t=0;for(i=2;i<=NF;i++)t+=$i;print t,$5+$6}' /proc/stat
echo "=MEM="
grep -E '^MemTotal:|^MemAvailable:' /proc/meminfo
echo "=DISK="
df / 2>/dev/null | tail -1
echo "=LOAD="
cat /proc/loadavg
echo "=SUPERVISOR="
timeout 10 sudo supervisorctl status 2>&1; true
echo "=PROCS="
ps -eo pid,comm,%cpu --sort=-%cpu 2>/dev/null | head -16
""".strip()


# ── 单地区采集 ────────────────────────────────────────────────────────────────

def _do_collect(region: str) -> ServerMetrics:
    """执行一次采集，不含重试逻辑。抛出异常由 collect() 处理。"""
    m = ServerMetrics(region)
    rc, output = _ssh.execute(_COLLECT_SCRIPT, region, timeout=25)
    sections = _split_sections(output)

    m.cpu_percent             = _parse_cpu_stat(sections.get("CPU", ""))
    m.mem_percent             = _parse_mem_proc(sections.get("MEM", ""))
    m.disk_percent            = _parse_disk(sections.get("DISK", ""))
    m.load_1, m.load_5, m.load_15 = _parse_load_proc(sections.get("LOAD", ""))
    m.supervisor_running, m.supervisor_total = _parse_supervisor(sections.get("SUPERVISOR", ""))
    m.process_high_cpu        = _parse_procs(sections.get("PROCS", ""))

    logger.debug(
        "[monitor] %s 采集完成 cpu=%.1f%% mem=%.1f%% disk=%.1f%% high_cpu_procs=%d",
        region,
        m.cpu_percent or 0,
        m.mem_percent or 0,
        m.disk_percent or 0,
        len(m.process_high_cpu),
    )
    return m


def collect(region: str) -> ServerMetrics:
    """
    SSH 到指定地区执行采集脚本，解析并返回 ServerMetrics。
    失败后自动等 3 秒重试一次，再次失败才标记 error。
    """
    for attempt in range(2):
        try:
            return _do_collect(region)
        except Exception as e:
            if attempt == 0:
                logger.warning("[monitor] %s 第1次采集失败，3秒后重试: %s", region, e)
                time.sleep(3)
            else:
                m = ServerMetrics(region)
                m.error = str(e)
                logger.error("[monitor] %s 采集失败（已重试）: %s", region, e)
                return m

    # 理论上不会走到这里
    m = ServerMetrics(region)
    m.error = "unknown error"
    return m


def _split_sections(output: str) -> dict[str, str]:
    """将脚本输出按 =SECTION= 标记拆分为字典。"""
    sections: dict[str, str] = {}
    current_key: str | None = None
    current_lines: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("=") and stripped.endswith("=") and len(stripped) > 2:
            if current_key:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key = stripped[1:-1]
            current_lines = []
        else:
            if current_key:
                current_lines.append(line)
    if current_key:
        sections[current_key] = "\n".join(current_lines).strip()
    return sections


def collect_all() -> list[ServerMetrics]:
    """并发采集所有已配置地区，返回结果列表（顺序与配置文件一致）。"""
    regions = _ssh.list_regions()
    results: dict[str, ServerMetrics] = {}

    with ThreadPoolExecutor(max_workers=len(regions) or 1) as executor:
        future_to_region = {executor.submit(collect, r): r for r in regions}
        for future in as_completed(future_to_region):
            r = future_to_region[future]
            try:
                results[r] = future.result()
            except Exception as e:
                m = ServerMetrics(r)
                m.error = str(e)
                results[r] = m

    # 保持配置文件中的地区顺序
    return [results[r] for r in regions if r in results]
