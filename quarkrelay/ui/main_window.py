"""主窗口：侧边导航 + 页面栈 + 托盘 + 全局快捷键 + 更新提示。"""

from __future__ import annotations

import logging
import os

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QStatusBar,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .. import __app_name__, __version__
from ..core import updater
from ..core.hotkey import GlobalHotkey
from ..paths import APP_DIR, pretty
from .pages.baidu import BaiduRelayPage
from .pages.accounts import AccountsPage
from .pages.about import AboutPage
from .pages.history import HistoryPage
from .pages.screenshot import ScreenshotPage
from .pages.settings import SettingsPage
from .pages.tasks import TasksPage
from .pages.transfer import TransferPage
from .services import AppServices
from .theme import PALETTES, app_icon, apply_theme
from .widgets import StatusDot, Toast, set_palette

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, services: AppServices, theme: str = "dark") -> None:
        super().__init__()
        self.services = services
        self.theme_name = theme
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.setWindowIcon(app_icon(256))
        self.resize(1280, 860)
        self.setMinimumSize(1080, 720)

        self._nav_buttons: list[QPushButton] = []
        self._pages: dict[str, QWidget] = {}
        self._task_badge: QLabel | None = None
        self._really_quit = False

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        layout = QHBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._build_sidebar())
        layout.addWidget(self._build_stack(), 1)

        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage("准备就绪")
        self._version_label = QLabel(f"v{__version__} · {pretty(APP_DIR)}")
        self._version_label.setObjectName("Faint")
        self.statusBar().addPermanentWidget(self._version_label)

        self._build_tray()
        self._wire_services()
        self._hotkey = GlobalHotkey(self)
        self._hotkey.activated.connect(lambda: self.navigate("screenshot", capture_region=True))
        spec = str(self.services.config.get("app.hotkey_screenshot", "Ctrl+Alt+Q") or "")
        if spec:
            if self._hotkey.register(spec):
                logger.info("全局快捷键已注册：%s", spec)
            else:
                logger.info("全局快捷键未能注册：%s", spec)

        self.navigate("transfer")
        QTimer.singleShot(2500, self._check_update)

    # ---------------------------------------------------------------- 侧边栏
    def _build_sidebar(self) -> QWidget:
        sidebar = QWidget()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(224)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 18, 14, 14)
        layout.setSpacing(6)

        brand = QHBoxLayout()
        brand.setSpacing(10)
        logo = QLabel()
        logo.setPixmap(app_icon(96).pixmap(QSize(38, 38)))
        brand.addWidget(logo)
        title_box = QVBoxLayout()
        title_box.setSpacing(0)
        name = QLabel(__app_name__)
        name.setObjectName("BrandName")
        sub = QLabel("Q U A R K R E L A Y")
        sub.setObjectName("BrandSub")
        title_box.addWidget(name)
        title_box.addWidget(sub)
        brand.addLayout(title_box)
        brand.addStretch(1)
        layout.addLayout(brand)
        layout.addSpacing(16)

        entries = [
            ("transfer", "夸克中转", TransferPage),
            ("screenshot", "截图识链", ScreenshotPage),
            ("baidu", "百度 → 夸克", BaiduRelayPage),
            ("tasks", "任务中心", TasksPage),
            ("history", "历史记录", HistoryPage),
            ("accounts", "账号管理", AccountsPage),
            ("settings", "设置", SettingsPage),
            ("about", "关于", AboutPage),
        ]
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, _cls in entries:
            button = QPushButton(f"  {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, name=key: self.navigate(name))
            self._group.addButton(button)
            self._nav_buttons.append(button)
            layout.addWidget(button)
            if key == "tasks":
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                layout.removeWidget(button)
                row.addWidget(button, 1)
                self._task_badge = QLabel("0")
                self._task_badge.setObjectName("NavBadge")
                self._task_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                self._task_badge.setVisible(False)
                row.addWidget(self._task_badge)
                layout.addLayout(row)

        layout.addStretch(1)

        self.quark_dot = StatusDot(PALETTES[self.theme_name].danger)
        self.baidu_dot = StatusDot(PALETTES[self.theme_name].danger)
        self.quark_label = QLabel("夸克：未登录")
        self.quark_label.setObjectName("Faint")
        self.baidu_label = QLabel("百度：未登录")
        self.baidu_label.setObjectName("Faint")
        for dot, label in ((self.quark_dot, self.quark_label), (self.baidu_dot, self.baidu_label)):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(dot)
            row.addWidget(label, 1)
            layout.addLayout(row)

        return sidebar

    def _build_stack(self) -> QWidget:
        self.stack = QStackedWidget()
        for key, _label, cls in (
            ("transfer", "", TransferPage),
            ("screenshot", "", ScreenshotPage),
            ("baidu", "", BaiduRelayPage),
            ("tasks", "", TasksPage),
            ("history", "", HistoryPage),
            ("accounts", "", AccountsPage),
            ("settings", "", SettingsPage),
            ("about", "", AboutPage),
        ):
            page = cls(self.services)
            if isinstance(page, ScreenshotPage):
                page.send_to_transfer = self.send_to_transfer
            self._pages[key] = page
            self.stack.addWidget(page)
        return self.stack

    # ---------------------------------------------------------------- 导航
    def navigate(self, key: str, *, capture_region: bool = False) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for index, button in enumerate(self._nav_buttons):
            if self._page_key(index) == key:
                button.setChecked(True)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()
        if capture_region:
            self.hide()
            QTimer.singleShot(
                220,
                lambda: (
                    page._start_region() if hasattr(page, "_start_region") else None
                ),
            )
            QTimer.singleShot(260, self.show)
            self.show()

    def _page_key(self, index: int) -> str:
        return list(self._pages.keys())[index] if index < len(self._pages) else ""

    def send_to_transfer(self, text: str) -> None:
        page = self._pages.get("transfer")
        if page is None:
            return
        current = page.input.toPlainText().strip()
        if text and text not in current:
            page.input.setPlainText((current + "\n" + text).strip())
        self.navigate("transfer")
        Toast.show_message(self, "已把链接送到「夸克中转站」", "success")

    # ---------------------------------------------------------------- 服务
    def _wire_services(self) -> None:
        self.services.toast.connect(self._on_toast)
        self.services.quark_changed.connect(self._refresh_accounts)
        self.services.baidu_changed.connect(self._refresh_accounts)
        self.services.links_detected.connect(self._on_links)
        self.services.status_message.connect(self._on_status_message)
        self.services.tasks.task_added.connect(lambda _t: self._refresh_badge())
        self.services.tasks.task_changed.connect(lambda _t: self._refresh_badge())
        self._refresh_accounts()
        self._refresh_badge()

    def _on_status_message(self, message: str) -> None:
        if "历史记录" in message:
            self.navigate("history")

    def _on_toast(self, text: str, kind: str) -> None:
        if self.services.config.get("app.notify_on_finish", True) or kind == "error":
            Toast.show_message(self, text, kind)
            if self.tray and self.tray.isVisible() and not self.isVisible():
                self.tray.showMessage(__app_name__, text, self.tray.icon(), 4000)

    def _refresh_accounts(self) -> None:
        quark_in = self.services.quark_logged_in
        baidu_in = self.services.baidu_logged_in
        self.quark_dot.set_color(PALETTES[self.theme_name].success if quark_in else PALETTES[self.theme_name].danger)
        self.baidu_dot.set_color(PALETTES[self.theme_name].success if baidu_in else PALETTES[self.theme_name].danger)
        self.quark_label.setText(f"夸克：{self.services.quark_display()}")
        self.baidu_label.setText(f"百度：{self.services.baidu_display()}")
        page = self._pages.get("accounts")
        if page is not None:
            page.refresh()

    def _refresh_badge(self) -> None:
        if self._task_badge is None:
            return
        count = self.services.tasks.active_count()
        self._task_badge.setText(str(count))
        self._task_badge.setVisible(count > 0)

    def _on_links(self, links) -> None:
        quark = [link for link in links if link.provider == "quark"]
        if not quark:
            return
        page = self._pages.get("transfer")
        if page is not None and self.stack.currentWidget() is not page:
            text = "\n".join(
                link.url + (f" 提取码：{link.code}" if link.code else "") for link in quark
            )
            current = page.input.toPlainText().strip()
            page.input.setPlainText((current + "\n" + text).strip())

    # ---------------------------------------------------------------- 托盘
    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = None
            return
        self.tray = QSystemTrayIcon(app_icon(256), self)
        self.tray.setToolTip(f"{__app_name__} v{__version__}")
        menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self._restore)
        menu.addAction(show_action)
        shot_action = QAction("截图识链", self)
        shot_action.triggered.connect(lambda: self.navigate("screenshot", capture_region=True))
        menu.addAction(shot_action)
        open_action = QAction("打开数据目录", self)
        open_action.triggered.connect(lambda: os.startfile(APP_DIR))  # noqa: S606
        menu.addAction(open_action)
        menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit)
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._restore()

    def _restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _quit(self) -> None:
        self._really_quit = True
        self.close()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if getattr(self, "_really_quit", False):
            event.accept()
            return
        if self.services.config.get("app.close_to_tray", True) and self.tray is not None:
            event.ignore()
            self.hide()
            self.tray.showMessage(
                __app_name__, "已最小化到托盘，双击图标可以呼出窗口", self.tray.icon(), 2500
            )
            return
        # 不驻留托盘时，关窗就是退出：主窗口建了托盘图标，而程序又设了
        # setQuitOnLastWindowClosed(False)，所以必须显式退出事件循环，
        # 否则窗口没了、进程却还赖在后台。
        self._really_quit = True
        event.accept()
        from PySide6.QtWidgets import QApplication

        QApplication.quit()

    # ---------------------------------------------------------------- 主题
    def apply_theme(self, name: str) -> None:
        from PySide6.QtWidgets import QApplication

        self.theme_name = name
        palette = apply_theme(QApplication.instance(), name)
        set_palette(palette)
        self.setWindowIcon(app_icon(256))
        self._refresh_accounts()

    # ---------------------------------------------------------------- 更新
    def _check_update(self) -> None:
        import threading

        def _worker() -> None:
            info = updater.check()
            if info.has_update:
                self.services.toast.emit(
                    f"发现新版本 v{info.version}，可在「关于」页查看", "info"
                )

        threading.Thread(target=_worker, name="update-check", daemon=True).start()
