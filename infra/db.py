"""
数据库连接提供者（多地区多数据库版本）。
从 db_connections.toml 读取各地区连接 DSN，按需建立连接。

其他模块统一从此处获取连接，不直接调用 pymysql.connect。
"""
import logging
import tomllib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import pymysql
import pymysql.cursors

from app_paths import app_path

logger = logging.getLogger(__name__)

# 配置文件路径（程序目录下）
_CONFIG_PATH = app_path("db_connections.toml")

# 地区中文名映射（供展示用）
REGION_LABELS = {
    "sa": "南美/巴西",
    "ap": "东南亚",
    "mx": "墨西哥",
    "sg": "新加坡",
}

# 缓存已加载的配置
_config: dict | None = None


def _get_config() -> dict:
    """加载并缓存 db_connections.toml 配置。"""
    global _config
    if _config is None:
        if not _CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"数据库配置文件不存在: {_CONFIG_PATH}\n"
                "请复制 db_connections.toml.example 并填写真实连接信息。"
            )
        with open(_CONFIG_PATH, "rb") as f:
            _config = tomllib.load(f)
        logger.info("[db] 加载数据库配置，地区: %s", list(_config.keys()))
    return _config


def _parse_dsn(dsn: str) -> dict:
    """解析 MySQL DSN URL → pymysql.connect 参数字典。"""
    p = urlparse(dsn)
    return {
        "host": p.hostname,
        "port": p.port or 3306,
        "user": p.username,
        "password": p.password,
        "db": p.path.lstrip("/"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "connect_timeout": 10,
    }


def list_regions() -> list[str]:
    """返回所有可用地区代码列表。"""
    return list(_get_config().keys())


def list_databases(region: str) -> list[str]:
    """返回指定地区下所有可用数据库名称列表。"""
    config = _get_config()
    if region not in config:
        return []
    return list(config[region].keys())


def is_valid(region: str, database: str) -> bool:
    """校验地区+数据库组合是否在配置中存在。"""
    config = _get_config()
    return region in config and database in config[region]


def format_available() -> str:
    """格式化所有可用地区和数据库，用于提示信息。"""
    config = _get_config()
    lines = ["可用地区和数据库："]
    for region, dbs in config.items():
        label = REGION_LABELS.get(region, region)
        db_list = ", ".join(dbs.keys())
        lines.append(f"  {region}（{label}）：{db_list}")
    return "\n".join(lines)


@contextmanager
def get_readonly_conn(region: str, database: str):
    """只读连接（autocommit=True），用于 AI 工具查询表结构、行数等。"""
    conn = _connect(region, database, autocommit=True)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def get_readwrite_conn(region: str, database: str):
    """读写连接（autocommit=False），用于执行 DML，需手动 commit。"""
    conn = _connect(region, database, autocommit=False)
    try:
        yield conn
    finally:
        conn.close()


def _connect(region: str, database: str, autocommit: bool) -> pymysql.connections.Connection:
    """建立到指定地区+数据库的连接。"""
    config = _get_config()
    if region not in config:
        raise ValueError(f"未知地区: {region}，可用: {list(config.keys())}")
    if database not in config[region]:
        raise ValueError(
            f"地区 {region} 下未找到数据库 {database}，"
            f"可用: {list(config[region].keys())}"
        )

    dsn = config[region][database]
    params = _parse_dsn(dsn)
    params["autocommit"] = autocommit

    logger.debug("[db] 连接 %s.%s → %s:%s/%s", region, database, params["host"], params["port"], params["db"])
    return pymysql.connect(**params)
