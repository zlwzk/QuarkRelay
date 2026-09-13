"""历史记录：所有生成过的「名字 + 链接」，可搜索、可批量复制、可导出。"""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...core.naming import render_many
from ..widgets import Card, EmptyState, Toast, copy_text, palette
from . import Page


class HistoryPage(Page):
    title = "历史记录"
    subtitle = "每一次生成的分享都会落在这里，支持搜索、批量复制「名字 + 链接」和导出 CSV"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        body = self.scrollable()

        card = Card("记录", "双击任意一行 = 复制这一条的「名字 + 链接」")
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索名字、链接或备注…")
        self.search.textChanged.connect(self._reload)
        toolbar.addWidget(self.search, 1)
        copy_btn = QPushButton("复制选中（名字+链接）")
        copy_btn.setObjectName("Primary")
        copy_btn.clicked.connect(self._copy_selected)
        toolbar.addWidget(copy_btn)
        copy_all = QPushButton("复制当前列表全部")
        copy_all.setObjectName("Ghost")
        copy_all.clicked.connect(self._copy_all)
        toolbar.addWidget(copy_all)
        export = QPushButton("导出 CSV")
        export.setObjectName("Ghost")
        export.clicked.connect(self._export)
        toolbar.addWidget(export)
        clear = QPushButton("清空历史")
        clear.setObjectName("Danger")
        clear.clicked.connect(self._clear)
        toolbar.addWidget(clear)
        card.add_layout(toolbar)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(["时间", "类型", "名字", "链接", "提取码", "备注"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(False)
        self.table.setMinimumHeight(420)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(2, 220)
        self.table.setColumnWidth(5, 200)
        self.table.doubleClicked.connect(lambda _index: self._copy_selected())
        card.add(self.table)

        self.empty = EmptyState("还没有历史记录", "生成分享后会自动记录")
        card.add(self.empty)
        body.addWidget(card)

        self._records = []
        self.services.history_changed.connect(self._reload)
        self._reload()

    # ------------------------------------------------------------------ 数据
    def _reload(self) -> None:
        self._records = self.services.records(self.search.text().strip(), limit=800)
        self.table.setRowCount(len(self._records))
        for row, record in enumerate(self._records):
            values = [
                time.strftime("%m-%d %H:%M", time.localtime(record.created_at)),
                {"quark_transfer": "夸克转存", "baidu_relay": "百度搬运", "share_export": "生成分享"}.get(
                    record.kind, record.kind
                ),
                record.name,
                record.link,
                record.code,
                record.note,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setToolTip(str(value))
                if column == 3:
                    item.setForeground(QColor(palette().accent))
                self.table.setItem(row, column, item)
        self.empty.setVisible(not self._records)

    def _selected_records(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [self._records[row] for row in rows if 0 <= row < len(self._records)]

    def _copy_selected(self) -> None:
        records = self._selected_records()
        if not records:
            Toast.show_message(self, "请先选中至少一行", "warning")
            return
        text = render_many([(r.name, r.link, r.code) for r in records])
        copy_text(text)
        Toast.show_message(self, f"已复制 {len(records)} 条", "success")

    def _copy_all(self) -> None:
        if not self._records:
            Toast.show_message(self, "列表是空的", "warning")
            return
        text = render_many([(r.name, r.link, r.code) for r in self._records])
        copy_text(text)
        Toast.show_message(self, f"已复制 {len(self._records)} 条", "success")

    def _export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "导出历史记录", "quarkrelay-history.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            count = self.services.history.export_csv(path, self.search.text().strip())
        except OSError as exc:
            Toast.show_message(self, f"导出失败：{exc}", "error")
            return
        Toast.show_message(self, f"已导出 {count} 条到 {path}", "success")

    def _clear(self) -> None:
        self.services.history.clear()
        self.services.history_changed.emit()
        Toast.show_message(self, "历史记录已清空", "info")

    def refresh(self) -> None:
        self._reload()
