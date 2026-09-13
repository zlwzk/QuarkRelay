"""检查更新：读 GitHub Releases 最新 tag，与本地版本比较。"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

from .. import __github__, __version__

logger = logging.getLogger(__name__)
API = "https://api.github.com/repos/zlwzk/QuarkRelay/releases/latest"


@dataclass
class UpdateInfo:
    has_update: bool = False
    version: str = ""
    notes: str = ""
    url: str = ""
    error: str = ""
    asset_size: int = 0

    @property
    def message(self) -> str:
        if self.error:
            return f"检查更新失败：{self.error}"
        if self.has_update:
            return f"发现新版本 v{self.version}"
        return f"当前已是最新版本 v{__version__}"


def _parse(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers[:4]) or (0,)


def check(timeout: int = 10) -> UpdateInfo:
    try:
        response = requests.get(
            API,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "QuarkRelay-Updater",
            },
        )
        if response.status_code == 404:
            return UpdateInfo(error="仓库还没有发布任何版本")
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        logger.warning("检查更新失败：%s", exc)
        return UpdateInfo(error=str(exc))
    except ValueError as exc:
        return UpdateInfo(error=f"返回内容异常：{exc}")

    tag = str(payload.get("tag_name") or "").lstrip("vV")
    url = str(payload.get("html_url") or __github__)
    notes = str(payload.get("body") or "")
    size = 0
    for asset in payload.get("assets") or []:
        name = str(asset.get("name") or "")
        if name.lower().endswith(".exe"):
            size = int(asset.get("size") or 0)
            break
    has_update = bool(tag) and _parse(tag) > _parse(__version__)
    return UpdateInfo(has_update=has_update, version=tag, notes=notes, url=url, asset_size=size)


def latest_download_url() -> str:
    return f"{__github__}/releases/latest"
