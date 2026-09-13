"""内置浏览器：登录取会话、以及百度下载直链的拦截获取。

设计要点
* 使用 QtWebEngine 的**命名持久化 Profile**，cookie 存在 %APPDATA%\\QuarkRelay\\webengine
  下，与系统浏览器完全隔离，不会影响你平时上网的登录状态。
* 用 `QWebEngineCookieStore.loadAllCookies()` 读取全部 cookie —— 包括 HttpOnly 的
  BDUSS / STOKEN，这些用 JS 的 document.cookie 是拿不到的。这也是选择 QtWebEngine
  而不是普通 WebView 的最主要原因。
* 百度下载直链由百度前端私有签名生成，本程序不逆向；而是让页面自己去请求下载，
  我们从 `QWebEngineProfile.downloadRequested` 里截获真实直链，再用同一套 cookie
  在后台流式读取数据（不落到桌面上）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from PySide6.QtCore import QEventLoop, QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtNetwork import QNetworkCookie
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..core.errors import BaiduError
from ..paths import WEB_DIR, ensure_dirs
from .widgets import Toast

logger = logging.getLogger(__name__)

DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_profiles: dict[str, QWebEngineProfile] = {}


def profile(name: str) -> QWebEngineProfile:
    """取得（并缓存）一个持久化浏览器 Profile。"""
    if name in _profiles:
        return _profiles[name]
    ensure_dirs()
    store_dir = WEB_DIR / name
    store_dir.mkdir(parents=True, exist_ok=True)
    prof = QWebEngineProfile(name)
    prof.setPersistentStoragePath(str(store_dir))
    prof.setCachePath(str(store_dir / "cache"))
    prof.setPersistentCookiesPolicy(
        QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
    )
    prof.setHttpUserAgent(DESKTOP_UA)
    prof.setHttpCacheType(QWebEngineProfile.HttpCacheType.DiskHttpCache)
    _profiles[name] = prof
    return prof


class CookieJar(QObject):
    """一次性收集 Profile 里的全部 cookie。"""

    def __init__(self, prof: QWebEngineProfile, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cookies: dict[str, str] = {}
        self._details: list[dict[str, str]] = []
        self._store = prof.cookieStore()
        self._store.cookieAdded.connect(self._on_added)

    def _on_added(self, cookie: QNetworkCookie) -> None:
        name = bytes(cookie.name()).decode("utf-8", "ignore")
        value = bytes(cookie.value()).decode("utf-8", "ignore")
        if not name:
            return
        self._cookies[name] = value
        self._details.append(
            {
                "name": name,
                "value": value,
                "domain": cookie.domain(),
                "path": cookie.path(),
            }
        )

    def collect(self, wait_ms: int = 900) -> dict[str, str]:
        loop = QEventLoop()
        self._store.loadAllCookies()
        QTimer.singleShot(wait_ms, loop.quit)
        loop.exec()
        return dict(self._cookies)


def read_cookies(prof: QWebEngineProfile, wait_ms: int = 900) -> dict[str, str]:
    jar = CookieJar(prof)
    try:
        return jar.collect(wait_ms)
    finally:
        try:
            jar._store.cookieAdded.disconnect(jar._on_added)
        except (RuntimeError, TypeError):
            pass


def cookie_string(cookies: dict[str, str], keys: list[str] | None = None) -> str:
    if keys:
        return "; ".join(f"{k}={cookies[k]}" for k in keys if k in cookies)
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


BAIDU_KEYS = ["BDUSS", "STOKEN", "PANWEB", "BAIDUID", "BAIDUID_BFESS", "H_PS_PSSID", "ZFY"]


# --------------------------------------------------------------------- 登录窗
class WebLoginDialog(QDialog):
    """内置浏览器登录窗口。

    autodetect 返回 True 时视为登录成功，窗口会自动关闭并回传 cookie。
    """

    def __init__(
        self,
        title: str,
        subtitle: str,
        url: str,
        profile_name: str,
        *,
        autodetect: Callable[[dict[str, str]], bool] | None = None,
        done_text: str = "完成登录",
        cookie_keys: list[str] | None = None,
        parent: QWidget | None = None,
        size: tuple[int, int] = (980, 720),
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(*size)
        self.setModal(True)
        self.cookies: dict[str, str] = {}
        self.cookie_keys = cookie_keys
        self._autodetect = autodetect
        self._detected = False

        self.prof = profile(profile_name)
        self.page = QWebEnginePage(self.prof, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)

        head = QVBoxLayout()
        head.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("PageTitle")
        head.addWidget(heading)
        sub = QLabel(subtitle)
        sub.setObjectName("Muted")
        sub.setWordWrap(True)
        head.addWidget(sub)
        layout.addLayout(head)

        layout.addWidget(self.view, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status = QLabel("正在加载…")
        self.status.setObjectName("Muted")
        footer.addWidget(self.status, 1)
        reload_btn = QPushButton("重新加载")
        reload_btn.setObjectName("Ghost")
        reload_btn.clicked.connect(lambda: self.view.reload())
        footer.addWidget(reload_btn)
        self.detect_btn = QPushButton("检测登录状态")
        self.detect_btn.setObjectName("Ghost")
        self.detect_btn.clicked.connect(lambda: self._check(manual=True))
        footer.addWidget(self.detect_btn)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        footer.addWidget(cancel)
        self.done_btn = QPushButton(done_text)
        self.done_btn.setObjectName("Primary")
        self.done_btn.clicked.connect(self._finish)
        footer.addWidget(self.done_btn)
        layout.addLayout(footer)

        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl(url))

        if self._autodetect:
            self._timer = QTimer(self)
            self._timer.setInterval(1500)
            self._timer.timeout.connect(lambda: self._check(manual=False))
            self._timer.start()
        else:
            self._timer = None

    def _on_loaded(self, ok: bool) -> None:
        self.status.setText("页面已加载，请完成登录/授权" if ok else "页面加载失败，可点「重新加载」")

    def _check(self, manual: bool = False) -> None:
        try:
            cookies = read_cookies(self.prof, wait_ms=400 if manual else 200)
        except Exception as exc:  # noqa: BLE001
            logger.debug("读取 cookie 失败：%s", exc)
            return
        if not cookies:
            if manual:
                self.status.setText("还没读到登录信息，请在页面里完成登录")
            return
        self.cookies = cookies
        if self._autodetect and self._autodetect(cookies):
            if not self._detected:
                self._detected = True
                self.status.setText("已检测到登录状态，正在完成…")
                QTimer.singleShot(400, self._finish)
        elif manual:
            self.status.setText("已读取到会话信息")

    def _finish(self) -> None:
        if self._timer:
            self._timer.stop()
        try:
            cookies = read_cookies(self.prof, wait_ms=600)
            if cookies:
                self.cookies = cookies
        except Exception:  # noqa: BLE001
            pass
        if self._autodetect and self.cookies and not self._autodetect(self.cookies):
            keep = self.status.text()
            self.status.setText("似乎还没登录成功，再试一次？（" + keep + "）")
            return
        self.accept()


# ------------------------------------------- 百度下载直链：浏览器拦截桥
JS_CLICK_DOWNLOAD = r"""
(function () {
  function visible(el) {
    if (!el) return false;
    var rect = el.getClientRects();
    return !!(el.offsetWidth || el.offsetHeight || (rect && rect.length));
  }
  function label(el) {
    return ((el.textContent || '') + ' ' +
            (el.getAttribute && (el.getAttribute('title') || el.getAttribute('aria-label')) || ''))
      .replace(/\s+/g, '');
  }
  var nodes = document.querySelectorAll('a,span,div,button,i,li');
  var best = null;
  for (var i = 0; i < nodes.length; i++) {
    var el = nodes[i];
    if (!visible(el)) continue;
    var text = label(el);
    if (text === '下载' || text === '下载文件') {
      // 优先选择更靠近文件列表的节点
      best = el;
      if (el.closest && el.closest('.nd-file-list, .file-list, [class*=filelist]')) return (el.click(), 'ok-in-list');
    }
  }
  if (best) { best.click(); return 'ok-toolbar'; }
  return 'not-found';
})();
"""


class _DLinkJob:
    def __init__(self, path: str, timeout: float) -> None:
        self.path = path
        self.event = threading.Event()
        self.url = ""
        self.error: Exception | None = None
        self.deadline = time.monotonic() + timeout


class BaiduDownloadBridge(QObject):
    """在主线程里操控隐藏浏览器，为后台线程提供百度下载直链。"""

    _job_requested = Signal(object)
    status_changed = Signal(str)
    window_needed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.prof = profile("baidu")
        self.prof.downloadRequested.connect(self._on_download_requested)
        self.page: QWebEnginePage | None = None
        self.view: QWebEngineView | None = None
        self._job: _DLinkJob | None = None
        self._attempt = 0
        self._timer = QTimer(self)
        self._timer.setInterval(2500)
        self._timer.timeout.connect(self._retry_click)
        self._job_requested.connect(self._start_job, Qt.ConnectionType.QueuedConnection)

    # ------------------------------------------------------------ 窗口管理
    def ensure_view(self) -> QWebEngineView:
        if self.view is None:
            self.view = QWebEngineView()
            self.view.setWindowTitle("百度网盘下载通道（可手动点击下载）")
            self.view.resize(1024, 720)
            self.view.setPage(self._page())
        return self.view

    def _page(self) -> QWebEnginePage:
        if self.page is None:
            self.page = QWebEnginePage(self.prof, self)
            self.page.loadFinished.connect(self._on_load_finished)
        return self.page

    def _on_load_finished(self, ok: bool) -> None:
        if self._job is None:
            return
        self._attempt += 1
        self._page().runJavaScript(JS_CLICK_DOWNLOAD, self._on_js_result)

    def _on_js_result(self, result) -> None:
        if self._job is None:
            return
        logger.debug("百度下载按钮点击结果：%s", result)

    def _retry_click(self) -> None:
        job = self._job
        if job is None:
            self._timer.stop()
            return
        if time.monotonic() > job.deadline:
            self._timer.stop()
            self._finish_job(None, BaiduError("自动获取下载直链超时，请改用「手动下载通道」"))
            return
        if self._attempt >= 4:
            self._timer.stop()
            message = "没能自动点到下载按钮，已打开浏览器窗口，请手动点一下「下载」"
            self.status_changed.emit(message)
            self.window_needed.emit(message)
            return
        self._page().runJavaScript(JS_CLICK_DOWNLOAD, self._on_js_result)

    def _on_download_requested(self, request) -> None:
        url = request.url().toString()
        logger.info("拦截到下载请求：%s", url.split("?")[0])
        try:
            request.cancel()
        except Exception:  # noqa: BLE001
            pass
        if url and (url.startswith("http://") or url.startswith("https://")):
            self._finish_job(url, None)
        else:
            self._finish_job(None, BaiduError("拦截到的下载地址无效"))

    def _finish_job(self, url: str | None, error: Exception | None) -> None:
        job, self._job = self._job, None
        self._timer.stop()
        if job is None:
            return
        job.url = url or ""
        job.error = error
        job.event.set()

    # --------------------------------------------------------------- 对外
    def request_dlink(self, remote_path: str, timeout: float = 180.0) -> str:
        """后台线程调用：阻塞直到拿到直链。"""
        job = _DLinkJob(remote_path, timeout)
        self._job_requested.emit(job)
        if not job.event.wait(timeout + 30):
            raise BaiduError("等待百度下载直链超时")
        if job.error:
            raise job.error
        if not job.url:
            raise BaiduError("没有拿到下载直链")
        return job.url

    # ----------------------------------------------------------- 主线程动作
    def _start_job(self, job: _DLinkJob) -> None:
        self.ensure_view()
        self._job = job
        self._attempt = 0
        self.status_changed.emit(f"正在获取直链：{job.path}")
        directory = job.path.rsplit("/", 1)[0] or "/"
        target = QUrl("https://pan.baidu.com/disk/main")
        target.setFragment(f"/index?category=all&path={directory}")
        self._page().load(target)
        self._timer.start()

    def prepare_manual(self, remote_path: str = "") -> None:
        """打开可视化窗口，让用户手动完成下载。"""
        view = self.ensure_view()
        if remote_path:
            directory = remote_path.rsplit("/", 1)[0] or "/"
            url = QUrl("https://pan.baidu.com/disk/main")
            url.setFragment(f"/index?category=all&path={directory}")
            self._page().load(url)
        view.show()
        view.raise_()
        view.activateWindow()
        Toast.show_message(view, "请在弹出的窗口里点一下「下载」，程序会自动截获直链", "info", 5000)


class ManualDLinkDialog(QDialog):
    """兜底：手动粘贴 dlink。"""

    def __init__(self, parent: QWidget | None = None, initial: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("手动填入下载直链")
        self.resize(720, 220)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        title = QLabel("粘贴百度网盘的下载直链（dlink）")
        title.setObjectName("CardTitle")
        layout.addWidget(title)
        hint = QLabel(
            "在浏览器里对文件点「下载」，从下载工具里复制 http://d.pcs.baidu.com/... 开头的地址，"
            "或直接粘贴到这里的输入框。程序会用同样的会话 Cookie 去读取。"
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        from PySide6.QtWidgets import QPlainTextEdit

        self.edit = QPlainTextEdit(initial)
        self.edit.setPlaceholderText("http://d.pcs.baidu.com/file/...")
        self.edit.setFixedHeight(70)
        layout.addWidget(self.edit)
        row = QHBoxLayout()
        row.addStretch(1)
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        row.addWidget(cancel)
        ok = QPushButton("使用这个地址")
        ok.setObjectName("Primary")
        ok.clicked.connect(self.accept)
        row.addWidget(ok)
        layout.addLayout(row)

    def value(self) -> str:
        return self.edit.toPlainText().strip()
