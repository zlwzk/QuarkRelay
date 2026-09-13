"""关于：版本、检查更新、公告与功能总览。"""

from __future__ import annotations

import os
import webbrowser

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTabWidget, QWidget

from ... import __app_name__, __github__, __version__
from ...core import updater
from ...paths import APP_DIR
from ..widgets import Card, Toast
from . import Page

try:  # 由 scripts/build-docs.py 生成
    from ...docs_content import FEATURES, RELEASE_NOTES  # type: ignore
except Exception:  # noqa: BLE001
    FEATURES = "功能总览尚未生成。"
    RELEASE_NOTES = "更新公告尚未生成。"


class _Updater(QObject):
    done = Signal(object)


class AboutPage(Page):
    title = "关于"
    subtitle = "夸克中转站 · 网盘链接中转与搬运工作站"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        body = self.scrollable()

        head = Card(f"{__app_name__} v{__version__}")
        head.add(
            QLabel(
                "把「别人的网盘链接」变成「你自己的分享链接」。支持夸克转存分享、"
                "截图识链、以及百度网盘整链路搬运到夸克。所有登录都在程序内置浏览器里完成。"
            )
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.update_button = QPushButton("检查更新")
        self.update_button.setObjectName("Primary")
        self.update_button.clicked.connect(self._check_update)
        row.addWidget(self.update_button)
        home = QPushButton("打开项目主页")
        home.setObjectName("Ghost")
        home.clicked.connect(lambda: webbrowser.open(__github__))
        row.addWidget(home)
        open_dir = QPushButton("打开数据目录")
        open_dir.setObjectName("Ghost")
        open_dir.clicked.connect(self._open_dir)
        row.addWidget(open_dir)
        row.addStretch(1)
        head.add_layout(row)
        self.update_hint = QLabel("")
        self.update_hint.setObjectName("Muted")
        self.update_hint.setWordWrap(True)
        head.add(self.update_hint)
        body.addWidget(head)

        tabs = QTabWidget()
        notes = QPlainTextEdit(RELEASE_NOTES)
        notes.setReadOnly(True)
        notes.setMinimumHeight(360)
        tabs.addTab(notes, "本版更新公告")
        features = QPlainTextEdit(FEATURES)
        features.setReadOnly(True)
        features.setMinimumHeight(360)
        tabs.addTab(features, "完整功能清单")
        body.addWidget(tabs)

        self._updater = _Updater(self)
        self._updater.done.connect(self._on_update_result)

    def _open_dir(self) -> None:
        try:
            os.startfile(APP_DIR)  # noqa: S606 - Windows 专用
        except OSError as exc:
            Toast.show_message(self, f"打不开目录：{exc}", "error")

    def _check_update(self) -> None:
        import threading

        self.update_button.setEnabled(False)
        self.update_hint.setText("正在检查更新…")

        def _worker() -> None:
            info = updater.check()
            self._updater.done.emit(info)

        threading.Thread(target=_worker, name="update-check", daemon=True).start()

    def _on_update_result(self, info) -> None:
        self.update_button.setEnabled(True)
        self.update_hint.setText(info.message)
        if info.has_update:
            Toast.show_message(self, f"发现新版本 v{info.version}，点「打开项目主页」去下载", "success", 5000)

    def refresh(self) -> None:
        pass
