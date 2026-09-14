"""跨盘搬运：百度 ↔ 夸克 双向互转。

两条链路都是「转存到自己的账号 → 后台取直链或流式下载 → 直接上传到对方网盘」，
中间不落桌面，缓冲区随用随删。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.errors import friendly
from ...core.links import ShareLink, extract
from ...core.net import human_size
from ..browser import ManualDLinkDialog
from ..shot import ScreenshotButton, pick_links
from ..widgets import Badge, Card, EmptyState, FieldRow, Toast, palette
from . import Page

logger = logging.getLogger(__name__)

BAIDU_FIRST = 0
QUARK_FIRST = 1
DIRECTIONS = ("百度 → 夸克", "夸克 → 百度")


@dataclass
class ShareItem:
    """两条链路的分享条目（把百度/夸克的字段归一化，好在同一张列表里展示）。"""

    name: str
    size: int = 0
    is_dir: bool = False
    fs_id: int = 0


class _Bridge(QObject):
    parsed = Signal(object, object)  # (list[ShareItem], error)
    message = Signal(str, str)


class FileRow(QWidget):
    """分享里的一行。selectable=False 时只做预览（夸克方向是整包转存）。"""

    def __init__(self, item: ShareItem, *, selectable: bool = True, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.item = item
        self.setObjectName("CardTight")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(8)

        self.check = QCheckBox()
        self.check.setChecked(selectable)
        self.check.setVisible(selectable)
        layout.addWidget(self.check)

        layout.addWidget(
            Badge(
                "目录" if item.is_dir else "文件",
                palette().accent if item.is_dir else palette().primary,
            )
        )
        name = QLabel(item.name)
        name.setWordWrap(True)
        layout.addWidget(name, 1)
        size = QLabel("—" if item.is_dir else human_size(item.size))
        size.setObjectName("Muted")
        layout.addWidget(size)

    @property
    def enabled(self) -> bool:
        return self.check.isChecked()


class CrossRelayPage(Page):
    title = "跨盘搬运"
    subtitle = (
        "百度 ↔ 夸克 双向互转：先在源网盘转存，再在后台取直链／流式下载，"
        "直接上传到对方网盘，全程不落桌面"
    )

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self._files: list[FileRow] = []
        self._bridge = _Bridge(self)
        self._bridge.parsed.connect(self._on_parsed)
        self._bridge.message.connect(self._on_message)
        self._manual_dlink = ""
        self.direction = BAIDU_FIRST

        body = self.scrollable()

        self.shot = ScreenshotButton(self, tip="截图识别分享链接")
        self.header.add_action(self.shot)
        self.shot.links_found.connect(self._on_shot_links)

        # ---------------------------------------------------------- 方向切换
        switch_card = Card(
            "搬运方向",
            "两个方向都会先把内容转到你自己的中转目录，再送到对方网盘；"
            "中途的文件走本地缓冲区，任务结束后立即删除。",
        )
        seg = QHBoxLayout()
        seg.setSpacing(8)
        self.dir_buttons: list[QPushButton] = []
        for index, label in enumerate(DIRECTIONS):
            button = QPushButton(label)
            button.setObjectName("Seg")
            button.setCheckable(True)
            button.setMinimumHeight(36)
            button.setChecked(index == BAIDU_FIRST)
            button.clicked.connect(lambda _=False, value=index: self.set_direction(value))
            seg.addWidget(button)
            self.dir_buttons.append(button)
        seg.addStretch(1)
        switch_card.add_layout(seg)
        body.addWidget(switch_card)

        # ---------------------------------------------------------- ① 分享链接
        self.source_card = Card("① 百度分享链接", "粘贴别人分享的链接，提取码可以一起粘进来，会自动识别。")
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
        self.source_card.add_layout(row)

        paste_row = QHBoxLayout()
        paste_btn = QPushButton("读取剪贴板")
        paste_btn.setObjectName("Ghost")
        paste_btn.clicked.connect(self._from_clipboard)
        paste_row.addWidget(paste_btn)
        paste_row.addStretch(1)
        self.source_card.add_layout(paste_row)
        body.addWidget(self.source_card)

        # ---------------------------------------------------------- ② 分享内容
        self.files_card = Card(
            "② 分享内容",
            "勾选要搬运的文件或目录：目录会整棵搬过来，夸克那边按原层级建好目录。",
        )
        self.files_box = QVBoxLayout()
        self.files_box.setSpacing(6)
        self.files_card.add_layout(self.files_box)
        self.files_empty = EmptyState("还没有解析", "先粘贴链接并点「解析」")
        self.files_card.add(self.files_empty)
        body.addWidget(self.files_card)

        # ---------------------------------------------------------- ③ 搬运设置
        target_card = Card("③ 搬运设置", step=3)
        self.baidu_dir = QLineEdit(str(self.services.config.get("baidu.default_target", "/夸克中转站")))
        self.baidu_dir_row = FieldRow("百度中转目录", self.baidu_dir)
        target_card.add(self.baidu_dir_row)

        self.quark_dir = QLineEdit(str(self.services.config.get("quark.default_dir", "夸克中转站")))
        self.quark_dir_row = FieldRow("夸克目标目录", self.quark_dir)
        target_card.add(self.quark_dir_row)

        self.share_name = QLineEdit()
        self.share_name.setPlaceholderText("留空则用目录名／第一个文件名")
        target_card.add(FieldRow("分享名字", self.share_name))

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

        # ---------------------------------------------------------- 下载通道
        self.channel_card = Card(
            "内置下载通道说明（仅百度方向需要）",
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
        self.channel_card.add_layout(channel_row)
        body.addWidget(self.channel_card)

        self.services.bridge.status_changed.connect(self.status_label.setText)
        self.services.bridge.window_needed.connect(self._on_window_needed)
        self._apply_direction()

    # ---------------------------------------------------------------- 方向
    def set_direction(self, index: int) -> None:
        if index == self.direction:
            return
        self.direction = index
        self._apply_direction()

    def _apply_direction(self) -> None:
        baidu_first = self.direction == BAIDU_FIRST
        for index, button in enumerate(self.dir_buttons):
            button.setChecked(index == self.direction)

        self.source_card.title_label.setText("① 百度分享链接" if baidu_first else "① 夸克分享链接")
        self.link_edit.setPlaceholderText(
            "https://pan.baidu.com/s/1xxxxxxxx  提取码：abcd"
            if baidu_first
            else "https://pan.quark.cn/s/xxxxxxxx  提取码：abcd"
        )
        # 百度方向：百度是「中转站」，夸克是目的地；反过来正好对调
        self.baidu_dir_row.label.setText("百度中转目录" if baidu_first else "百度目标目录")
        self.quark_dir_row.label.setText("夸克目标目录" if baidu_first else "夸克中转目录")
        self.make_share.setText(
            "搬运完成后生成夸克分享链接" if baidu_first else "搬运完成后生成百度分享链接"
        )
        self.run_button.setText("开始搬运到夸克" if baidu_first else "开始搬运到百度")
        self.channel_card.setVisible(baidu_first)
        if self.files_card.subtitle_label is not None:
            self.files_card.subtitle_label.setText(
                "勾选要搬运的文件或目录：目录会整棵搬过来，夸克那边按原层级建好目录。"
                if baidu_first
                else "夸克 → 百度目前只能搬文件：分享里若只有文件夹，先把里面的文件单独分享一下。"
            )

        self.link_edit.clear()
        self.code_edit.clear()
        self.files_card.title_label.setText("② 分享内容" if baidu_first else "② 分享内容（整包转存预览）")
        self.files_empty.setVisible(True)
        self._clear_files()

    def _on_shot_links(self, links) -> None:
        provider = "baidu" if self.direction == BAIDU_FIRST else "quark"
        matched = pick_links(links, provider)
        if not matched:
            other = "夸克" if provider == "baidu" else "百度"
            Toast.show_message(self, f"截图里没有{provider}链接（当前方向只认{provider}，{other}链接请切换方向）", "warning")
            return
        link = matched[0]
        self.link_edit.setText(link.url)
        if link.code:
            self.code_edit.setText(link.code)
        Toast.show_message(self, f"已从截图填入{provider}链接", "success")

    # ---------------------------------------------------------------- 输入
    def _from_clipboard(self) -> None:
        links = self.services.take_clipboard_links()
        provider = "baidu" if self.direction == BAIDU_FIRST else "quark"
        matched = pick_links(links, provider)
        if not matched:
            Toast.show_message(self, f"剪贴板里没有识别到{provider}网盘链接", "warning")
            return
        link = matched[0]
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
    def _reset_parse_button(self) -> None:
        self.parse_button.setEnabled(True)
        self.parse_button.setText("解析")

    def _current_link(self) -> ShareLink | None:
        """把输入框里的东西解析成当前方向需要的分享链接。"""
        raw = self.link_edit.text().strip()
        if not raw:
            Toast.show_message(self, "请先粘贴分享链接", "warning")
            return None
        provider = "baidu" if self.direction == BAIDU_FIRST else "quark"
        matched = pick_links(extract(raw), provider)
        if not matched:
            Toast.show_message(self, f"没能认出{provider}分享链接", "warning")
            return None
        link = matched[0]
        link.code = self.code_edit.text().strip() or link.code
        self.code_edit.setText(link.code)
        return link

    def _parse(self) -> None:
        link = self._current_link()
        if link is None:
            return
        self.parse_button.setEnabled(False)
        self.parse_button.setText("解析中…")
        self._clear_files()
        self.files_empty.setVisible(True)

        if self.direction == BAIDU_FIRST:
            if not self.services.ensure_baidu(self):
                self._reset_parse_button()
                return
            worker = self._parse_baidu(link)
        else:
            if not self.services.ensure_quark(self):
                self._reset_parse_button()
                return
            worker = self._parse_quark(link)
        threading.Thread(target=worker, name="share-parse", daemon=True).start()

    def _parse_baidu(self, link: ShareLink):
        client = self.services.baidu

        def _worker() -> None:
            try:
                share = client.resolve_share(link.url, link.code)
                files = client.list_share(share)
                items = [
                    ShareItem(name=item.name, size=item.size, is_dir=item.is_dir, fs_id=item.fs_id)
                    for item in files
                ]
                self._bridge.parsed.emit(items, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("解析百度分享失败：%s", exc)
                self._bridge.parsed.emit(None, exc)

        return _worker

    def _parse_quark(self, link: ShareLink):
        client = self.services.quark

        def _worker() -> None:
            try:
                detail = client.share_detail(link.pwd_id, link.code)
                payload = detail.get("list") or []
                items = [
                    ShareItem(
                        name=str(entry.get("filename") or entry.get("file_name") or "未命名"),
                        size=int(entry.get("size") or 0),
                        is_dir=bool(entry.get("dir")) or int(entry.get("file_type") or 0) == 1,
                        fs_id=int(entry.get("fid") or 0),
                    )
                    for entry in payload
                    if entry.get("filename") or entry.get("file_name")
                ]
                self._bridge.parsed.emit(items, None)
            except Exception as exc:  # noqa: BLE001
                logger.warning("解析夸克分享失败：%s", exc)
                self._bridge.parsed.emit(None, exc)

        return _worker

    def _on_parsed(self, files, error) -> None:
        self._reset_parse_button()
        self._clear_files()
        if error is not None:
            self.files_empty.setVisible(True)
            Toast.show_message(self, f"解析失败：{friendly(error)}", "error")
            return
        if not files:
            self.files_empty.setVisible(True)
            Toast.show_message(self, "这个分享里没有可搬运的内容", "warning")
            return
        selectable = self.direction == BAIDU_FIRST
        self.files_empty.setVisible(False)
        for item in files:
            row = FileRow(item, selectable=selectable)
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
        link = self._current_link()
        if link is None:
            return

        baidu_dir = self.baidu_dir.text().strip() or "/夸克中转站"
        quark_dir = self.quark_dir.text().strip() or "夸克中转站"
        self.services.config.update(
            {
                "baidu.default_target": baidu_dir,
                "quark.default_dir": quark_dir,
            }
        )
        share_name = self.share_name.text().strip()

        if self.direction == BAIDU_FIRST:
            selected = [row.item for row in self._files if row.enabled]
            if self._files and not selected:
                Toast.show_message(self, "请至少勾选一个文件", "warning")
                return
            task = self.services.start_baidu_relay(
                link.url,
                link.code,
                fs_ids=[item.fs_id for item in selected] or None,
                quark_dir=quark_dir,
                baidu_dir=baidu_dir,
                make_share=self.make_share.isChecked(),
                share_name=share_name,
                keep_buffer=self.keep_buffer.isChecked(),
                parent=self,
            )
            label = "夸克"
        else:
            task = self.services.start_quark_to_baidu(
                link,
                quark_dir=quark_dir,
                baidu_dir=baidu_dir,
                make_share=self.make_share.isChecked(),
                share_name=share_name,
                keep_buffer=self.keep_buffer.isChecked(),
                parent=self,
            )
            label = "百度"

        if task:
            self.status_label.setText("已提交搬运任务，可在「任务中心」查看进度")
            Toast.show_message(self, f"搬运到{label}的任务已开始", "success")

    def refresh(self) -> None:
        self.baidu_dir.setText(str(self.services.config.get("baidu.default_target", "/夸克中转站")))
        self.quark_dir.setText(str(self.services.config.get("quark.default_dir", "夸克中转站")))
