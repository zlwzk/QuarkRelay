"""配置读写。凭据（token / cookie）与偏好设置同存于 config.json，仅保存在本机。"""

from __future__ import annotations

import copy
import json
import threading
from typing import Any

from .paths import CONFIG_FILE, ensure_dirs

DEFAULTS: dict[str, Any] = {
    "quark": {
        "access_token": "",
        "refresh_token": "",
        "device_id": "",
        "expires_at": 0,
        "nickname": "",
        "user_id": "",
        "default_dir": "夸克中转站",
        "share_expired_type": 1,
        "share_url_type": 2,
        "naming_template": "{name} {link}",
        "recent_dirs": [],
    },
    "baidu": {
        "cookies": "",
        "bdstoken": "",
        "nickname": "",
        "uk": "",
        "default_target": "/我的资源/夸克中转站",
        "stream_upload": True,
        "fast_upload": True,
        "user_agent": "",
    },
    "app": {
        "theme": "dark",
        "watch_clipboard": True,
        "auto_transfer": False,
        "hotkey_screenshot": "Ctrl+Alt+Q",
        "hotkey_main": "",
        "max_parallel": 2,
        "speed_limit_kbps": 0,
        "close_to_tray": True,
        "notify_on_finish": True,
        "copy_after_share": True,
        "history_limit": 1000,
        "log_level": "INFO",
        "first_run": True,
        "last_version": "",
        "auto_check_update": True,
        "auto_install_update": True,
    },
}

_LOCK = threading.RLock()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Config:
    """线程安全的点号式配置访问器。"""

    def __init__(self) -> None:
        ensure_dirs()
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ------------------------------------------------------------------ io
    def load(self) -> None:
        with _LOCK:
            raw: dict[str, Any] = {}
            if CONFIG_FILE.exists():
                try:
                    # utf-8-sig：用户用记事本手改过配置会带上 BOM，不能让整份配置被当成损坏
                    raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    raw = {}
            if not isinstance(raw, dict):
                raw = {}
            self._data = _deep_merge(DEFAULTS, raw)

    def save(self) -> None:
        with _LOCK:
            ensure_dirs()
            tmp = CONFIG_FILE.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(CONFIG_FILE)

    # --------------------------------------------------------------- access
    def get(self, path: str, default: Any = None) -> Any:
        with _LOCK:
            node: Any = self._data
            for part in path.split("."):
                if not isinstance(node, dict) or part not in node:
                    return default
                node = node[part]
            return node

    def set(self, path: str, value: Any, *, autosave: bool = True) -> None:
        with _LOCK:
            parts = path.split(".")
            node = self._data
            for part in parts[:-1]:
                child = node.get(part)
                if not isinstance(child, dict):
                    child = {}
                    node[part] = child
                node = child
            node[parts[-1]] = value
        if autosave:
            self.save()

    def update(self, values: dict[str, Any], *, autosave: bool = True) -> None:
        for key, value in values.items():
            self.set(key, value, autosave=False)
        if autosave:
            self.save()

    def section(self, name: str) -> dict[str, Any]:
        with _LOCK:
            value = self._data.get(name)
            return copy.deepcopy(value) if isinstance(value, dict) else {}

    def as_dict(self) -> dict[str, Any]:
        with _LOCK:
            return copy.deepcopy(self._data)

    # ------------------------------------------------------------- helpers
    def push_recent(self, key: str, value: str, limit: int = 8) -> None:
        if not value:
            return
        items = list(self.get(key, []) or [])
        if value in items:
            items.remove(value)
        items.insert(0, value)
        self.set(key, items[:limit])

    def clear_credentials(self) -> None:
        self.update(
            {
                "quark.access_token": "",
                "quark.refresh_token": "",
                "quark.device_id": "",
                "quark.expires_at": 0,
                "quark.nickname": "",
                "quark.user_id": "",
                "baidu.cookies": "",
                "baidu.bdstoken": "",
                "baidu.nickname": "",
                "baidu.uk": "",
            }
        )


_config: Config | None = None


def config() -> Config:
    global _config
    if _config is None:
        _config = Config()
    return _config
