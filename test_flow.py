"""
端到端流程模拟测试（不依赖飞书连接和真实数据库）。

覆盖场景：
  1. /help 命令
  2. 普通对话（AI 问答）
  3. SQL 审查完整流程：提交 → AI 审核 → 确认 → 执行
  4. SQL 审查：取消确认
  5. SQL 对话记录校验（BLOCKED 场景）
  6. 裸 SQL（无地区前缀）引导提示
  7. 本地过滤拒绝（DROP TABLE）
"""
import sys
import os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
# Windows 控制台强制 UTF-8 输出
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
load_dotenv(".env")

# ── Mock：拦截 send_text，不真正发飞书消息 ──────────────────────
import infra.feishu as _feishu_mod
_sent: list[str] = []

def mock_send_text(chat_id: str, text: str) -> None:
    _sent.append(text)
    print(f"  [飞书回复] {text[:120].replace(chr(10), ' | ')}")

_feishu_mod.send_text = mock_send_text

# ── Mock：拦截 sql_executor.execute，不真正连数据库 ────────────
import tools.sql_executor as _exec_mod
def mock_execute(sql: str, region: str, database: str) -> str:
    return f"(mock) {region}.{database} 执行成功，影响行数：1"

_exec_mod.execute = mock_execute

# ── Mock：拦截 call_with_tools，不真正调 AI ────────────────────
import ai.client as _ai_mod

_ai_responses: list[str] = []  # 预设 AI 依次返回的内容

def mock_call_with_tools(messages, tools, call_tool_fn, max_rounds=5):
    resp = _ai_responses.pop(0) if _ai_responses else "[REJECT]\n未预设 AI 响应"
    print(f"  [AI 返回] {resp[:80].replace(chr(10), ' | ')}")
    return resp

_ai_mod.call_with_tools = mock_call_with_tools

import ai.client
ai.client.call_with_tools = mock_call_with_tools

# call_once 用于普通对话
from openai.types.chat import ChatCompletionMessage
def mock_call_once(messages, tools=None):
    msg = ChatCompletionMessage(role="assistant", content="你好！我是 AI 助手，有什么可以帮你？")
    return msg

_ai_mod.call_once = mock_call_once

# ── 重新导入 router（确保 mock 生效）──────────────────────────
# 需要在 mock 之后导入，使 sql_review 内部引用的 execute/call_with_tools 已被替换
from handlers import router
from handlers.reviews.sql import sql_review

# ── 测试工具 ───────────────────────────────────────────────────
CHAT_ID  = "oc_test_chat"
USER_ID  = "ou_test_user"

def run(label: str, text: str, ai_resp: str = None):
    """模拟用户发送一条消息。"""
    _sent.clear()
    if ai_resp:
        _ai_responses.append(ai_resp)
    print(f"\n{'='*60}")
    print(f"场景：{label}")
    print(f"用户：{text}")
    router.dispatch(text=text, chat_id=CHAT_ID, user_id=USER_ID)

def assert_reply_contains(keyword: str):
    full = "\n".join(_sent)
    assert keyword in full, f"期望回复含 {keyword!r}，实际：{full[:200]}"
    print(f"  [断言通过] 回复含 {keyword!r}")

def assert_pending(expected: bool):
    actual = router.has_pending(USER_ID)
    assert actual == expected, f"期望 pending={expected}，实际={actual}"
    print(f"  [断言通过] pending={expected}")

# ══════════════════════════════════════════════════════════════
print("\n>>> 开始测试流程")

# 场景 1：/help
run("help 命令", "/help")
assert_reply_contains("/sql")

# 场景 2：普通对话
run("普通对话", "你好")
assert_reply_contains("AI 助手")

# 场景 3：本地过滤拒绝
run("DROP TABLE 被拦截", "/sql sa.user DROP TABLE users")
assert_reply_contains("[REJECT]")
assert_pending(False)

# 场景 4：未知地区
run("未知地区", "/sql xx.user SELECT 1")
assert_reply_contains("未知地区")
assert_pending(False)

# 场景 5：裸 SQL 引导
run("裸 SQL 引导", "SELECT * FROM users WHERE id=1")
assert_reply_contains("请指定目标数据库")
assert_pending(False)

# 场景 6：SQL 审查 → [EXECUTE] → 确认执行
run(
    "SQL 审查 [EXECUTE] → 确认",
    "/sql sa.user SELECT id, name FROM users WHERE id=1",
    ai_resp="[EXECUTE]\n有明确 WHERE 条件，字段精确，可以直接执行。",
)
assert_reply_contains("[EXECUTE]")
assert_pending(True)

run("确认执行", "确认执行")
assert_reply_contains("执行结果")
assert_pending(False)

# 场景 7：SQL 审查 → [OPTIMIZE] → 取消
run(
    "SQL 审查 [OPTIMIZE] → 取消",
    "/sql sg.statistics SELECT * FROM orders",
    ai_resp=(
        "[OPTIMIZE]\nSELECT * 建议指定字段。\n"
        "---SQL---\nSELECT id, amount FROM orders\n---END---\n"
        "请员工确认后回复「确认执行」。"
    ),
)
assert_reply_contains("[OPTIMIZE]")
assert_pending(True)

run("取消执行", "取消")
assert_reply_contains("已取消")
assert_pending(False)

# 场景 8：对话记录校验（BLOCKED）
# 手动向 pending 注入一个未经展示的 SQL，模拟篡改
from handlers.reviews.sql import sql_review as _sr
_sr._pending[USER_ID] = ("DELETE FROM users", ("sa", "user"))
# _shown 中不登记这条 SQL

_sent.clear()
print(f"\n{'='*60}")
print("场景：对话记录校验 BLOCKED（pending 被篡改）")
print("用户：确认执行")
_sr.handle_pending_reply("确认执行", CHAT_ID, USER_ID)
assert_reply_contains("[BLOCKED]")
assert_pending(False)

print(f"\n{'='*60}")
print(">>> 全部测试通过 ✓")
