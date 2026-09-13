"""统一的路径管理：所有用户数据都放在 %APPDATA%\\QuarkRelay 下，绝不写进程序目录。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_ID = "QuarkRelay"


def _base_dir() -> Path:
    override = os.environ.get("QUARKRELAY_HOME")
    if override:
        return Path(override).expanduser()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / APP_ID
    return Path.home() / f".{APP_ID.lower()}"


APP_DIR = _base_dir()
LOG_DIR = APP_DIR / "logs"
CACHE_DIR = APP_DIR / "cache"
WEB_DIR = APP_DIR / "webengine"
CONFIG_FILE = APP_DIR / "config.json"
HISTORY_FILE = APP_DIR / "history.db"
OCR_SCRIPT = APP_DIR / "ocr_win.ps1"


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 中。"""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """只读资源目录（打包后为解包目录）。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def app_dir() -> Path:
    """程序所在目录（便携模式用）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def ensure_dirs() -> None:
    for path in (APP_DIR, LOG_DIR, CACHE_DIR, WEB_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass


def temp_buffer_dir() -> Path:
    """边下边传时的落盘缓冲目录（仅临时文件，不会出现在桌面上）。"""
    path = CACHE_DIR / "buffer"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_download_dir() -> Path:
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


def pretty(path: str | os.PathLike[str]) -> str:
    """把绝对路径里的用户名换成占位符，方便展示与反馈。"""
    text = str(path)
    home = str(Path.home())
    if home and home in text:
        text = text.replace(home, "%USERPROFILE%")
    appdata = os.environ.get("APPDATA")
    if appdata and appdata in text:
        text = text.replace(appdata, "%APPDATA%")
    return text
