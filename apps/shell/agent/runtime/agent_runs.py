"""Agent run creation helpers for the shared runtime surface."""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.config import FINAL_RUN_STATUSES
from apps.shell.agent.runtime.direct_request_policy import (
    agent_with_direct_request_approvals,
)
from apps.shell.agent.runtime.errors import (
    AgentApprovalRequired,
    AgentDirectOutcomeUnverified,
)
from apps.shell.agent.runtime.outcome_evaluator import evaluate_main_chat_outcome
from apps.shell.agent.runtime.run_group_attachments import (
    RUN_GROUP_ATTACHMENT_PAYLOAD_KEY,
    require_internal_run_group_attachment,
    validate_existing_run_group_child_attachment,
    validate_run_group_child_attachment,
)
from apps.shell.agent.runtime.tool_brokers import (
    close_owned_browser_target_best_effort,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES, RuntimePolicyCompiler
from apps.shell.yachiyo_agent.desktop_execution_policy import (
    with_daily_entrypoint_desktop_execution_policy,
)
from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
    planner_first_direct_decision_and_tool_requests,
)
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_requests_from_envelope_payload,
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class AgentRunStart:
    run: dict[str, Any]
    root_group: bool
    existing: bool = False
    lease_generation: int = 0
    lease_owner_token: str = field(default="", repr=False)
    takeover: bool = False


class RuntimeAgentRunStarter:
    """Creates Agent Run rows while preserving legacy idempotency semantics."""

    def __init__(
        self,
        *,
        get_run_group: Callable[[str], dict[str, Any]],
        get_run: Callable[[str], dict[str, Any]],
        insert_run_group: Callable[..., dict[str, Any]],
        insert_run: Callable[..., dict[str, Any]],
        run_by_client_request_id: Callable[[str], dict[str, Any] | None],
        client_request_id_from_payload: Callable[[dict[str, Any]], str],
        agent_workspace_dir: Callable[[dict[str, Any]], str],
        delete_empty_run_group: Callable[[str], bool] | None = None,
        normalize_user_goal: Callable[[Any], str] = str,
        error_type: type[Exception] = RuntimeError,
        async_execution_lease_by_client_request_id: Callable[
            [str], tuple[dict[str, Any], int, str, str] | None
        ]
        | None = None,
        try_take_over_async_execution_lease: Callable[..., dict[str, Any] | None]
        | None = None,
        renew_async_execution_lease: Callable[..., bool] | None = None,
        owns_async_execution_lease: Callable[..., bool] | None = None,
        release_async_execution_lease: Callable[..., bool] | None = None,
        bind_async_execution_lease: Callable[..., AbstractContextManager[Any]]
        | None = None,
        run_group_attachment_transaction: Callable[[], AbstractContextManager[Any]]
        | None = None,
        now_utc: Callable[[], datetime] = _utc_now,
        async_lease_seconds: float = 60.0,
        owner_token_factory: Callable[[], str] = lambda: uuid4().hex,
    ) -> None:
        self._get_run_group = get_run_group
        self._get_run = get_run
        self._insert_run_group = insert_run_group
        self._insert_run = insert_run
        self._run_by_client_request_id = run_by_client_request_id
        self._client_request_id_from_payload = client_request_id_from_payload
        self._agent_workspace_dir = agent_workspace_dir
        self._delete_empty_run_group = delete_empty_run_group
        self._normalize_user_goal = normalize_user_goal
        self._error_type = error_type
        self._async_execution_lease_by_client_request_id = (
            async_execution_lease_by_client_request_id
        )
        self._try_take_over_async_execution_lease = (
            try_take_over_async_execution_lease
        )
        self._renew_async_execution_lease = renew_async_execution_lease
        self._owns_async_execution_lease = owns_async_execution_lease
        self._release_async_execution_lease = release_async_execution_lease
        self._bind_async_execution_lease = bind_async_execution_lease
        self._run_group_attachment_transaction = run_group_attachment_transaction
        self._now_utc = now_utc
        self._async_lease_seconds = max(1.0, float(async_lease_seconds or 60.0))
        self._owner_token_factory = owner_token_factory
        self._fallback_lock = threading.RLock()

    def start_sync(
        self,
        payload: dict[str, Any],
        *,
        agent: dict[str, Any],
        lock: AbstractContextManager[Any] | None = None,
    ) -> AgentRunStart:
        return self._start_with_claim(
            payload,
            agent=agent,
            lock=lock or self._fallback_lock,
        )

    def start_async(
        self,
        payload: dict[str, Any],
        *,
        agent: dict[str, Any],
        lock: AbstractContextManager[Any] | None = None,
    ) -> AgentRunStart:
        if not self._async_lease_enabled:
            return self._start_with_claim(
                payload,
                agent=agent,
                lock=lock or self._fallback_lock,
            )
        client_request_id = self._client_request_id_from_payload(payload)
        if not client_request_id:
            return self._start_with_claim(
                payload,
                agent=agent,
                lock=lock or self._fallback_lock,
            )
        existing = self._async_execution_lease_by_client_request_id(
            client_request_id
        )
        if existing is not None:
            return self._existing_async_start(existing, payload=payload, agent=agent)
        with (lock or self._fallback_lock):
            existing = self._async_execution_lease_by_client_request_id(
                client_request_id
            )
            if existing is not None:
                return self._existing_async_start(existing, payload=payload, agent=agent)
            heartbeat_at, lease_expires_at = self._lease_window()
            owner_token = self._new_owner_token()
            try:
                return self._insert_new_run(
                    payload,
                    agent=agent,
                    client_request_id=client_request_id,
                    lease_generation=1,
                    lease_owner_token=owner_token,
                    lease_expires_at=lease_expires_at,
                    lease_heartbeat_at=heartbeat_at,
                )
            except sqlite3.IntegrityError:
                existing = self._async_execution_lease_by_client_request_id(
                    client_request_id
                )
                if existing is not None:
                    return self._existing_async_start(
                        existing,
                        payload=payload,
                        agent=agent,
                    )
                raise

    @property
    def _async_lease_enabled(self) -> bool:
        return all(
            callable(callback)
            for callback in (
                self._async_execution_lease_by_client_request_id,
                self._try_take_over_async_execution_lease,
                self._renew_async_execution_lease,
                self._owns_async_execution_lease,
                self._release_async_execution_lease,
                self._bind_async_execution_lease,
            )
        )

    @property
    def async_heartbeat_interval_seconds(self) -> float:
        return max(0.1, self._async_lease_seconds / 3.0)

    def heartbeat_async_lease(
        self,
        run_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        if not self._async_lease_enabled or not owner_token:
            return False
        heartbeat_at, lease_expires_at = self._lease_window()
        return bool(
            self._renew_async_execution_lease(
                run_id,
                generation=generation,
                owner_token=owner_token,
                heartbeat_at=heartbeat_at,
                lease_expires_at=lease_expires_at,
            )
        )

    def owns_async_lease(
        self,
        run_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        if not self._async_lease_enabled or not owner_token:
            return True
        return bool(
            self._owns_async_execution_lease(
                run_id,
                generation=generation,
                owner_token=owner_token,
            )
        )

    def release_async_lease(
        self,
        run_id: str,
        generation: int,
        owner_token: str,
    ) -> bool:
        if not self._async_lease_enabled or not owner_token:
            return True
        return bool(
            self._release_async_execution_lease(
                run_id,
                generation=generation,
                owner_token=owner_token,
            )
        )

    def execution_lease_context(
        self,
        run_id: str,
        generation: int,
        owner_token: str,
        cancellation_event: Any | None = None,
    ) -> AbstractContextManager[Any]:
        if not self._async_lease_enabled or not owner_token:
            return nullcontext()
        return self._bind_async_execution_lease(
            run_id,
            generation=generation,
            owner_token=owner_token,
            cancellation_event=cancellation_event,
        )

    def _existing_async_start(
        self,
        existing: tuple[dict[str, Any], int, str, str],
        *,
        payload: dict[str, Any],
        agent: dict[str, Any],
    ) -> AgentRunStart:
        run, generation, current_owner_token, current_expires_at = existing
        run = self._validated_existing_run(run, payload=payload, agent=agent)
        if str(run.get("status") or "") != "running":
            return AgentRunStart(run, root_group=False, existing=True)
        now = self._normalized_now()
        if not _lease_is_expired(current_expires_at, now):
            return AgentRunStart(run, root_group=False, existing=True)
        heartbeat_at, lease_expires_at = self._lease_window(now)
        next_owner_token = self._new_owner_token()
        takeover = self._try_take_over_async_execution_lease(
            str(run.get("run_id") or ""),
            expected_generation=generation,
            expected_owner_token=current_owner_token,
            expected_expires_at=current_expires_at,
            owner_token=next_owner_token,
            heartbeat_at=heartbeat_at,
            lease_expires_at=lease_expires_at,
        )
        if takeover is None:
            client_request_id = self._client_request_id_from_payload(payload)
            winner = self._async_execution_lease_by_client_request_id(
                client_request_id
            )
            if winner is not None:
                winner_run = self._validated_existing_run(
                    winner[0],
                    payload=payload,
                    agent=agent,
                )
                return AgentRunStart(winner_run, root_group=False, existing=True)
            return AgentRunStart(run, root_group=False, existing=True)
        return AgentRunStart(
            takeover,
            root_group=not bool(str(payload.get("run_group_id") or "").strip()),
            existing=False,
            lease_generation=max(0, int(generation)) + 1,
            lease_owner_token=next_owner_token,
            takeover=True,
        )

    def _normalized_now(self) -> datetime:
        value = self._now_utc()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _lease_window(self, now: datetime | None = None) -> tuple[str, str]:
        heartbeat = now or self._normalized_now()
        return (
            heartbeat.isoformat(),
            (heartbeat + timedelta(seconds=self._async_lease_seconds)).isoformat(),
        )

    def _new_owner_token(self) -> str:
        token = str(self._owner_token_factory() or "").strip()[:128]
        return token or uuid4().hex

    def _start_with_claim(
        self,
        payload: dict[str, Any],
        *,
        agent: dict[str, Any],
        lock: AbstractContextManager[Any],
    ) -> AgentRunStart:
        client_request_id = self._client_request_id_from_payload(payload)
        if client_request_id:
            existing = self._run_by_client_request_id(client_request_id)
            if existing is not None:
                return self._existing_start(existing, payload=payload, agent=agent)
        with lock:
            if client_request_id:
                existing = self._run_by_client_request_id(client_request_id)
                if existing is not None:
                    return self._existing_start(existing, payload=payload, agent=agent)
            try:
                return self._insert_new_run(
                    payload,
                    agent=agent,
                    client_request_id=client_request_id,
                )
            except sqlite3.IntegrityError:
                existing = self._run_by_client_request_id(client_request_id)
                if existing is not None:
                    return self._existing_start(existing, payload=payload, agent=agent)
                raise

    def _existing_start(
        self,
        existing: dict[str, Any],
        *,
        payload: dict[str, Any],
        agent: dict[str, Any],
    ) -> AgentRunStart:
        existing = self._validated_existing_run(existing, payload=payload, agent=agent)
        return AgentRunStart(existing, root_group=False, existing=True)

    def _validated_existing_run(
        self,
        existing: dict[str, Any],
        *,
        payload: dict[str, Any],
        agent: dict[str, Any],
    ) -> dict[str, Any]:
        expected_runnable_id = str(
            payload.get("agent_id")
            or payload.get("runnable_id")
            or agent.get("agent_id")
            or ""
        )
        expected_goal = self._normalize_user_goal(
            str(payload.get("user_goal") or payload.get("goal") or "").strip()
        )
        identity_matches = (
            str(existing.get("kind") or "") == "agent_run"
            and str(existing.get("runnable_id") or "") == expected_runnable_id
            and str(existing.get("user_goal") or "") == expected_goal
        )
        if not identity_matches:
            raise self._error_type(
                "idempotency key conflict: existing run identity does not match request"
            )
        run_group_id = str(payload.get("run_group_id") or "").strip()
        if run_group_id:
            attachment_scope = (
                self._run_group_attachment_transaction()
                if self._run_group_attachment_transaction is not None
                else nullcontext()
            )
            with attachment_scope:
                group = self._get_run_group(run_group_id)
                validate_existing_run_group_child_attachment(
                    payload.get(RUN_GROUP_ATTACHMENT_PAYLOAD_KEY),
                    group=group,
                    run_group_id=run_group_id,
                    existing_child=existing,
                    child_kind="agent_run",
                    child_runnable_id=expected_runnable_id,
                    expected_child_identity=self._client_request_id_from_payload(
                        payload
                    ),
                    get_run=self._get_run,
                    error_type=self._error_type,
                )
        return existing

    def _insert_new_run(
        self,
        payload: dict[str, Any],
        *,
        agent: dict[str, Any],
        client_request_id: str,
        lease_generation: int = 0,
        lease_owner_token: str = "",
        lease_expires_at: str = "",
        lease_heartbeat_at: str = "",
    ) -> AgentRunStart:
        user_goal = str(payload.get("user_goal") or payload.get("goal") or "").strip()
        run_group_id = str(payload.get("run_group_id") or "").strip()
        created_root_group = False
        attachment_scope = (
            self._run_group_attachment_transaction()
            if run_group_id and self._run_group_attachment_transaction is not None
            else nullcontext()
        )
        with attachment_scope:
            if run_group_id:
                attachment = require_internal_run_group_attachment(
                    payload.get(RUN_GROUP_ATTACHMENT_PAYLOAD_KEY),
                    error_type=self._error_type,
                )
                group = self._get_run_group(run_group_id)
                runnable_id = str(
                    payload.get("agent_id")
                    or payload.get("runnable_id")
                    or agent.get("agent_id")
                    or ""
                )
                validate_run_group_child_attachment(
                    attachment,
                    group=group,
                    run_group_id=run_group_id,
                    child_kind="agent_run",
                    child_runnable_id=runnable_id,
                    expected_child_identity=client_request_id,
                    get_run=self._get_run,
                    error_type=self._error_type,
                )
            else:
                group = self._insert_run_group(
                    title=f"{agent['name']}: {user_goal[:80]}",
                    source=str(payload.get("source") or "agent"),
                    workspace_dir=self._agent_workspace_dir(agent),
                )
                run_group_id = group["run_group_id"]
                created_root_group = True
            project_root_group = created_root_group and _project_root_group_requested(
                payload
            )
            try:
                insert_kwargs = {
                    "kind": "agent_run",
                    "runnable_id": str(
                        payload.get("agent_id")
                        or payload.get("runnable_id")
                        or agent.get("agent_id")
                        or ""
                    ),
                    "user_goal": user_goal,
                    "run_group_id": run_group_id,
                    "client_request_id": client_request_id,
                    "project_root_group": project_root_group,
                }
                if lease_owner_token:
                    insert_kwargs.update(
                        {
                            "async_lease_generation": max(0, int(lease_generation)),
                            "async_lease_owner_token": lease_owner_token,
                            "async_lease_expires_at": lease_expires_at,
                            "async_lease_heartbeat_at": lease_heartbeat_at,
                        }
                    )
                run = self._insert_run(
                    **insert_kwargs,
                )
            except sqlite3.IntegrityError:
                if created_root_group and self._delete_empty_run_group is not None:
                    self._delete_empty_run_group(run_group_id)
                raise
        return AgentRunStart(
            run,
            root_group=project_root_group,
            lease_generation=max(0, int(lease_generation)),
            lease_owner_token=lease_owner_token,
        )


def _project_root_group_requested(payload: dict[str, Any]) -> bool:
    value = payload.get("project_root_group")
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _lease_is_expired(expires_at: str, now: datetime) -> bool:
    text = str(expires_at or "").strip()
    if not text:
        return True
    try:
        expires = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    return expires.astimezone(timezone.utc) <= now


class RuntimeAgentRunExecutor:
    """Executes a prepared Agent Run and projects terminal/approval outcomes."""

    def __init__(
        self,
        *,
        preparer: Any,
        continue_custom_api_agent: Callable[..., str],
        agent_run_outcomes: Any,
        approval_pause: Any,
        list_run_events: Callable[..., Any] | None = None,
    ) -> None:
        self._preparer = preparer
        self._continue_custom_api_agent = continue_custom_api_agent
        self._agent_run_outcomes = agent_run_outcomes
        self._approval_pause = approval_pause
        self._list_run_events = list_run_events

    def execute(
        self,
        run_id: str,
        agent: dict[str, Any],
        user_goal: str,
        upstream: str = "",
        run_group_id: str = "",
        workflow_run_id: str = "",
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
        runtime_execution_metadata: dict[str, Any] | None = None,
        daily_desktop_planning_context: str | None = None,
    ) -> dict[str, Any]:
        preparation = None
        timeline: list[dict[str, Any]] = []
        artifacts: list[dict[str, Any]] = []
        preserve_browser_target = False
        try:
            preparation = self._preparer.prepare(
                run_id,
                agent,
                user_goal,
                upstream,
                run_group_id=run_group_id,
                workflow_run_id=workflow_run_id,
                **({"runtime_execution_envelope": runtime_execution_envelope}
                   if runtime_execution_envelope and supports_keyword(
                       self._preparer.prepare, "runtime_execution_envelope"
                   ) else {}),
            )
            timeline = preparation.timeline
            artifacts = preparation.artifacts
            self._preparer.write_context_artifact(run_id, preparation)
            runtime_execution_metadata = _agent_run_runtime_execution_metadata(
                agent,
                runtime_execution_metadata,
            )
            preparation_goal_contract = getattr(
                preparation,
                "goal_contract",
                {},
            )
            if preparation_goal_contract and not _runtime_execution_envelope_declares_goal_contract(
                runtime_execution_envelope
            ):
                # A Runtime execution envelope is the immutable output of the
                # entrypoint planner.  Recompiling the same goal during Agent
                # preparation can legitimately produce a different contract
                # when its tool policy is broader, but injecting that second
                # contract makes the fail-closed resolver reject the run before
                # any tool executes.  Keep the envelope as the single authority;
                # explicit conflicting caller metadata is still validated and
                # rejected by runtime_goal_contract.
                runtime_execution_metadata = {
                    **dict(runtime_execution_metadata or {}),
                    "goal_contract": dict(preparation_goal_contract),
                }
            original_goal_kwargs = (
                {"original_goal": user_goal}
                if supports_keyword(
                    self._continue_custom_api_agent,
                    "original_goal",
                )
                else {}
            )
            # Only events appended after preparation belong to this executor
            # turn.  A pre-existing/public timeline entry must never become
            # fresh completion evidence merely because the persistence reader
            # is one transaction behind.
            authoritative_tail_start = len(timeline)
            result = self._continue_custom_api_agent(
                agent,
                preparation.context,
                preparation.broker,
                timeline,
                artifacts,
                daily_desktop_planning_context=(
                    daily_desktop_planning_context
                    if daily_desktop_planning_context is not None
                    else (
                        _runtime_planner_entrypoint_context(agent, user_goal)
                        if (
                            agent.get("_daily_desktop_policy_overlay") is True
                            or agent.get("_runtime_planner_entrypoint") is True
                        )
                        else ""
                    )
                ),
                direct_tool_request=direct_tool_request,
                direct_tool_requests=direct_tool_requests,
                runtime_execution_envelope=runtime_execution_envelope,
                runtime_execution_metadata=runtime_execution_metadata,
                run_id=run_id,
                **original_goal_kwargs,
            )
            self._assert_terminal_outcome_allows_completion(
                run_id,
                timeline=timeline,
                authoritative_tail_start=authoritative_tail_start,
            )
            return self._agent_run_outcomes.completed(
                run_id,
                result,
                timeline=timeline,
                artifacts=artifacts,
            )
        except AgentApprovalRequired as exc:
            preserve_browser_target = True
            return self._approval_pause.project_tool_required(
                run_id,
                pending_approval=exc.pending_approval,
                timeline=timeline,
                artifacts=artifacts,
            )
        except Exception as exc:
            return self._agent_run_outcomes.failed(
                run_id,
                exc,
                timeline=timeline,
                artifacts=artifacts,
            )
        finally:
            if preparation is not None and not preserve_browser_target:
                close_owned_browser_target_best_effort(preparation.broker)

    def _assert_terminal_outcome_allows_completion(
        self,
        run_id: str,
        *,
        timeline: list[dict[str, Any]],
        authoritative_tail_start: int = 0,
    ) -> None:
        events = self._terminal_outcome_events(
            run_id,
            fallback=timeline,
            authoritative_tail_start=authoritative_tail_start,
        )
        outcome = evaluate_main_chat_outcome({"run_id": run_id}, events)
        if outcome.allows_completion:
            return
        raise AgentDirectOutcomeUnverified(
            outcome.message or "任务执行结果尚未通过验证。",
            reason=outcome.reason or "outcome_verification_failed",
        )

    def _terminal_outcome_events(
        self,
        run_id: str,
        *,
        fallback: list[dict[str, Any]],
        authoritative_tail_start: int = 0,
    ) -> list[dict[str, Any]]:
        if self._list_run_events is None:
            return list(fallback)
        events: list[dict[str, Any]] = []
        after_sequence = 0
        for _page_index in range(50):
            try:
                page = self._list_run_events(
                    run_id,
                    after_sequence=after_sequence,
                    limit=500,
                    include_internal=True,
                )
            except Exception as exc:
                raise AgentDirectOutcomeUnverified(
                    "任务执行记录暂时无法验证，请稍后重试。",
                    reason="outcome_event_history_unavailable",
                ) from exc
            if not isinstance(page, dict) or not isinstance(page.get("events"), list):
                raise AgentDirectOutcomeUnverified(
                    "任务执行记录不完整，无法安全确认完成。",
                    reason="outcome_event_history_incomplete",
                )
            page_events = page["events"]
            if any(not isinstance(event, dict) for event in page_events):
                raise AgentDirectOutcomeUnverified(
                    "任务执行记录不完整，无法安全确认完成。",
                    reason="outcome_event_history_incomplete",
                )
            events.extend(dict(event) for event in page_events)
            if not page.get("has_more"):
                return _merge_authoritative_runtime_tail(
                    events,
                    fallback,
                    start_index=authoritative_tail_start,
                    run_id=run_id,
                )
            next_after_sequence = int(page.get("next_after_sequence") or 0)
            if next_after_sequence <= after_sequence:
                break
            after_sequence = next_after_sequence
        raise AgentDirectOutcomeUnverified(
            "任务执行记录过长或分页异常，无法安全确认完成。",
            reason="outcome_event_history_incomplete",
        )


def _merge_authoritative_runtime_tail(
    persisted: list[dict[str, Any]],
    runtime_timeline: list[dict[str, Any]],
    *,
    start_index: int,
    run_id: str,
) -> list[dict[str, Any]]:
    """Merge the current executor turn that may be invisible until commit."""

    merged = [dict(event) for event in persisted if isinstance(event, dict)]
    seen: set[tuple[Any, ...]] = set()
    for event in merged:
        seen.update(_runtime_event_identities(event, run_id=run_id))
    for raw_event in runtime_timeline[max(0, int(start_index or 0)) :]:
        if not isinstance(raw_event, dict):
            continue
        event = dict(raw_event)
        identities = _runtime_event_identities(event, run_id=run_id)
        if identities and seen.intersection(identities):
            continue
        merged.append(event)
        seen.update(identities)
    return merged


def _runtime_event_identities(
    event: dict[str, Any],
    *,
    run_id: str,
) -> set[tuple[Any, ...]]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    event_type = str(event.get("event_type") or event.get("event") or "").strip()
    event_run_id = str(event.get("run_id") or payload.get("run_id") or run_id or "").strip()
    identities: set[tuple[Any, ...]] = set()
    event_id = str(event.get("event_id") or payload.get("event_id") or "").strip()
    if event_id:
        identities.add(("event_id", event_run_id, event_id))
    sequence = event.get("sequence")
    if isinstance(sequence, int):
        identities.add(("sequence", event_run_id, sequence))
    tool_call_id = str(
        event.get("tool_call_id") or payload.get("tool_call_id") or ""
    ).strip()
    request_id = str(event.get("request_id") or payload.get("request_id") or "").strip()
    contract_id = str(
        event.get("contract_id") or payload.get("contract_id") or ""
    ).strip()
    tool = str(
        event.get("tool")
        or event.get("detail")
        or payload.get("tool")
        or payload.get("detail")
        or ""
    ).strip()
    step_id = str(
        event.get("step_id")
        or payload.get("step_id")
        or event.get("planner_step_id")
        or payload.get("planner_step_id")
        or ""
    ).strip()
    plan_id = str(event.get("plan_id") or payload.get("plan_id") or "").strip()
    if any((tool_call_id, request_id, contract_id, tool, step_id, plan_id)):
        identities.add(
            (
                "runtime_identity",
                event_run_id,
                event_type,
                tool_call_id,
                request_id,
                contract_id,
                tool,
                step_id,
                plan_id,
            )
        )
    return identities


class RuntimeAgentRunCoordinator:
    """Coordinates synchronous Agent Run validation, creation, and execution."""

    def __init__(
        self,
        *,
        get_agent_private: Callable[[str], dict[str, Any]],
        validate_agent_run_readiness: Callable[[dict[str, Any]], None],
        starter: RuntimeAgentRunStarter,
        execute_agent_run: Callable[..., dict[str, Any]],
        project_agent_run_group_if_root: Callable[[dict[str, Any]], dict[str, Any]],
        lock: AbstractContextManager[Any],
        error_type: type[Exception],
    ) -> None:
        self._get_agent_private = get_agent_private
        self._validate_agent_run_readiness = validate_agent_run_readiness
        self._starter = starter
        self._execute_agent_run = execute_agent_run
        self._project_agent_run_group_if_root = project_agent_run_group_if_root
        self._lock = lock
        self._error_type = error_type

    def create_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id") or payload.get("runnable_id") or "")
        user_goal = str(payload.get("user_goal") or payload.get("goal") or "").strip()
        if not agent_id:
            raise self._error_type("缺少 agent_id")
        if not user_goal:
            raise self._error_type("运行目标不能为空")
        agent = self._agent_for_payload(payload, agent_id)
        self._validate_agent_run_readiness(agent)
        start = self._starter.start_sync(payload, agent=agent, lock=self._lock)
        if start.existing:
            return start.run
        run = start.run
        execute_kwargs = {
            "upstream": str(payload.get("upstream") or ""),
            "run_group_id": str(run.get("run_group_id") or ""),
            **_agent_run_execution_options(payload),
        }
        workflow_run_id = str(payload.get("workflow_run_id") or "").strip()
        if workflow_run_id:
            execute_kwargs["workflow_run_id"] = workflow_run_id
        result = self._execute_agent_run(
            run["run_id"],
            agent,
            user_goal,
            **execute_kwargs,
        )
        if start.root_group:
            result = self._project_agent_run_group_if_root(result)
        return result

    def _agent_for_payload(self, payload: dict[str, Any], agent_id: str) -> dict[str, Any]:
        override = payload.get("agent_override")
        if not isinstance(override, dict):
            agent = self._get_agent_private(agent_id)
            return _with_entrypoint_runtime_planner(agent, payload)
        override_agent_id = str(override.get("agent_id") or override.get("id") or agent_id)
        if override_agent_id != agent_id:
            raise self._error_type("agent_override 与 agent_id 不一致")
        return _with_entrypoint_runtime_planner({**override, "agent_id": agent_id}, payload)


class RuntimeAgentRunAsyncCoordinator:
    """Starts Agent Runs for background execution while preserving return shape."""

    def __init__(
        self,
        *,
        get_agent_private: Callable[[str], dict[str, Any]],
        validate_agent_run_readiness: Callable[[dict[str, Any]], None],
        starter: RuntimeAgentRunStarter,
        execute_agent_run: Callable[..., dict[str, Any]],
        project_agent_run_group_if_root: Callable[[dict[str, Any]], dict[str, Any]],
        resolve_runnable: Callable[..., dict[str, Any] | None],
        get_run: Callable[[str], dict[str, Any]],
        project_agent_run_failure: Callable[..., dict[str, Any]],
        redact_error: Callable[[Any], str],
        error_type: type[Exception],
        lock: AbstractContextManager[Any] | None = None,
        thread_factory: Callable[..., Any] = threading.Thread,
        heartbeat_thread_factory: Callable[..., Any] = threading.Thread,
        heartbeat_interval_seconds: float | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._get_agent_private = get_agent_private
        self._validate_agent_run_readiness = validate_agent_run_readiness
        self._starter = starter
        self._execute_agent_run = execute_agent_run
        self._project_agent_run_group_if_root = project_agent_run_group_if_root
        self._resolve_runnable = resolve_runnable
        self._get_run = get_run
        self._project_agent_run_failure = project_agent_run_failure
        self._redact_error = redact_error
        self._error_type = error_type
        self._lock = lock or threading.RLock()
        self._thread_factory = thread_factory
        self._heartbeat_thread_factory = heartbeat_thread_factory
        self._heartbeat_interval_seconds = max(
            0.01,
            float(
                heartbeat_interval_seconds
                if heartbeat_interval_seconds is not None
                else getattr(starter, "async_heartbeat_interval_seconds", 20.0)
            ),
        )
        self._logger = logger or logging.getLogger(__name__)

    def create_async(
        self,
        payload: dict[str, Any],
        *,
        on_complete: Callable[[dict[str, Any]], None] | None = None,
        deferred_execution_start_sink: Callable[[Callable[[], None]], None]
        | None = None,
    ) -> dict[str, Any]:
        agent_id = str(payload.get("agent_id") or payload.get("runnable_id") or "")
        user_goal = str(payload.get("user_goal") or payload.get("goal") or "").strip()
        if not agent_id:
            raise self._error_type("缺少 agent_id")
        if not user_goal:
            raise self._error_type("运行目标不能为空")
        agent = self._agent_for_payload(payload, agent_id)
        self._validate_agent_run_readiness(agent)
        start = self._starter.start_async(payload, agent=agent, lock=self._lock)
        run = start.run
        runnable = self._resolve_runnable(runnable_id=agent_id)
        if start.existing:
            return {
                **run,
                "runnable": runnable,
                "agent_run_id": run["run_id"],
            }
        if start.takeover:
            # An expired lease proves only that the previous worker stopped
            # heartbeating.  It does not prove which external side effects it
            # already committed.  Re-entering the ordinary executor from step
            # zero can therefore duplicate messages, file writes or desktop
            # actions.  Until a durable execution checkpoint exists, a new
            # owner must fail closed and preserve the prior evidence instead
            # of guessing where to resume.
            resume_error = (
                "async_execution_resume_checkpoint_required: "
                "expired execution cannot be replayed safely"
            )
            lease_context = self._starter.execution_lease_context(
                run["run_id"],
                start.lease_generation,
                start.lease_owner_token,
            )
            with lease_context:
                failed_run = self._project_failure_from_fresh_run(
                    run["run_id"],
                    resume_error,
                )
            failed_result = {
                **failed_run,
                "runnable": runnable,
                "agent_run_id": run["run_id"],
            }
            if not _is_terminal_run(failed_run):
                return failed_result
            released = self._starter.release_async_lease(
                run["run_id"],
                start.lease_generation,
                start.lease_owner_token,
            )
            if released and on_complete:
                on_complete(failed_result)
            return failed_result
        result = {
            **run,
            "status": "processing",
            "runnable": runnable,
            "agent_run_id": run["run_id"],
        }
        lease_lost = threading.Event()

        def record_failure(exc: Exception) -> str:
            safe_error = self._redact_error(exc)
            if start.lease_owner_token and lease_lost.is_set():
                return safe_error
            if start.lease_owner_token and not self._starter.owns_async_lease(
                run["run_id"],
                start.lease_generation,
                start.lease_owner_token,
            ):
                return safe_error
            self._logger.error("异步 Agent Run 执行失败: %s", exc, exc_info=True)
            try:
                lease_context = (
                    self._starter.execution_lease_context(
                        run["run_id"],
                        start.lease_generation,
                        start.lease_owner_token,
                        cancellation_event=lease_lost,
                    )
                    if start.lease_owner_token
                    else nullcontext()
                )
                with lease_context:
                    projected = self._project_failure_from_fresh_run(
                        run["run_id"],
                        safe_error,
                    )
            except self._error_type:
                if start.lease_owner_token:
                    return safe_error
                raise
            if not _is_terminal_run(projected):
                return safe_error
            if start.lease_owner_token and not self._starter.release_async_lease(
                run["run_id"],
                start.lease_generation,
                start.lease_owner_token,
            ):
                return safe_error
            if on_complete:
                on_complete(projected)
            return safe_error

        def execute_in_background() -> None:
            heartbeat_stop = threading.Event()
            heartbeat_thread: Any | None = None
            heartbeat_started = False

            def maintain_lease() -> None:
                while not heartbeat_stop.wait(self._heartbeat_interval_seconds):
                    try:
                        renewed = self._starter.heartbeat_async_lease(
                            run["run_id"],
                            start.lease_generation,
                            start.lease_owner_token,
                        )
                        if not renewed:
                            if not self._starter.owns_async_lease(
                                run["run_id"],
                                start.lease_generation,
                                start.lease_owner_token,
                            ):
                                lease_lost.set()
                            return
                    except Exception as exc:
                        lease_lost.set()
                        self._logger.warning(
                            "异步 Agent Run lease heartbeat 失败: %s",
                            self._redact_error(exc),
                        )
                        return

            try:
                if start.lease_owner_token:
                    try:
                        initial_heartbeat = self._starter.heartbeat_async_lease(
                            run["run_id"],
                            start.lease_generation,
                            start.lease_owner_token,
                        )
                    except Exception as exc:
                        lease_lost.set()
                        self._logger.warning(
                            "异步 Agent Run initial lease heartbeat 失败: %s",
                            self._redact_error(exc),
                        )
                        return
                    if not initial_heartbeat:
                        lease_lost.set()
                        return
                    heartbeat_thread = self._heartbeat_thread_factory(
                        target=maintain_lease,
                        name=f"agent-run-heartbeat-{run['run_id'][:8]}",
                        daemon=True,
                    )
                    heartbeat_thread.start()
                    heartbeat_started = True
                execute_kwargs = {
                    "upstream": str(payload.get("upstream") or ""),
                    "run_group_id": str(run.get("run_group_id") or ""),
                    **_agent_run_execution_options(payload),
                }
                workflow_run_id = str(payload.get("workflow_run_id") or "").strip()
                if workflow_run_id:
                    execute_kwargs["workflow_run_id"] = workflow_run_id
                lease_context = (
                    self._starter.execution_lease_context(
                        run["run_id"],
                        start.lease_generation,
                        start.lease_owner_token,
                        cancellation_event=lease_lost,
                    )
                    if start.lease_owner_token
                    else nullcontext()
                )
                with lease_context:
                    exec_result = self._execute_agent_run(
                        run["run_id"],
                        agent,
                        user_goal,
                        **execute_kwargs,
                    )
                if start.lease_owner_token:
                    if lease_lost.is_set() or not self._starter.owns_async_lease(
                        run["run_id"],
                        start.lease_generation,
                        start.lease_owner_token,
                    ):
                        return
                if start.root_group:
                    exec_result = self._project_agent_run_group_if_root(exec_result)
                if start.lease_owner_token and not self._starter.release_async_lease(
                    run["run_id"],
                    start.lease_generation,
                    start.lease_owner_token,
                ):
                    return
                if on_complete:
                    on_complete(exec_result)
            except Exception as exc:
                record_failure(exc)
            finally:
                heartbeat_stop.set()
                if heartbeat_started and heartbeat_thread is not None:
                    heartbeat_thread.join(
                        timeout=max(1.0, self._heartbeat_interval_seconds * 2.0)
                    )

        thread = self._thread_factory(
            target=execute_in_background,
            name=f"agent-run-{run['run_id'][:8]}",
            daemon=True,
        )
        activated = False

        def activate() -> None:
            nonlocal activated
            if activated:
                return
            # A thread start failure cannot prove that the target never ran.
            # Fence retries before touching the external world a second time.
            activated = True
            try:
                thread.start()
            except Exception as exc:
                safe_error = record_failure(exc)
                raise self._error_type(safe_error) from exc

        if deferred_execution_start_sink is not None:
            deferred_execution_start_sink(activate)
        else:
            activate()
        return result

    def _project_failure_from_fresh_run(
        self,
        run_id: str,
        safe_error: str,
    ) -> dict[str, Any]:
        current = self._get_run(run_id)
        projected = self._project_agent_run_failure(
            run_id,
            self._error_type(safe_error),
            timeline=[
                dict(event)
                for event in current.get("timeline") or []
                if isinstance(event, dict)
            ],
            artifacts=[
                dict(item)
                for item in current.get("artifacts") or []
                if isinstance(item, dict)
            ],
        )
        if not isinstance(projected, dict):
            raise self._error_type("agent_run_failure_projection_missing")
        return projected

    def _agent_for_payload(self, payload: dict[str, Any], agent_id: str) -> dict[str, Any]:
        override = payload.get("agent_override")
        if not isinstance(override, dict):
            agent = self._get_agent_private(agent_id)
            return _with_entrypoint_runtime_planner(agent, payload)
        override_agent_id = str(override.get("agent_id") or override.get("id") or agent_id)
        if override_agent_id != agent_id:
            raise self._error_type("agent_override 与 agent_id 不一致")
        return _with_entrypoint_runtime_planner({**override, "agent_id": agent_id}, payload)


def _is_terminal_run(run: dict[str, Any]) -> bool:
    return str(run.get("status") or "").strip().lower() in FINAL_RUN_STATUSES


def _runtime_planner_entrypoint_context(agent: dict[str, Any], user_goal: str) -> str:
    clean_goal = str(user_goal or "").strip()
    if (
        agent.get("_daily_desktop_policy_overlay") is True
        and agent.get("_runtime_planner_entrypoint") is not True
    ):
        return clean_goal
    if agent.get("_runtime_planner_entrypoint") is True:
        context = str(agent.get("_runtime_planner_entrypoint_context") or "").strip()
        candidate = context or clean_goal
        policy = agent.get("tool_policy") if isinstance(agent.get("tool_policy"), dict) else {}
        allowed = _string_list(policy.get("allowed_tools"))
        return candidate if _runtime_planner_entrypoint_should_execute(candidate, allowed) else ""
    return clean_goal


def _agent_run_runtime_execution_metadata(
    agent: dict[str, Any],
    runtime_execution_metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if agent.get("_daily_desktop_policy_overlay") is not True:
        return runtime_execution_metadata
    return with_daily_entrypoint_desktop_execution_policy(
        runtime_execution_metadata,
        surface="agent_run",
    )


def _runtime_execution_envelope_declares_goal_contract(
    envelope: dict[str, Any] | None,
) -> bool:
    """Return whether the planner envelope already owns completion semantics."""

    if not isinstance(envelope, dict):
        return False
    containers = [envelope]
    for key in ("task_core", "plan", "runtime_plan"):
        nested = envelope.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
            nested_task_core = nested.get("task_core")
            if isinstance(nested_task_core, dict):
                containers.append(nested_task_core)
    return any(isinstance(container.get("goal_contract"), dict) for container in containers)


def _runtime_planner_entrypoint_should_execute(context: str, allowed_tools: list[str]) -> bool:
    clean_context = str(context or "").strip()
    if not clean_context:
        return False
    _decision, direct_requests = planner_first_direct_decision_and_tool_requests(
        clean_context,
        allowed_tools,
    )
    return bool(direct_requests)


def _with_entrypoint_runtime_planner(agent: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    agent = _with_daily_desktop_policy_overlay(agent, payload)
    execution_requests = _payload_execution_tool_requests(payload)
    agent = agent_with_direct_request_approvals(
        agent,
        execution_requests,
    )
    if not payload.get("runtime_planner_entrypoint"):
        return agent
    user_goal = str(payload.get("user_goal") or payload.get("goal") or "").strip()
    planning_goal = _payload_daily_desktop_planning_context(payload) or user_goal
    if (
        _looks_like_daily_desktop_howto_question(user_goal)
        or _looks_like_daily_desktop_howto_question(planning_goal)
    ):
        return agent
    if _payload_has_runtime_execution_plan(payload):
        return {
            **agent,
            "_runtime_planner_entrypoint": True,
            "_runtime_planner_entrypoint_context": planning_goal,
        }
    policy = agent.get("tool_policy") if isinstance(agent.get("tool_policy"), dict) else {}
    allowed = _string_list(policy.get("allowed_tools"))
    _decision, direct_requests = planner_first_direct_decision_and_tool_requests(
        planning_goal,
        allowed,
    )
    if not direct_requests:
        overlay_selection = _daily_desktop_policy_overlay_selection(planning_goal)
        if not overlay_selection:
            return agent
        agent = _agent_with_daily_desktop_policy_overlay(agent)
    return {
        **agent,
        "_runtime_planner_entrypoint": True,
        "_runtime_planner_entrypoint_context": planning_goal,
    }


def _with_daily_desktop_policy_overlay(agent: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    if not payload.get("daily_desktop_policy_overlay"):
        return agent
    user_goal = str(payload.get("user_goal") or payload.get("goal") or "").strip()
    planning_goal = _payload_daily_desktop_planning_context(payload) or user_goal
    if (
        _looks_like_daily_desktop_howto_question(user_goal)
        or _looks_like_daily_desktop_howto_question(planning_goal)
    ):
        return agent
    direct_requests = _payload_direct_tool_requests(payload)
    if not direct_requests and not _payload_has_runtime_execution_plan(payload):
        _decision, direct_requests = planner_first_direct_decision_and_tool_requests(
            planning_goal,
            list(DAILY_DESKTOP_TOOL_NAMES),
        )
        if not direct_requests:
            return agent
    return _agent_with_daily_desktop_policy_overlay(agent)


def _daily_desktop_policy_overlay_selection(planning_goal: str) -> bool:
    try:
        decision, direct_requests = planner_first_direct_decision_and_tool_requests(
            planning_goal,
            list(DAILY_DESKTOP_TOOL_NAMES),
        )
    except Exception:
        return False
    if not direct_requests:
        return False
    return _decision_supports_daily_desktop_policy_overlay(decision)


def _decision_supports_daily_desktop_policy_overlay(decision: Any) -> bool:
    intent = getattr(decision, "selected_intent", None)
    kind = str(getattr(intent, "kind", "") or "").strip()
    return kind in {
        "desktop_operation",
        "media_playback",
        "system_control",
        "clipboard_operation",
        "web_research",
        "information_capture",
        "communication",
        "schedule",
    }


def _agent_with_daily_desktop_policy_overlay(agent: dict[str, Any]) -> dict[str, Any]:
    policy = agent.get("tool_policy") if isinstance(agent.get("tool_policy"), dict) else {}
    allowed = _string_list(policy.get("allowed_tools"))
    approval_required = _daily_desktop_overlay_approval_required(policy)
    return {
        **agent,
        "_daily_desktop_policy_overlay": True,
        "tool_policy": {
            **policy,
            "allowed_tools": _unique_tools([*allowed, *DAILY_DESKTOP_TOOL_NAMES]),
            "approval_required": approval_required,
        },
    }


def _daily_desktop_overlay_approval_required(policy: dict[str, Any]) -> dict[str, Any]:
    approval_required = (
        dict(policy.get("approval_required"))
        if isinstance(policy.get("approval_required"), dict)
        else {}
    )
    default_approval = RuntimePolicyCompiler.default_tool_policy("custom")[
        "approval_required"
    ]
    for tool in DAILY_DESKTOP_TOOL_NAMES:
        if default_approval.get(tool):
            approval_required.setdefault(tool, True)
    return approval_required


def _payload_daily_desktop_planning_context(payload: dict[str, Any]) -> str:
    return str(payload.get("daily_desktop_planning_context") or "").strip()


def _agent_run_execution_options(payload: dict[str, Any]) -> dict[str, Any]:
    options: dict[str, Any] = {}
    direct_tool_request = _normalized_payload_direct_tool_request(
        payload.get("direct_tool_request"),
    )
    if direct_tool_request:
        options["direct_tool_request"] = direct_tool_request
    direct_tool_requests = _payload_direct_tool_requests(payload)
    if direct_tool_requests:
        options["direct_tool_requests"] = direct_tool_requests
    if isinstance(payload.get("runtime_execution_envelope"), dict):
        options["runtime_execution_envelope"] = dict(payload["runtime_execution_envelope"])
    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        options["runtime_execution_metadata"] = dict(metadata)
    if "daily_desktop_planning_context" in payload:
        options["daily_desktop_planning_context"] = str(
            payload.get("daily_desktop_planning_context") or ""
        )
    return options


def _payload_direct_tool_requests(payload: dict[str, Any]) -> list[dict[str, Any]]:
    value = payload.get("direct_tool_requests")
    raw_items: list[Any] = list(value) if isinstance(value, list) else []
    single = payload.get("direct_tool_request")
    if isinstance(single, dict):
        raw_items.insert(0, single)
    requests: list[dict[str, Any]] = []
    for item in raw_items:
        request = _normalized_payload_direct_tool_request(item)
        if request:
            requests.append(request)
    return requests


def _payload_execution_tool_requests(payload: dict[str, Any]) -> list[dict[str, Any]]:
    direct_requests = _payload_direct_tool_requests(payload)
    if direct_requests:
        return direct_requests
    envelope = payload.get("runtime_execution_envelope")
    return runtime_execution_requests_from_envelope_payload(envelope)


def _payload_has_runtime_execution_plan(payload: dict[str, Any]) -> bool:
    envelope = payload.get("runtime_execution_envelope")
    if not isinstance(envelope, dict):
        return False
    requests = envelope.get("requests")
    return isinstance(requests, list) and bool(requests)


def _normalized_payload_direct_tool_request(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    tool_name = str(value.get("tool") or value.get("tool_name") or "").strip()
    if not tool_name:
        return None
    request_input = value.get("input") if isinstance(value.get("input"), dict) else {}
    return {**value, "tool": tool_name, "input": dict(request_input)}


def _looks_like_daily_desktop_howto_question(text: str) -> bool:
    lowered = str(text or "").strip().lower()
    if not lowered:
        return False
    return (
        lowered.startswith(("怎么", "如何", "怎样"))
        or "怎么用" in lowered
        or "如何用" in lowered
        or "how to " in lowered
        or "how do i " in lowered
        or "how can i " in lowered
    )


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _unique_tools(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result
