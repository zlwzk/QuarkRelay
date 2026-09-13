"""账号管理：夸克与百度各自的登录状态、容量信息与内置登录入口。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...paths import APP_DIR, pretty
from ..widgets import Badge, Card, StatusDot, Toast, palette
from . import Page


class AccountCard(QWidget):
    def __init__(self, name: str, description: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.dot = StatusDot(palette().danger)
        top.addWidget(self.dot)
        title = QLabel(name)
        title.setObjectName("CardTitle")
        top.addWidget(title)
        self.badge = Badge("未登录", palette().danger)
        top.addWidget(self.badge)
        top.addStretch(1)
        layout.addLayout(top)

        self.detail = QLabel(description)
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(8)
        layout.addLayout(self.buttons)

    def add_button(self, text: str, handler, primary: bool = False) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("Primary" if primary else "Ghost")
        button.clicked.connect(handler)
        self.buttons.addWidget(button)
        return button

    def set_state(self, logged_in: bool, detail: str) -> None:
        self.dot.set_color(palette().success if logged_in else palette().danger)
        self.badge.setText("已登录" if logged_in else "未登录")
        self.badge.set_color(palette().success if logged_in else palette().danger)
        self.detail.setText(detail)


class AccountsPage(Page):
    title = "账号管理"
    subtitle = "两个网盘的登录都在内置浏览器里完成，Cookie / 令牌只存在本机，不会动你系统浏览器里的登录状态"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        body = self.scrollable()

        quark_card = Card("夸克网盘", "通过夸克网盘开放平台的授权码流程登录，授权成功后本程序只保存访问令牌与刷新令牌。")
        self.quark_widget = AccountCard("夸克网盘", "未登录")
        self.quark_login = self.quark_widget.add_button("登录夸克网盘", self._login_quark, primary=True)
        self.quark_refresh = self.quark_widget.add_button("刷新容量", self._refresh_quark)
        self.quark_logout = self.quark_widget.add_button("退出登录", self._logout_quark)
        quark_card.add(self.quark_widget)
        body.addWidget(quark_card)

        baidu_card = Card(
            "百度网盘",
            "在内置浏览器里登录百度网盘后，程序会读取 BDUSS / STOKEN 这些 HttpOnly Cookie 保存在本机。"
            "这些 Cookie 只在程序内部使用，用于转存、列目录和下载；不会写入你的系统浏览器。",
        )
        self.baidu_widget = AccountCard("百度网盘", "未登录")
        self.baidu_login = self.baidu_widget.add_button("登录百度网盘", self._login_baidu, primary=True)
        self.baidu_channel = self.baidu_widget.add_button("打开下载通道窗口", self._open_channel)
        self.baidu_logout = self.baidu_widget.add_button("退出登录", self._logout_baidu)
        baidu_card.add(self.baidu_widget)
        body.addWidget(baidu_card)

        info_card = Card("本机数据位置", "下面这些文件都只保存在你自己的电脑上，卸载程序时可以一并删除。")
        for text in (
            f"配置与凭据：{pretty(APP_DIR / 'config.json')}",
            f"运行日志：{pretty(APP_DIR / 'logs')}",
            f"浏览器数据：{pretty(APP_DIR / 'webengine')}（内置浏览器的 Cookie 在这里）",
            f"历史记录：{pretty(APP_DIR / 'history.db')}",
        ):
            label = QLabel(text)
            label.setObjectName("Faint")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info_card.add(label)
        body.addWidget(info_card)

        self.services.quark_changed.connect(self.refresh)
        self.services.baidu_changed.connect(self.refresh)
        self.refresh()

    # ------------------------------------------------------------------ 动作
    def _login_quark(self) -> None:
        if self.services.login_quark(self):
            self.refresh()

    def _logout_quark(self) -> None:
        self.services.logout_quark()
        self.refresh()

    def _refresh_quark(self) -> None:
        if not self.services.quark_logged_in:
            Toast.show_message(self, "还没登录夸克网盘", "warning")
            return
        self.refresh()
        Toast.show_message(self, "已刷新夸克账号信息", "info")

    def _login_baidu(self) -> None:
        if self.services.login_baidu(self):
            self.refresh()

    def _logout_baidu(self) -> None:
        self.services.logout_baidu()
        self.refresh()

    def _open_channel(self) -> None:
        if not self.services.ensure_baidu(self):
            return
        self.services.bridge.prepare_manual()

    # ------------------------------------------------------------------ 刷新
    def refresh(self) -> None:
        quark_in = self.services.quark_logged_in
        storage = self.services.quark_storage() if quark_in else ""
        detail = f"账号：{self.services.quark_display()}"
        if storage:
            detail += f"　·　{storage}"
        if not quark_in:
            detail = "未登录。点左侧按钮，在弹出的浏览器窗口里用夸克 App 扫码授权即可。"
        self.quark_widget.set_state(quark_in, detail)
        self.quark_login.setText("重新登录" if quark_in else "登录夸克网盘")
        self.quark_logout.setEnabled(quark_in)
        self.quark_refresh.setEnabled(quark_in)

        baidu_in = self.services.baidu_logged_in
        baidu_detail = f"账号：{self.services.baidu_display()}"
        if not baidu_in:
            baidu_detail = "未登录。点左侧按钮，在弹出的浏览器窗口里扫码登录百度网盘即可。"
        self.baidu_widget.set_state(baidu_in, baidu_detail)
        self.baidu_login.setText("重新登录" if baidu_in else "登录百度网盘")
        self.baidu_logout.setEnabled(baidu_in)
