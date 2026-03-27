"""
系统命令执行器。
四层安全防御：白名单 → 黑名单模式 → 子进程沙箱 → 超时控制。
"""
import logging
import os
import re
import shlex
import subprocess

from config import settings

logger = logging.getLogger(__name__)

# 允许执行的命令白名单（只读/诊断类）
ALLOWED_COMMANDS = {
    "ls", "cat", "head", "tail", "find", "du", "df", "wc", "file",
    "ps", "top", "free", "uptime", "uname", "hostname", "whoami", "id",
    "date", "env", "printenv",
    "ping", "curl", "wget", "netstat", "ss", "ip",
    "systemctl", "journalctl",
    "python", "python3", "pip", "pip3",
    "git",
    "grep", "awk", "sed", "sort", "uniq", "cut", "tr",
    "echo", "which", "type",
    "supervisorctl",
}

# 危险模式黑名单
BLOCKED_PATTERNS = [
    r"[;&|`]",
    r"\$\(",
    r"\.\./",
    r"\brm\b",
    r"\brmdir\b",
    r"\bchmod\b",
    r"\bchown\b",
    r"\bsudo\b",
    r"\bsu\b\s",
    r"/etc/shadow",
    r"/etc/passwd",
    r"\bmkfs\b",
    r"\bdd\b.*of=",
    r">\s*/",
    r"\bkill\b",
    r"\bpkill\b",
    r"\breboot\b",
    r"\bshutdown\b",
    r"\bpoweroff\b",
]

# Git 只允许只读子命令
ALLOWED_GIT_SUBCMDS = {"log", "status", "diff", "show", "branch", "tag", "remote", "describe"}

# supervisorctl 允许的子命令（只读 + 进程控制）
ALLOWED_SUPERVISORCTL_SUBCMDS = {
    "status", "pid", "version",          # 只读查询
    "tail",                              # 查日志
    "start", "stop", "restart",          # 进程控制
    "reload", "reread", "update",        # 配置刷新
}

# 最小化安全环境变量（不传入 API Key 等敏感信息）
SAFE_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": os.environ.get("HOME", "/tmp"),
    "LANG": "en_US.UTF-8",
    "TERM": "xterm",
}

MAX_OUTPUT_LENGTH = 3000


def execute(cmd_text: str) -> str:
    """安全执行系统命令，返回输出文本。"""
    # 黑名单检查
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd_text):
            logger.warning("[command_executor] 黑名单拒绝: %r", cmd_text[:100])
            return "命令包含不允许的字符或操作，已拒绝执行。"

    # 安全分词
    try:
        parts = shlex.split(cmd_text)
    except ValueError as e:
        return "命令解析失败：{}".format(e)

    if not parts:
        return "命令为空。"

    base_cmd = parts[0]

    # 白名单检查
    if base_cmd not in ALLOWED_COMMANDS:
        logger.warning("[command_executor] 白名单拒绝: %r", base_cmd)
        return "命令 `{}` 不在允许列表中。\n允许的命令：{}".format(
            base_cmd, ", ".join(sorted(ALLOWED_COMMANDS))
        )

    # Git 子命令限制
    if base_cmd == "git":
        git_subcmd = parts[1] if len(parts) > 1 else ""
        if git_subcmd not in ALLOWED_GIT_SUBCMDS:
            return "git `{}` 不在允许的子命令列表中。允许：{}".format(
                git_subcmd, ", ".join(sorted(ALLOWED_GIT_SUBCMDS))
            )

    # supervisorctl 子命令限制
    if base_cmd == "supervisorctl":
        ctl_subcmd = parts[1] if len(parts) > 1 else ""
        if ctl_subcmd not in ALLOWED_SUPERVISORCTL_SUBCMDS:
            return "supervisorctl `{}` 不在允许的子命令列表中。允许：{}".format(
                ctl_subcmd, ", ".join(sorted(ALLOWED_SUPERVISORCTL_SUBCMDS))
            )

    logger.info("[command_executor] 执行: %s", cmd_text[:200])
    try:
        result = subprocess.run(
            parts,
            capture_output=True,
            text=True,
            timeout=settings.cmd_timeout_seconds,
            cwd=settings.cmd_working_dir,
            env=SAFE_ENV,
        )
        combined = (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired:
        return "命令执行超时（>{}s），已强制终止。".format(settings.cmd_timeout_seconds)
    except FileNotFoundError:
        return "命令 `{}` 未找到，请确认已安装。".format(base_cmd)

    if not combined:
        return "命令执行完成（退出码 {}），无输出。".format(result.returncode)

    if len(combined) > MAX_OUTPUT_LENGTH:
        combined = combined[:MAX_OUTPUT_LENGTH] + "\n... (输出已截断)"

    return combined
