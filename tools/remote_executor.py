"""
远程服务器命令执行器。
通过 SSH 在指定地区的运维服务器上执行命令，内置安全校验。
"""
import logging
import re
import shlex

from config import settings
from infra import ssh

logger = logging.getLogger(__name__)

# supervisorctl 允许的子命令
ALLOWED_SUPERVISORCTL_SUBCMDS = {
    "status", "pid", "version",
    "tail",
    "start", "stop", "restart",
    "reload", "reread", "update",
}

# 允许远程执行的命令白名单
ALLOWED_COMMANDS = {
    # 诊断/查看
    "ls", "cat", "head", "tail", "find", "du", "df", "wc",
    "ps", "top", "free", "uptime", "uname", "hostname",
    "date", "env",
    "netstat", "ss", "ip",
    "grep", "awk", "sed", "sort", "uniq", "cut",
    "echo", "which",
    # 进程/服务管理
    "supervisorctl",
    "systemctl", "journalctl",
    # 日志
    "journalctl",
}

# 禁止的危险模式（shell 注入 / 破坏性操作）
BLOCKED_PATTERNS = [
    r"[;&|`]",
    r"\$\(",
    r"\.\./",
    r"\brm\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bsudo\b",
    r"/etc/shadow",
    r"/etc/passwd",
    r"\bmkfs\b",
    r"\bdd\b.*of=",
    r">\s*/",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bpoweroff\b",
    r"\bpkill\b",
    r"\bkillall\b",
]

MAX_OUTPUT_LENGTH = 3000


def execute(cmd_text: str, region: str) -> str:
    """
    在指定地区服务器上安全执行命令，返回输出文本。
    先做本地安全校验，通过后再发起 SSH 执行。
    """
    # 黑名单检查
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd_text):
            logger.warning("[remote_executor] 黑名单拒绝 region=%s cmd=%r", region, cmd_text[:100])
            return "命令包含不允许的字符或操作，已拒绝执行。"

    # 安全分词
    try:
        parts = shlex.split(cmd_text)
    except ValueError as e:
        return f"命令解析失败：{e}"

    if not parts:
        return "命令为空。"

    base_cmd = parts[0]

    # 白名单检查
    if base_cmd not in ALLOWED_COMMANDS:
        logger.warning("[remote_executor] 白名单拒绝: %r", base_cmd)
        return "命令 `{}` 不在允许列表中。\n允许的命令：{}".format(
            base_cmd, ", ".join(sorted(ALLOWED_COMMANDS))
        )

    # supervisorctl 子命令限制，并自动加 sudo
    if base_cmd == "supervisorctl":
        subcmd = parts[1] if len(parts) > 1 else ""
        if subcmd not in ALLOWED_SUPERVISORCTL_SUBCMDS:
            return "supervisorctl `{}` 不在允许的子命令列表中。允许：{}".format(
                subcmd, ", ".join(sorted(ALLOWED_SUPERVISORCTL_SUBCMDS))
            )
        cmd_text = "sudo " + cmd_text

    logger.info("[remote_executor] region=%s 执行: %s", region, cmd_text[:200])

    try:
        rc, output = ssh.execute(cmd_text, region, timeout=settings.cmd_timeout_seconds)
    except TimeoutError:
        return f"命令执行超时（>{settings.cmd_timeout_seconds}s），已终止。"
    except Exception as e:
        logger.error("[remote_executor] SSH 执行异常 region=%s: %s", region, e)
        return f"SSH 执行失败：{e}"

    if not output:
        return f"命令执行完成（退出码 {rc}），无输出。"

    if len(output) > MAX_OUTPUT_LENGTH:
        output = output[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"

    return output
