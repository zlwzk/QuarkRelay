"""应用服务层：把配置、网盘客户端、任务中心、历史、内置浏览器串起来，界面只管调用。"""

from __future__ import annotations

import logging
import threading
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from ..config import config
from ..core import relay
from ..core.baidu import BaiduClient
from ..core.errors import AppError, BaiduAuthError, friendly
from ..core.links import ShareLink, extract
from ..core.naming import render_for
from ..core.quark import QuarkAuth, QuarkClient
from ..core.tasks import Task, TaskCenter, TaskStatus
from ..store import Record, history
from .browser import (
    BAIDU_KEYS,
    BaiduDownloadBridge,
    WebLoginDialog,
    cookie_string,
)

logger = logging.getLogger(__name__)


class _SignalBridge(QObject):
    """把工作线程的结果搬回主线程。"""

    done = Signal(object)


class AppServices(QObject):
    quark_changed = Signal()
    baidu_changed = Signal()
    history_changed = Signal()
    links_detected = Signal(list)
    toast = Signal(str, str)
    status_message = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config()
        self.tasks = TaskCenter(max_workers=int(self.config.get("app.max_parallel", 2) or 2), parent=self)
        self.history = history()
        self.quark = self._make_quark()
        self.baidu = self._make_baidu()
        self.bridge = BaiduDownloadBridge(self)
        self._last_clipboard = ""
        self._clipboard_timer = QTimer(self)
        self._clipboard_timer.setInterval(900)
        self._clipboard_timer.timeout.connect(self._poll_clipboard)
        if self.config.get("app.watch_clipboard", True):
            self._clipboard_timer.start()
        self.tasks.task_finished.connect(self._on_task_finished)

    # ------------------------------------------------------------------ 客户端
    def _make_quark(self) -> QuarkClient:
        auth = QuarkAuth(
            access_token=str(self.config.get("quark.access_token", "") or ""),
            refresh_token=str(self.config.get("quark.refresh_token", "") or ""),
            device_id=str(self.config.get("quark.device_id", "") or ""),
            expires_at=float(self.config.get("quark.expires_at", 0) or 0),
            user_id=str(self.config.get("quark.user_id", "") or ""),
            nickname=str(self.config.get("quark.nickname", "") or ""),
        )
        return QuarkClient(auth)

    def _make_baidu(self) -> BaiduClient:
        return BaiduClient(
            cookies=str(self.config.get("baidu.cookies", "") or ""),
            user_agent=str(self.config.get("baidu.user_agent", "") or ""),
        )

    # ------------------------------------------------------------------ 状态
    @property
    def quark_logged_in(self) -> bool:
        return self.quark.auth.signed_in

    @property
    def baidu_logged_in(self) -> bool:
        return bool(self.config.get("baidu.cookies", ""))

    def quark_display(self) -> str:
        if not self.quark_logged_in:
            return "未登录"
        return self.quark.auth.nickname or str(self.config.get("quark.nickname", "") or "") or "已登录"

    def baidu_display(self) -> str:
        if not self.baidu_logged_in:
            return "未登录"
        return str(self.config.get("baidu.nickname", "") or "") or "已登录"

    def quark_storage(self) -> str:
        if not self.quark_logged_in:
            return ""
        try:
            return self.quark.storage_summary()
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取夸克容量失败：%s", exc)
            return ""

    def _persist_quark(self) -> None:
        for key, value in self.quark.auth.to_dict().items():
            self.config.set(f"quark.{key}", value, autosave=False)
        self.config.save()
        self.quark_changed.emit()

    def _persist_baidu(self) -> None:
        self.config.update(
            {
                "baidu.cookies": self.baidu.export_cookies(),
                "baidu.bdstoken": self.baidu.bdstoken,
                "baidu.uk": self.baidu.uk,
                "baidu.nickname": self.baidu.nickname,
            }
        )
        self.baidu_changed.emit()

    # ------------------------------------------------------------------ 登录
    def login_quark(self, parent=None) -> bool:
        client = QuarkClient(QuarkAuth(device_id=str(self.config.get("quark.device_id", "") or "")))
        try:
            info = client.start_authorize()
        except AppError as exc:
            self.toast.emit(friendly(exc), "error")
            return False
        except Exception as exc:  # noqa: BLE001
            self.toast.emit(f"打开夸克授权页失败：{friendly(exc)}", "error")
            return False

        dialog = WebLoginDialog(
            "登录夸克网盘",
            "在下方页面用夸克 App 扫码或账号登录并同意授权，成功后窗口会自动关闭。"
            "登录数据只保存在本机 %APPDATA%\\QuarkRelay，不影响系统浏览器里的登录状态。",
            info["authorize_page_url"],
            "quark",
            done_text="我已授权完成",
            parent=parent,
        )

        box: dict[str, Any] = {}
        bridge = _SignalBridge(self)

        def _close_if_open(_payload: object) -> None:
            if dialog.isVisible():
                dialog.accept()

        bridge.done.connect(_close_if_open)

        def _worker() -> None:
            try:
                box["code"] = client.poll_authorize_code(info["page_code"], timeout=280)
            except Exception as exc:  # noqa: BLE001
                box["error"] = exc
            bridge.done.emit(None)

        thread = threading.Thread(target=_worker, name="quark-auth", daemon=True)
        thread.start()
        accepted = dialog.exec() == dialog.DialogCode.Accepted
        thread.join(timeout=1.5)

        code = box.get("code")
        if not code:
            if box.get("error"):
                self.toast.emit(f"夸克授权未完成：{friendly(box['error'])}", "warning")
            elif accepted:
                self.toast.emit("还没拿到授权码，请再试一次", "warning")
            return False
        try:
            client.exchange_auth_code(code)
            payload = client.user_info()
            client.auth.nickname = str(payload.get("nickname") or "")
            if not client.auth.user_id:
                client.auth.user_id = str(payload.get("user_id") or "")
        except Exception as exc:  # noqa: BLE001
            self.toast.emit(f"换取夸克访问令牌失败：{friendly(exc)}", "error")
            return False

        self.quark = client
        self._persist_quark()
        self.toast.emit("夸克网盘登录成功", "success")
        return True

    def login_baidu(self, parent=None) -> bool:
        dialog = WebLoginDialog(
            "登录百度网盘",
            "在下方页面登录你的百度网盘账号（支持扫码）。检测到登录后窗口会自动关闭。"
            "会话 Cookie 只保存在本机，不会写入系统浏览器，也不影响你正常使用百度网盘。",
            "https://pan.baidu.com/",
            "baidu",
            autodetect=lambda cookies: bool(cookies.get("BDUSS")),
            cookie_keys=BAIDU_KEYS,
            parent=parent,
        )
        if dialog.exec() != dialog.DialogCode.Accepted:
            return False
        cookie_text = cookie_string(dialog.cookies, BAIDU_KEYS) or cookie_string(dialog.cookies)
        if "BDUSS" not in cookie_text:
            self.toast.emit("没有取到会话信息（BDUSS），可能登录还没完成", "warning")
            return False
        client = BaiduClient(cookies=cookie_text)
        try:
            client.verify()
        except BaiduAuthError as exc:
            self.toast.emit(friendly(exc), "error")
            return False
        except Exception as exc:  # noqa: BLE001
            self.toast.emit(f"校验百度登录失败：{friendly(exc)}", "error")
            return False
        self.baidu = client
        self._persist_baidu()
        self.toast.emit("百度网盘登录成功", "success")
        return True

    def logout_quark(self) -> None:
        self.quark = QuarkClient(QuarkAuth(device_id=str(self.config.get("quark.device_id", "") or "")))
        for key, value in (("access_token", ""), ("refresh_token", ""), ("nickname", ""), ("user_id", "")):
            self.config.set(f"quark.{key}", value, autosave=False)
        self.config.set("quark.expires_at", 0, autosave=False)
        self.config.save()
        self.quark_changed.emit()
        self.toast.emit("已退出夸克网盘", "info")

    def logout_baidu(self) -> None:
        self.baidu = BaiduClient()
        for key in ("cookies", "bdstoken", "nickname", "uk"):
            self.config.set(f"baidu.{key}", "", autosave=False)
        self.config.save()
        self.baidu_changed.emit()
        self.toast.emit("已退出百度网盘", "info")

    def ensure_quark(self, parent=None) -> bool:
        if self.quark_logged_in:
            return True
        return self.login_quark(parent)

    def ensure_baidu(self, parent=None) -> bool:
        if self.baidu_logged_in:
            try:
                if not self.baidu.bdstoken:
                    self.baidu.verify()
                return True
            except Exception as exc:  # noqa: BLE001
                logger.info("百度会话已失效，准备重新登录：%s", exc)
        return self.login_baidu(parent)

    # ---------------------------------------------------------------- 业务流程
    def start_quark_transfer(
        self,
        link: ShareLink,
        name: str,
        target_dir: str = "",
        *,
        url_type: int | None = None,
        expired_type: int | None = None,
        parent=None,
    ) -> Task | None:
        if not self.ensure_quark(parent):
            return None
        directory = target_dir or str(self.config.get("quark.default_dir", "夸克中转站"))
        url_type = int(self.config.get("quark.share_url_type", 2) if url_type is None else url_type)
        expired_type = int(
            self.config.get("quark.share_expired_type", 1) if expired_type is None else expired_type
        )
        client = self.quark

        def worker(task: Task) -> dict[str, Any]:
            return relay.quark_transfer_worker(
                task,
                client,
                pwd_id=link.pwd_id,
                passcode=link.code,
                name=name,
                target_dir=directory,
                url_type=url_type,
                expired_type=expired_type,
            )

        return self.tasks.submit(
            "quark_transfer",
            f"转存并分享：{name}",
            worker,
            payload={"source": link.url, "code": link.code, "dir": directory, "name": name},
        )

    def start_baidu_relay(
        self,
        share_url: str,
        password: str,
        *,
        fs_ids: list[int] | None,
        quark_dir: str,
        baidu_dir: str,
        make_share: bool,
        share_name: str,
        keep_buffer: bool = False,
        parent=None,
    ) -> Task | None:
        if not self.ensure_baidu(parent):
            return None
        if not self.ensure_quark(parent):
            return None
        baidu, quark = self.baidu, self.quark
        bridge = self.bridge
        speed = int(self.config.get("app.speed_limit_kbps", 0) or 0)

        def worker(task: Task) -> dict[str, Any]:
            return relay.baidu_to_quark_worker(
                task,
                baidu,
                quark,
                share_url=share_url,
                password=password,
                fs_ids=fs_ids,
                baidu_dir=baidu_dir,
                quark_dir=quark_dir,
                dlink_provider=bridge.request_dlink,
                speed_kbps=speed,
                keep_buffer=keep_buffer,
                share_name=share_name,
                make_share=make_share,
                url_type=int(self.config.get("quark.share_url_type", 2)),
                expired_type=int(self.config.get("quark.share_expired_type", 1)),
            )

        return self.tasks.submit(
            "baidu_relay",
            f"百度搬运到夸克（{share_name or '未命名'}）",
            worker,
            payload={"share": share_url, "code": password},
        )

    def start_export_share(self, names: list[str], share_name: str, target_dir: str = "", parent=None) -> Task | None:
        if not self.ensure_quark(parent):
            return None
        client = self.quark
        directory = target_dir or str(self.config.get("quark.default_dir", "夸克中转站"))

        def worker(task: Task) -> dict[str, Any]:
            return relay.quark_export_share_worker(
                task,
                client,
                names=names,
                target_dir=directory,
                share_name=share_name,
                url_type=int(self.config.get("quark.share_url_type", 2)),
                expired_type=int(self.config.get("quark.share_expired_type", 1)),
            )

        return self.tasks.submit("share_export", f"生成分享：{share_name}", worker)

    # ---------------------------------------------------------------- 任务回调
    def _on_task_finished(self, task: Task) -> None:
        if task.status == TaskStatus.FAILED:
            self.toast.emit(f"{task.title} 失败：{task.error}", "error")
            text = task.error or ""
            if "登录" in text or "令牌" in text or "会话" in text:
                if task.kind == "baidu_relay":
                    self.baidu_changed.emit()
                else:
                    self.quark_changed.emit()
            return
        if task.status != TaskStatus.SUCCESS:
            return

        result = task.result or {}
        link = str(result.get("link") or "")
        if link:
            name = str(result.get("name") or task.title)
            code = str(result.get("code") or "")
            self.history.add(
                kind=task.kind,
                name=name,
                link=link,
                code=code,
                source="baidu" if task.kind == "baidu_relay" else "quark",
                target_path=str(result.get("target_dir") or ""),
                note="、".join(str(item) for item in (result.get("files") or []))[:300],
            )
            self.history_changed.emit()
            if self.config.get("app.copy_after_share", True):
                QGuiApplication.clipboard().setText(
                    str(result.get("combined") or render_for(name, link, code))
                )
        if task.kind == "baidu_relay":
            self.quark_changed.emit()
        self.toast.emit(f"{task.title} 已完成", "success")

    # ---------------------------------------------------------------- 剪贴板
    def set_clipboard_watch(self, enabled: bool) -> None:
        self.config.set("app.watch_clipboard", bool(enabled))
        if enabled:
            self._clipboard_timer.start()
        else:
            self._clipboard_timer.stop()

    def _poll_clipboard(self) -> None:
        try:
            text = QGuiApplication.clipboard().text() or ""
        except Exception:  # noqa: BLE001
            return
        if not text or text == self._last_clipboard or len(text) > 4000:
            return
        self._last_clipboard = text
        links = extract(text)
        if links:
            self.links_detected.emit(links)

    def take_clipboard_links(self) -> list[ShareLink]:
        return extract(QGuiApplication.clipboard().text() or "")

    def scan_text(self, text: str) -> list[ShareLink]:
        return extract(text)

    # ---------------------------------------------------------------- 其它
    def records(self, keyword: str = "", limit: int = 400) -> list[Record]:
        return self.history.list(keyword=keyword, limit=limit)

    def apply_parallelism(self, workers: int) -> None:
        workers = max(1, int(workers))
        self.config.set("app.max_parallel", workers)
        self.tasks.set_max_workers(workers)

    def shutdown(self) -> None:
        self._clipboard_timer.stop()
        self.tasks.shutdown()
        self.history.close()
