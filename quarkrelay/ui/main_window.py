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
from ..core.hotkey import GlobalHotkey
from ..paths import APP_DIR, pretty
from .pages.accounts import AccountsPage
from .pages.about import AboutPage
from .pages.baidu import CrossRelayPage
from .pages.history import HistoryPage
from .pages.settings import SettingsPage
from .pages.tasks import TasksPage
from .pages.transfer import TransferPage
from .services import AppServices
from .theme import PALETTES, app_icon, apply_theme
from .widgets import StatusDot, Toast, set_palette

logger = logging.getLogger(__name__)

# (页面 key, 侧栏文案, 页面类)。页面只在第一次切过去时才实例化：
# 七个页面一起装配会拖慢首屏，而且账号页会顺手发网络请求。
PAGE_ENTRIES = (
    ("transfer", "夸克中转", TransferPage),
    ("baidu", "跨盘搬运", CrossRelayPage),
    ("tasks", "任务中心", TasksPage),
    ("history", "历史记录", HistoryPage),
    ("accounts", "账号管理", AccountsPage),
    ("settings", "设置", SettingsPage),
    ("about", "关于", AboutPage),
)


class MainWindow(QMainWindow):
    def __init__(self, services: AppServices, theme: str = "light") -> None:
        super().__init__()
        self.services = services
        self.theme_name = theme
        self.setWindowTitle(f"{__app_name__} v{__version__}")
        self.setWindowIcon(app_icon(256))
        self.resize(1280, 860)
        self.setMinimumSize(1080, 720)

        self._nav_buttons: dict[str, QPushButton] = {}
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
        self._task_status = QLabel("")
        self._task_status.setObjectName("Faint")
        self.statusBar().addPermanentWidget(self._task_status)
        self._version_label = QLabel(f"v{__version__} · {pretty(APP_DIR)}")
        self._version_label.setObjectName("Faint")
        self.statusBar().addPermanentWidget(self._version_label)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1500)
        self._status_timer.timeout.connect(self._refresh_task_status)
        self._status_timer.start()

        self._build_tray()
        self._wire_services()
        self._hotkey = GlobalHotkey(self)
        self._hotkey.activated.connect(self.trigger_screenshot)
        spec = str(self.services.config.get("app.hotkey_screenshot", "Ctrl+Alt+Q") or "")
        if spec:
            if self._hotkey.register(spec):
                logger.info("全局快捷键已注册：%s", spec)
            else:
                logger.info("全局快捷键未能注册：%s", spec)

        self.navigate("transfer")
        quiet = bool(os.environ.get("QUARKRELAY_SELFCHECK"))
        if not quiet and bool(self.services.config.get("app.auto_check_update", True)):
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

        entries = PAGE_ENTRIES
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        for key, label, _cls in entries:
            button = QPushButton(f"  {label}")
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, name=key: self.navigate(name))
            self._group.addButton(button)
            self._nav_buttons[key] = button
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
        # 首屏只装「夸克中转」这一页，其余页面等用户真的点过去再建
        self._ensure_page("transfer")
        return self.stack

    def _ensure_page(self, key: str) -> QWidget | None:
        page = self._pages.get(key)
        if page is not None:
            return page
        entry = next((item for item in PAGE_ENTRIES if item[0] == key), None)
        if entry is None:
            return None
        page = entry[2](self.services)
        if key == "transfer":
            page.cross_relay_requested.connect(self._open_cross_relay)
        self._pages[key] = page
        self.stack.addWidget(page)
        return page

    def preload_all_pages(self) -> None:
        """把全部页面装配出来（只给离屏自检用，正常启动不调用）。"""
        for key, _label, _cls in PAGE_ENTRIES:
            self._ensure_page(key)

    @property
    def page_count(self) -> int:
        return len(PAGE_ENTRIES)

    # ---------------------------------------------------------------- 导航
    def navigate(self, key: str) -> None:
        page = self._ensure_page(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        for name, button in self._nav_buttons.items():
            button.setChecked(name == key)
        refresh = getattr(page, "refresh", None)
        if callable(refresh):
            refresh()

    def trigger_screenshot(self) -> None:
        """全局快捷键 / 托盘里的「截图识链」：直接交给当前页的小相机按钮。

        截图按钮长在需要输入链接的页面上（夸克中转、跨盘搬运），
        所以在哪一页就识别哪种链接；当前页没有按钮时退回夸克中转页。
        """
        page = self.stack.currentWidget()
        button = getattr(page, "shot", None)
        if button is None:
            self.navigate("transfer")
            button = getattr(self._pages.get("transfer"), "shot", None)
        if button is None:
            return
        self._restore()
        QTimer.singleShot(150, button.capture_region)

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
        self._refresh_task_status()

    def _open_cross_relay(self, link) -> None:
        """夸克中转页发现百度链接时，把链接带到跨盘搬运页并切过去。"""
        page = self._ensure_page("baidu")
        if page is None:
            return
        page.accept_link(link)
        self.navigate("baidu")

    def _refresh_task_status(self) -> None:
        """状态栏常驻一条「几个任务在跑、加起来多快」，不用切到任务中心也能看到。"""
        active = [task for task in self.services.tasks.tasks() if task.status.active]
        if not active:
            self._task_status.setText("")
            return
        speed = sum(task.speed_bps for task in active if task.speed_bps > 0)
        text = f"{len(active)} 个任务进行中"
        if speed > 0:
            if speed >= 1024 * 1024:
                text += f" · {speed / (1024 * 1024):.1f} MB/s"
            else:
                text += f" · {speed / 1024:.0f} KB/s"
        self._task_status.setText(text)

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
        # 剪贴板里出现百度链接、而且用户正在跨盘搬运页时，顺手填进去；
        # 不在这一页就不打扰（免得自己切换方向）。
        cross = self._pages.get("baidu")
        if cross is not None and self.stack.currentWidget() is cross:
            want = "baidu"
            if cross.direction == 1:
                want = "quark"
            matched = [link for link in links if link.provider == want]
            if matched and not cross.link_edit.text().strip():
                cross.link_edit.setText(matched[0].url)
                if matched[0].code:
                    cross.code_edit.setText(matched[0].code)
                Toast.show_message(self, f"已从剪贴板填入一条{want}链接", "info")
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
        shot_action.triggered.connect(self.trigger_screenshot)
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
        """启动后静默查一次版本；发现新版本时提示（可选自动下载安装）。"""
        page = self._pages.get("about")
        if page is not None and hasattr(page, "check_for_update"):
            page.check_for_update(silent=True)
