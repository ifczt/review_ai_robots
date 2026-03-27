"""
SQL 审查模块。

继承 ReviewBase，在通用状态机和安全校验基础上，实现 SQL 专属逻辑：
  - 本地硬性过滤（is_forbidden）
  - AI 审核（带 Function Calling，按需查表结构/行数/索引）
  - 审核结论解析（[EXECUTE] / [OPTIMIZE] / [REJECT]）
  - 实际 SQL 执行

对外只暴露一个单例实例 sql_review，router 直接调用其方法即可。

未来新增 git_review 时，只需在 handlers/reviews/ 下新建 git.py，
同样继承 ReviewBase，实现 handle() 和 _do_execute()，router 中加一条路由。
"""
import logging
import re
import time

from ai.client import call_with_tools
from ai.prompts import SQL_REVIEW_PROMPT
from ai.sql_tools import TOOLS, make_call_tool
from infra.feishu import send_text
from tools.sql_executor import execute, is_forbidden

from .base import ReviewBase

logger = logging.getLogger(__name__)


class SqlReview(ReviewBase):
    """
    SQL 审查流程。

    执行上下文（ctx）结构：(region: str, database: str)
    展示内容（item）：即将执行的 SQL 字符串（可能是原始 SQL 或 AI 优化后的 SQL）

    完整安全链：
      ① is_forbidden()  — 本地关键字过滤，拦截 DROP/TRUNCATE/GRANT 等
      ② AI 审核         — 语义分析，判断执行/优化/拒绝
      ③ 人工确认        — 所有 SQL 必须用户回复「确认执行」
      ④ 对话记录校验    — 执行的 SQL 必须在本次对话中曾展示给用户（继承自 ReviewBase）
    """

    name = "sql_review"

    def handle(self, sql: str, chat_id: str, user_id: str, region: str, database: str) -> None:
        """
        SQL 审查主入口，由 router 调用。

        参数：
          sql      - 待审查的 SQL 语句
          chat_id  - 飞书群聊 ID，用于发送消息
          user_id  - 发送者 open_id，用于状态隔离
          region   - 目标地区（如 sa / ap / mx / sg）
          database - 目标数据库（如 user / system / statistics）
        """
        logger.info(
            "[sql_review] 开始审查 user=%s target=%s.%s sql=%r",
            user_id, region, database, sql[:100],
        )

        # ── ① 本地硬性过滤 ────────────────────────────────────
        # DROP / TRUNCATE / GRANT 等危险操作直接拒绝，不送 AI
        if is_forbidden(sql):
            logger.warning("[sql_review] 本地过滤拒绝 user=%s sql=%r", user_id, sql[:100])
            send_text(chat_id, (
                "[REJECT]\n包含禁止操作（DROP / TRUNCATE / GRANT 等），系统直接拒绝。\n"
                "⚠️ 该操作存在安全风险，请联系负责人处理。"
            ))
            return

        send_text(chat_id, "正在审核 SQL（目标：{}.{}），请稍候...".format(region, database))

        # ── ② AI 审核 ─────────────────────────────────────────
        # AI 可按需调用工具（get_table_schema / get_table_row_count / get_table_indexes）
        # 获取真实表结构，避免虚构字段名或错误估算影响行数
        review = self._ai_review(sql, region, database)
        logger.info("[sql_review] AI 审核完成，结论=%r", review[:80])

        # ── ③ 根据 AI 结论分发 ────────────────────────────────
        if review.startswith("[EXECUTE]"):
            self._on_execute(sql, review, chat_id, user_id, region, database)

        elif review.startswith("[OPTIMIZE]"):
            self._on_optimize(sql, review, chat_id, user_id, region, database)

        elif review.startswith("[REJECT]"):
            logger.info("[sql_review] AI 拒绝执行 user=%s", user_id)
            send_text(chat_id, review)

        else:
            # AI 未按格式响应，原样输出交人工判断
            logger.error("[sql_review] AI 返回格式异常 user=%s review=%r", user_id, review[:200])
            send_text(chat_id, "AI 审核结果如下，请负责人判断：\n\n{}".format(review))

    def _do_execute(self, sql: str, ctx, chat_id: str, user_id: str) -> None:
        """
        实际执行 SQL（由基类在校验通过后调用）。

        参数：
          sql  - 展示给用户的 SQL（与对话记录一致）
          ctx  - (region, database) 元组
        """
        region, database = ctx
        logger.info(
            "[sql_review] 执行 SQL user=%s target=%s.%s sql=%r",
            user_id, region, database, sql[:100],
        )
        send_text(chat_id, "正在执行 SQL，请稍候...")
        t0 = time.time()
        result = execute(sql, region, database)
        elapsed = time.time() - t0
        logger.info("[sql_review] 执行完成，耗时 %.2fs result=%r", elapsed, result[:100])
        send_text(chat_id, "执行结果：\n```\n{}\n```".format(result))

    # ── 内部辅助方法 ──────────────────────────────────────────

    def _ai_review(self, sql: str, region: str, database: str) -> str:
        """
        调用 AI 审核 SQL，返回审核结论字符串。
        AI 会在需要时自动调用工具查询表结构、行数和索引，无需外部干预。
        """
        t0 = time.time()
        messages = [
            {"role": "system", "content": SQL_REVIEW_PROMPT},
            {
                "role": "user",
                "content": (
                    "目标数据库：{region}.{database}\n\n"
                    "请审核以下 SQL：\n\n```sql\n{sql}\n```"
                ).format(region=region, database=database, sql=sql),
            },
        ]
        # make_call_tool 将 region+database 注入工具闭包，AI 只需传 table_name
        result = call_with_tools(messages, TOOLS, make_call_tool(region, database))
        logger.info("[sql_review] AI 耗时 %.2fs", time.time() - t0)
        return result

    def _on_execute(
        self,
        sql: str,
        review: str,
        chat_id: str,
        user_id: str,
        region: str,
        database: str,
    ) -> None:
        """
        AI 判定可直接执行：展示审核结论 + 写入待确认。
        原始 SQL（用户提交的）作为展示内容登记。
        """
        # 先发消息展示给用户，再写入 pending（确保展示与登记严格对应）
        send_text(chat_id, review + "\n\n请回复「确认执行」或「取消」。")
        self._register_pending(user_id, sql, (region, database))

    def _on_optimize(
        self,
        original_sql: str,
        review: str,
        chat_id: str,
        user_id: str,
        region: str,
        database: str,
    ) -> None:
        """
        AI 建议优化：提取优化后 SQL，展示审核结论 + 写入待确认。
        优化后的 SQL（对话中展示的）作为展示内容登记，执行时也以此为准。
        """
        # 从 AI 返回的 ---SQL---...---END--- 块中提取优化后的 SQL
        match = re.search(r"---SQL---\s*(.*?)\s*---END---", review, re.DOTALL)
        if match:
            optimized = match.group(1).strip()
        else:
            # 提取失败则回退到原始 SQL，并记录警告
            optimized = original_sql
            logger.warning(
                "[sql_review] 无法从 [OPTIMIZE] 中提取优化 SQL，回退到原始 SQL user=%s",
                user_id,
            )

        # 先发消息展示给用户，再写入 pending（确保展示与登记严格对应）
        send_text(chat_id, review)
        self._register_pending(user_id, optimized, (region, database))


# ── 单例 ──────────────────────────────────────────────────────
# router 直接 import 此实例使用，无需实例化
sql_review = SqlReview()
