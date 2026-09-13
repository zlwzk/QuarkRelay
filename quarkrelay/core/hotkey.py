"""全局快捷键：独立的 Windows 消息循环线程里注册 WM_HOTKEY，再回抛给 Qt。

不引入额外依赖（纯 ctypes），注册失败也只是没这个功能，不影响主流程。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wintypes
import logging
import threading

from PySide6.QtCore import QObject, Signal

logger = logging.getLogger(__name__)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
WM_HOTKEY = 0x0312

_MODIFIERS = {
    "ctrl": MOD_CONTROL,
    "control": MOD_CONTROL,
    "alt": MOD_ALT,
    "shift": MOD_SHIFT,
    "win": MOD_WIN,
    "super": MOD_WIN,
    "meta": MOD_WIN,
}

user32 = ctypes.windll.user32 if hasattr(ctypes, "windll") else None
kernel32 = ctypes.windll.kernel32 if hasattr(ctypes, "windll") else None


def parse(spec: str) -> tuple[int, int] | None:
    """把 "Ctrl+Alt+Q" 解析成 (modifiers, vk)。"""
    if not spec or "+" not in spec:
        return None
    parts = [part.strip().lower() for part in spec.split("+") if part.strip()]
    if not parts:
        return None
    key = parts[-1]
    modifiers = 0
    for part in parts[:-1]:
        modifiers |= _MODIFIERS.get(part, 0)
    if len(key) == 1:
        vk = ord(key.upper())
    else:
        named = {
            "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
            "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
            "space": 0x20, "esc": 0x1B, "escape": 0x1B, "enter": 0x0D, "tab": 0x09,
        }
        vk = named.get(key, 0)
    if not vk or not modifiers:
        return None
    return modifiers, vk


class GlobalHotkey(QObject):
    """注册一个全局快捷键，按下时发出 activated 信号。"""

    activated = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._registered = False
        self._ready = threading.Event()

    @property
    def registered(self) -> bool:
        return self._registered

    def register(self, spec: str) -> bool:
        if user32 is None:
            return False
        parsed = parse(spec)
        if parsed is None:
            logger.info("快捷键格式无法解析：%s", spec)
            return False
        modifiers, vk = parsed
        self.stop()
        self._ready.clear()
        self._registered = False
        self._thread = threading.Thread(
            target=self._run, args=(modifiers, vk), name="quarkrelay-hotkey", daemon=True
        )
        self._thread.start()
        self._ready.wait(timeout=2.0)
        return self._registered

    def _run(self, modifiers: int, vk: int) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, modifiers, vk):
            logger.info("全局快捷键注册失败（可能已被其它程序占用）")
            self._ready.set()
            return
        self._registered = True
        self._ready.set()
        message = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) != 0:
                if message.message == WM_HOTKEY:
                    self.activated.emit()
        finally:
            user32.UnregisterHotKey(None, 1)
            self._registered = False

    def stop(self) -> None:
        if self._thread_id and user32 is not None:
            try:
                user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:  # noqa: BLE001
                pass
        self._thread = None
        self._thread_id = 0
        self._registered = False
