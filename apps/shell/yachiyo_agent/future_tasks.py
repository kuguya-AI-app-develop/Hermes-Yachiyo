"""FutureTask public snapshot adapters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shell.agent.runtime.events import redact_secrets

from .contracts import FutureTaskSnapshot, FutureTaskTriggerResultSnapshot
from .timelines import run_timeline_snapshot_from_payload


def future_task_snapshot_from_payload(payload: Mapping[str, Any] | None) -> FutureTaskSnapshot:
    raw = payload if isinstance(payload, Mapping) else {}
    return FutureTaskSnapshot(
        future_task_id=str(raw.get("future_task_id") or ""),
        title=_text(raw.get("title")),
        prompt=_text(raw.get("prompt")),
        runnable_id=_optional_text(raw.get("runnable_id")),
        runnable_name=_optional_text(raw.get("runnable_name")),
        status=str(raw.get("status") or "scheduled"),
        scheduled_at_epoch=_float(raw.get("scheduled_at_epoch")),
        cron=_optional_text(raw.get("cron")),
        source_run_id=_optional_text(raw.get("source_run_id")),
        last_run_id=_optional_text(raw.get("last_run_id")),
        run_count=_int(raw.get("run_count")),
        error=_optional_text(raw.get("error")),
        created_at=str(raw.get("created_at") or ""),
        updated_at=str(raw.get("updated_at") or ""),
        cancelled_at=_optional_text(raw.get("cancelled_at")),
        trigger_in_flight=raw.get("trigger_in_flight") is True,
    )


def future_task_trigger_result_snapshot_from_payload(
    payload: Mapping[str, Any] | None,
) -> FutureTaskTriggerResultSnapshot:
    raw = payload if isinstance(payload, Mapping) else {}
    future_task_payload = raw.get("future_task")
    run_payload = raw.get("run")
    return FutureTaskTriggerResultSnapshot(
        ok=raw.get("ok") is not False,
        future_task=(
            future_task_snapshot_from_payload(future_task_payload)
            if isinstance(future_task_payload, Mapping)
            else None
        ),
        run=(
            run_timeline_snapshot_from_payload(run_payload)
            if isinstance(run_payload, Mapping)
            else None
        ),
        error=_optional_text(raw.get("error")),
    )


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _text(value: Any) -> str:
    return str(redact_secrets(value) or "").strip()


def _float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
