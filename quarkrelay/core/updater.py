"""检查更新与自动更新：读 GitHub Releases → 下载新版 exe → 就地替换并重启。

Windows 上运行中的 exe 没法覆盖自己，所以自动更新分三步：
1. 把新版下载到 `%APPDATA%\\QuarkRelay\\update\\`，校验文件头和大小（有 sha256 就再校验一遍）；
2. 生成一个批处理，等本进程退出后把旧 exe 挪走、把新版挪进来、再启动程序；
3. 调用方收到「脚本已启动」后立刻退出，剩下的活交给脚本。

程序目录不可写（比如装在 `Program Files` 又没提权）时不硬来，只提示手动下载。
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

from .. import __github__, __version__
from ..paths import APP_DIR, is_frozen
from .net import human_size

logger = logging.getLogger(__name__)

API = "https://api.github.com/repos/zlwzk/QuarkRelay/releases/latest"
UPDATE_DIR = APP_DIR / "update"
USER_AGENT = "QuarkRelay-Updater"


class UpdateError(Exception):
    """检查更新或下载安装过程中的可预期错误。"""


@dataclass
class UpdateInfo:
    has_update: bool = False
    version: str = ""
    notes: str = ""
    url: str = ""
    error: str = ""
    asset_name: str = ""
    asset_url: str = ""
    asset_size: int = 0
    asset_digest: str = ""
    published_at: str = ""

    @property
    def has_asset(self) -> bool:
        return bool(self.asset_url)

    @property
    def size_text(self) -> str:
        return human_size(self.asset_size) if self.asset_size else "未知大小"

    @property
    def date_text(self) -> str:
        return (self.published_at or "")[:10]

    @property
    def message(self) -> str:
        if self.error:
            return f"检查更新失败：{self.error}"
        if self.has_update:
            extra = f"（{self.size_text}"
            extra += f" · {self.date_text}）" if self.date_text else "）"
            return f"发现新版本 v{self.version} {extra}"
        return f"当前已是最新版本 v{__version__}"


def _parse(version: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", version or "")
    return tuple(int(n) for n in numbers[:4]) or (0,)


def is_newer(remote: str, local: str = "") -> bool:
    """远端版本是否比本地新。按数字段比较，所以 1.10.0 比 1.9.9 新。"""
    return _parse(remote) > _parse(local or __version__)


def _pick_asset(assets: list[dict]) -> dict:
    """挑出要下载的附件：优先和当前 exe 同名的，其次任意 exe。"""
    candidates = [a for a in assets if str(a.get("name") or "").lower().endswith(".exe")]
    if not candidates:
        return {}
    if is_frozen():
        current = Path(sys.executable).name.lower()
        for asset in candidates:
            if str(asset.get("name") or "").lower() == current:
                return asset
    return candidates[0]


def check(timeout: int = 10) -> UpdateInfo:
    """读一次 GitHub 最新 Release，和本地版本比对。"""
    try:
        response = requests.get(
            API,
            timeout=timeout,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": USER_AGENT,
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
    asset = _pick_asset(list(payload.get("assets") or []))
    return UpdateInfo(
        has_update=bool(tag) and is_newer(tag),
        version=tag,
        notes=str(payload.get("body") or ""),
        url=str(payload.get("html_url") or __github__),
        asset_name=str(asset.get("name") or ""),
        asset_url=str(asset.get("browser_download_url") or ""),
        asset_size=int(asset.get("size") or 0),
        asset_digest=str(asset.get("digest") or ""),
        published_at=str(payload.get("published_at") or ""),
    )


def latest_download_url() -> str:
    return f"{__github__}/releases/latest"


# ------------------------------------------------------------------ 自动更新
def current_exe() -> Path | None:
    """当前运行的 exe；源码运行时返回 None。"""
    if not is_frozen():
        return None
    return Path(sys.executable).resolve()


def can_self_update() -> bool:
    """能不能就地替换自己：必须是打包运行，且程序目录可写。"""
    exe = current_exe()
    if exe is None or not exe.is_file():
        return False
    probe = exe.parent / ".quarkrelay-write-test"
    try:
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        logger.info("程序目录不可写，自动更新将退化为手动下载：%s", exe.parent)
        return False


def download(
    info: UpdateInfo,
    *,
    dest: Path | None = None,
    progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: int = 30,
) -> Path:
    """把新版 exe 下载到 update 目录，校验通过后返回文件路径。"""
    if not info.asset_url:
        raise UpdateError("这个版本没有提供 exe 附件，请到项目主页手动下载")
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    target = Path(dest) if dest else UPDATE_DIR / (info.asset_name or "QuarkRelay.exe")
    temp = target.with_suffix(target.suffix + ".part")

    done = 0
    try:
        with requests.get(
            info.asset_url,
            stream=True,
            timeout=timeout,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT, "Accept": "application/octet-stream"},
        ) as response:
            response.raise_for_status()
            total = int(response.headers.get("Content-Length") or 0) or info.asset_size
            with open(temp, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    if should_cancel and should_cancel():
                        raise UpdateError("已取消更新")
                    if not chunk:
                        continue
                    handle.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
    except requests.RequestException as exc:
        temp.unlink(missing_ok=True)
        raise UpdateError(f"下载失败：{exc}") from exc
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise UpdateError(f"写入更新文件失败：{exc}") from exc
    except UpdateError:
        temp.unlink(missing_ok=True)
        raise

    _verify(temp, info)
    try:
        temp.replace(target)
    except OSError as exc:
        raise UpdateError(f"替换更新文件失败：{exc}") from exc
    logger.info("更新包已就绪：%s（%s）", target, human_size(done))
    return target


def _verify(path: Path, info: UpdateInfo) -> None:
    """最小但有效的校验：大小对得上 + 真的是 Windows 可执行文件。"""
    size = path.stat().st_size
    if size <= 0:
        path.unlink(missing_ok=True)
        raise UpdateError("下载到的文件是空的，请重试")
    if info.asset_size and size != info.asset_size:
        path.unlink(missing_ok=True)
        raise UpdateError(f"下载不完整（{human_size(size)}/{human_size(info.asset_size)}），请重试")
    with open(path, "rb") as handle:
        if handle.read(2) != b"MZ":
            path.unlink(missing_ok=True)
            raise UpdateError("下载到的文件不是可执行程序，已丢弃")
    if info.asset_digest.startswith("sha256:"):
        expected = info.asset_digest.split(":", 1)[1].strip().lower()
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        if digest.hexdigest() != expected:
            path.unlink(missing_ok=True)
            raise UpdateError("更新包校验失败（sha256 不一致），已丢弃")


# 注意：脚本里刻意不出现管道（a | b）。本进程是用 DETACHED_PROCESS 拉起来的，
# 这种「没有控制台」的 cmd 里管道会永久挂住（tasklist | find 永远不返回），
# 于是更新会静悄悄卡在第一步。要过滤输出就把结果先落盘、再让 find 读文件。
_SCRIPT = """@echo off
setlocal
{charset}set "TARGET={target}"
set "SOURCE={source}"
set "TARGETDIR={targetdir}"
set "PID={pid}"
set "TEMPFILE={tempfile}"
set "LOGFILE={logfile}"
set /a waited=0
set /a tries=0

rem 等旧进程彻底退出：运行中的 exe 没法覆盖，而且旧实例的互斥体会让新实例直接退出
:wait
tasklist /FI "PID eq %PID%" /NH > "%TEMPFILE%" 2>&1
find "%PID%" "%TEMPFILE%" >NUL
set "ALIVE=%errorlevel%"
del /q "%TEMPFILE%" >NUL 2>&1
if not "%ALIVE%"=="0" goto replace
set /a waited+=1
if %waited% geq 60 goto replace
ping -n 2 127.0.0.1 >NUL
goto wait

rem 先备份再替换：万一新版挪不过去还能滚回来
:replace
if not exist "%SOURCE%" goto giveup
if exist "%TARGET%" move /y "%TARGET%" "%TARGET%.old" >NUL 2>&1
move /y "%SOURCE%" "%TARGET%" >NUL 2>&1
if not exist "%SOURCE%" goto done
rem 被杀软或资源管理器占用时会失败，收回备份、隔一秒再来
if exist "%TARGET%.old" if not exist "%TARGET%" move /y "%TARGET%.old" "%TARGET%" >NUL 2>&1
set /a tries+=1
if %tries% geq 30 goto giveup
ping -n 2 127.0.0.1 >NUL
goto replace

:done
del /q "%TARGET%.old" >NUL 2>&1
cd /d "%TARGETDIR%"
{restart}
goto bye

:giveup
if exist "%TARGET%.old" if not exist "%TARGET%" move /y "%TARGET%.old" "%TARGET%" >NUL 2>&1
echo [%DATE% %TIME%] 自动更新失败：没能把 "%SOURCE%" 替换成 "%TARGET%" >>"%LOGFILE%"
goto bye

:bye
rem 批处理自己删掉自己（先中止读取，再删）
(goto) 2>NUL & del "%~f0"
exit /b 0
"""


def _render_script(
    target: Path,
    source: Path,
    pid: int,
    *,
    restart: bool = True,
    workdir: Path | None = None,
) -> str:
    """渲染替换脚本。restart=False 时不自启动（自检里要跑一遍这个脚本）。"""
    work = Path(workdir) if workdir else UPDATE_DIR
    return _SCRIPT.format(
        charset="",
        target=target,
        source=source,
        targetdir=target.parent,
        pid=pid,
        tempfile=work / "old-pid.txt",
        logfile=work / "update.log",
        restart='start "" "%TARGET%"' if restart else "rem 本次不自启动",
    )


def _write_script(text: str, path: Path | None = None) -> Path:
    """写批处理脚本。优先用系统 ANSI 代码页，中文路径才不会变乱码。"""
    script = Path(path) if path else UPDATE_DIR / "apply-update.bat"
    script.parent.mkdir(parents=True, exist_ok=True)
    try:
        script.write_text(text, encoding="mbcs", newline="\r\n")
    except (LookupError, UnicodeEncodeError):
        script.write_text(text, encoding="utf-8", newline="\r\n")
    return script


def apply_update(new_exe: Path, *, restart: bool = True) -> Path:
    """生成并启动替换脚本；调用方紧接着应该退出程序。返回脚本路径。"""
    target = current_exe()
    if target is None:
        raise UpdateError("当前是源码运行模式，无法自动替换，请手动更新")
    source = Path(new_exe).resolve()
    if not source.is_file():
        raise UpdateError("更新包不见了，请重新下载")

    script = _write_script(
        _render_script(target, source, os.getpid(), restart=restart)
    )
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        subprocess.Popen(  # noqa: S603 - 命令与参数都由本模块生成，没有外部输入
            ["cmd", "/c", str(script)],
            cwd=str(script.parent),
            creationflags=flags,
            close_fds=True,
        )
    except OSError as exc:
        raise UpdateError(f"启动更新脚本失败：{exc}") from exc
    if not restart:
        logger.info("更新脚本已启动（不自动重启）")
    logger.info("更新脚本已启动，退出后会自动替换并重启：%s", script)
    time.sleep(0.3)
    return script
