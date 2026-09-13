"""统一的业务异常类型，界面层只认这几种，方便给出人话提示。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可预期的业务错误的基类。"""

    def __init__(self, message: str, *, code: Any = None, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.payload = payload or {}

    def __str__(self) -> str:  # pragma: no cover - 简单转发
        return self.message


class QuarkError(AppError):
    """夸克侧错误。"""


class QuarkAuthError(QuarkError):
    """夸克登录态失效，需要重新登录。"""


class BaiduError(AppError):
    """百度侧错误。"""


class BaiduAuthError(BaiduError):
    """百度登录态失效，需要重新登录。"""


class CancelledError(AppError):
    """用户主动取消。"""

    def __init__(self, message: str = "任务已取消") -> None:
        super().__init__(message)


def friendly(exc: BaseException) -> str:
    """把异常翻译成适合展示的一句话。"""
    if isinstance(exc, AppError):
        return exc.message
    if isinstance(exc, TimeoutError):
        return "网络超时，请检查网络后重试"
    text = str(exc) or exc.__class__.__name__
    return text
