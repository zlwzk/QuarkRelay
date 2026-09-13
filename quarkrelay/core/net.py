"""网络小工具：限速、体积/速度格式化、带进度的分块读写。"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable


def human_size(size: float) -> str:
    value = float(size or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def human_speed(bytes_per_second: float) -> str:
    return f"{human_size(bytes_per_second)}/s"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分"


class Throttle:
    """简单的令牌桶限速器，kbps<=0 表示不限速。"""

    def __init__(self, kbps: int = 0) -> None:
        self.kbps = max(0, int(kbps or 0))
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._bytes = 0

    def set_rate(self, kbps: int) -> None:
        with self._lock:
            self.kbps = max(0, int(kbps or 0))

    def __call__(self, size: int) -> None:
        if self.kbps <= 0 or size <= 0:
            return
        per_second = self.kbps * 1024
        with self._lock:
            self._bytes += size
            elapsed = time.monotonic() - self._start
            expected = self._bytes / per_second
        if expected > elapsed:
            time.sleep(min(expected - elapsed, 2.0))


class SpeedMeter:
    """滚动窗口测速，用于界面展示实时速度。"""

    def __init__(self, window: float = 3.0) -> None:
        self._window = window
        self._samples: list[tuple[float, int]] = []
        self._lock = threading.Lock()

    def update(self, total_bytes: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._samples.append((now, total_bytes))
            cutoff = now - self._window
            self._samples = [s for s in self._samples if s[0] >= cutoff]
            if len(self._samples) > 240:
                self._samples = self._samples[-240:]

    @property
    def speed(self) -> float:
        with self._lock:
            if len(self._samples) < 2:
                return 0.0
            first_t, first_b = self._samples[0]
            last_t, last_b = self._samples[-1]
            delta_t = last_t - first_t
            if delta_t <= 0:
                return 0.0
            return max(0.0, (last_b - first_b) / delta_t)

    def reset(self) -> None:
        with self._lock:
            self._samples.clear()


def progress_wrapper(
    callback: Callable[[int, int], None] | None,
    meter: SpeedMeter | None = None,
    throttle: Throttle | None = None,
) -> Callable[[int, int], None]:
    """组合限速与测速的进度回调。"""

    def _inner(done: int, total: int) -> None:
        if meter is not None:
            meter.update(done)
        if callback is not None:
            callback(done, total)

    return _inner


def join_lines(lines: Iterable[str]) -> str:
    return "\n".join(str(line) for line in lines if line)
