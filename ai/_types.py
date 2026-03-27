"""AI 客户端统一返回类型，屏蔽不同 SDK 的差异。"""
from dataclasses import dataclass


@dataclass
class AIMessage:
    """call_once 的统一返回类型。

    content   - AI 的文本回复（纯文本轮次）
    两个后端都保证返回此类型，调用方只需访问 .content。
    """
    content: str | None
