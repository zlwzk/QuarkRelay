"""日志：文件 + 内存环形缓冲（界面日志页订阅）。"""

from __future__ import annotations

import logging
import logging.handlers
import threading
from collections import deque
from collections.abc import Callable

from .paths import LOG_DIR, ensure_dirs

LOG_FILE = LOG_DIR / "quarkrelay.log"
_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


class MemoryHandler(logging.Handler):
    """保留最近若干条日志，并支持界面订阅。"""

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self.records: deque[str] = deque(maxlen=capacity)
        self._listeners: list[Callable[[str], None]] = []
        self._lock = threading.RLock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            text = self.format(record)
        except Exception:  # pragma: no cover - 格式化失败不应影响主流程
            return
        with self._lock:
            self.records.append(text)
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener(text)
            except Exception:
                pass

    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._listeners.append(callback)

    def unsubscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self.records)


memory_handler = MemoryHandler()


def setup(level: str = "INFO") -> None:
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter(_FORMAT)
    memory_handler.setFormatter(fmt)
    root.addHandler(memory_handler)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)
    except OSError:
        pass

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    root.addHandler(stream)

    logging.getLogger("PySide6").setLevel(logging.WARNING)
