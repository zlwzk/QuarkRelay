"""任务中心：线程池 + 进度上报 + 取消，界面只订阅信号。"""

from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, Signal

from .errors import CancelledError, friendly

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return {
            "pending": "排队中",
            "running": "进行中",
            "success": "已完成",
            "failed": "失败",
            "cancelled": "已取消",
        }[self.value]

    @property
    def active(self) -> bool:
        return self in (TaskStatus.PENDING, TaskStatus.RUNNING)


@dataclass
class Task:
    """一条可观察的任务。"""

    id: str
    kind: str
    title: str = ""
    status: TaskStatus = TaskStatus.PENDING
    progress: float = 0.0
    stage: str = ""
    detail: str = ""
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)

    # ------------------------------------------------------------ 状态修改
    def set_stage(self, stage: str) -> None:
        self.stage = stage
        self.detail = stage

    def set_progress(self, value: float) -> None:
        self.progress = max(0.0, min(100.0, float(value)))

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def cancel(self) -> None:
        self._cancel.set()

    def should_cancel(self) -> bool:
        return self._cancel.is_set()

    @property
    def duration(self) -> float:
        end = self.finished_at or time.time()
        start = self.started_at or self.created_at
        return max(0.0, end - start)


class TaskCenter(QObject):
    """任务调度：把耗时逻辑丢进线程池，用 Qt 信号回主线程。"""

    task_added = Signal(object)
    task_changed = Signal(object)
    task_finished = Signal(object)
    log = Signal(str)

    def __init__(self, max_workers: int = 2, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=max(1, max_workers), thread_name_prefix="qr-task")
        self._tasks: dict[str, Task] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ api
    def submit(
        self,
        kind: str,
        title: str,
        worker: Callable[[Task], dict[str, Any]],
        *,
        payload: dict[str, Any] | None = None,
    ) -> Task:
        task = Task(id=uuid.uuid4().hex[:12], kind=kind, title=title, payload=payload or {})
        with self._lock:
            self._tasks[task.id] = task
        self.task_added.emit(task)
        self._executor.submit(self._run, task, worker)
        return task

    def _run(self, task: Task, worker: Callable[[Task], dict[str, Any]]) -> None:
        task.status = TaskStatus.RUNNING
        task.started_at = time.time()
        task.set_stage("开始")
        self.task_changed.emit(task)
        try:
            result = worker(task) or {}
            if task.cancelled:
                raise CancelledError()
            task.result = result
            task.status = TaskStatus.SUCCESS
            task.set_progress(100.0)
            task.set_stage("完成")
        except CancelledError:
            task.status = TaskStatus.CANCELLED
            task.error = "任务已取消"
            task.set_stage("已取消")
        except Exception as exc:  # noqa: BLE001 - 需要兜住所有业务异常
            logger.error("任务 %s 失败：%s\n%s", task.title, exc, traceback.format_exc(limit=6))
            task.status = TaskStatus.FAILED
            task.error = friendly(exc)
            task.set_stage(task.error)
        finally:
            task.finished_at = time.time()
            self.task_finished.emit(task)
            self.task_changed.emit(task)

    def cancel(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
        if task and task.status.active:
            task.cancel()
            task.set_stage("正在取消…")
            self.task_changed.emit(task)

    def cancel_all(self) -> None:
        for task in self.tasks():
            if task.status.active:
                self.cancel(task.id)

    def tasks(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda item: item.created_at, reverse=True)

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def active_count(self) -> int:
        return sum(1 for task in self.tasks() if task.status.active)

    def clear_finished(self) -> int:
        with self._lock:
            done = [tid for tid, task in self._tasks.items() if not task.status.active]
            for tid in done:
                self._tasks.pop(tid, None)
        return len(done)

    def set_max_workers(self, workers: int) -> None:
        workers = max(1, int(workers))
        with self._lock:
            old = self._executor
            self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qr-task")
        old.shutdown(wait=False, cancel_futures=False)

    def shutdown(self) -> None:
        self.cancel_all()
        self._executor.shutdown(wait=False, cancel_futures=True)
