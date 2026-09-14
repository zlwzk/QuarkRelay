"""可复用的界面组件。"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core.tasks import Task, TaskStatus
from .theme import PALETTES, Palette

_current_palette: Palette = PALETTES["dark"]


def set_palette(palette: Palette) -> None:
    global _current_palette
    _current_palette = palette


def palette() -> Palette:
    return _current_palette


def copy_text(text: str) -> None:
    QGuiApplication.clipboard().setText(text or "")


# --------------------------------------------------------------------- 基础
class HLine(QFrame):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)


class Card(QFrame):
    """带标题的卡片容器。"""

    def __init__(
        self,
        title: str = "",
        subtitle: str = "",
        *,
        step: int | None = None,
        tight: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("CardTight" if tight else "Card")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 16)
        outer.setSpacing(10)

        if title:
            header = QHBoxLayout()
            header.setSpacing(8)
            if step is not None:
                badge = QLabel(str(step))
                badge.setObjectName("StepBadge")
                badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
                badge.setFixedSize(20, 20)
                header.addWidget(badge)
            self.title_label = QLabel(title)
            self.title_label.setObjectName("CardTitle")
            header.addWidget(self.title_label)
            header.addStretch(1)
            self.header_layout = header
            outer.addLayout(header)
            self.subtitle_label: QLabel | None = None
            if subtitle:
                hint = QLabel(subtitle)
                hint.setObjectName("Muted")
                hint.setWordWrap(True)
                outer.addWidget(hint)
                self.subtitle_label = hint
        else:
            self.header_layout = QHBoxLayout()
            self.title_label = QLabel()
            self.subtitle_label = None
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body)

    def add(self, widget: QWidget) -> QWidget:
        self.body.addWidget(widget)
        return widget

    def add_layout(self, layout) -> None:
        self.body.addLayout(layout)

    def add_header_widget(self, widget: QWidget) -> QWidget:
        self.header_layout.addWidget(widget)
        return widget


class PageHeader(QWidget):
    """页面标题区。"""

    def __init__(self, title: str, subtitle: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        row.setSpacing(10)
        self.title = QLabel(title)
        self.title.setObjectName("PageTitle")
        row.addWidget(self.title)
        row.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        row.addLayout(self.actions)
        layout.addLayout(row)
        self.subtitle = QLabel(subtitle)
        self.subtitle.setObjectName("PageSubtitle")
        self.subtitle.setWordWrap(True)
        self.subtitle.setVisible(bool(subtitle))
        layout.addWidget(self.subtitle)

    def add_action(self, widget: QWidget) -> QWidget:
        self.actions.addWidget(widget)
        return widget


class SectionLabel(QLabel):
    def __init__(self, text: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SectionTitle")


class StatusDot(QWidget):
    """账号状态小圆点。"""

    def __init__(self, color: str = "#34D399", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Dot")
        self.setFixedSize(10, 10)
        self.set_color(color)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(f"#Dot {{ background: {color}; border-radius: 5px; }}")


class Badge(QLabel):
    def __init__(self, text: str, color: str | None = None, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_color(color or palette().primary)

    def set_color(self, color: str) -> None:
        self.setStyleSheet(
            f"background: {color}22; color: {color}; border: 1px solid {color}55;"
            " border-radius: 8px; padding: 1px 8px; font-size: 11px; font-weight: 600;"
        )


class StatTile(QFrame):
    def __init__(self, label: str, value: str = "-", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("CardTight")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(2)
        self.value = QLabel(value)
        self.value.setObjectName("StatValue")
        self.label = QLabel(label)
        self.label.setObjectName("StatLabel")
        layout.addWidget(self.value)
        layout.addWidget(self.label)

    def set_value(self, value: str) -> None:
        self.value.setText(value)


class PathPicker(QWidget):
    """输入框 + 浏览按钮。"""

    def __init__(
        self,
        placeholder: str = "",
        text: str = "",
        *,
        button_text: str = "浏览",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        layout.addWidget(self.edit, 1)
        self.button = QPushButton(button_text)
        self.button.setObjectName("Ghost")
        layout.addWidget(self.button)

    def text(self) -> str:
        return self.edit.text()

    def set_text(self, value: str) -> None:
        self.edit.setText(value)


class CopyRow(QWidget):
    """一条「名字 + 链接」的结果行。"""

    copy_requested = Signal(str)

    def __init__(
        self,
        name: str,
        link: str,
        code: str = "",
        combined: str = "",
        note: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.name = name
        self.link = link
        self.code = code
        self.combined = combined or f"{name} {link}".strip()
        self.setObjectName("CardTight")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(8)
        name_label = QLabel(name or "（未命名）")
        name_label.setObjectName("CardTitle")
        name_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        top.addWidget(name_label, 1)
        if code:
            top.addWidget(Badge(f"提取码 {code}", palette().accent))
        layout.addLayout(top)

        link_label = QLabel(link or "（没有链接）")
        link_label.setObjectName("LinkText")
        link_label.setWordWrap(True)
        link_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(link_label)

        if note:
            note_label = QLabel(note)
            note_label.setObjectName("Faint")
            note_label.setWordWrap(True)
            layout.addWidget(note_label)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        for text, payload in (
            ("复制名字", self.name),
            ("复制链接", self.link),
            ("复制全部", self.combined),
        ):
            button = QPushButton(text)
            button.setObjectName("Ghost")
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _=False, value=payload, t=text: self._copy(value, t))
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

    def _copy(self, value: str, label: str) -> None:
        copy_text(value)
        self.copy_requested.emit(f"{label} 成功")
        Toast.show_message(self.window(), f"{label} 成功", "success")


class EmptyState(QWidget):
    def __init__(self, text: str, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 30, 20, 30)
        layout.setSpacing(6)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title = QLabel(text)
        title.setObjectName("Muted")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        if hint:
            sub = QLabel(hint)
            sub.setObjectName("Faint")
            sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
            sub.setWordWrap(True)
            layout.addWidget(sub)


class LogPane(QPlainTextEdit):
    """日志面板，订阅内存日志器。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("LogView")
        self.setReadOnly(True)
        self.setMaximumBlockCount(3000)
        self.setPlaceholderText("运行日志会显示在这里…")

    def append_line(self, text: str) -> None:
        self.appendPlainText(text)


class TaskRow(QFrame):
    """任务中心里的一行。"""

    cancel_requested = Signal(str)
    open_requested = Signal(str)

    def __init__(self, task: Task, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.task_id = task.id
        self.setObjectName("CardTight")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        top = QHBoxLayout()
        top.setSpacing(8)
        self.title = QLabel(task.title or task.kind)
        self.title.setObjectName("CardTitle")
        top.addWidget(self.title, 1)
        self.percent = QLabel("")
        self.percent.setObjectName("Percent")
        top.addWidget(self.percent)
        self.badge = Badge(task.status.label)
        top.addWidget(self.badge)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.setObjectName("Ghost")
        self.cancel_button.clicked.connect(lambda: self.cancel_requested.emit(self.task_id))
        top.addWidget(self.cancel_button)
        self.open_button = QPushButton("查看结果")
        self.open_button.setObjectName("Ghost")
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.task_id))
        top.addWidget(self.open_button)
        layout.addLayout(top)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)

        self.detail = QLabel("")
        self.detail.setObjectName("Muted")
        self.detail.setWordWrap(True)
        layout.addWidget(self.detail)

        self.update_from(task)

    def update_from(self, task: Task) -> None:
        running = task.status == TaskStatus.RUNNING
        succeeded = task.status == TaskStatus.SUCCESS
        value = 100 if succeeded else max(0, min(100, int(task.progress)))
        self.progress.setValue(value * 10)
        # 搬运和上传都是长活，光看进度条判断不了「还剩多少」，所以把百分比写出来
        show_percent = succeeded or running or task.progress > 0
        self.percent.setText(f"{value}%" if show_percent else "")
        self.percent.setVisible(show_percent)
        text = task.error or task.stage or task.status.label
        self.detail.setText(text)
        color = {
            TaskStatus.SUCCESS: palette().success,
            TaskStatus.FAILED: palette().danger,
            TaskStatus.CANCELLED: palette().text_faint,
        }.get(task.status, palette().primary)
        self.badge.setText(task.status.label)
        self.badge.set_color(color)
        self.cancel_button.setVisible(task.status.active)
        self.open_button.setVisible(succeeded)
        self.progress.setVisible(running or task.progress > 0)


class Toast(QFrame):
    """右下角浮层提示。"""

    _active: list["Toast"] = []

    def __init__(self, parent: QWidget, text: str, kind: str = "info") -> None:
        super().__init__(parent)
        self.setObjectName("ToastFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(10)
        color = {
            "success": palette().success,
            "error": palette().danger,
            "warning": palette().warning,
        }.get(kind, palette().primary)
        dot = StatusDot(color)
        layout.addWidget(dot)
        label = QLabel(text)
        label.setWordWrap(True)
        label.setMaximumWidth(360)
        layout.addWidget(label)
        self.adjustSize()

        self._effect = QGraphicsOpacityEffect(self)
        self.setGraphicsEffect(self._effect)
        self._effect.setOpacity(0.0)
        self._anim = QPropertyAnimation(self._effect, b"opacity", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.Type.OutCubic)

    @classmethod
    def show_message(cls, parent: QWidget | None, text: str, kind: str = "info", msec: int = 2600) -> None:
        window = parent.window() if parent else None
        if window is None:
            return
        if len(cls._active) >= 4:
            cls._active.pop(0).deleteLater()
        toast = cls(window, text, kind)
        toast.reposition()
        toast.show()
        toast.raise_()
        toast._effect.setOpacity(1.0)
        cls._active.append(toast)
        QTimer.singleShot(msec, toast.dismiss)

    def reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        index = Toast._active.index(self) if self in Toast._active else 0
        self.adjustSize()
        x = parent.width() - self.width() - 24
        y = parent.height() - self.height() - 30 - index * (self.height() + 10)
        self.move(max(12, x), max(12, y))

    def dismiss(self) -> None:
        if self in Toast._active:
            Toast._active.remove(self)
        self._anim.stop()
        self._anim.setStartValue(self._effect.opacity())
        self._anim.setEndValue(0.0)
        self._anim.finished.connect(self.deleteLater)
        self._anim.start()


class FieldRow(QWidget):
    """标签 + 控件 的横向表单行。"""

    def __init__(self, label: str, widget: QWidget, hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        self.label = QLabel(label)
        self.label.setFixedWidth(96)
        self.label.setObjectName("Muted")
        layout.addWidget(self.label)
        layout.addWidget(widget, 1)
        if hint:
            hint_label = QLabel(hint)
            hint_label.setObjectName("Faint")
            layout.addWidget(hint_label)


def combo(items: list[str], current: str = "") -> QComboBox:
    box = QComboBox()
    box.addItems(items)
    if current and current in items:
        box.setCurrentText(current)
    return box


def checkbox(text: str, checked: bool = False) -> QCheckBox:
    box = QCheckBox(text)
    box.setChecked(checked)
    return box


def hbox(*widgets: QWidget, spacing: int = 8, stretch_at_end: bool = True) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setSpacing(spacing)
    for widget in widgets:
        if widget is None:
            layout.addStretch(1)
        else:
            layout.addWidget(widget)
    if stretch_at_end and all(w is not None for w in widgets):
        pass
    return layout


def bind_copy(button: QPushButton, text_provider: Callable[[], str], message: str = "已复制") -> None:
    def _click() -> None:
        copy_text(text_provider())
        Toast.show_message(button.window(), message, "success")

    button.clicked.connect(_click)


def flash(widget: QWidget, text: str, kind: str = "success") -> None:
    Toast.show_message(widget, text, kind)


def screen_geometry():
    return QApplication.primaryScreen().availableGeometry()
