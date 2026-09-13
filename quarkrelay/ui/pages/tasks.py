"""任务中心：队列进度 + 运行日志。"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QPushButton, QVBoxLayout, QWidget

from ...core.tasks import TaskStatus
from ...logging_setup import memory_handler
from ..widgets import Card, EmptyState, LogPane, StatTile, TaskRow, Toast
from . import Page


class TasksPage(Page):
    title = "任务中心"
    subtitle = "所有转存、搬运、生成分享的任务都在这里排队执行，可以随时取消"

    def __init__(self, services, parent=None) -> None:
        super().__init__(services, parent)
        self._rows: dict[str, TaskRow] = {}
        body = self.scrollable()

        stats = QHBoxLayout()
        stats.setSpacing(12)
        self.stat_active = StatTile("进行中", "0")
        self.stat_done = StatTile("已完成", "0")
        self.stat_failed = StatTile("失败", "0")
        for tile in (self.stat_active, self.stat_done, self.stat_failed):
            stats.addWidget(tile, 1)
        body.addLayout(stats)

        card = Card("任务列表", "点「取消」可以中断进行中的任务；搬运任务会在当前分片结束后停止。")
        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        clear_btn = QPushButton("清空已结束")
        clear_btn.setObjectName("Ghost")
        clear_btn.clicked.connect(self._clear_finished)
        toolbar.addWidget(clear_btn)
        cancel_all = QPushButton("全部取消")
        cancel_all.setObjectName("Ghost")
        cancel_all.clicked.connect(self.services.tasks.cancel_all)
        toolbar.addWidget(cancel_all)
        toolbar.addStretch(1)
        card.add_layout(toolbar)

        self.list_box = QVBoxLayout()
        self.list_box.setSpacing(8)
        card.add_layout(self.list_box)
        self.empty = EmptyState("还没有任务", "在其它页面发起一次转存或搬运试试")
        card.add(self.empty)
        body.addWidget(card)

        log_card = Card("运行日志", "最近 3000 条，文件同时写在 %APPDATA%\\QuarkRelay\\logs 下")
        self.log_view = LogPane()
        self.log_view.setMinimumHeight(220)
        self.log_view.setPlainText("\n".join(memory_handler.snapshot()[-400:]))
        log_card.add(self.log_view)
        body.addWidget(log_card)

        memory_handler.subscribe(self._append_log)
        self.services.tasks.task_added.connect(self._on_added)
        self.services.tasks.task_changed.connect(self._on_changed)

    def _append_log(self, text: str) -> None:
        from PySide6.QtCore import QTimer

        QTimer.singleShot(0, lambda: self.log_view.append_line(text))

    def _on_added(self, task) -> None:
        row = TaskRow(task)
        row.cancel_requested.connect(self.services.tasks.cancel)
        self.list_box.insertWidget(0, row)
        self._rows[task.id] = row
        self.empty.setVisible(False)
        self._refresh_stats()

    def _on_changed(self, task) -> None:
        row = self._rows.get(task.id)
        if row is None:
            self._on_added(task)
            return
        row.update_from(task)
        self._refresh_stats()

    def _refresh_stats(self) -> None:
        tasks = self.services.tasks.tasks()
        active = sum(1 for t in tasks if t.status.active)
        done = sum(1 for t in tasks if t.status == TaskStatus.SUCCESS)
        failed = sum(1 for t in tasks if t.status in (TaskStatus.FAILED, TaskStatus.CANCELLED))
        self.stat_active.set_value(str(active))
        self.stat_done.set_value(str(done))
        self.stat_failed.set_value(str(failed))

    def _clear_finished(self) -> None:
        for task_id, row in list(self._rows.items()):
            task = self.services.tasks.get(task_id)
            if task is None or not task.status.active:
                self.list_box.removeWidget(row)
                row.deleteLater()
                self._rows.pop(task_id, None)
        self.services.tasks.clear_finished()
        self.empty.setVisible(not self._rows)
        self._refresh_stats()
        Toast.show_message(self, "已清理结束的任务", "info")

    def refresh(self) -> None:
        tasks = self.services.tasks.tasks()
        if tasks and not self._rows:
            for task in reversed(tasks):
                self._on_added(task)
        self._refresh_stats()

    def active_count(self) -> int:
        return self.services.tasks.active_count()
