"""截图：整屏抓取 + 跨屏区域框选（每个屏幕一个覆盖层，支持多显示器与高 DPI）。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QWidget

from ..paths import temp_buffer_dir
from .theme import PALETTES

logger = logging.getLogger(__name__)


def screen_under_cursor():
    pos = QGuiApplication.primaryScreen().virtualGeometry().topLeft()
    try:
        from PySide6.QtGui import QCursor

        pos = QCursor.pos()
    except Exception:  # noqa: BLE001
        pass
    return QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()


def grab_screen(screen=None) -> QPixmap:
    """抓取整块屏幕（物理像素）。"""
    target = screen or screen_under_cursor()
    if target is None:
        return QPixmap()
    return target.grabWindow(0)


def grab_all() -> QPixmap:
    """把所有屏幕拼成一张图（用于整屏 OCR）。"""
    screens = QGuiApplication.screens()
    if not screens:
        return QPixmap()
    if len(screens) == 1:
        return grab_screen(screens[0])

    union = QRect()
    for screen in screens:
        union = union.united(screen.geometry())
    ratio = max((screen.devicePixelRatio() for screen in screens), default=1.0)
    canvas = QPixmap(int(union.width() * ratio), int(union.height() * ratio))
    canvas.fill(QColor(0, 0, 0))
    painter = QPainter(canvas)
    for screen in screens:
        pixmap = grab_screen(screen)
        if pixmap.isNull():
            continue
        offset = screen.geometry().topLeft() - union.topLeft()
        painter.drawPixmap(
            int(offset.x() * ratio), int(offset.y() * ratio),
            int(screen.geometry().width() * ratio), int(screen.geometry().height() * ratio),
            pixmap,
        )
    painter.end()
    return canvas


def save_temp(pixmap: QPixmap, name: str = "shot") -> Path:
    buffer = temp_buffer_dir()
    path = buffer / f"{name}.png"
    pixmap.save(str(path), "PNG")
    return path


class _Overlay(QWidget):
    """单个屏幕上的框选覆盖层。"""

    selected = Signal(object)
    cancelled = Signal()

    def __init__(self, screen) -> None:
        super().__init__(None)
        self.screen = screen
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setGeometry(screen.geometry())
        self.pixmap: QPixmap = screen.grabWindow(0)
        self._origin: QPoint | None = None
        self._current: QPoint | None = None
        palette = PALETTES.get("dark")
        self._accent = QColor(palette.accent if palette else "#22D3EE")

    # ------------------------------------------------------------- 绘制
    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.drawPixmap(self.rect(), self.pixmap)
        painter.fillRect(self.rect(), QColor(6, 10, 20, 150))

        rect = self.selection()
        if rect.isValid() and rect.width() > 1 and rect.height() > 1:
            painter.drawPixmap(rect, self.pixmap, self._to_physical(rect))
            pen = QPen(self._accent, 2)
            painter.setPen(pen)
            painter.drawRect(rect.adjusted(0, 0, -1, -1))
            label = f"{rect.width()} × {rect.height()}"
            metrics = painter.fontMetrics()
            box = metrics.boundingRect(label).adjusted(-8, -4, 8, 4)
            box.moveTopLeft(QPoint(rect.left(), max(0, rect.top() - box.height() - 6)))
            painter.fillRect(box, QColor(10, 16, 28, 220))
            painter.setPen(QColor("#E9EFFA"))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QColor(233, 239, 250))
        hint = "拖动选择识别区域 · Enter 确认 · Esc 取消"
        painter.drawText(
            self.rect().adjusted(0, 16, 0, 0),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            hint,
        )
        painter.end()

    def _to_physical(self, rect: QRect) -> QRect:
        if self.width() <= 0:
            return rect
        ratio = self.pixmap.width() / self.width()
        return QRect(
            int(rect.x() * ratio),
            int(rect.y() * ratio),
            int(rect.width() * ratio),
            int(rect.height() * ratio),
        )

    def selection(self) -> QRect:
        if self._origin is None or self._current is None:
            return QRect()
        return QRect(self._origin, self._current).normalized()

    # ------------------------------------------------------------- 交互
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._current = self._origin
            self.update()
        elif event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._origin is not None:
            self._current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        rect = self.selection()
        if rect.width() < 6 or rect.height() < 6:
            self.cancelled.emit()
            return
        cropped = self.pixmap.copy(self._to_physical(rect))
        self.selected.emit(cropped)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            rect = self.selection()
            if rect.width() > 6 and rect.height() > 6:
                self.selected.emit(self.pixmap.copy(self._to_physical(rect)))
        else:
            super().keyPressEvent(event)


class RegionCapture(QWidget):
    """跨屏框选控制器：在所有屏幕上铺覆盖层，谁先完成就用谁的结果。"""

    finished = Signal(object)  # QPixmap or None

    def __init__(self) -> None:
        super().__init__(None)
        self._overlays: list[_Overlay] = []
        self._done = False

    def start(self) -> None:
        screens = QGuiApplication.screens()
        if not screens:
            self.finished.emit(None)
            return
        for screen in screens:
            overlay = _Overlay(screen)
            overlay.selected.connect(self._on_selected)
            overlay.cancelled.connect(self._on_cancelled)
            overlay.show()
            overlay.raise_()
            overlay.activateWindow()
            self._overlays.append(overlay)
        if self._overlays:
            self._overlays[0].setFocus()

    def _close_all(self) -> None:
        for overlay in self._overlays:
            overlay.close()
            overlay.deleteLater()
        self._overlays.clear()

    def _on_selected(self, pixmap: QPixmap) -> None:
        if self._done:
            return
        self._done = True
        self._close_all()
        self.finished.emit(pixmap)

    def _on_cancelled(self) -> None:
        if self._done:
            return
        self._done = True
        self._close_all()
        self.finished.emit(None)


def flash_screen() -> None:
    """截图前让窗口短暂隐藏，避免把本程序也拍进去。"""
    QApplication.processEvents()
