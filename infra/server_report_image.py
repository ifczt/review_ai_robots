"""Server patrol report image rendering helpers."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from app_paths import app_path
from config import settings
from infra import server_monitor as _mon

_RENDERER_DIR = app_path("tools", "server_monitor_chart")
_RENDERER_ENTRY = _RENDERER_DIR / "render.mjs"
_PACKAGE_JSON = _RENDERER_DIR / "package.json"
_NODE_MODULES = _RENDERER_DIR / "node_modules"


def _state(m: _mon.ServerMetrics) -> str:
    if not m.ok:
        return "fail"
    if m.alerts():
        return "warn"
    return "ok"


def _payload(metrics_list: list[_mon.ServerMetrics]) -> dict:
    return {
        "timestamp": metrics_list[0].collected_at.strftime("%Y-%m-%d %H:%M:%S") if metrics_list else "",
        "thresholds": {
            "cpu": settings.server_monitor_cpu_threshold,
            "mem": settings.server_monitor_mem_threshold,
            "disk": settings.server_monitor_disk_threshold,
            "proc": settings.server_monitor_process_cpu_threshold,
        },
        "metrics": [
            {
                "region": m.region,
                "label": m.label,
                "state": _state(m),
                "cpu_percent": m.cpu_percent,
                "mem_percent": m.mem_percent,
                "disk_percent": m.disk_percent,
                "load": [m.load_1, m.load_5, m.load_15],
                "supervisor": {
                    "running": m.supervisor_running,
                    "total": m.supervisor_total,
                },
                "alerts": m.alerts() if m.ok else [],
                "error": m.error,
            }
            for m in metrics_list
        ],
    }


def _assert_renderer_ready() -> None:
    if not _PACKAGE_JSON.exists() or not _RENDERER_ENTRY.exists():
        raise FileNotFoundError(f"renderer files missing: {_RENDERER_DIR}")
    if not _NODE_MODULES.exists():
        raise RuntimeError(
            f"renderer dependencies are not installed: {_NODE_MODULES}\n"
            f"run `npm install` in `{_RENDERER_DIR}` first"
        )


def render_patrol_image(metrics_list: list[_mon.ServerMetrics]) -> Path:
    """Render patrol metrics to a local PNG using the BizCharts renderer."""
    _assert_renderer_ready()

    payload = _payload(metrics_list)
    temp_dir = Path(tempfile.mkdtemp(prefix="server_patrol_", dir=None))
    input_path = temp_dir / "payload.json"
    output_path = temp_dir / "patrol.png"
    input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    proc = subprocess.run(
        ["node", str(_RENDERER_ENTRY), "--input", str(input_path), "--output", str(output_path)],
        cwd=str(_RENDERER_DIR),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=90,
    )
    if proc.returncode != 0 or not output_path.exists():
        raise RuntimeError(
            "render patrol image failed\n"
            f"stdout:\n{proc.stdout}\n"
            f"stderr:\n{proc.stderr}"
        )
    return output_path
