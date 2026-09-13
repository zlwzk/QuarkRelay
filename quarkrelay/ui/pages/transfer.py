"""夸克中转站：粘贴别人的夸克链接 → 转存到我的目录 → 生成带名字的分享链接。"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.links import ShareLink, extract
from ...core.naming import render_many
from ...core.quark import EXPIRED_TYPES
from ..widgets import Badge, Card, CopyRow, EmptyState, Toast, copy_text, palette
from . import Page

logger = logging.getLogger(__name__)


class LinkRow(QWidget):
    """一条待处理的分享链接。"""

    changed = Signal()

    def __init__(self, link: ShareLink, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.link = link
        self.setObjectName("CardTight")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        self.check = QCheckBox()
        self.check.setChecked(True)
        self.check.stateChanged.connect(lambda _=0: self.changed.emit())
        layout.addWidget(self.check)

        badge = Badge(link.provider_name, palette().primary if link.provider == "quark" else palette().warning)
        layout.addWidget(badge)

        text_box = QVBoxLayout()
        text_box.setSpacing(1)
        url = QLabel(link.url)
        url.setObjectName("LinkText")
        url.setWordWrap(False)
        url.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_box.addWidget(url)
        hint = QLabel(link.pwd_id or "（未能解析出分享标识）")
        hint.setObjectName("Faint")
        text_box.addWidget(hint)
        layout.addLayout(text_box, 1)

        code_label = QLabel("提取码")
        code_label.setObjectName("Faint")
        layout.addWidget(code_label)
        self.code_edit = QLineEdit(link.code)
        self.code_edit.setFixedWidth(76)
        self.code_edit.setPlaceholderText("选填")
        layout.addWidget(self.code_edit)

        self.remove = QPushButton("移除")
        self.remove.setObjectName("IconBtn")
        self.remove.clicked.connect(self._remove)
        layout.addWidget(self.remove)

        self._on_remove = None

    def _remove(self) -> None:
        if callable(self._on_remove):
            self._on_remove(self)

    def bind_remove(self, callback) -> None:
        self._on_remove = callback

    @property
    def enabled(self) -> bool:
        return self.check.isChecked()

    def code(self) -> str:
        return self.code_edit.text().strip()


class TransferPage(Page):
    title = "夸克中转站"
    subtitle = "粘贴别人的夸克分享链接，自动转存到你的网盘目录，并生成一条属于你的分享链接（名字 + 链接直接复制走）"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self._rows: list[LinkRow] = []
        self._result_rows: list[CopyRow] = []
        self._my_tasks: set[str] = set()
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(450)
        self._debounce.timeout.connect(self._rebuild_rows)

        body = self.scrollable()

        # ---------------------------------------------------------- 输入区
        input_card = Card("粘贴夸克分享链接", "支持一次粘贴多条；链接后面的文字、提取码、二维码说明都可以一起丢进来，会自动识别。")
        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            "例：https://pan.quark.cn/s/xxxxxxxx  提取码：abcd\n"
            "也可以直接在当前页面按 Ctrl+V —— 复制到剪贴板的内容会被自动识别。"
        )
        self.input.setFixedHeight(96)
        self.input.textChanged.connect(self._debounce.start)
        input_card.add(self.input)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        paste_btn = QPushButton("读取剪贴板")
        paste_btn.setObjectName("Ghost")
        paste_btn.clicked.connect(self._from_clipboard)
        actions.addWidget(paste_btn)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("Ghost")
        clear_btn.clicked.connect(lambda: self.input.setPlainText(""))
        actions.addWidget(clear_btn)
        self.count_label = QLabel("尚未识别到链接")
        self.count_label.setObjectName("Muted")
        actions.addWidget(self.count_label, 1)
        input_card.add_layout(actions)
        body.addWidget(input_card)

        # ---------------------------------------------------------- 识别结果
        self.links_card = Card("识别到的链接", "勾选需要处理的条目；提取码不正确时可以手动改。")
        self.links_box = QVBoxLayout()
        self.links_box.setSpacing(8)
        self.links_card.add_layout(self.links_box)
        self.links_empty = EmptyState("还没有识别到链接", "在上面的框里粘贴或按 Ctrl+V")
        self.links_card.add(self.links_empty)
        body.addWidget(self.links_card)

        # ---------------------------------------------------------- 转存设置
        settings_card = Card("转存与分享设置", step=2)
        row1 = QHBoxLayout()
        row1.setSpacing(10)
        dir_label = QLabel("转存目录")
        dir_label.setFixedWidth(72)
        dir_label.setObjectName("Muted")
        row1.addWidget(dir_label)
        self.dir_box = QComboBox()
        self.dir_box.setEditable(True)
        self.dir_box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row1.addWidget(self.dir_box, 1)
        settings_card.add_layout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(10)
        name_label = QLabel("分享名字")
        name_label.setFixedWidth(72)
        name_label.setObjectName("Muted")
        row2.addWidget(name_label)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空则自动使用分享标题／文件名")
        row2.addWidget(self.name_edit, 1)
        settings_card.add_layout(row2)

        row3 = QHBoxLayout()
        row3.setSpacing(10)
        exp_label = QLabel("有效期")
        exp_label.setFixedWidth(72)
        exp_label.setObjectName("Muted")
        row3.addWidget(exp_label)
        self.expired_box = QComboBox()
        self.expired_box.addItems(list(EXPIRED_TYPES.keys()))
        row3.addWidget(self.expired_box)
        type_label = QLabel("分享方式")
        type_label.setObjectName("Muted")
        row3.addWidget(type_label)
        self.type_box = QComboBox()
        self.type_box.addItems(["私密（带提取码，推荐）", "公开（无提取码）"])
        row3.addWidget(self.type_box)
        row3.addStretch(1)
        settings_card.add_layout(row3)

        body.addWidget(settings_card)

        # ---------------------------------------------------------- 主按钮
        run_row = QHBoxLayout()
        run_row.setSpacing(10)
        self.run_button = QPushButton("开始转存并生成分享")
        self.run_button.setObjectName("Primary")
        self.run_button.setMinimumHeight(42)
        self.run_button.clicked.connect(self._start)
        run_row.addWidget(self.run_button, 1)
        self.auto_check = QCheckBox("复制到剪贴板后自动填充")
        self.auto_check.setChecked(bool(self.services.config.get("app.watch_clipboard", True)))
        self.auto_check.stateChanged.connect(
            lambda: self.services.set_clipboard_watch(self.auto_check.isChecked())
        )
        run_row.addWidget(self.auto_check)
        body.addLayout(run_row)

        # ---------------------------------------------------------- 结果区
        result_card = Card("生成结果")
        self.result_box = QVBoxLayout()
        self.result_box.setSpacing(8)
        result_card.add_layout(self.result_box)
        self.result_empty = EmptyState("还没有生成分享", "转存成功后会在这里列出「名字 + 链接」")
        result_card.add(self.result_empty)

        self.combined = QPlainTextEdit()
        self.combined.setReadOnly(True)
        self.combined.setFixedHeight(84)
        self.combined.setPlaceholderText("组合结果预览（按 设置 → 命名模板 生成）")
        result_card.add(self.combined)

        result_actions = QHBoxLayout()
        result_actions.setSpacing(8)
        copy_all = QPushButton("复制全部（名字+链接）")
        copy_all.setObjectName("Primary")
        copy_all.clicked.connect(self._copy_all)
        result_actions.addWidget(copy_all)
        open_history = QPushButton("去历史记录")
        open_history.setObjectName("Ghost")
        open_history.clicked.connect(lambda: self.services.status_message.emit("打开历史记录"))
        result_actions.addWidget(open_history)
        result_actions.addStretch(1)
        self.progress_hint = QLabel("")
        self.progress_hint.setObjectName("Muted")
        result_actions.addWidget(self.progress_hint)
        result_card.add_layout(result_actions)
        body.addWidget(result_card)

        self._load_dirs()
        self.services.tasks.task_finished.connect(self._on_task_finished)
        self.services.links_detected.connect(self._on_clipboard_links)

    # ------------------------------------------------------------- 数据装配
    def _load_dirs(self) -> None:
        recent = list(self.services.config.get("quark.recent_dirs", []) or [])
        default = str(self.services.config.get("quark.default_dir", "夸克中转站"))
        items = [default] + [item for item in recent if item != default]
        self.dir_box.clear()
        self.dir_box.addItems(items)
        self.dir_box.setCurrentText(default)
        self.expired_box.setCurrentText("永久")
        url_type = int(self.services.config.get("quark.share_url_type", 2) or 2)
        self.type_box.setCurrentIndex(1 if url_type == 1 else 0)

    def _from_clipboard(self) -> None:
        links = self.services.take_clipboard_links()
        if not links:
            Toast.show_message(self, "剪贴板里没有识别到网盘链接", "warning")
            return
        text = "\n".join(link.url + (f" 提取码：{link.code}" if link.code else "") for link in links)
        self.input.setPlainText(text)

    def _on_clipboard_links(self, links) -> None:
        if not links:
            return
        quark_links = [link for link in links if link.provider == "quark"]
        if not quark_links:
            return
        current = self.input.toPlainText().strip()
        addition = "\n".join(link.url + (f" 提取码：{link.code}" if link.code else "") for link in quark_links)
        if addition and addition not in current:
            self.input.setPlainText((current + "\n" + addition).strip())
            Toast.show_message(self, f"已从剪贴板识别到 {len(quark_links)} 个夸克链接", "info")

    def _rebuild_rows(self) -> None:
        text = self.input.toPlainText()
        links = extract(text)
        while self.links_box.count():
            item = self.links_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        for link in links:
            row = LinkRow(link)
            row.bind_remove(self._remove_row)
            self.links_box.addWidget(row)
            self._rows.append(row)
        self.links_empty.setVisible(not links)
        self.links_card.title_label.setText("识别到的链接")
        quark_count = sum(1 for link in links if link.provider == "quark")
        if links:
            extra = "" if quark_count == len(links) else f"（其中夸克 {quark_count} 条）"
            self.count_label.setText(f"共识别到 {len(links)} 条链接{extra}")
        else:
            self.count_label.setText("尚未识别到链接")

    def _remove_row(self, row: LinkRow) -> None:
        self._rows = [item for item in self._rows if item is not row]
        self.links_box.removeWidget(row)
        row.deleteLater()

    # ---------------------------------------------------------------- 执行
    def _start(self) -> None:
        if not self._rows:
            self._rebuild_rows()
        selected = [row for row in self._rows if row.enabled]
        if not selected:
            Toast.show_message(self, "请先粘贴并勾选至少一条链接", "warning")
            return

        unsupported = [row for row in selected if row.link.provider != "quark"]
        for row in selected:
            link = row.link
            if link.provider != "quark":
                continue
            link.code = row.code()

        base_name = self.name_edit.text().strip()
        directory = self.dir_box.currentText().strip()
        if directory:
            self.services.config.push_recent("quark.recent_dirs", directory)
            self.services.config.set("quark.default_dir", directory)
        url_type = 1 if selected and self.type_box.currentIndex() == 1 else 2
        expired_type = EXPIRED_TYPES.get(self.expired_box.currentText(), 1)

        quark_rows = [row for row in selected if row.link.provider == "quark"]
        if not quark_rows:
            Toast.show_message(self, "目前只支持夸克分享链接的转存", "warning")
            return
        if unsupported:
            Toast.show_message(
                self, f"已跳过 {len(unsupported)} 条非夸克链接（可用「百度→夸克」页处理百度链接）", "info"
            )

        started = 0
        for index, row in enumerate(quark_rows, start=1):
            name = base_name
            if base_name and len(quark_rows) > 1:
                name = f"{base_name} {index}"
            task = self.services.start_quark_transfer(
                row.link,
                name,
                directory,
                url_type=url_type,
                expired_type=expired_type,
                parent=self,
            )
            if task:
                self._my_tasks.add(task.id)
                started += 1
        if started:
            self.progress_hint.setText(f"已提交 {started} 个任务，可在「任务中心」查看进度")
            Toast.show_message(self, f"已提交 {started} 个转存任务", "success")

    def _on_task_finished(self, task) -> None:
        if task.id not in self._my_tasks:
            return
        from ...core.tasks import TaskStatus

        if task.status != TaskStatus.SUCCESS:
            return
        result = task.result or {}
        link = str(result.get("link") or "")
        if not link:
            return
        name = str(result.get("name") or "")
        row = CopyRow(
            name=name,
            link=link,
            code=str(result.get("code") or ""),
            combined=str(result.get("combined") or ""),
            note="转存目录：" + str(result.get("target_dir") or "") + "　文件：" + "、".join(
                str(item) for item in (result.get("files") or [])[:4]
            ),
        )
        self.result_box.addWidget(row)
        self._result_rows.append(row)
        self.result_empty.setVisible(False)
        self._refresh_combined()

    def _refresh_combined(self) -> None:
        items = [(row.name, row.link, row.code) for row in self._result_rows]
        self.combined.setPlainText(render_many(items))

    def _copy_all(self) -> None:
        text = self.combined.toPlainText().strip()
        if not text:
            Toast.show_message(self, "还没有可复制的结果", "warning")
            return
        copy_text(text)
        Toast.show_message(self, "已复制全部「名字 + 链接」", "success")

    def refresh(self) -> None:
        self._load_dirs()
        if self.services.quark_logged_in and not self.dir_box.currentText().strip():
            self.dir_box.setCurrentText("夸克中转站")
