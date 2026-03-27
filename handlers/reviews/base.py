"""
审查流程抽象基类。

所有审查模块（SQL 审查、Git 审查等）均继承此类，复用：
  - 待确认状态机（_pending）
  - 对话展示记录校验（_shown_sqls）
  - 确认/取消关键词匹配
  - 执行前安全校验

子类只需实现两个方法：
  - handle()       审查入口，负责预过滤 + AI 审核 + 登记展示记录 + 写入 pending
  - _do_execute()  审查通过且用户确认后的实际执行逻辑

典型流程：
  handle() 被 router 调用
      │
      ├─ 预过滤不通过 → send_text 拒绝，return
      │
      ├─ AI 审核 → [REJECT] → send_text 拒绝，return
      │
      ├─ AI 审核 → [EXECUTE] / [OPTIMIZE]
      │       │
      │       ├─ _register_shown(user_id, item)    # 登记展示记录
      │       └─ _pending[user_id] = (item, ctx)   # 写入待确认
      │
      └─ 用户回复「确认执行」
              │
              ├─ _verify_shown(user_id, item)  # 对话记录校验
              │       └─ 不通过 → [BLOCKED]，记录日志，return
              │
              ├─ _cleanup_shown(user_id, item) # 一次性消费，防止重放
              └─ _do_execute(item, ctx, ...)   # 子类实现的执行逻辑
"""
import logging

from infra.feishu import send_text

logger = logging.getLogger(__name__)

# ── 确认 / 取消关键词 ─────────────────────────────────────────
_CONFIRM_WORDS = {"确认执行", "确认", "是", "yes", "ok", "确定", "执行"}
_CANCEL_WORDS  = {"取消", "否", "no", "cancel", "放弃", "算了"}


class ReviewBase:
    """
    审查流程基类，封装「待确认状态机 + 展示记录校验」。

    状态说明：
      _pending   : user_id → (shown_item, execution_ctx)
                   shown_item    - 展示给用户的核心内容（SQL / git diff 等），用于执行前校验
                   execution_ctx - 执行所需的上下文（地区、库名等），子类自定义

      _shown     : user_id → {shown_item, ...}
                   记录所有曾向用户展示过的内容，执行前必须通过此表校验
    """

    # 子类可覆盖，用于日志标识
    name: str = "review"

    def __init__(self):
        # 待确认队列，每个 user_id 同一时刻只允许一个待确认项
        self._pending: dict[str, tuple] = {}
        # 展示记录表，key = user_id，value = 曾展示过的 item 集合
        self._shown: dict[str, set] = {}

    # ── 对外接口 ──────────────────────────────────────────────

    def has_pending(self, user_id: str) -> bool:
        """是否有待确认的任务（供 router 判断是否处理无 @ 消息）。"""
        return user_id in self._pending

    def handle_pending_reply(self, text: str, chat_id: str, user_id: str) -> None:
        """
        处理用户对待确认任务的回复。

        三种情况：
          1. 确认关键词 → 校验展示记录 → 调用 _do_execute
          2. 取消关键词 → 清除 pending 和展示记录
          3. 其他内容  → 提示用户回复确认或取消
        """
        stripped = text.strip().lower()

        if stripped in _CONFIRM_WORDS:
            self._on_confirm(chat_id, user_id)

        elif stripped in _CANCEL_WORDS:
            self._on_cancel(chat_id, user_id)

        else:
            send_text(chat_id, "当前有待确认的操作，请回复「确认执行」或「取消」。")

    # ── 子类必须实现 ──────────────────────────────────────────

    def handle(self, *args, **kwargs) -> None:
        """
        审查主入口，由子类实现。
        子类负责：预过滤 → AI 审核 → 调用 _register_pending() 写入待确认。
        """
        raise NotImplementedError

    def _do_execute(self, item: str, ctx, chat_id: str, user_id: str) -> None:
        """
        实际执行逻辑，由子类实现。
        仅在展示记录校验通过后由 handle_pending_reply 调用。

        参数：
          item : 展示给用户的核心内容（与 _register_pending 时传入的一致）
          ctx  : 执行上下文（子类自定义，如 (region, database)）
        """
        raise NotImplementedError

    # ── 内部工具方法（子类可调用）─────────────────────────────

    def _register_pending(self, user_id: str, item: str, ctx) -> None:
        """
        将审查结果写入待确认状态，同时登记展示记录。

        调用时机：AI 审核通过（[EXECUTE] 或 [OPTIMIZE]）且已 send_text 给用户之后。

        参数：
          item : 即将执行的核心内容（必须与发给用户的内容完全一致）
          ctx  : 执行上下文（子类自定义）
        """
        # 先登记展示记录，再写入 pending
        self._shown.setdefault(user_id, set()).add(item)
        self._pending[user_id] = (item, ctx)
        logger.info("[%s] 登记展示记录并写入 pending，user=%s item=%r", self.name, user_id, item[:100])

    # ── 内部流程（不对外暴露）────────────────────────────────

    def _on_confirm(self, chat_id: str, user_id: str) -> None:
        """用户确认后的处理：校验 → 执行。"""
        item, ctx = self._pending.pop(user_id)
        logger.info("[%s] 用户确认，user=%s item=%r", self.name, user_id, item[:100])

        # ── 对话记录校验 ────────────────────────────────────
        # 确保即将执行的内容与本次对话中曾展示给用户的完全一致，
        # 防止 pending 被外部篡改或内存中出现未经展示的内容。
        if item not in self._shown.get(user_id, set()):
            logger.error(
                "[%s] 对话记录校验失败！item 未在展示列表中，阻止执行 user=%s item=%r",
                self.name, user_id, item[:200],
            )
            send_text(chat_id, (
                "[BLOCKED]\n安全校验未通过：即将执行的内容与对话记录不符，已阻止执行。\n"
                "⚠️ 此次操作已记录，请联系负责人排查。"
            ))
            return

        # 一次性消费：执行后从展示记录移除，防止同一内容被重复确认执行
        self._shown[user_id].discard(item)

        # 调用子类实现的执行逻辑
        self._do_execute(item, ctx, chat_id, user_id)

    def _on_cancel(self, chat_id: str, user_id: str) -> None:
        """用户取消后的处理：清除状态。"""
        item, _ = self._pending.pop(user_id)
        self._shown.get(user_id, set()).discard(item)
        logger.info("[%s] 用户取消，user=%s", self.name, user_id)
        send_text(chat_id, "已取消，操作未执行。")
