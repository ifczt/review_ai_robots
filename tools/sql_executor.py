"""
SQL 执行器。
负责：本地禁止操作检测 + 生产数据库执行（读写连接）。
只做执行，不做审核逻辑。
"""
import logging
import re
from datetime import datetime

import pymysql

from infra.db import get_readwrite_conn

logger = logging.getLogger(__name__)

# 绝对禁止的操作关键字（不送 AI，直接拒绝）
_FORBIDDEN = re.compile(
    r"\b(DROP|TRUNCATE|CREATE\s+DATABASE|DROP\s+DATABASE"
    r"|GRANT|REVOKE|SHUTDOWN|LOAD\s+DATA\s+INFILE"
    r"|INTO\s+OUTFILE|ALTER\s+USER)\b",
    re.IGNORECASE,
)


def is_forbidden(sql: str) -> bool:
    """检查 SQL 是否包含绝对禁止的操作。"""
    return bool(_FORBIDDEN.search(sql))


def execute(sql: str, region: str, database: str) -> str:
    """
    在指定地区+数据库上执行 SQL，返回结果摘要。
    - SELECT：返回前 50 行结果
    - DML（INSERT/UPDATE/DELETE）：使用事务，返回影响行数
    """
    sql = sql.strip().rstrip(";")
    is_select = sql.upper().lstrip().startswith("SELECT")

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    logger.info("[sql_executor] %s | target=%s.%s | %s", timestamp, region, database, sql[:200])

    try:
        with get_readwrite_conn(region, database) as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                logger.info("[sql_executor] SQL 执行完毕，is_select=%s", is_select)

                if is_select:
                    rows = cur.fetchmany(50)
                    total = len(rows)
                    logger.info("[sql_executor] SELECT 返回 %d 行", total)
                    if not rows:
                        return "查询完成，结果为空。"
                    headers = list(rows[0].keys())
                    lines = [" | ".join(headers)]
                    lines.append("-" * len(lines[0]))
                    for row in rows:
                        lines.append(" | ".join(str(v) for v in row.values()))
                    result = "\n".join(lines)
                    if len(result) > 2000:
                        result = result[:2000] + "\n... (已截断，共返回 {} 行)".format(total)
                    return result
                else:
                    conn.commit()
                    affected = cur.rowcount
                    logger.info("[sql_executor] DML 执行成功，影响行数 %d", affected)
                    return "执行成功，影响行数：{}".format(affected)

    except pymysql.MySQLError as e:
        logger.error("[sql_executor] 数据库错误: %s", e)
        return "数据库错误：{}".format(e)
    except Exception as e:
        logger.exception("[sql_executor] 未知异常: %s", e)
        return "执行异常：{}".format(e)
