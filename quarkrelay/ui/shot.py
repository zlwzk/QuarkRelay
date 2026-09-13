"""页内小截图按钮：点一下就框选识别，识别出的链接直接交给所在的页面。

以前截图识链是一个独立页面，用户得先在那边截好、再「送到夸克中转站」，
链路太长。现在把它做成一个 18px 的相机小图标，长在需要输入链接的页面标题栏上：
左键框选识别，右键弹更多取图方式（整屏 / 剪贴板 / 打开图片）。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QFileDialog, QMenu, QPushButton, QWidget

from ..core.links import ShareLink
from ..core.ocr import RecognizeResult, recognize, recognize_text
from .capture import RegionCapture, grab_all, save_temp
from .widgets import Toast, palette

logger = logging.getLogger(__name__)


def camera_icon(color: str | None = None, size: int = 18, scale: int = 3) -> QIcon:
    """手绘一个相机图标：不依赖图标字体，也不怕高分屏缩放。"""
    tint = QColor(color or palette().text_dim)
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(scale, scale)

    unit = size / 18.0
    pen = QPen(tint, max(1.15, 1.35 * unit))
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # 取景器小凸起 + 机身 + 镜头
    painter.drawRoundedRect(QRectF(6.2 * unit, 2.6 * unit, 5.6 * unit, 2.6 * unit), 1.2 * unit, 1.2 * unit)
    painter.drawRoundedRect(QRectF(1.6 * unit, 5.0 * unit, 14.8 * unit, 10.6 * unit), 2.6 * unit, 2.6 * unit)
    painter.drawEllipse(QPointF(9.0 * unit, 10.2 * unit), 3.1 * unit, 3.1 * unit)
    painter.end()
    return QIcon(pixmap)


def pick_links(links: list[ShareLink], provider: str | None = None) -> list[ShareLink]:
    """从识别结果里挑出某个网盘的链接（provider 为 None 表示全都要）。"""
    if provider is None:
        return list(links)
    return [link for link in links if link.provider == provider]


def link_text(link: ShareLink) -> str:
    return link.url + (f" 提取码：{link.code}" if link.code else "")


class _OcrSignals(QObject):
    done = Signal(object)


class _OcrTask(QRunnable):
    def __init__(self, path: Path, use_ocr: bool, use_qr: bool) -> None:
        super().__init__()
        self.path = path
        self.use_ocr = use_ocr
        self.use_qr = use_qr
        self.signals = _OcrSignals()

    def run(self) -> None:  # noqa: D102
        try:
            result = recognize(self.path, use_ocr=self.use_ocr, use_qr=self.use_qr)
        except Exception as exc:  # noqa: BLE001
            logger.exception("截图识别失败")
            result = RecognizeResult(error=str(exc))
        self.signals.done.emit(result)


class ScreenshotButton(QPushButton):
    """小相机按钮：左键框选识别，右键选其它取图方式。"""

    recognized = Signal(object)  # RecognizeResult
    links_found = Signal(list)  # list[ShareLink]

    def __init__(self, parent: QWidget | None = None, *, tip: str = "截图识别链接") -> None:
        super().__init__(parent)
        self.setObjectName("IconBtn")
        self.setIcon(camera_icon())
        self.setIconSize(QSize(18, 18))
        self.setFixedSize(34, 30)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setToolTip(f"{tip}：左键框选识别，右键更多方式")
        self._capture: RegionCapture | None = None
        self._task: _OcrTask | None = None
        self._busy = False
        self.clicked.connect(self.capture_region)

    # ---------------------------------------------------------------- 取图
    def contextMenuEvent(self, event) -> None:  # noqa: N802
        menu = QMenu(self)
        menu.addAction("框选识别", self.capture_region)
        menu.addAction("整屏识别", self.capture_full)
        menu.addAction("读剪贴板", self.capture_clipboard)
        menu.addAction("打开图片…", self.capture_file)
        position = event.globalPosition().toPoint()
        menu.exec(position)

    def capture_region(self) -> None:
        if self._busy:
            return
        window = self.window()
        window.hide()
        self._capture = RegionCapture()
        self._capture.finished.connect(self._on_region_done)
        self._capture.start()

    def _on_region_done(self, pixmap) -> None:
        window = self.window()
        window.show()
        window.raise_()
        window.activateWindow()
        self._capture = None
        if pixmap is None or pixmap.isNull():
            Toast.show_message(self, "已取消框选", "info")
            return
        self.recognize_pixmap(pixmap)

    def capture_full(self) -> None:
        if self._busy:
            return
        pixmap = grab_all()
        if pixmap.isNull():
            Toast.show_message(self, "屏幕截图失败", "error")
            return
        self.recognize_pixmap(pixmap)

    def capture_clipboard(self) -> None:
        if self._busy:
            return
        clipboard = QGuiApplication.clipboard()
        pixmap = clipboard.pixmap()
        if not pixmap.isNull():
            self.recognize_pixmap(pixmap)
            return
        text = clipboard.text() or ""
        if not text.strip():
            Toast.show_message(self, "剪贴板里既没有图片也没有文字", "warning")
            return
        self._handle(recognize_text(text))

    def capture_file(self) -> None:
        if self._busy:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)"
        )
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            Toast.show_message(self, "这张图片打不开", "error")
            return
        self.recognize_pixmap(pixmap)

    # ---------------------------------------------------------------- 识别
    def recognize_pixmap(self, pixmap: QPixmap) -> None:
        try:
            path = save_temp(pixmap, f"shot-{int(time.time() * 1000)}")
        except OSError as exc:
            Toast.show_message(self, f"保存截图失败：{exc}", "error")
            return
        self._run(path)

    def _run(self, path: Path, *, use_ocr: bool = True, use_qr: bool = True) -> None:
        if self._busy:
            return
        self._busy = True
        self.setEnabled(False)
        task = _OcrTask(path, use_ocr, use_qr)
        self._task = task
        task.signals.done.connect(self._handle)
        QThreadPool.globalInstance().start(task)

    def _handle(self, result: RecognizeResult) -> None:
        self._busy = False
        self._task = None
        self.setEnabled(True)
        links = result.all_links
        if links:
            self.links_found.emit(links)
        elif result.text.strip():
            Toast.show_message(self, "识别到文字，但里面没有网盘链接", "warning")
        elif result.error:
            Toast.show_message(self, result.error, "warning")
        else:
            Toast.show_message(self, "没有识别到任何内容", "warning")
        self.recognized.emit(result)
