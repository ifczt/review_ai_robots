"""
SSH 连接管理器（多地区运维服务器）。
从 ssh_servers.toml 读取各地区连接配置，按需建立 SSH 连接。
每个地区维护一个长连接，断线自动重连。
"""
import logging
import threading
import tomllib
from pathlib import Path

import paramiko

from app_paths import APP_ROOT, app_path
from config import settings

logger = logging.getLogger(__name__)

# 程序根目录（ssh_servers.toml 同级）
_PROJECT_ROOT = APP_ROOT

_CONFIG_PATH = app_path("ssh_servers.toml")

# 地区中文名（与 db.py 保持一致）
REGION_LABELS = {
    "sa": "南美/巴西",
    "mx": "墨西哥",
    "sg": "新加坡",
}

_config: dict | None = None
_clients: dict[str, paramiko.SSHClient] = {}   # region → SSHClient
_lock = threading.Lock()


def _get_config() -> dict:
    global _config
    if _config is None:
        if not _CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"SSH 配置文件不存在: {_CONFIG_PATH}\n"
                "请复制 ssh_servers.toml.example 为 ssh_servers.toml 并填写连接信息。"
            )
        with open(_CONFIG_PATH, "rb") as f:
            _config = tomllib.load(f)
        logger.info("[ssh] 加载 SSH 配置，地区: %s", list(_config.keys()))
    return _config


def list_regions() -> list[str]:
    """返回所有已配置的地区代码。"""
    return list(_get_config().keys())


def is_valid_region(region: str) -> bool:
    return region in _get_config()


def format_available() -> str:
    config = _get_config()
    lines = ["可用运维服务器："]
    for region, cfg in config.items():
        label = REGION_LABELS.get(region, region)
        lines.append(f"  {region}（{label}）：{cfg['user']}@{cfg['host']}:{cfg['port']}")
    return "\n".join(lines)


def _connect(region: str) -> paramiko.SSHClient:
    """建立或重用 SSH 连接。连接断开时自动重连。"""
    config = _get_config()
    if region not in config:
        raise ValueError(f"未知地区: {region}，可用: {list(config.keys())}")

    cfg = config[region]
    client = _clients.get(region)

    # 检查连接是否仍然存活
    if client is not None:
        transport = client.get_transport()
        if transport is not None and transport.is_active():
            return client
        logger.info("[ssh] %s 连接已断开，重新连接", region)
        client.close()

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    raw_path = Path(cfg["key_path"]).expanduser()
    # 相对路径则以项目根目录为基准
    key_path = raw_path if raw_path.is_absolute() else _PROJECT_ROOT / raw_path

    passphrase = settings.ssh_key_passphrase or None
    logger.info("[ssh] 连接 %s → %s@%s:%s", region, cfg["user"], cfg["host"], cfg["port"])

    client.connect(
        hostname=cfg["host"],
        port=int(cfg["port"]),
        username=cfg["user"],
        key_filename=str(key_path),
        passphrase=passphrase,
        timeout=15,
        banner_timeout=30,
        allow_agent=False,
        look_for_keys=False,
    )
    _clients[region] = client
    logger.info("[ssh] %s 连接成功", region)
    return client


def execute(cmd: str, region: str, timeout: int = 30) -> tuple[int, str]:
    """
    在指定地区服务器上执行命令。
    返回 (returncode, output) 元组。
    output 合并了 stdout 和 stderr。
    """
    with _lock:
        client = _connect(region)

    logger.info("[ssh] %s 执行: %r", region, cmd[:200])
    stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    stdin.close()

    out = stdout.read().decode("utf-8", errors="replace")
    err = stderr.read().decode("utf-8", errors="replace")
    rc = stdout.channel.recv_exit_status()

    combined = (out + err).strip()
    logger.debug("[ssh] %s 退出码=%d 输出长度=%d", region, rc, len(combined))
    return rc, combined


def close_all() -> None:
    """关闭所有 SSH 连接（进程退出时调用）。"""
    for region, client in _clients.items():
        try:
            client.close()
            logger.info("[ssh] %s 连接已关闭", region)
        except Exception:
            pass
    _clients.clear()
