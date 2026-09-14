"""内置浏览器：登录取会话、以及百度下载直链的拦截获取。

设计要点
* 使用 QtWebEngine 的**命名持久化 Profile**，cookie 存在 %APPDATA%\\QuarkRelay\\webengine
  下，与系统浏览器完全隔离，不会影响你平时上网的登录状态。
* 关键 cookie（BDUSS / STOKEN）是 HttpOnly 的，JS 的 document.cookie 拿不到，所以会话
  靠两条路一起取：对话框全程订着 `cookieAdded`（页面新写的 cookie 立刻到手，HttpOnly 也一样），
  再直接读一次 Profile 的 cookie 库垫底。**不能用 `loadAllCookies()`**：Qt 6.11 上实测
  它一条都回不出来（库里有 40 条、其中就有 BDUSS，回调却是空的），
  这正是「扫码登录明明成功了却一直显示未登录」的根因之一。
* 百度下载直链由百度前端私有签名生成，本程序不逆向；而是让页面自己去请求下载，
  我们从 `QWebEngineProfile.downloadRequested` 里截获真实直链，再用同一套 cookie
  在后台流式读取数据（不落到桌面上）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QEventLoop, QObject, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QGuiApplication
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
from .qr import CARD_SIZE, QrPanel, QrWatcher
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
    # 目录必须事先建好：CachePath 指向不存在的目录时 Chromium 会报
    # 「Unable to move the cache」，缓存和 cookie 持久化都会受影响。
    (store_dir / "cache").mkdir(parents=True, exist_ok=True)
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


def _cookie_db_path(prof: QWebEngineProfile) -> Path | None:
    """内置浏览器的 cookie 库在哪。"""
    try:
        root = Path(prof.persistentStoragePath())
    except Exception:  # noqa: BLE001
        return None
    # 老版本 Chromium 把库放在 Profile 根目录，新版本挪进了 Network 子目录
    for relative in ("Network/Cookies", "Cookies"):
        path = root / relative
        if path.is_file():
            return path
    return None


def read_persisted_cookies(prof: QWebEngineProfile) -> dict[str, dict[str, str]]:
    """直接读一次 Profile 的 cookie 库，返回 {域名: {名字: 值}}。

    只用它垫底：读不到（库被占用、字段变了、还没生成）就返回空字典，绝不抛异常。
    本机 Qt 把 cookie 的 value 列存成明文，所以不需要做任何解密。
    """
    path = _cookie_db_path(prof)
    if path is None:
        return {}
    # Chromium 的时间戳是「1601-01-01 起的微秒」
    now_us = int((time.time() + 11644473600) * 1_000_000)
    sql = (
        "select host_key, name, value from cookies"
        " where value != '' and (has_expires = 0 or expires_utc > ?)"
    )
    rows: list[tuple[object, object, object]] = []
    for attempt in range(3):
        try:
            con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.6)
            try:
                rows = list(con.execute(sql, (now_us,)))
            finally:
                con.close()
            break
        except sqlite3.Error as exc:
            logger.debug("读 cookie 库失败（第 %s 次）：%s", attempt + 1, exc)
            if attempt == 2:
                return {}
            time.sleep(0.1)

    cookies: dict[str, dict[str, str]] = {}
    for host, name, value in rows:
        name, value = str(name or ""), str(value or "")
        if not name or not value:
            continue
        cookies.setdefault(str(host or "").lstrip(".").lower(), {})[name] = value
    return cookies


def clear_profile_cookies(prof: QWebEngineProfile) -> None:
    """清空某个 Profile 的 cookie（会话已失效时用，好让下次能重新扫码登录）。"""
    try:
        prof.cookieStore().deleteAllCookies()
    except Exception as exc:  # noqa: BLE001
        logger.debug("清空 cookie 失败：%s", exc)


class CookieJar(QObject):
    """收集 Profile 里的 cookie（含 HttpOnly 的那些）。

    两条来源合起来用：
      * 常驻订 `cookieAdded`：页面刚写下的 cookie 立刻到手，扫码登录全靠它；
      * 构造时直接读一次 cookie 库：冷启动时那些**已经躺在库里**的 cookie
        不会走回调（`loadAllCookies()` 在 Qt 6.11 上一条都不回），不垫这一层
        就会出现「库里明明有 BDUSS，程序却说没登录」。
    """

    def __init__(self, prof: QWebEngineProfile, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._cookies: dict[str, str] = {}
        self._by_domain: dict[str, dict[str, str]] = {}
        self._details: list[dict[str, str]] = []
        self._last_seen = 0.0
        self._store = prof.cookieStore()
        self._store.cookieAdded.connect(self._on_added)
        # 域名从「粗」到「细」垫底，细的（pan.baidu.com）覆盖粗的（baidu.com）
        seeded = read_persisted_cookies(prof)
        for domain in sorted(seeded, key=lambda item: item.count(".")):
            self._cookies.update(seeded[domain])
            self._by_domain.setdefault(domain, {}).update(seeded[domain])

    @staticmethod
    def _decode(raw: object) -> str:
        try:
            return bytes(raw).decode("utf-8", "ignore")  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return str(raw or "")

    def _on_added(self, cookie: QNetworkCookie) -> None:
        name = self._decode(cookie.name())
        value = self._decode(cookie.value())
        if not name:
            return
        domain = (cookie.domain() or "").lstrip(".").lower()
        self._cookies[name] = value
        self._by_domain.setdefault(domain, {})[name] = value
        self._details.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": cookie.path(),
            }
        )
        self._last_seen = time.monotonic()

    def collect(self, min_ms: int = 700, quiet_ms: int = 400, max_ms: int = 8000) -> dict[str, str]:
        """等回调流静默下来再收工：至少 min_ms，连续 quiet_ms 没新 cookie 就走，最多 max_ms。"""
        loop = QEventLoop()
        started = time.monotonic()
        self._last_seen = started
        # 该调用的实现在新版 Qt 上不回任何东西，留着只是照顾老版本 —— 别指望它
        self._store.loadAllCookies()

        poll = QTimer(self)
        poll.setInterval(120)
        watchdog = QTimer(self)
        watchdog.setSingleShot(True)

        def tick() -> None:
            elapsed = (time.monotonic() - started) * 1000
            quiet = (time.monotonic() - self._last_seen) * 1000
            if elapsed >= max_ms or (elapsed >= min_ms and quiet >= quiet_ms):
                loop.quit()

        poll.timeout.connect(tick)
        watchdog.timeout.connect(loop.quit)
        poll.start()
        watchdog.start(int(max_ms))
        loop.exec()
        poll.stop()
        watchdog.stop()
        return dict(self._cookies)

    def cookies_for(self, *domain_suffixes: str) -> dict[str, str]:
        """按域名取 cookie。

        同名 cookie（比如 `BDUSS`）在不同域下可能值不同，合在一起会互相覆盖，
        所以建会话时要按域名挑。同一层级的冲突按「域名越具体越优先」来定，
        免得 `STOKEN` 取成 passport 那一份而不是 pan 那一份。
        """
        merged: dict[str, str] = {}
        for domain in sorted(self._by_domain, key=lambda item: item.count(".")):
            if any(domain == s or domain.endswith("." + s) for s in domain_suffixes):
                merged.update(self._by_domain[domain])
        return merged

    @property
    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    @property
    def by_domain(self) -> dict[str, dict[str, str]]:
        return {domain: dict(items) for domain, items in self._by_domain.items()}


def read_cookie_jar(prof: QWebEngineProfile, **kwargs: int) -> CookieJar:
    """读一次 cookie，返回收集器（含按域名分组的信息）。"""
    jar = CookieJar(prof)
    try:
        jar.collect(**kwargs)
    finally:
        try:
            jar._store.cookieAdded.disconnect(jar._on_added)
        except (RuntimeError, TypeError):
            pass
    return jar


def read_cookies(prof: QWebEngineProfile, **kwargs: int) -> dict[str, str]:
    return read_cookie_jar(prof, **kwargs).cookies


def cookie_string(cookies: dict[str, str], keys: list[str] | None = None) -> str:
    if keys:
        return "; ".join(f"{k}={cookies[k]}" for k in keys if k in cookies)
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


BAIDU_KEYS = ["BDUSS", "STOKEN", "PANWEB", "BAIDUID", "BAIDUID_BFESS", "H_PS_PSSID", "ZFY"]


# --------------------------------------------------------------------- 登录窗
class WebLoginDialog(QDialog):
    """内置浏览器登录窗口。

    autodetect 返回 True 时视为登录成功，窗口会自动关闭并回传 cookie。
    传入 qr_hint 时窗口里**只摆那枚放大后的二维码**（页面里的那枚太小、还常被
    浮层压着）；登录页默认收起来，需要时点「显示登录页」再展开。
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
        qr_hint: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self._base_size = size
        self.resize(*self._fit_size(size, qr_hint))
        self.setModal(True)
        self.cookies: dict[str, str] = {}
        self.cookie_keys = cookie_keys
        self._autodetect = autodetect
        self._detected = False
        self.await_result: Callable[[], tuple[bool, str]] | None = None
        self._await_timer: QTimer | None = None
        self._body: QHBoxLayout | None = None
        self.page_btn: QPushButton | None = None

        self.prof = profile(profile_name)
        self.page = QWebEnginePage(self.prof, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        # 全程订着 cookie：页面一写进来就到手，扫码登录不必轮着翻库
        self.jar: CookieJar = CookieJar(self.prof, self)

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

        self.qr_panel: QrPanel | None = None
        self.qr_watcher: QrWatcher | None = None
        if qr_hint:
            # 只摆二维码，两边各留一条等长的空白 → 二维码正好居中。
            # 登录页**不进布局**：布局里的隐藏控件会被算成 0 尺寸，页面照 0 宽排版，
            # 二维码就抠不出来了。所以给它一个真实尺寸，单独藏着。
            body = QHBoxLayout()
            body.setSpacing(14)
            body.addStretch(1)
            self.qr_panel = QrPanel(qr_hint, self, on_refresh=self._refresh_qr)
            body.addWidget(self.qr_panel, 0, Qt.AlignmentFlag.AlignTop)
            body.addStretch(1)
            layout.addLayout(body, 1)
            self._body = body
            self.view.resize(1100, 800)
            self.view.hide()
            self.qr_watcher = QrWatcher(self.page, self.qr_panel, self)
        else:
            layout.addWidget(self.view, 1)

        footer = QHBoxLayout()
        footer.setSpacing(8)
        self.status = QLabel("正在加载…")
        self.status.setObjectName("Muted")
        footer.addWidget(self.status, 1)
        if self.qr_panel is not None:
            self.page_btn = QPushButton("显示登录页")
            self.page_btn.setObjectName("Ghost")
            self.page_btn.clicked.connect(self._toggle_page)
            footer.addWidget(self.page_btn)
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

        if self.qr_watcher is not None:
            self.qr_watcher.start()
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl(url))

        if self._autodetect:
            self._timer = QTimer(self)
            self._timer.setInterval(1500)
            self._timer.timeout.connect(lambda: self._check(manual=False))
            self._timer.start()
        else:
            self._timer = None

    @staticmethod
    def _fit_size(
        size: tuple[int, int], qr_hint: str | None, page_visible: bool = True
    ) -> tuple[int, int]:
        """窗口要装得下二维码卡片；连着登录页一起看时再宽一点。

        但都别顶出屏幕：屏幕小的时候以可用区为准。
        """
        width, height = size
        if qr_hint:
            if page_visible:
                # 左边让给二维码卡片，右边给登录页留够宽度（登录页本身就是窄表单）
                width = CARD_SIZE + 24 + max(720, width - 260)
            else:
                width = CARD_SIZE + 140
                height = min(height, 780)
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            room = screen.availableGeometry()
            width = min(width, room.width() - 60)
            height = min(height, room.height() - 60)
        return max(520 if qr_hint else 720, width), max(520, height)

    def _toggle_page(self) -> None:
        """「只看二维码」⇄「连着登录页一起看」。"""
        if self.view.isVisible():
            self.view.hide()
            if self._body is not None:
                self._body.removeWidget(self.view)
            if self.page_btn is not None:
                self.page_btn.setText("显示登录页")
            self.resize(*self._fit_size(self._base_size, "qr", False))
        else:
            if self._body is not None:
                self._body.addWidget(self.view, 1)
            self.view.show()
            if self.page_btn is not None:
                self.page_btn.setText("只看二维码")
            self.resize(*self._fit_size(self._base_size, "qr", True))

    def _refresh_qr(self) -> None:
        if self.qr_watcher is not None:
            self.qr_watcher.refresh()

    def _on_loaded(self, ok: bool) -> None:
        self.status.setText("页面已加载，请完成登录/授权" if ok else "页面加载失败，可点「重新加载」")

    def set_status(self, text: str) -> None:
        self.status.setText(text)

    def watch_result(self, callback: Callable[[], tuple[bool, str]], interval_ms: int = 1200) -> None:
        """挂一个「后端结果查询」回调，返回 (能否收工, 给用户看的话)。

        夸克这种登录，授权结果只存在于后端轮询里：用户点完「同意授权」时
        cookie 里什么都看不出来，所以对话框既不能立刻关（授权码还没到），
        也不能一直傻等 —— 交给这个回调判断，能收工时自动关闭。
        """
        self.await_result = callback
        if self._await_timer is None:
            self._await_timer = QTimer(self)
            self._await_timer.setInterval(interval_ms)
            self._await_timer.timeout.connect(self._poll_await)
        self._await_timer.start()

    def _poll_await(self) -> None:
        if self.await_result is None:
            return
        try:
            ready, message = self.await_result()
        except Exception as exc:  # noqa: BLE001
            logger.debug("查询授权结果失败：%s", exc)
            return
        if message:
            self.status.setText(message)
        if ready:
            if self._await_timer:
                self._await_timer.stop()
            self.accept()

    def _check(self, manual: bool = False) -> None:
        # cookie 由常驻的 jar 全程收着，这里只取一份快照 —— 不再每次都去翻库
        # （翻库那一下要把事件循环停住，界面会一顿一顿的）。
        cookies = self.jar.cookies
        if not cookies:
            if manual:
                self.status.setText("还没读到登录信息，请在手机 App 里完成扫码")
            return
        self.cookies = cookies
        if self._autodetect and self._autodetect(cookies):
            if not self._detected:
                self._detected = True
                self.status.setText("已检测到登录状态，正在完成…")
                QTimer.singleShot(400, self._finish)
        elif manual:
            self.status.setText("已读到会话信息，点「完成登录」继续")

    def _finish(self) -> None:
        if self._timer:
            self._timer.stop()
        if self.jar.cookies:
            self.cookies = self.jar.cookies
        if self._autodetect and self.cookies and not self._autodetect(self.cookies):
            keep = self.status.text()
            self.status.setText("似乎还没登录成功，再试一次？（" + keep + "）")
            return
        if self.await_result is not None:
            # 结果由后端说了算：没到就继续等，别让用户白点一次
            self._poll_await()
            return
        self.accept()


# ------------------------------------------- 百度下载直链：浏览器拦截桥
# 点「下载」之前必须先认准**是哪一行**。
# 旧写法是「点文件列表里第一个可见的『下载』」，一个目录里只要不止一个文件，
# 点中的就是第一行那个文件 —— 取回来的直链是别人的，还照样按目标文件的名字传上去。
JS_CLICK_DOWNLOAD = r"""
(function (fileName) {
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
  function text(el) {
    return (el && el.textContent ? el.textContent : '').replace(/\s+/g, '');
  }
  var DOWNLOAD = ['下载', '下载文件', '下载到本地', '下载此文件'];
  function findDownload(scope, loose) {
    var nodes = scope.querySelectorAll('a,span,div,button,i,li');
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!visible(el)) continue;
      var text = label(el);
      for (var j = 0; j < DOWNLOAD.length; j++) {
        if (text === DOWNLOAD[j] || (loose && text.indexOf(DOWNLOAD[j]) >= 0)) return el;
      }
    }
    return null;
  }
  // 一、按文件名找到那一行
  var row = null;
  var rows = document.querySelectorAll(
    '[class*="file-list"] [class*="item"], [class*="filelist"] li, [class*="list-item"], tbody tr, tr'
  );
  for (var i = 0; i < rows.length; i++) {
    if (text(rows[i]).indexOf(fileName) < 0) continue;
    row = rows[i];
    break;
  }
  if (!row) {
    // 二、结构认不出来就退一步：找到写着文件名的节点，再往上找像「一行」的那层
    var nodes = document.querySelectorAll('a,span,div');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (!visible(node) || label(node) !== fileName) continue;
      var el = node;
      for (var up = 0; up < 8 && el; up++) {
        if (el.querySelector &&
            (findDownload(el, false) || el.querySelector('[class*="more"], [class*="operate"], [class*="dot"]'))) {
          row = el;
          break;
        }
        el = el.parentElement;
      }
      if (row) break;
    }
  }
  if (!row) return 'row-not-found';
  // 有些界面的操作按钮是悬停才显示，先晃一下这一行
  try {
    row.dispatchEvent(new MouseEvent('mouseover', {bubbles: true}));
    row.dispatchEvent(new MouseEvent('mouseenter', {bubbles: false}));
  } catch (e) {}
  var hit = findDownload(row, false);
  if (hit) { hit.click(); return 'ok-row'; }
  // 三、下载藏在「更多」菜单里：先展开菜单，再点菜单项
  var more = null;
  var menus = row.querySelectorAll('[class*="more"], [class*="operate"], [class*="dot"], [title*="更多"]');
  for (var i = 0; i < menus.length; i++) {
    if (visible(menus[i])) { more = menus[i]; break; }
  }
  if (!more) return 'more-not-found';
  more.click();
  setTimeout(function () {
    var item = findDownload(document.body, true);
    if (item) item.click();
  }, 400);
  return 'ok-menu';
})(__FILE_NAME__);
"""


def click_download_js(file_name: str) -> str:
    """把文件名安全地嵌进上面那段脚本（文件名里可能有引号、中文）。"""
    return JS_CLICK_DOWNLOAD.replace("__FILE_NAME__", json.dumps(file_name, ensure_ascii=False))


class _DLinkJob:
    def __init__(self, path: str, timeout: float) -> None:
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
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
        job = self._job
        if job is None:
            return
        self._attempt += 1
        self._page().runJavaScript(click_download_js(job.name), self._on_js_result)

    def _on_js_result(self, result) -> None:
        job = self._job
        if job is None:
            return
        logger.debug("百度下载按钮点击结果：%s", result)
        if result == "row-not-found":
            logger.info("页面里暂时没看到「%s」这一行（可能列表还没渲染完），稍后再试", job.name)

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
            message = f"没能自动点到「{job.name}」的下载，已打开浏览器窗口，请手动点它的「下载」"
            self.status_changed.emit(message)
            self.window_needed.emit(message)
            # 窗口要真的打开、且停在这个文件所在的目录，不然用户还得自己找过去
            self.prepare_manual(job.path)
            return
        self._page().runJavaScript(click_download_js(job.name), self._on_js_result)

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
