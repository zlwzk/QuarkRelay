"""截图识别：整屏 / 框选 / 图片文件 / 剪贴板，四条路都指向同一套识别管线。"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.ocr import RecognizeResult, ocr_available, recognize, recognize_text
from ..capture import RegionCapture, grab_all, save_temp
from ..widgets import Badge, Card, EmptyState, Toast, copy_text, palette
from . import Page

logger = logging.getLogger(__name__)


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


class ResultRow(QWidget):
    """一条识别出来的链接。"""

    def __init__(self, text: str, provider_name: str, source: str, on_send, parent=None) -> None:
        super().__init__(parent)
        self.link_text = text
        self.on_send = on_send
        self.setObjectName("CardTight")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(Badge(provider_name, palette().primary))
        top.addWidget(Badge(source, palette().accent))
        top.addStretch(1)
        layout.addLayout(top)

        value = QLabel(text)
        value.setObjectName("LinkText")
        value.setWordWrap(True)
        value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(value)

        actions = QHBoxLayout()
        actions.setSpacing(6)
        copy_btn = QPushButton("复制链接")
        copy_btn.setObjectName("Ghost")
        copy_btn.clicked.connect(self._copy)
        actions.addWidget(copy_btn)
        send_btn = QPushButton("送到夸克中转站")
        send_btn.setObjectName("Primary")
        send_btn.clicked.connect(self._send)
        actions.addWidget(send_btn)
        actions.addStretch(1)
        layout.addLayout(actions)

    def _copy(self) -> None:
        copy_text(self.link_text)
        Toast.show_message(self, "链接已复制", "success")

    def _send(self) -> None:
        if callable(self.on_send):
            self.on_send(self.link_text)


class ScreenshotPage(Page):
    title = "截图识链"
    subtitle = "截图或读剪贴板，自动把图里的网盘链接和二维码抠出来；QR 码、文字两路同时识别"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self.pool = QThreadPool.globalInstance()
        self._capture: RegionCapture | None = None
        self._pixmap: QPixmap | None = None
        self.send_to_transfer = None
        self._rows: list[QWidget] = []

        body = self.scrollable()

        capture_card = Card("① 取图", "「框选识别」会打开全屏遮罩，拖出要识别的区域；「整屏识别」直接拍下当前屏幕。")
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, handler, primary in (
            ("框选识别", self._start_region, True),
            ("整屏识别", self._start_full, False),
            ("打开图片…", self._open_file, False),
            ("读剪贴板", self._from_clipboard, False),
        ):
            button = QPushButton(text)
            button.setObjectName("Primary" if primary else "Ghost")
            button.setMinimumHeight(38)
            button.clicked.connect(handler)
            row.addWidget(button)
        capture_card.add_layout(row)

        options = QHBoxLayout()
        options.setSpacing(14)
        self.use_ocr = QCheckBox("文字 OCR")
        self.use_ocr.setChecked(True)
        self.use_qr = QCheckBox("二维码解码")
        self.use_qr.setChecked(True)
        options.addWidget(self.use_ocr)
        options.addWidget(self.use_qr)
        self.ocr_hint = QLabel("")
        self.ocr_hint.setObjectName("Faint")
        options.addWidget(self.ocr_hint, 1)
        capture_card.add_layout(options)
        body.addWidget(capture_card)

        preview_card = Card("② 预览")
        self.preview = QLabel("还没有取图")
        self.preview.setObjectName("Muted")
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumHeight(180)
        self.preview.setStyleSheet(
            f"background: {palette().surface_alt}; border-radius: 10px;"
            f" border: 1px dashed {palette().border};"
        )
        preview_card.add(self.preview)
        body.addWidget(preview_card)

        result_card = Card("③ 识别结果")
        self.result_box = QVBoxLayout()
        self.result_box.setSpacing(8)
        result_card.add_layout(self.result_box)
        self.result_empty = EmptyState("还没有识别结果", "截图后这里会列出找到的链接")
        result_card.add(self.result_empty)
        body.addWidget(result_card)

        text_card = Card("OCR 原文（可用来核对）")
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setFixedHeight(120)
        self.raw_text.setPlaceholderText("识别到的文字会出现在这里")
        text_card.add(self.raw_text)
        body.addWidget(text_card)

        self._update_ocr_hint()

    # ---------------------------------------------------------------- 取图
    def _update_ocr_hint(self) -> None:
        if ocr_available():
            self.ocr_hint.setText("已检测到系统 OCR 引擎，可离线识别中文与英文")
        else:
            self.ocr_hint.setText("系统未提供 OCR 引擎（仅二维码可用），可在 Windows 设置里安装「中文(简体)」语言包")

    def _start_full(self) -> None:
        pixmap = grab_all()
        if pixmap.isNull():
            Toast.show_message(self, "屏幕截图失败", "error")
            return
        self._set_pixmap(pixmap)

    def _start_region(self) -> None:
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
        self._set_pixmap(pixmap)

    def _open_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片文件 (*.png *.jpg *.jpeg *.bmp *.webp *.gif)"
        )
        if not path:
            return
        pixmap = QPixmap(path)
        if pixmap.isNull():
            Toast.show_message(self, "这张图片打不开", "error")
            return
        self._set_pixmap(pixmap, reuse_path=Path(path))

    def _from_clipboard(self) -> None:
        from PySide6.QtGui import QGuiApplication

        clipboard = QGuiApplication.clipboard()
        pixmap = clipboard.pixmap()
        if not pixmap.isNull():
            self._set_pixmap(pixmap)
            return
        text = clipboard.text() or ""
        if not text.strip():
            Toast.show_message(self, "剪贴板里既没有图片也没有文字", "warning")
            return
        self._render(recognize_text(text))

    # ---------------------------------------------------------------- 识别
    def _set_pixmap(self, pixmap: QPixmap, reuse_path: Path | None = None) -> None:
        self._pixmap = pixmap
        scaled = pixmap.scaled(
            720,
            260,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.preview.setPixmap(scaled)
        self.preview.setText("")
        self.raw_text.setPlainText("识别中…")
        self._clear_results()

        path = reuse_path or save_temp(pixmap, "shot")
        if reuse_path is None:
            pixmap.save(str(path), "PNG")
        task = _OcrTask(path, self.use_ocr.isChecked(), self.use_qr.isChecked())
        self._ocr_task = task
        task.signals.done.connect(self._render)
        self.pool.start(task)

    def _clear_results(self) -> None:
        while self.result_box.count():
            item = self.result_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        self.result_empty.setVisible(True)

    def _render(self, result: RecognizeResult) -> None:
        self._clear_results()
        links = result.all_links
        for link in links:
            row = ResultRow(
                text=link.url + (f"  提取码：{link.code}" if link.code else ""),
                provider_name=link.provider_name,
                source="二维码" if link in result.qr_links else (result.engine or "OCR"),
                on_send=self.send_to_transfer,
            )
            self.result_box.addWidget(row)
            self._rows.append(row)
        self.result_empty.setVisible(not links)
        if not links and result.text:
            self.result_empty.setVisible(False)
            hint = EmptyState("识别到文字，但里面没有网盘链接", "可以复制原文后手动处理")
            self.result_box.addWidget(hint)
            self._rows.append(hint)
        self.raw_text.setPlainText(result.text or (result.error or "没有识别到任何文字或二维码"))
        if links:
            Toast.show_message(self, f"识别到 {len(links)} 条链接", "success")
        elif result.error:
            Toast.show_message(self, result.error, "warning")

    def refresh(self) -> None:
        self._update_ocr_hint()
