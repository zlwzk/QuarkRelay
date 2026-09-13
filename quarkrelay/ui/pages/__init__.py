"""页面基类与公共小工具。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QScrollArea, QVBoxLayout, QWidget

from ..services import AppServices


class Page(QWidget):
    """所有页面的基类：统一内边距与可滚动内容区。"""

    title = ""
    subtitle = ""
    icon_text = ""

    def __init__(self, services: AppServices, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.services = services
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(0, 0, 0, 0)
        self.outer.setSpacing(0)

    def scrollable(self, spacing: int = 14, margins: tuple[int, int, int, int] = (24, 20, 24, 24)) -> QVBoxLayout:
        """创建「页头 + 可滚动正文」结构，返回正文布局。"""
        from ..widgets import PageHeader

        container = QWidget()
        inner = QVBoxLayout(container)
        inner.setContentsMargins(*margins)
        inner.setSpacing(spacing)

        header = PageHeader(self.title, self.subtitle)
        self.header = header
        inner.addWidget(header)
        self.body = QVBoxLayout()
        self.body.setSpacing(spacing)
        inner.addLayout(self.body)
        inner.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(container)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll = scroll
        self.outer.addWidget(scroll)
        return self.body

    def refresh(self) -> None:
        """页面被切换到前台时调用。"""


def elide(text: str, length: int = 64) -> str:
    text = text or ""
    return text if len(text) <= length else text[: length - 1] + "…"


def section_hint(text: str) -> QWidget:
    from ..widgets import HLine, SectionLabel

    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(SectionLabel(text))
    layout.addWidget(HLine())
    return box
