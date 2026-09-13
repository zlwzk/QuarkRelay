"""百度 → 夸克：解析百度分享、转存、内置浏览器取直链、流式下载后上传夸克。"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.baidu import BaiduFile
from ...core.errors import friendly
from ...core.links import extract
from ...core.net import human_size
from ..browser import ManualDLinkDialog
from ..widgets import Badge, Card, EmptyState, Toast, palette
from . import Page

logger = logging.getLogger(__name__)


class _Bridge(QObject):
    parsed = Signal(object, object)  # (files, error)
    message = Signal(str, str)


class FileRow(QWidget):
    def __init__(self, item: BaiduFile, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("CardTight")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)
        self.check = QCheckBox()
        self.check.setChecked(True)
        layout.addWidget(self.check)
        layout.addWidget(Badge("目录" if item.is_dir else "文件", palette().accent if item.is_dir else palette().primary))
        name = QLabel(item.name)
        name.setWordWrap(True)
        layout.addWidget(name, 1)
        size = QLabel("—" if item.is_dir else human_size(item.size))
        size.setObjectName("Muted")
        layout.addWidget(size)

    @property
    def enabled(self) -> bool:
        return self.check.isChecked()


class BaiduRelayPage(Page):
    title = "百度 → 夸克"
    subtitle = "别人分享的百度网盘链接，转存到你的百度账号后，用内置浏览器在后台取直链、流式下载并直接上传到夸克，不落桌面"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self._files: list[FileRow] = []
        self._bridge = _Bridge(self)
        self._bridge.parsed.connect(self._on_parsed)
        self._bridge.message.connect(self._on_message)
        self._manual_dlink = ""

        body = self.scrollable()

        source_card = Card("① 百度分享链接", "粘贴别人分享的百度网盘链接，提取码可以一起粘进来，会自动识别。")
        row = QHBoxLayout()
        row.setSpacing(8)
        self.link_edit = QLineEdit()
        self.link_edit.setPlaceholderText("https://pan.baidu.com/s/1xxxxxxxx  提取码：abcd")
        row.addWidget(self.link_edit, 1)
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("提取码")
        self.code_edit.setFixedWidth(96)
        row.addWidget(self.code_edit)
        self.parse_button = QPushButton("解析")
        self.parse_button.setObjectName("Primary")
        self.parse_button.clicked.connect(self._parse)
        row.addWidget(self.parse_button)
        source_card.add_layout(row)
        paste_row = QHBoxLayout()
        paste_btn = QPushButton("读取剪贴板")
        paste_btn.setObjectName("Ghost")
        paste_btn.clicked.connect(self._from_clipboard)
        paste_row.addWidget(paste_btn)
        paste_row.addStretch(1)
        source_card.add_layout(paste_row)
        body.addWidget(source_card)

        files_card = Card("② 分享内容", "勾选要搬运的文件；目录暂不支持整目录搬运。")
        self.files_box = QVBoxLayout()
        self.files_box.setSpacing(6)
        files_card.add_layout(self.files_box)
        self.files_empty = EmptyState("还没有解析", "先粘贴链接并点「解析」")
        files_card.add(self.files_empty)
        body.addWidget(files_card)

        target_card = Card("③ 搬运设置", step=3)
        row1 = QHBoxLayout()
        row1.setSpacing(8)
        label1 = QLabel("百度中转目录")
        label1.setFixedWidth(88)
        label1.setObjectName("Muted")
        row1.addWidget(label1)
        self.baidu_dir = QLineEdit(str(self.services.config.get("baidu.default_target", "/夸克中转站")))
        row1.addWidget(self.baidu_dir, 1)
        target_card.add_layout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(8)
        label2 = QLabel("夸克目标目录")
        label2.setFixedWidth(88)
        label2.setObjectName("Muted")
        row2.addWidget(label2)
        self.quark_dir = QLineEdit(str(self.services.config.get("quark.default_dir", "夸克中转站")))
        row2.addWidget(self.quark_dir, 1)
        target_card.add_layout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(8)
        label3 = QLabel("分享名字")
        label3.setFixedWidth(88)
        label3.setObjectName("Muted")
        row3.addWidget(label3)
        self.share_name = QLineEdit()
        self.share_name.setPlaceholderText("留空则使用第一个文件名")
        row3.addWidget(self.share_name, 1)
        target_card.add_layout(row3)

        options = QHBoxLayout()
        options.setSpacing(16)
        self.make_share = QCheckBox("搬运完成后生成夸克分享链接")
        self.make_share.setChecked(True)
        options.addWidget(self.make_share)
        self.keep_buffer = QCheckBox("保留下载缓冲文件")
        self.keep_buffer.setChecked(False)
        options.addWidget(self.keep_buffer)
        options.addStretch(1)
        target_card.add_layout(options)
        body.addWidget(target_card)

        run_row = QHBoxLayout()
        run_row.setSpacing(10)
        self.run_button = QPushButton("开始搬运到夸克")
        self.run_button.setObjectName("Primary")
        self.run_button.setMinimumHeight(42)
        self.run_button.clicked.connect(self._start)
        run_row.addWidget(self.run_button, 1)
        body.addLayout(run_row)

        channel_card = Card(
            "内置下载通道说明",
            "百度网盘的下载直链由百度前端用私有签名算法生成。本程序不破解签名，而是让内置浏览器里的页面「自己点下载」，"
            "程序再截获那条真实请求，用同一份会话 Cookie 在后台流式读取，所以既稳定又不需要把文件放到桌面上。",
        )
        channel_row = QHBoxLayout()
        channel_row.setSpacing(8)
        open_channel = QPushButton("打开下载通道窗口")
        open_channel.setObjectName("Ghost")
        open_channel.clicked.connect(lambda: self.services.bridge.prepare_manual())
        channel_row.addWidget(open_channel)
        manual = QPushButton("手动填下载直链")
        manual.setObjectName("Ghost")
        manual.clicked.connect(self._manual)
        channel_row.addWidget(manual)
        channel_row.addStretch(1)
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        channel_row.addWidget(self.status_label)
        channel_card.add_layout(channel_row)
        body.addWidget(channel_card)

        self.services.bridge.status_changed.connect(self.status_label.setText)
        self.services.bridge.window_needed.connect(self._on_window_needed)

    # ---------------------------------------------------------------- 输入
    def _from_clipboard(self) -> None:
        links = self.services.take_clipboard_links()
        baidu = [link for link in links if link.provider == "baidu"]
        if not baidu:
            Toast.show_message(self, "剪贴板里没有识别到百度网盘链接", "warning")
            return
        link = baidu[0]
        self.link_edit.setText(link.url)
        if link.code:
            self.code_edit.setText(link.code)

    def _manual(self) -> None:
        dialog = ManualDLinkDialog(self, self._manual_dlink)
        if dialog.exec() == dialog.DialogCode.Accepted:
            self._manual_dlink = dialog.value()
            if self._manual_dlink:
                Toast.show_message(self, "已记录手动直链，本页下一次搬运会优先使用它", "info")

    def _on_window_needed(self, message: str) -> None:
        Toast.show_message(self, message, "warning", 5000)

    # ---------------------------------------------------------------- 解析
    def _parse(self) -> None:
        url = self.link_edit.text().strip()
        if not url:
            Toast.show_message(self, "请先粘贴百度分享链接", "warning")
            return
        link = extract(url)
        if link:
            url = link[0].url
            if not self.code_edit.text().strip() and link[0].code:
                self.code_edit.setText(link[0].code)
        code = self.code_edit.text().strip()

        if not self.services.ensure_baidu(self):
            return
        self.parse_button.setEnabled(False)
        self.parse_button.setText("解析中…")
        client = self.services.baidu

        def _worker() -> None:
            try:
                share = client.resolve_share(url, code)
                files = client.list_share(share)
                self._bridge.parsed.emit(files, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("解析百度分享失败：%s", exc)
                self._bridge.parsed.emit(None, exc)

        threading.Thread(target=_worker, name="baidu-parse", daemon=True).start()

    def _on_parsed(self, files, error) -> None:
        self.parse_button.setEnabled(True)
        self.parse_button.setText("解析")
        self._clear_files()
        if error is not None:
            self.files_empty.setVisible(True)
            Toast.show_message(self, f"解析失败：{friendly(error)}", "error")
            return
        if not files:
            self.files_empty.setVisible(True)
            Toast.show_message(self, "这个分享里没有文件", "warning")
            return
        self.files_empty.setVisible(False)
        for item in files:
            row = FileRow(item)
            self.files_box.addWidget(row)
            self._files.append(row)
        Toast.show_message(self, f"解析到 {len(files)} 个条目", "success")

    def _clear_files(self) -> None:
        while self.files_box.count():
            entry = self.files_box.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self._files = []

    def _on_message(self, text: str, kind: str) -> None:
        Toast.show_message(self, text, kind)

    # ---------------------------------------------------------------- 执行
    def _start(self) -> None:
        url = self.link_edit.text().strip()
        if not url:
            Toast.show_message(self, "请先粘贴百度分享链接", "warning")
            return
        link = extract(url)
        if link:
            url = link[0].url
        code = self.code_edit.text().strip()
        selected = [row.item for row in self._files if row.enabled]
        if self._files and not selected:
            Toast.show_message(self, "请至少勾选一个文件", "warning")
            return

        baidu_dir = self.baidu_dir.text().strip() or "/夸克中转站"
        quark_dir = self.quark_dir.text().strip() or "夸克中转站"
        self.services.config.update(
            {
                "baidu.default_target": baidu_dir,
                "quark.default_dir": quark_dir,
            }
        )
        task = self.services.start_baidu_relay(
            url,
            code,
            fs_ids=[item.fs_id for item in selected] or None,
            quark_dir=quark_dir,
            baidu_dir=baidu_dir,
            make_share=self.make_share.isChecked(),
            share_name=self.share_name.text().strip(),
            keep_buffer=self.keep_buffer.isChecked(),
            parent=self,
        )
        if task:
            self.status_label.setText("已提交搬运任务，可在「任务中心」查看进度")
            Toast.show_message(self, "搬运任务已开始", "success")

    def refresh(self) -> None:
        self.baidu_dir.setText(str(self.services.config.get("baidu.default_target", "/夸克中转站")))
        self.quark_dir.setText(str(self.services.config.get("quark.default_dir", "夸克中转站")))
