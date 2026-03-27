from pydantic_settings import BaseSettings

from app_paths import app_path

# .env 文件统一放在程序目录，兼容源码运行和 PyInstaller 打包后的 exe 运行
_ENV_FILE = app_path(".env")


class Settings(BaseSettings):
    # 飞书应用凭证（长连接模式只需要这两个）
    feishu_app_id: str
    feishu_app_secret: str

    # AI 后端选择：anthropic（默认）或 openai
    ai_backend: str = "anthropic"

    # Anthropic 原生 SDK（推荐，ai_backend=anthropic 时生效）
    anthropic_api_key: str = ""
    anthropic_base_url: str = ""   # 中转代理地址，留空使用官方 api.anthropic.com

    # OpenAI 兼容格式（ai_backend=openai 时生效）
    openai_api_key: str = ""
    openai_base_url: str = ""      # 中转代理地址，留空使用官方 api.openai.com

    # ChatGPT（ai_backend=chatgpt 时生效）
    chatgpt_api_key: str = ""
    chatgpt_base_url: str = ""     # 中转代理地址，留空使用官方 api.openai.com
    chatgpt_model: str = "gpt-4o"
    chatgpt_max_tokens: int = 4096

    # 通用模型配置
    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 2048

    # SSH 私钥密码
    ssh_key_passphrase: str = ""

    # 命令执行配置
    cmd_timeout_seconds: int = 30
    cmd_working_dir: str = "/tmp"

    # Gitea 代码审查配置
    gitea_base_url: str = ""          # e.g. https://gitea.example.com
    gitea_token: str = ""             # Gitea access token（需要 repo 读写权限）
    gitea_auto_merge_method: str = "merge"  # merge / rebase / squash

    # BW 包网项目测试群
    bw_chat_id: str = ""              # BW包网项目群聊 chat_id，用于测试通知

    # 每日代码审查日报
    daily_report_chat_id: str = ""    # 接收 21:30 日报的群聊 chat_id，留空则不发送

    class Config:
        env_file = str(_ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"  # 忽略 .env 中的未知字段，兼容旧配置


settings = Settings()
