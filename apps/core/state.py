"""应用状态管理

管理任务列表与状态，供 Core Runtime 和 Bridge 使用。
不直接处理 HTTP 请求。
"""

from __future__ import annotations

import logging
import threading
from collections import Counter
from datetime import datetime, timezone
from uuid import uuid4

from packages.protocol.enums import RiskLevel, TaskStatus, TaskType
from packages.protocol.schemas import TaskInfo

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _compact_log_detail(value: str | None, limit: int = 220) -> str:
    """压缩多行错误，避免任务状态日志只有 failed 而没有诊断信息。"""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = " ".join(part.strip() for part in text.split("\n") if part.strip())
    if len(text) > limit:
        return text[: limit - 3] + "..."
    return text


class AppState:
    """应用状态容器"""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = threading.RLock()

    def get_task_counts(self) -> dict[str, int]:
        with self._lock:
            counts = Counter(t.status for t in self._tasks.values())
            return {s.value: counts.get(s, 0) for s in TaskStatus}

    def list_tasks(self) -> list[TaskInfo]:
        with self._lock:
            return list(self._tasks.values())

    def get_task(self, task_id: str) -> TaskInfo | None:
        with self._lock:
            return self._tasks.get(task_id)

    def create_task(
        self,
        description: str,
        task_type: TaskType = TaskType.GENERAL,
        risk_level: RiskLevel = RiskLevel.LOW,
        attachments: list[dict] | None = None,
        chat_session_id: str | None = None,
        response_context: str | None = None,
    ) -> TaskInfo:
        with self._lock:
            now = _now()
            task = TaskInfo(
                task_id=uuid4().hex[:12],
                description=description,
                task_type=task_type,
                status=TaskStatus.PENDING,
                risk_level=risk_level,
                created_at=now,
                updated_at=now,
                attachments=list(attachments or []),
                chat_session_id=(chat_session_id or None),
                response_context=response_context,
            )
            self._tasks[task.task_id] = task
        logger.info("任务已创建: %s", task.task_id)
        return task

    def cancel_task(self, task_id: str) -> TaskInfo:
        """取消任务，返回更新后的任务信息

        Raises:
            KeyError: 任务不存在
            ValueError: 任务状态不可取消
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")
            if task.status not in (TaskStatus.PENDING, TaskStatus.RUNNING):
                raise ValueError(f"任务 {task_id} 状态为 {task.status}，无法取消")
            task.status = TaskStatus.CANCELLED
            task.updated_at = _now()
        logger.info("任务已取消: %s", task_id)
        return task

    def update_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        result: str | None = None,
        error: str | None = None,
        progress_label: str | None = None,
    ) -> TaskInfo:
        """更新任务状态。供执行器或外部调用者推进任务生命周期。

        合法状态流转：
          pending   → running | cancelled | failed
          running   → completed | cancelled | failed
          其他终态（completed/cancelled/failed）不可再变更

        Raises:
            KeyError:  任务不存在
            ValueError: 非法状态流转
        """
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise KeyError(f"任务 {task_id} 不存在")

            _TERMINAL = {TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED}
            if task.status in _TERMINAL:
                raise ValueError(
                    f"任务 {task_id} 已处于终态 {task.status.value}，不可再变更"
                )

            task.status = status
            task.updated_at = _now()
            if result is not None:
                task.result = result
            if error is not None:
                task.error = error
            if progress_label is not None:
                task.progress_label = str(progress_label or "").strip() or None
                task.progress_updated_at = task.updated_at
        if status == TaskStatus.FAILED and task.error:
            logger.info(
                "任务状态已更新: %s → %s | error=%s",
                task_id,
                status.value,
                _compact_log_detail(task.error),
            )
        else:
            logger.info("任务状态已更新: %s → %s", task_id, status.value)
        return task

    def update_task_progress(self, task_id: str, label: str) -> TaskInfo | None:
        """Update the lightweight user-visible progress label for an active task."""
        label = str(label or "").strip()
        if not label:
            return self.get_task(task_id)
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.progress_label = label
            task.progress_updated_at = _now()
            task.updated_at = task.progress_updated_at
            return task
