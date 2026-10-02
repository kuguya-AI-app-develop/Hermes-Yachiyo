"""Main chat run lifecycle helpers for the legacy runtime entrypoint."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Mapping
from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable

from .desktop_provider_session_events import desktop_provider_session_timeline_events
from .errors import AgentRuntimeError
from .supplied_images import (
    SUPPLIED_IMAGE_EVENT,
    persisted_supplied_image_binding,
    supplied_image_binding_from_messages,
)


class MainChatRunLifecycle:
    """Starts and completes daily Chat runs while preserving task projections."""

    def __init__(
        self,
        *,
        main_chat_agent_id: str,
        insert_run: Callable[..., dict[str, Any]],
        link_task_run: Callable[..., Any],
        get_run: Callable[[str], dict[str, Any]],
        update_run: Callable[..., dict[str, Any] | None],
        task_run_links: Any,
        task_events: Any,
        timeline_factory: Callable[..., dict[str, Any]],
        redact_secrets: Callable[[Any], str],
        final_statuses: set[str],
        append_run_event: Callable[..., Any] | None = None,
        run_by_client_request_id: Callable[[str], dict[str, Any] | None] | None = None,
        client_request_id_from_payload: Callable[[dict[str, Any]], str] | None = None,
        lock: AbstractContextManager[Any] | None = None,
        error_type: type[Exception] = RuntimeError,
        transaction_scope: Callable[[], AbstractContextManager[Any]] | None = None,
    ) -> None:
        self._main_chat_agent_id = main_chat_agent_id
        self._insert_run = insert_run
        self._link_task_run = link_task_run
        self._get_run = get_run
        self._update_run = update_run
        self._task_run_links = task_run_links
        self._task_events = task_events
        self._timeline = timeline_factory
        self._redact_secrets = redact_secrets
        self._final_statuses = final_statuses
        self._append_run_event = append_run_event
        self._run_by_client_request_id = run_by_client_request_id
        self._client_request_id_from_payload = client_request_id_from_payload
        self._lock = lock or threading.RLock()
        self._error_type = error_type
        self._transaction_scope = transaction_scope

    def start(
        self,
        *,
        task_id: str,
        session_id: str,
        user_goal: str,
        client_run_id: str = "",
        metadata: dict[str, Any] | None = None,
        runtime_execution_envelope: Any | None = None,
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        run, existing = self._claim_run(
            user_goal=user_goal,
            client_run_id=client_run_id,
        )
        if existing:
            link = self._task_run_links.for_run(str(run.get("run_id") or ""))
            if isinstance(link, Mapping):
                return {
                    **run,
                    "task_id": str(link.get("task_id") or ""),
                    "session_id": str(link.get("session_id") or ""),
                }
            return run
        self._link_task_run(task_id=task_id, run_id=run["run_id"], session_id=session_id)
        start_payload = _main_chat_start_payload(
            task_id=task_id,
            session_id=session_id,
            metadata=metadata,
            runtime_execution_envelope=runtime_execution_envelope,
            direct_tool_request=direct_tool_request,
            direct_tool_requests=direct_tool_requests,
        )
        timeline = [
            self._timeline(
                "run.started",
                "Native main chat run started",
                **start_payload,
            ),
            *[
                self._timeline(
                    event["event"],
                    event["detail"],
                    **event["payload"],
                )
                for event in desktop_provider_session_timeline_events(
                    start_payload,
                    redact=self._redact_secrets,
                )
            ],
            self._timeline("task.created", str(task_id or ""), task_id=str(task_id or "")),
            self._timeline("task.started", str(task_id or ""), task_id=str(task_id or "")),
            self._timeline("task.linked", str(task_id or ""), task_id=str(task_id or "")),
        ]
        run = self._update_run(run["run_id"], timeline=timeline)
        self._task_events.started(
            run["run_id"],
            task_id=str(task_id or ""),
            session_id=str(session_id or ""),
        )
        return run

    def _claim_run(
        self,
        *,
        user_goal: str,
        client_run_id: str,
    ) -> tuple[dict[str, Any], bool]:
        client_request_id = str(client_run_id or "").strip()[:128]
        if self._client_request_id_from_payload is not None:
            client_request_id = self._client_request_id_from_payload(
                {"client_run_id": client_run_id}
            )
        if client_request_id and self._run_by_client_request_id is not None:
            existing = self._run_by_client_request_id(client_request_id)
            if existing is not None:
                return self._validated_existing_run(existing, user_goal=user_goal), True
        with self._lock:
            if client_request_id and self._run_by_client_request_id is not None:
                existing = self._run_by_client_request_id(client_request_id)
                if existing is not None:
                    return self._validated_existing_run(existing, user_goal=user_goal), True
            try:
                run = self._insert_run(
                    kind="main_chat_run",
                    runnable_id=self._main_chat_agent_id,
                    user_goal=self._redact_secrets(user_goal),
                    client_request_id=client_request_id,
                )
            except sqlite3.IntegrityError:
                existing = (
                    self._run_by_client_request_id(client_request_id)
                    if client_request_id and self._run_by_client_request_id is not None
                    else None
                )
                if existing is not None:
                    return self._validated_existing_run(existing, user_goal=user_goal), True
                raise
            return run, False

    def bind_supplied_images(
        self, run_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        scope = self._transaction_scope() if self._transaction_scope else nullcontext()
        with scope:
            run = self._get_run(run_id)
            if run.get("kind") != "main_chat_run":
                raise self._error_type("supplied_image_binding_requires_main_chat_run")
            if run.get("status") != "running" or run.get("pending_approval"):
                return run
            goal = str(run.get("user_goal") or "")
            binding = supplied_image_binding_from_messages(
                run_id=run_id, original_goal=goal, messages=messages
            )
            if binding is None:
                return run
            timeline = [event for event in run.get("timeline") or [] if isinstance(event, dict)]
            existing = persisted_supplied_image_binding(
                run_id=run_id, original_goal=goal, timeline=timeline
            )
            if existing is not None:
                if existing != binding:
                    raise self._error_type("supplied_image_binding_conflict")
                return run
            updated = self._update_run(
                run_id,
                timeline=[*timeline, self._timeline(
                    SUPPLIED_IMAGE_EVENT, "User image input bound", visibility="internal", **binding
                )],
                expected_status="running",
                expected_updated_at=str(run.get("updated_at") or ""),
                expected_pending_approval_absent=True,
            )
            if updated is None:
                return self._get_run(run_id)
            if self._append_run_event is None:
                raise self._error_type("main_chat_run_event_appender_required")
            _require_run_event(self._append_run_event(
                run_id, SUPPLIED_IMAGE_EVENT, {**binding, "visibility": "internal"},
                visibility="internal",
                **_terminal_event_fence(updated, status="running"),
            ))
            return updated

    def _validated_existing_run(
        self,
        existing: dict[str, Any],
        *,
        user_goal: str,
    ) -> dict[str, Any]:
        identity_matches = (
            str(existing.get("kind") or "") == "main_chat_run"
            and str(existing.get("runnable_id") or "") == self._main_chat_agent_id
            and str(existing.get("user_goal") or "") == self._redact_secrets(user_goal)
        )
        if not identity_matches:
            raise self._error_type(
                "idempotency key conflict: existing run identity does not match request"
            )
        return existing

    def complete(self, run_id: str, result: str) -> dict[str, Any]:
        safe_result = self._redact_secrets(result)
        scope = self._transaction_scope() if self._transaction_scope else nullcontext()
        with scope:
            run = self._get_run(run_id)
            status = str(run.get("status") or "").strip().lower()
            terminal = run if status in self._final_statuses else None
            if terminal is not None:
                return terminal
            if status in {"approval_required", "awaiting_user"} or run.get(
                "pending_approval"
            ):
                return run
            timeline = [
                *[
                    event
                    for event in run.get("timeline") or []
                    if isinstance(event, dict)
                ],
                self._timeline("run.completed", "Native main chat run completed"),
            ]
            completed = self._update_run(
                run_id,
                status="completed",
                result=safe_result,
                timeline=timeline,
                pending_approval=None,
                expected_status=status,
                expected_updated_at=str(run.get("updated_at") or ""),
                expected_pending_approval_absent=True,
            )
            if completed is None:
                return self._get_run(run_id)
            link = self._task_run_links.for_run(run_id)
            _require_run_event(
                self._task_events.completed(
                    run_id,
                    task_id=str((link or {}).get("task_id") or ""),
                    session_id=str((link or {}).get("session_id") or ""),
                    result=safe_result,
                    **_terminal_event_fence(completed, status="completed"),
                )
            )
        return completed

    def fail(
        self,
        run_id: str,
        error: Any,
        *,
        timeline: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
        run_events: list[tuple[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        safe_error = self._redact_secrets(error)
        scope = self._transaction_scope() if self._transaction_scope else nullcontext()
        with scope:
            run = self._get_run(run_id)
            status = str(run.get("status") or "").strip().lower()
            terminal = run if status in self._final_statuses else None
            if terminal is not None:
                return terminal
            if status == "approval_required" or run.get("pending_approval"):
                return run
            next_timeline = [
                *[
                    event
                    for event in (
                        timeline if timeline is not None else run.get("timeline") or []
                    )
                    if isinstance(event, dict)
                ],
                self._timeline("run.failed", safe_error),
            ]
            failed = self._update_run(
                run_id,
                status="failed",
                result=safe_error,
                timeline=next_timeline,
                **({"artifacts": artifacts} if artifacts is not None else {}),
                pending_approval=None,
                expected_status=status,
                expected_updated_at=str(run.get("updated_at") or ""),
                expected_pending_approval_absent=True,
            )
            if failed is None:
                return self._get_run(run_id)
            event_fence = _terminal_event_fence(failed, status="failed")
            if run_events:
                if self._append_run_event is None:
                    raise AgentRuntimeError("main_chat_run_event_appender_required")
                for event_type, payload in run_events:
                    _require_run_event(
                        self._append_run_event(
                            run_id,
                            str(event_type or ""),
                            dict(payload or {}),
                            **event_fence,
                        )
                    )
            link = self._task_run_links.for_run(run_id)
            _require_run_event(
                self._task_events.failed(
                    run_id,
                    task_id=str((link or {}).get("task_id") or ""),
                    session_id=str((link or {}).get("session_id") or ""),
                    error=safe_error,
                    **event_fence,
                )
            )
        return failed


def _terminal_event_fence(
    run: dict[str, Any],
    *,
    status: str,
) -> dict[str, str]:
    return {
        "expected_status": status,
        "expected_updated_at": str(run.get("updated_at") or ""),
    }


def _require_run_event(event: Any) -> None:
    if event is None:
        raise AgentRuntimeError("run_event_fence_mismatch")


def _main_chat_start_payload(
    *,
    task_id: str,
    session_id: str,
    metadata: dict[str, Any] | None,
    runtime_execution_envelope: Any | None,
    direct_tool_request: dict[str, Any] | None,
    direct_tool_requests: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "task_id": str(task_id or ""),
        "session_id": str(session_id or ""),
    }
    metadata_payload = dict(metadata) if isinstance(metadata, Mapping) else {}
    envelope = _runtime_execution_envelope_payload(
        runtime_execution_envelope,
        metadata_payload,
    )
    if metadata_payload:
        payload["metadata"] = metadata_payload
    if envelope is not None:
        payload["runtime_execution_envelope"] = envelope
    if isinstance(direct_tool_request, Mapping):
        payload["direct_tool_request"] = dict(direct_tool_request)
    requests = _direct_tool_requests_payload(direct_tool_requests)
    if requests:
        payload["direct_tool_requests"] = requests
    return payload


def _runtime_execution_envelope_payload(
    runtime_execution_envelope: Any | None,
    metadata: Mapping[str, Any],
) -> dict[str, Any] | None:
    if isinstance(runtime_execution_envelope, Mapping):
        return dict(runtime_execution_envelope)
    for key in ("runtime_execution_envelope", "yachiyo_execution_envelope"):
        envelope = metadata.get(key)
        if isinstance(envelope, Mapping):
            return dict(envelope)
    return None


def _direct_tool_requests_payload(
    direct_tool_requests: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if not isinstance(direct_tool_requests, list):
        return []
    return [dict(request) for request in direct_tool_requests if isinstance(request, Mapping)]
