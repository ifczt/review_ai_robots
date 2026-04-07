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
    daily_report_lookback_days: int = 0

    # 服务器状态监控
    server_monitor_chat_id: str = ""           # 告警 & 状态汇报目标群
    server_monitor_interval_minutes: int = 5   # 巡检间隔（分钟）
    server_monitor_report_hour: int = 9        # 每日定时汇报小时
    server_monitor_report_minute: int = 0      # 每日定时汇报分钟
    server_monitor_cpu_threshold: int = 90     # CPU 告警阈值（%）
    server_monitor_mem_threshold: int = 90     # 内存告警阈值（%）
    server_monitor_disk_threshold: int = 85    # 磁盘告警阈值（%）
    server_monitor_alert_cooldown_minutes: int = 30  # 同一指标告警冷却时间（分钟）
    server_monitor_process_cpu_threshold: int = 50   # 单进程 CPU 占用告警阈值（%），0 表示不监控

    class Config:
        env_file = str(_ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
