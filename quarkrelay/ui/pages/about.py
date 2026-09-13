"""关于：版本、检查更新与一键自动更新、公告与功能总览。"""

from __future__ import annotations

import logging
import os
import threading
import webbrowser
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QTabWidget,
)

from ... import __app_name__, __github__, __version__
from ...core import updater
from ...core.errors import friendly
from ...core.net import human_size
from ...paths import APP_DIR
from ..widgets import Card, Toast
from . import Page

logger = logging.getLogger(__name__)

try:  # 由 scripts/build-docs.py 生成
    from ...docs_content import FEATURES, RELEASE_NOTES  # type: ignore
except Exception:  # noqa: BLE001
    FEATURES = "功能总览尚未生成。"
    RELEASE_NOTES = "更新公告尚未生成。"


class _Updater(QObject):
    """把更新线程的结果搬回主线程。"""

    checked = Signal(object)
    progress = Signal(int, int)
    downloaded = Signal(object)  # Path 或 Exception


class AboutPage(Page):
    title = "关于"
    subtitle = "夸克中转站 · 网盘链接中转与搬运工作站"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self._info: updater.UpdateInfo | None = None
        self._cancel = threading.Event()
        self._silent = False
        self._checking = False
        self._manual_only = False
        self._notes_index = -1

        body = self.scrollable()

        head = Card(f"{__app_name__} v{__version__}")
        head.add(
            QLabel(
                "把「别人的网盘链接」变成「你自己的分享链接」。支持夸克转存分享、"
                "百度 ↔ 夸克双向整链路搬运，以及随手截图识别链接。"
                "所有登录都在程序内置浏览器里完成。"
            )
        )
        row = QHBoxLayout()
        row.setSpacing(8)
        self.update_button = QPushButton("检查更新")
        self.update_button.setObjectName("Primary")
        self.update_button.clicked.connect(lambda: self.check_for_update())
        row.addWidget(self.update_button)
        self.install_button = QPushButton("立即更新")
        self.install_button.setObjectName("Primary")
        self.install_button.clicked.connect(self._install)
        self.install_button.setVisible(False)
        row.addWidget(self.install_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Ghost")
        self.cancel_button.clicked.connect(lambda: self._cancel.set())
        self.cancel_button.setVisible(False)
        row.addWidget(self.cancel_button)
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

        self.update_hint = QLabel(
            "「检查更新」会读取 GitHub 上的最新版本；发现新版本时可以在这里一键下载并自动替换重启。"
        )
        self.update_hint.setObjectName("Muted")
        self.update_hint.setWordWrap(True)
        head.add(self.update_hint)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setVisible(False)
        head.add(self.progress)
        body.addWidget(head)

        self.tabs = QTabWidget()
        notes = QPlainTextEdit(RELEASE_NOTES)
        notes.setReadOnly(True)
        notes.setMinimumHeight(360)
        self.tabs.addTab(notes, "本版更新公告")
        features = QPlainTextEdit(FEATURES)
        features.setReadOnly(True)
        features.setMinimumHeight(360)
        self.tabs.addTab(features, "完整功能清单")
        self.remote_notes = QPlainTextEdit()
        self.remote_notes.setReadOnly(True)
        self.remote_notes.setMinimumHeight(360)
        body.addWidget(self.tabs)

        self._updater = _Updater(self)
        self._updater.checked.connect(self._on_check_result)
        self._updater.progress.connect(self._on_progress)
        self._updater.downloaded.connect(self._on_downloaded)

    # ---------------------------------------------------------------- 检查更新
    def check_for_update(self, silent: bool = False) -> None:
        """连 GitHub Release 查版本。silent=True 时（启动静默检查）不弹「已是最新」。"""
        if self._checking:
            return
        self._checking = True
        self._silent = silent
        self.update_button.setEnabled(False)
        self.update_button.setText("检查中…")
        if not silent:
            self.update_hint.setText("正在连接 GitHub Release 检查版本…")

        def _worker() -> None:
            info = updater.check()
            self._updater.checked.emit(info)

        threading.Thread(target=_worker, name="update-check", daemon=True).start()

    def _on_check_result(self, info: updater.UpdateInfo) -> None:
        self._checking = False
        self.update_button.setEnabled(True)
        self.update_button.setText("检查更新")
        self._info = info
        self.update_hint.setText(info.message)

        if info.error:
            self.install_button.setVisible(False)
            if not self._silent:
                Toast.show_message(self, info.message, "warning", 4000)
            return
        if not info.has_update:
            self.install_button.setVisible(False)
            if not self._silent:
                Toast.show_message(self, f"已经是最新版 v{__version__}", "success")
            return

        # 打包版且程序目录可写 → 可以就地替换自己；否则退化成「去下载页」
        self._manual_only = not updater.can_self_update()
        if self._manual_only:
            self.install_button.setText("去下载新版")
        else:
            self.install_button.setText(f"立即更新到 v{info.version}")
        self.install_button.setVisible(True)
        self.install_button.setEnabled(True)
        self._show_remote_notes(info)
        Toast.show_message(
            self,
            f"发现新版本 v{info.version}（{info.size_text}）",
            "success",
            5000,
        )
        # 启动时的静默检查如果开了「自动更新」，就直接下载安装（有任务在跑时不动）
        if self._silent and self._auto_install_ready():
            logger.info("自动更新已开启，开始静默下载 v%s", info.version)
            self._install()

    def _auto_install_ready(self) -> bool:
        """能不能在后台悄悄把更新装上：开关打开、能自替换、且当前没有任务在跑。"""
        if not bool(self.services.config.get("app.auto_install_update", True)):
            return False
        if self._manual_only:
            return False
        active = 0
        try:
            active = int(self.services.tasks.active_count())
        except Exception:  # noqa: BLE001 - 拿不到就当作有任务，宁可不动
            active = 1
        if active:
            logger.info("还有 %s 个任务在跑，暂不自动安装更新", active)
            return False
        return True

    def _show_remote_notes(self, info: updater.UpdateInfo) -> None:
        if not info.notes.strip():
            return
        self.remote_notes.setPlainText(info.notes)
        title = f"新版本 v{info.version} 公告"
        if self._notes_index < 0:
            self._notes_index = self.tabs.addTab(self.remote_notes, title)
        else:
            self.tabs.setTabText(self._notes_index, title)
        self.tabs.setCurrentIndex(self._notes_index)

    # ---------------------------------------------------------------- 更新安装
    def _install(self) -> None:
        info = self._info
        if info is None:
            return
        if self._manual_only:
            webbrowser.open(info.url or updater.latest_download_url())
            Toast.show_message(self, "已打开下载页，下载后直接覆盖旧文件即可", "info", 4000)
            return

        self._cancel.clear()
        self.install_button.setEnabled(False)
        self.install_button.setText("下载中…")
        self.cancel_button.setVisible(True)
        self.progress.setVisible(True)
        self.progress.setValue(0)
        self.update_hint.setText(f"正在下载 v{info.version}（{info.size_text}）…")

        def _worker() -> None:
            try:
                path = updater.download(
                    info,
                    progress=lambda done, total: self._updater.progress.emit(done, total),
                    should_cancel=self._cancel.is_set,
                )
            except Exception as exc:  # noqa: BLE001
                self._updater.downloaded.emit(exc)
                return
            self._updater.downloaded.emit(path)

        threading.Thread(target=_worker, name="update-download", daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)
            self.update_hint.setText(
                f"正在下载更新包… {human_size(done)} / {human_size(total)}"
                f"（{(done / total * 100):.0f}%）"
            )
        else:
            self.update_hint.setText(f"正在下载更新包… {human_size(done)}")

    def _on_downloaded(self, result: object) -> None:
        self.cancel_button.setVisible(False)
        if isinstance(result, Exception):
            self.progress.setVisible(False)
            cancelled = "取消" in str(result)
            self.install_button.setEnabled(True)
            self.install_button.setText("重试更新")
            text = friendly(result)
            self.update_hint.setText(text)
            Toast.show_message(self, text, "info" if cancelled else "error", 4000)
            return

        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        try:
            updater.apply_update(Path(str(result)))
        except Exception as exc:  # noqa: BLE001
            text = friendly(exc)
            self.update_hint.setText(text)
            Toast.show_message(self, text, "error", 5000)
            return

        self.update_hint.setText("更新已就绪，程序会立刻重启完成替换…")
        Toast.show_message(self, "更新已就绪，正在重启完成安装", "success", 3000)
        QTimer.singleShot(1500, self._restart)

    def _restart(self) -> None:
        window = self.window()
        quit_fn = getattr(window, "_quit", None)
        if callable(quit_fn):
            quit_fn()

    # ---------------------------------------------------------------- 其它
    def _open_dir(self) -> None:
        try:
            os.startfile(APP_DIR)  # noqa: S606 - Windows 专用
        except OSError as exc:
            Toast.show_message(self, f"打不开目录：{exc}", "error")

    def refresh(self) -> None:
        """页面被切到前台时：如果已经查过，把按钮状态恢复出来。"""
        if self._info is not None and self._info.has_update:
            self.install_button.setVisible(True)
