"""
SQL 审核专用工具集（供 AI Function Calling 使用）。
AI 审核 SQL 时按需调用，获取真实表结构，不猜测字段名。

工具函数只需传入 table_name，region 和 database 由调用方通过
make_call_tool() 工厂捕获，对 AI 透明。
"""
import json
import logging

from infra.db import get_readonly_conn

logger = logging.getLogger(__name__)

# ── OpenAI tools schema ─────────────────────────────────────
# 仅暴露 table_name，region/database 由 make_call_tool 闭包注入
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_table_schema",
            "description": "获取指定表的字段结构（DESCRIBE），包含字段名、类型、主键、默认值等。需要了解表有哪些字段时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "表名，不含反引号"},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_row_count",
            "description": "获取指定表的总行数，用于评估全表扫描的影响范围。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "表名，不含反引号"},
                },
                "required": ["table_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_table_indexes",
            "description": "获取指定表的所有索引信息，用于判断查询条件是否能命中索引。",
            "parameters": {
                "type": "object",
                "properties": {
                    "table_name": {"type": "string", "description": "表名，不含反引号"},
                },
                "required": ["table_name"],
            },
        },
    },
]


# ── 工具实现（需注入 region + database）───────────────────────

def _get_table_schema(region: str, database: str, table_name: str) -> str:
    logger.info("[sql_tools] get_table_schema: %s.%s.%s", region, database, table_name)
    try:
        with get_readonly_conn(region, database) as conn:
            with conn.cursor() as cur:
                cur.execute("DESCRIBE `{}`".format(table_name))
                rows = cur.fetchall()
        if not rows:
            return "表 `{}` 不存在或无字段。".format(table_name)
        lines = ["表 `{}` 字段结构：".format(table_name)]
        for r in rows:
            meta = []
            if r.get("Key") == "PRI":
                meta.append("主键")
            if r.get("Null") == "NO":
                meta.append("NOT NULL")
            if r.get("Extra"):
                meta.append(r["Extra"])
            if r.get("Default") is not None:
                meta.append("默认值={}".format(r["Default"]))
            lines.append("  {} {}{}".format(
                r["Field"], r["Type"],
                " ({})".format(", ".join(meta)) if meta else "",
            ))
        return "\n".join(lines)
    except Exception as e:
        logger.error("[sql_tools] get_table_schema error: %s", e)
        return "查询失败：{}".format(e)


def _get_table_row_count(region: str, database: str, table_name: str) -> str:
    logger.info("[sql_tools] get_table_row_count: %s.%s.%s", region, database, table_name)
    try:
        with get_readonly_conn(region, database) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) as cnt FROM `{}`".format(table_name))
                row = cur.fetchone()
        return "表 `{}` 当前共 {} 行数据。".format(table_name, row["cnt"] if row else 0)
    except Exception as e:
        logger.error("[sql_tools] get_table_row_count error: %s", e)
        return "查询失败：{}".format(e)


def _get_table_indexes(region: str, database: str, table_name: str) -> str:
    logger.info("[sql_tools] get_table_indexes: %s.%s.%s", region, database, table_name)
    try:
        with get_readonly_conn(region, database) as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW INDEX FROM `{}`".format(table_name))
                rows = cur.fetchall()
        if not rows:
            return "表 `{}` 无索引。".format(table_name)
        lines = ["表 `{}` 索引信息：".format(table_name)]
        for r in rows:
            unique = "唯一" if r.get("Non_unique") == 0 else "普通"
            lines.append("  {} - {} 索引，字段: {}".format(
                r.get("Key_name"), unique, r.get("Column_name")
            ))
        return "\n".join(lines)
    except Exception as e:
        logger.error("[sql_tools] get_table_indexes error: %s", e)
        return "查询失败：{}".format(e)


_TOOL_FUNCS = {
    "get_table_schema": _get_table_schema,
    "get_table_row_count": _get_table_row_count,
    "get_table_indexes": _get_table_indexes,
}


def make_call_tool(region: str, database: str):
    """
    工厂函数：返回一个已绑定 region+database 的工具分发函数。
    AI 调用工具时只需提供 table_name，region/database 自动注入。
    """
    def call_tool(name: str, arguments: str) -> str:
        func = _TOOL_FUNCS.get(name)
        if not func:
            return "未知工具：{}".format(name)
        try:
            kwargs = json.loads(arguments)
            return func(region, database, **kwargs)
        except Exception as e:
            logger.error("[sql_tools] call_tool error name=%s err=%s", name, e)
            return "工具调用异常：{}".format(e)

    return call_tool
