from pydantic_settings import BaseSettings

from app_paths import app_path

_ENV_FILE = app_path(".env")


class Settings(BaseSettings):
    feishu_app_id: str
    feishu_app_secret: str

    ai_backend: str = "anthropic"

    anthropic_api_key: str = ""
    anthropic_base_url: str = ""

    openai_api_key: str = ""
    openai_base_url: str = ""

    chatgpt_api_key: str = ""
    chatgpt_base_url: str = ""
    chatgpt_model: str = "gpt-4o"
    chatgpt_max_tokens: int = 4096

    claude_model: str = "claude-sonnet-4-6"
    claude_max_tokens: int = 2048

    ssh_key_passphrase: str = ""

    cmd_timeout_seconds: int = 30
    cmd_working_dir: str = "/tmp"

    gitea_base_url: str = ""
    gitea_token: str = ""
    gitea_auto_merge_method: str = "merge"

    bw_chat_id: str = ""

    daily_report_chat_id: str = ""
    daily_report_private_recipients: str = ""
    daily_report_send_hour: int = 19
    daily_report_send_minute: int = 30
    daily_report_lookback_days: int = 1

    class Config:
        env_file = str(_ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
