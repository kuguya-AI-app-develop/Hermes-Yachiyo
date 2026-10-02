"""Durable FutureTask persistence for proactive Agent self-wakeups."""

from __future__ import annotations

import re
import time
from typing import Any, Callable
from uuid import uuid4

from apps.shell.agent.runtime.errors import AgentRuntimeError


class AgentFutureTaskStore:
    """Durable FutureTask control store for proactive Agent self-wakeups."""

    def __init__(
        self,
        conn: Any,
        db_lock: Any,
        *,
        source_run_id: str = "",
        default_runnable_id: str = "",
        now: Callable[[], str],
        json_dump: Callable[[Any], str],
        redact_json_value: Callable[[Any], Any],
        redact_secrets: Callable[[Any], str],
        error_type: type[Exception] = AgentRuntimeError,
    ) -> None:
        self._conn = conn
        self._db_lock = db_lock
        self.source_run_id = str(source_run_id or "").strip()
        self.default_runnable_id = str(default_runnable_id or "").strip()
        self._now = now
        self._json_dump = json_dump
        self._redact_json_value = redact_json_value
        self._redact_secrets = redact_secrets
        self._error_type = error_type

    @staticmethod
    def _next_cron_epoch(cron: str, now: float) -> float:
        clean = str(cron or "").strip().lower()
        if clean == "@hourly":
            return now + 3600
        if clean == "@daily":
            return now + 86400
        if clean == "@weekly":
            return now + 7 * 86400
        match = re.fullmatch(r"every\s+(\d+)\s+(minute|minutes|hour|hours|day|days)", clean)
        if not match:
            raise AgentRuntimeError(
                "FutureTask cron 目前支持 @hourly、@daily、@weekly 或 every N minutes/hours/days"
            )
        amount = max(1, int(match.group(1)))
        unit = match.group(2)
        if unit.startswith("minute"):
            return now + amount * 60
        if unit.startswith("hour"):
            return now + amount * 3600
        return now + amount * 86400

    @classmethod
    def _coerce_scheduled_epoch(
        cls,
        *,
        delay_seconds: Any = None,
        scheduled_at_epoch: Any = None,
        cron: str = "",
        now: float | None = None,
    ) -> float:
        current = time.time() if now is None else float(now)
        clean_cron = str(cron or "").strip()
        if scheduled_at_epoch not in (None, ""):
            try:
                epoch = float(scheduled_at_epoch)
            except (TypeError, ValueError) as exc:
                raise AgentRuntimeError("FutureTask scheduled_at_epoch 必须是 Unix epoch 秒数") from exc
            if clean_cron:
                cls._next_cron_epoch(clean_cron, current)
            return max(current, epoch)
        if delay_seconds not in (None, ""):
            try:
                delay = max(0.0, float(delay_seconds))
            except (TypeError, ValueError) as exc:
                raise AgentRuntimeError("FutureTask delay_seconds 必须是数字") from exc
            if clean_cron:
                cls._next_cron_epoch(clean_cron, current)
            return current + delay
        if clean_cron:
            return cls._next_cron_epoch(clean_cron, current)
        return current

    @staticmethod
    def _row_to_future_task(row: Any) -> dict[str, Any]:
        return {
            "future_task_id": str(row["future_task_id"]),
            "title": str(row["title"] or ""),
            "prompt": str(row["prompt"] or ""),
            "runnable_id": str(row["runnable_id"] or ""),
            "runnable_name": str(row["runnable_name"] or ""),
            "status": str(row["status"] or "scheduled"),
            "scheduled_at_epoch": float(row["scheduled_at_epoch"] or 0.0),
            "cron": str(row["cron"] or ""),
            "source_run_id": str(row["source_run_id"] or ""),
            "last_run_id": str(row["last_run_id"] or ""),
            "run_count": int(row["run_count"] or 0),
            "error": str(row["error"] or ""),
            "created_at": str(row["created_at"] or ""),
            "updated_at": str(row["updated_at"] or ""),
            "cancelled_at": str(row["cancelled_at"] or ""),
            "trigger_in_flight": bool(row["trigger_in_flight"]),
        }

    def _record_event(self, future_task_id: str, action: str, payload: dict[str, Any]) -> None:
        self._conn.execute(
            """
            INSERT INTO future_task_events (
                event_id, future_task_id, action, actor, payload_json, source_run_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"future_event_{uuid4().hex[:16]}",
                str(future_task_id or ""),
                str(action or ""),
                "agent_runtime",
                self._json_dump(self._redact_json_value(payload)),
                self.source_run_id,
                self._now(),
            ),
        )

    def schedule(
        self,
        *,
        title: str,
        prompt: str,
        runnable_id: str = "",
        runnable_name: str = "",
        delay_seconds: Any = None,
        scheduled_at_epoch: Any = None,
        cron: str = "",
    ) -> dict[str, Any]:
        clean_prompt = self._redact_secrets(prompt).strip()
        if not clean_prompt:
            raise self._error_type("FutureTask prompt 不能为空")
        clean_title = self._redact_secrets(title).strip() or clean_prompt[:80] or "Future task"
        clean_runnable_id = str(runnable_id or self.default_runnable_id or "").strip()
        clean_runnable_name = str(runnable_name or "").strip()
        if not clean_runnable_id and not clean_runnable_name:
            raise self._error_type("FutureTask 需要 runnable_id 或 runnable_name")
        clean_cron = str(cron or "").strip()
        scheduled_epoch = self._coerce_scheduled_epoch(
            delay_seconds=delay_seconds,
            scheduled_at_epoch=scheduled_at_epoch,
            cron=clean_cron,
        )
        future_task_id = f"future_{uuid4().hex[:16]}"
        now = self._now()
        with self._db_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    """
                    INSERT INTO future_tasks (
                        future_task_id, title, prompt, runnable_id, runnable_name,
                        status, scheduled_at_epoch, cron, source_run_id, last_run_id,
                        run_count, error, created_at, updated_at, cancelled_at
                    ) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, '', 0, '', ?, ?, '')
                    """,
                    (
                        future_task_id,
                        clean_title,
                        clean_prompt,
                        clean_runnable_id,
                        clean_runnable_name,
                        scheduled_epoch,
                        clean_cron,
                        self.source_run_id,
                        now,
                        now,
                    ),
                )
                self._record_event(
                    future_task_id,
                    "future_task.schedule",
                    {
                        "title": clean_title,
                        "runnable_id": clean_runnable_id,
                        "runnable_name": clean_runnable_name,
                        "scheduled_at_epoch": scheduled_epoch,
                        "cron": clean_cron,
                    },
                )
                row = self._conn.execute(
                    "SELECT * FROM future_tasks WHERE future_task_id=?",
                    (future_task_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"ok": True, "future_task": self._row_to_future_task(row)}

    def list_tasks(self, *, include_finished: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        where = "" if include_finished else "WHERE status IN ('scheduled', 'claimed', 'executing')"
        rows = self._conn.execute(
            f"""
            SELECT *
              FROM future_tasks
              {where}
             ORDER BY status='scheduled' DESC, scheduled_at_epoch ASC, updated_at DESC
             LIMIT ?
            """,
            (max(1, min(int(limit or 100), 500)),),
        ).fetchall()
        return [self._row_to_future_task(row) for row in rows]

    def cancel(self, future_task_id: str, *, reason: str = "") -> dict[str, Any]:
        clean_id = str(future_task_id or "").strip()
        if not clean_id:
            raise self._error_type("future_task.cancel 需要 future_task_id")
        with self._db_lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM future_tasks WHERE future_task_id=?",
                    (clean_id,),
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise KeyError(clean_id)
                if str(row["status"] or "") not in {"scheduled", "claimed", "executing"}:
                    self._conn.rollback()
                    return {
                        "ok": True,
                        "future_task": self._row_to_future_task(row),
                        "already_terminal": True,
                    }
                now = self._now()
                self._conn.execute(
                    """
                    UPDATE future_tasks
                       SET status='cancelled', error=?, updated_at=?, cancelled_at=?,
                           trigger_claim_id=CASE WHEN trigger_in_flight=1
                               THEN trigger_claim_id ELSE '' END
                     WHERE future_task_id=?
                    """,
                    (self._redact_secrets(reason).strip(), now, now, clean_id),
                )
                self._record_event(clean_id, "future_task.cancel", {
                    "reason": reason,
                    "current_trigger_continues": bool(row["trigger_in_flight"]),
                    "cancellation_policy": "stop_future_triggers",
                })
                updated = self._conn.execute(
                    "SELECT * FROM future_tasks WHERE future_task_id=?",
                    (clean_id,),
                ).fetchone()
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return {"ok": True, "future_task": self._row_to_future_task(updated)}
