"""Deterministic terminal outcome evaluation for main-chat Agent runs.

The evaluator deliberately trusts runtime facts only. Model prose is not
evidence that a desktop side effect succeeded.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from apps.shell.agent.runtime.app_aliases import APP_ALIASES, compact_app_alias
from apps.shell.agent.runtime.dispatch_semantics import (
    is_semantic_safe_key,
    is_semantic_safe_shortcut,
)
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_contract import GoalCoordinator
from apps.shell.agent.runtime.goal_runtime import (
    runtime_goal_assessment,
    runtime_goal_contract,
)
from apps.shell.agent.runtime.tool_outcomes import (
    is_permission_diagnostic_tool,
    is_successful_permission_diagnostic_result,
)

_DESKTOP_TOOL_PREFIXES = (
    "app.",
    "browser.",
    "calendar.",
    "clipboard.",
    "desktop.",
    "media.",
    "notes.",
    "reminders.",
    "screen.",
    "system.",
)
_FAILED_TOOL_EVENT_TYPES = {
    "agent.desktop.intent_unavailable",
    "agent.desktop.intent_unverified",
    "agent.tool.denied",
    "agent.tool.failed",
    "agent.tool.skipped",
    "tool.cancelled",
    "tool.denied",
    "tool.failed",
    "tool.rejected",
    "tool.skipped",
}
_FAILED_STATUSES = {
    "blocked",
    "cancelled",
    "canceled",
    "denied",
    "error",
    "failed",
    "permission_required",
    "rejected",
    "skipped",
    "unavailable",
    "verification_failed",
}
_TERMINAL_DESKTOP_EVENT_TYPES = {
    *_FAILED_TOOL_EVENT_TYPES,
    "agent.desktop.intent_completed",
    "agent.desktop.permission_recovery",
    "agent.tool.call",
    "tool.completed",
    "tool.succeeded",
}
_VERIFICATION_TOOLS = {
    "desktop.active_window",
    "desktop.read_ui",
    "desktop.running_apps",
    "desktop.ui_elements",
    "desktop.verify",
}
_NON_ACTION_LIFECYCLE_EVENT_TYPES = {
    "agent.desktop.intent_planned",
    "agent.post_action_verification.enqueued",
    "agent.post_action_verification.satisfied",
    "agent.run.resumed",
    "agent.tool.approval_approved",
    "agent.tool.approval_cancelled",
    "agent.tool.approval_required",
    "agent.tool.approval_rejected",
    "agent.tool.approval_timeout",
    "agent.tool.input_resolved",
    "agent.tool.outcome",
    "agent.tool.foreground_session_notice",
    "agent.tool.policy_decision",
    "agent.tool.requested",
    "agent.tool.started",
    "tool.approval_required",
    "tool.input_resolved",
    "tool.policy_decision",
    "tool.requested",
    "tool.started",
}
_POSTCONDITION_ACTION_TOOLS = {
    "app.focus",
    "app.focus_window",
    "app.hide",
    "app.minimize",
    "app.open",
    "app.show",
    "desktop.open_app",
}
_DISPATCH_RECEIPT_TOOLS = {
    "desktop.close_window",
    "desktop.hide_app",
    "desktop.minimize_window",
    "desktop.quit_app",
    "desktop.safe_shortcut",
}
_POSTCONDITION_TRUE_KEYS = {
    "focus_verified",
    "foreground_ready",
    "launch_verified",
    "playback_ok",
    "postcondition_verified",
    "quit_verified",
    "target_reached",
    "target_visible",
    "verified",
}
_CONTENT_POSTCONDITION_KEYS = {
    "content_match_verified",
    "content_verified",
    "content_visible_verified",
    "draft_content_verified",
    "typed_content_verified",
}
_POSTCONDITION_FALSE_KEYS = {
    *_CONTENT_POSTCONDITION_KEYS,
    "focus_verified",
    "foreground_ready",
    "launch_verified",
    "postcondition_verified",
    "target_reached",
    "target_visible",
    "verified",
}
_CONTENT_MUTATION_TOOLS = {
    "app.focus_and_safe_type_text",
    "app.open_and_safe_type_text",
    "desktop.safe_type_text",
    "desktop.type",
    "desktop.type_into_ui_element",
    "desktop.type_text",
}
_CONTENT_MUTATION_RUNTIME_ROLES = {"draft_message", "type_ui"}
_CONTAINER_PREPARATION_ACTIONS = {"new_document", "new_note", "new_task"}
_COPY_SAFE_SHORTCUT_ACTIONS = {"copy", "copy_current_page_link"}
_APP_MANAGEMENT_POSTCONDITIONS = {
    "app.focus_window": {
        "status_key": "focus_status",
        "success_statuses": {"focused"},
        "verified_keys": {"focus_verified"},
        "target_input_key": "title_contains",
        "observed_keys": {"title", "window_title"},
    },
    "app.show": {
        "status_key": "show_status",
        "success_statuses": {"launched", "shown"},
        "verified_keys": {"show_verified"},
    },
    "app.hide": {
        "status_key": "hide_status",
        "success_statuses": {"hidden"},
        "verified_keys": {"hide_verified"},
    },
    "app.minimize": {
        "status_key": "minimize_status",
        "success_statuses": {"minimized"},
        "verified_keys": {"minimize_verified", "minimized_verified"},
    },
}
_UNVERIFIED_APP_MANAGEMENT_STATUSES = {
    "no_matching_window",
    "no_windows",
    "not_found",
    "not_running",
    "unknown",
    "unverified",
}
_ATTEMPT_ID_KEYS = (
    "attempt_id",
    "execution_attempt_id",
    "replan_id",
    "replan_request_id",
)
_AGGREGATE_STEP_INHERITED_SCOPE_KEYS = (
    "run_id",
    "decision_id",
    "plan_id",
    *_ATTEMPT_ID_KEYS,
)
_AGGREGATE_STEP_NESTED_FACT_KEYS = (
    "result",
    "output_preview",
    "data",
    "input",
    "input_preview",
    "metadata",
)
_AGGREGATE_ACTION_COPY_KEY = "_aggregate_action_copy"
_SCOPED_PERMISSION_TARGET_TOOL_PREFIXES = {
    # Chrome CDP readiness is a browser-control capability.  A cached global
    # preflight may surface it while a non-browser desktop action is planned;
    # it must not be attributed to that action merely because the legacy
    # projection copied the first planned tool into ``affected_tools``.
    "chrome_cdp": ("browser.",),
    "music_app": ("media.apple_music",),
    "screen_capture_probe_failed": ("screen.capture",),
    "screen_recording": ("screen.capture",),
}
_APPLE_MUSIC_ALIAS_SEARCH_CALL_ID = re.compile(
    r"apple-music-alias-search-(?P<iteration>0|[1-9][0-9]*)"
    r"(?:-(?P<scope>[0-9a-f]{12}))?"
)
_APPLE_MUSIC_ALIAS_EXTRACT_CALL_ID = re.compile(
    r"apple-music-alias-extract-(?P<iteration>0|[1-9][0-9]*)"
    r"(?:-(?P<scope>[0-9a-f]{12}))?"
)


@dataclass(frozen=True)
class MainChatOutcomeEvaluation:
    """Internal completion decision derived from durable execution facts."""

    kind: str
    reason: str = ""
    message: str = ""
    desktop_observed: bool = False

    @property
    def allows_completion(self) -> bool:
        return self.kind == "completed"


@dataclass(frozen=True)
class _DesktopFact:
    index: int
    position: int
    event_type: str
    tool: str
    payload: dict[str, Any]
    result: dict[str, Any]


@dataclass(frozen=True)
class _CanonicalOutcomeFact:
    index: int
    tool: str
    status: str
    reason: str
    payload: dict[str, Any]


def evaluate_main_chat_outcome(
    run: Mapping[str, Any] | None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> MainChatOutcomeEvaluation:
    """Return a conservative terminal decision without consulting model text."""

    run_payload = dict(run or {})
    pending = run_payload.get("pending_approval")
    status = str(run_payload.get("status") or "").strip().lower()
    if status in {"failed", "cancelled", "canceled"}:
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="cancelled" if status in {"cancelled", "canceled"} else "failed",
            message=str(run_payload.get("result") or f"Native Run {status}"),
        )
    if status == "awaiting_user":
        return MainChatOutcomeEvaluation(
            kind="awaiting_user",
            reason="clarification_required",
            message=str(
                run_payload.get("result")
                or "请补充任务目标、对象或期望结果。"
            ),
        )
    if status == "approval_required" or (isinstance(pending, Mapping) and pending):
        return MainChatOutcomeEvaluation(
            kind="approval_required",
            reason="approval_required",
            message="桌面操作仍在等待审批。",
        )

    source_events = [dict(event) for event in events or [] if isinstance(event, Mapping)]
    if not source_events:
        source_events = [
            dict(event)
            for event in run_payload.get("timeline") or []
            if isinstance(event, Mapping)
        ]
    goal_gate = evaluate_goal_contract_outcome(run_payload, source_events)
    if goal_gate is not None and goal_gate.allows_completion:
        return goal_gate
    canonical_blocker = _canonical_non_desktop_outcome_blocker(
        source_events,
        run_id=str(run_payload.get("run_id") or "").strip(),
    )
    if canonical_blocker is not None:
        return _canonical_outcome_failure(canonical_blocker)
    facts = _desktop_facts(source_events)
    facts = _desktop_facts_for_run(
        facts,
        run_id=str(run_payload.get("run_id") or "").strip(),
    )
    if not facts:
        return goal_gate or MainChatOutcomeEvaluation(kind="completed")
    if all(_is_non_action_projection_fact(fact) for fact in facts):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="outcome_event_history_incomplete",
            message=(
                "桌面操作的执行事件历史不完整，只有进度或 Provider 投影，"
                "无法安全确认任务已完成。"
            ),
            desktop_observed=True,
        )

    scoped_facts = _effective_latest_desktop_attempt_facts(facts)
    partial_background_library_fact = (
        _partial_background_library_not_found_outcome_fact(scoped_facts)
    )
    scoped_facts = _without_failed_optional_apple_music_alias_evidence(
        scoped_facts,
        partial_background_library_fact,
    )
    desktop_observed = any(_execution_outcome_observed(fact) for fact in scoped_facts)
    partial_user_action_fact = _partial_user_action_required_outcome_fact(
        scoped_facts
    )
    partial_outcome_fact = (
        partial_user_action_fact or partial_background_library_fact
    )
    required_actions = [
        fact for fact in scoped_facts if _requires_postcondition_evidence(fact)
    ]
    blocking_facts = _blocking_outcome_facts(scoped_facts, required_actions)
    soft_readiness_facts = [
        fact for fact in scoped_facts if _soft_runtime_readiness_pending_fact(fact)
    ]
    if soft_readiness_facts and all(
        _soft_runtime_readiness_pending_fact(fact)
        or _is_non_action_projection_fact(fact)
        for fact in scoped_facts
    ):
        pending_fact = soft_readiness_facts[-1]
        return MainChatOutcomeEvaluation(
            kind="pending",
            reason=_first_text(
                pending_fact.result.get("blocked_by"),
                pending_fact.payload.get("blocked_by"),
                "runtime_execution_readiness_deferred",
            ),
            message=_first_text(
                pending_fact.result.get("blocked_summary"),
                pending_fact.payload.get("blocked_summary"),
                pending_fact.result.get("summary"),
                pending_fact.payload.get("summary"),
                "桌面执行环境尚未完成就绪检查，任务仍在等待 Runtime 探测。",
            ),
            desktop_observed=True,
        )
    terminal_intent_failure_fact = next(
        (
            fact
            for fact in reversed(blocking_facts)
            if fact.event_type.endswith(
                ("desktop.intent_unavailable", "desktop.intent_unverified")
            )
        ),
        None,
    )
    if terminal_intent_failure_fact is not None:
        reason = _first_text(
            terminal_intent_failure_fact.result.get("reason"),
            terminal_intent_failure_fact.payload.get("reason"),
        )
        unavailable = terminal_intent_failure_fact.event_type.endswith(
            "desktop.intent_unavailable"
        )
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason=reason or "desktop_verification_missing",
            message=(
                _first_text(
                    terminal_intent_failure_fact.result.get("error"),
                    terminal_intent_failure_fact.payload.get("error"),
                    terminal_intent_failure_fact.result.get("summary"),
                    terminal_intent_failure_fact.payload.get("summary"),
                    terminal_intent_failure_fact.result.get("blocked_summary"),
                    terminal_intent_failure_fact.payload.get("blocked_summary"),
                )
                or (
                    "桌面操作未完成：当前没有可用的桌面执行环境。"
                    "请检查桌面 Provider 或系统权限后重试。"
                    if unavailable
                    else "桌面操作已执行，但操作效果未能验证。"
                )
            ),
            desktop_observed=True,
        )
    required_actions_to_verify = [
        fact
        for fact in required_actions
        if fact is not partial_outcome_fact
        and not (
            partial_outcome_fact is not None
            and _facts_share_trusted_partial_receipt(
                partial_outcome_fact,
                fact,
            )
        )
    ]
    postcondition_verified = (
        all(
            _latest_postcondition_evidence_matches(action, scoped_facts)
            for action in required_actions_to_verify
        )
        if required_actions_to_verify
        else True
        if partial_outcome_fact is not None
        else any(_has_postcondition_evidence(fact) for fact in scoped_facts)
    )
    requires_postcondition = bool(required_actions)

    if any(_verification_failed(fact) for fact in blocking_facts):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_verification_failed",
            message=(
                "桌面操作未完成：执行后的状态验证失败。"
                "请检查目标应用后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    if any(_permission_failed(fact) for fact in blocking_facts):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_permission_required",
            message=(
                "桌面操作未完成：当前缺少必要的系统权限。"
                "请完成授权后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    if partial_outcome_fact is not None and any(
        fact is not partial_outcome_fact
        and _permission_warning_relevant_to_actions(
            fact,
            [partial_outcome_fact],
        )
        for fact in scoped_facts
    ):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_permission_required",
            message=(
                "桌面操作未完成：当前缺少必要的系统权限。"
                "请完成授权后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    if any(_tool_failed(fact) for fact in blocking_facts):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_tool_failed",
            message=(
                "桌面操作未完成：工具执行失败。"
                "请检查目标应用或输入后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    if (
        not postcondition_verified
        and any(_permission_warning(fact) for fact in blocking_facts)
    ):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_permission_required",
            message=(
                "桌面操作未完成：当前缺少必要的系统权限。"
                "请完成授权后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    if partial_user_action_fact is not None and postcondition_verified:
        return goal_gate or MainChatOutcomeEvaluation(
            kind="completed",
            reason="partial_user_action_required",
            desktop_observed=desktop_observed,
        )
    if partial_background_library_fact is not None and postcondition_verified:
        return goal_gate or MainChatOutcomeEvaluation(
            kind="completed",
            reason="partial_background_library_not_found",
            desktop_observed=desktop_observed,
        )
    if requires_postcondition and not postcondition_verified:
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="desktop_verification_missing",
            message=(
                "桌面操作未完成：缺少执行后的可验证状态证据。"
                "请确认目标应用或内容已到达预期状态后重试。"
            ),
            desktop_observed=desktop_observed,
        )
    return goal_gate or MainChatOutcomeEvaluation(
        kind="completed",
        desktop_observed=desktop_observed,
    )


def evaluate_goal_contract_outcome(
    run: Mapping[str, Any] | None,
    events: Iterable[Mapping[str, Any]] | None = None,
) -> MainChatOutcomeEvaluation | None:
    """Evaluate only the task-level contract, without legacy attempt blockers."""

    run_payload = dict(run or {})
    source_events = [dict(event) for event in events or [] if isinstance(event, Mapping)]
    if not source_events:
        source_events = [
            dict(event)
            for event in run_payload.get("timeline") or []
            if isinstance(event, Mapping)
        ]
    return _goal_contract_completion_gate(
        source_events,
        run_id=str(run_payload.get("run_id") or "").strip(),
        explicit_contract_container=run_payload,
    )


def _goal_contract_completion_gate(
    events: list[dict[str, Any]],
    *,
    run_id: str,
    explicit_contract_container: Mapping[str, Any] | None = None,
) -> MainChatOutcomeEvaluation | None:
    assessment_payload: Mapping[str, Any] | None = None
    contract_event_index = -1
    try:
        contract = runtime_goal_contract(
            run_id=run_id,
            original_goal=None,
            goal_contract_template=explicit_contract_container,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=(),
            timeline=events,
        )
    except (TypeError, ValueError):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="goal_contract_invalid",
            message="任务的完成条件记录已损坏，无法安全确认任务完成。",
            desktop_observed=True,
        )
    if contract is None:
        return None
    for index, event in enumerate(events):
        event_type = str(event.get("event_type") or event.get("event") or "").strip()
        nested = event.get("payload")
        payload = dict(nested) if isinstance(nested, Mapping) else dict(event)
        event_run_id = str(event.get("run_id") or payload.get("run_id") or "").strip()
        if run_id and event_run_id and event_run_id != run_id:
            continue
        if event_type == "agent.goal.contract":
            if contract_event_index < 0:
                contract_event_index = index
            continue
        if event_type == "agent.goal.assessed" and index >= contract_event_index:
            candidate = _json_mapping(payload.get("goal_assessment_json"))
            if candidate is None and isinstance(payload.get("goal_assessment"), Mapping):
                candidate = payload["goal_assessment"]
            if candidate is not None:
                assessment_payload = candidate
    try:
        coordinator = GoalCoordinator()
        runtime_assessment = runtime_goal_assessment(
            contract,
            events[contract_event_index + 1 :],
        )
        if assessment_payload is None:
            assessment = runtime_assessment
        else:
            persisted_assessment = coordinator.restore_assessment(
                contract,
                assessment_payload,
            )
            combined = persisted_assessment.to_payload()
            combined["evidence"] = _unique_goal_records(
                [
                    *persisted_assessment.to_payload()["evidence"],
                    *runtime_assessment.to_payload()["evidence"],
                ],
                identity_key="evidence_id",
            )
            combined["subgoals"] = _unique_goal_records(
                [
                    *persisted_assessment.to_payload()["subgoals"],
                    *runtime_assessment.to_payload()["subgoals"],
                ],
                identity_key="subgoal_id",
            )
            assessment = coordinator.restore_assessment(contract, combined)
    except (TypeError, ValueError):
        return MainChatOutcomeEvaluation(
            kind="failed",
            reason="goal_contract_invalid",
            message="任务的完成条件记录已损坏，无法安全确认任务完成。",
            desktop_observed=True,
        )
    if assessment.completed:
        # A persisted GoalContract is the authoritative task-level completion
        # contract.  Returning ``None`` here re-enabled the legacy attempt
        # evaluator, which could let an earlier recoverable miss overwrite a
        # later source-correlated, verified recovery success.  Keep legacy
        # evaluation only for runs that do not have a GoalContract.
        return MainChatOutcomeEvaluation(
            kind="completed",
            reason="goal_contract_completed",
            desktop_observed=bool(assessment.evidence),
        )
    effectful = any(
        criterion.effectful
        for criterion in contract.criteria
        if criterion.criterion_id in assessment.unsatisfied_criterion_ids
    )
    return MainChatOutcomeEvaluation(
        kind="failed",
        reason="goal_contract_incomplete",
        message=(
            "任务尚未完成：仍缺少与原始目标关联的执行或验证证据。"
            "Agent 应继续恢复、执行并验证，而不是把中间结果当作完成。"
        ),
        desktop_observed=effectful,
    )


def _json_mapping(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        payload = json.loads(value)
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _unique_goal_records(
    records: Iterable[Mapping[str, Any]],
    *,
    identity_key: str,
) -> list[dict[str, Any]]:
    unique: dict[str, dict[str, Any]] = {}
    for record in records:
        identity = str(record.get(identity_key) or "").strip()
        if identity:
            unique[identity] = dict(record)
    return list(unique.values())


def _canonical_non_desktop_outcome_blocker(
    events: Iterable[Mapping[str, Any]],
    *,
    run_id: str,
) -> _CanonicalOutcomeFact | None:
    """Return the latest unsuperseded canonical non-desktop blocker."""

    facts: list[_CanonicalOutcomeFact] = []
    resolved_recovery_sources: list[tuple[int, str]] = []
    for index, event in enumerate(events):
        event_type = str(event.get("event_type") or event.get("event") or "").strip()
        nested = event.get("payload")
        payload = dict(nested) if isinstance(nested, Mapping) else dict(event)
        visibility = str(
            event.get("visibility") or payload.get("visibility") or ""
        ).strip()
        if visibility != "internal":
            continue
        event_run_id = str(event.get("run_id") or payload.get("run_id") or "").strip()
        if run_id and event_run_id and event_run_id != run_id:
            continue
        if event_type == "agent.recovery.completed":
            source_tool_call_id = str(
                payload.get("source_tool_call_id") or ""
            ).strip()
            if (
                source_tool_call_id
                and str(payload.get("result_disposition") or "").strip()
                in {"continue_plan", "terminal_completion"}
            ):
                resolved_recovery_sources.append((index, source_tool_call_id))
            continue
        if event_type != "agent.tool.outcome":
            continue
        tool = str(payload.get("tool") or "").strip()
        if not tool or tool.startswith(_DESKTOP_TOOL_PREFIXES):
            continue
        status = str(payload.get("status") or "").strip().lower()
        if not status:
            continue
        facts.append(
            _CanonicalOutcomeFact(
                index=index,
                tool=tool,
                status=status,
                reason=str(payload.get("reason") or "").strip(),
                payload=payload,
            )
        )

    blocking_statuses = {"action_required", "failed", "partial", "skipped"}
    for fact_index in range(len(facts) - 1, -1, -1):
        fact = facts[fact_index]
        if fact.status not in blocking_statuses:
            continue
        if str(fact.payload.get("completion_impact") or "").strip() in {
            "continue_without_tool",
            "report_refusal",
        }:
            continue
        blocked_call_id = str(fact.payload.get("tool_call_id") or "").strip()
        if blocked_call_id and any(
            recovery_index > fact.index and source_call_id == blocked_call_id
            for recovery_index, source_call_id in resolved_recovery_sources
        ):
            continue
        if any(
            later.status in {"completed", "success"}
            and _canonical_outcome_success_supersedes(fact, later)
            for later in facts[fact_index + 1 :]
        ):
            continue
        return fact
    return None


def _canonical_outcome_success_supersedes(
    blocked: _CanonicalOutcomeFact,
    later: _CanonicalOutcomeFact,
) -> bool:
    blocked_call_id = str(blocked.payload.get("tool_call_id") or "").strip()
    later_call_id = str(later.payload.get("tool_call_id") or "").strip()
    if blocked_call_id and later_call_id == blocked_call_id:
        return True
    later_source_call_id = str(
        later.payload.get("source_tool_call_id") or ""
    ).strip()
    if blocked_call_id and later_source_call_id == blocked_call_id:
        if later.tool == blocked.tool:
            return True
        suggested_tools = {
            str(value or "").strip()
            for value in (
                blocked.payload.get("suggested_tools")
                if isinstance(blocked.payload.get("suggested_tools"), list)
                else []
            )
            if str(value or "").strip()
        }
        if (
            str(later.payload.get("recovery_link_kind") or "").strip()
            == "suggested_tool"
            and str(later.payload.get("recovery_source_tool") or "").strip()
            == blocked.tool
            and str(later.payload.get("recovery_suggested_tool") or "").strip()
            == later.tool
            and later.tool in suggested_tools
        ):
            return True
    blocked_request_id = str(blocked.payload.get("request_id") or "").strip()
    later_request_id = str(later.payload.get("request_id") or "").strip()
    if (
        blocked_request_id
        and later_request_id == blocked_request_id
        and later.tool == blocked.tool
    ):
        return True
    blocked_step_id = str(
        blocked.payload.get("step_id")
        or blocked.payload.get("planner_step_id")
        or ""
    ).strip()
    later_step_id = str(
        later.payload.get("step_id")
        or later.payload.get("planner_step_id")
        or ""
    ).strip()
    if not blocked_step_id or later_step_id != blocked_step_id:
        return False
    for key in ("decision_id", "plan_id"):
        blocked_scope = str(blocked.payload.get(key) or "").strip()
        later_scope = str(later.payload.get(key) or "").strip()
        if blocked_scope and later_scope and blocked_scope != later_scope:
            return False
    return True


def _canonical_outcome_failure(
    fact: _CanonicalOutcomeFact,
) -> MainChatOutcomeEvaluation:
    if fact.status == "action_required":
        message = "工具执行仍需要用户授权或补充操作。"
    elif fact.status == "partial":
        message = "工具只完成了部分结果，任务尚未确认完成。"
    elif fact.status == "skipped":
        message = "必要的工具步骤未执行，任务尚未完成。"
    else:
        message = "工具执行未完成，任务不能标记为成功。"
    return MainChatOutcomeEvaluation(
        kind="failed",
        reason=fact.reason or f"tool_outcome_{fact.status}",
        message=message,
    )


def _partial_user_action_required_outcome_fact(
    facts: Iterable[_DesktopFact],
) -> _DesktopFact | None:
    """Accept only the explicit Apple Music no-match/search fallback receipt.

    This remains deliberately narrow: permission errors, launch/search
    failures, and unverified generic media controls continue to fail closed.
    """

    candidates = [
        fact
        for fact in facts
        if _is_trusted_partial_user_action_required_fact(fact)
    ]
    for fact in reversed(candidates):
        if fact.event_type == "agent.tool.call":
            return fact
    for fact in reversed(candidates):
        if not _is_non_action_projection_fact(fact):
            return fact
    return None


def _is_trusted_partial_user_action_required_fact(fact: _DesktopFact) -> bool:
    if fact.tool != "media.apple_music_play":
        return False
    result = fact.result
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    return bool(
        result.get("ok") is True
        and not _first_text(result.get("error"), fact.payload.get("error"))
        and result.get("permission_error") is not True
        and fact.payload.get("permission_error") is not True
        and not _permission_warning(fact)
        and not any(
            _nonempty(source.get("permission_targets"))
            or _nonempty(source.get("missing_permissions"))
            for source in _fact_sources(fact)
        )
        and not _blocking_conditions(fact)
        and data.get("status") == "not_found"
        and data.get("search_opened") is True
        and str(data.get("target_app") or "").strip() == "Music"
        and data.get("dispatch_verified") is True
        and (
            data.get("foreground_verified") is True
            or (
                data.get("foreground_verified") is False
                and data.get("focus_changed_after_search") is True
            )
        )
        and data.get("search_query_verified") is True
        and (
            data.get("search_query_identity_verified") is True
            or data.get("search_result_changed_from_nonmatching_baseline") is True
        )
        and data.get("playback_started") is False
        and data.get("outcome") == "partial"
        and data.get("user_action_required") is True
    )


def _partial_background_library_not_found_outcome_fact(
    facts: Iterable[_DesktopFact],
) -> _DesktopFact | None:
    """Accept only a fully evidenced, no-foreground Apple Music library miss."""

    candidates = [
        fact
        for fact in facts
        if _is_trusted_background_library_not_found_fact(fact)
    ]
    for fact in reversed(candidates):
        if fact.event_type == "agent.tool.call":
            return fact
    for fact in reversed(candidates):
        if not _is_non_action_projection_fact(fact):
            return fact
    return None


def _is_trusted_background_library_not_found_fact(fact: _DesktopFact) -> bool:
    if fact.tool != "media.apple_music_play":
        return False
    result = fact.result
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    status = str(data.get("status") or "").strip()
    base_partial = bool(
        result.get("ok") is True
        and _has_runtime_local_tool_broker_provenance(result)
        and not _first_text(result.get("error"), fact.payload.get("error"))
        and result.get("permission_error") is not True
        and fact.payload.get("permission_error") is not True
        and not _permission_warning(fact)
        and not any(
            _nonempty(source.get("permission_targets"))
            or _nonempty(source.get("missing_permissions"))
            for source in _fact_sources(fact)
        )
        and not _blocking_conditions(fact)
        and status
        and data.get("library_search_completed") is True
        and str(data.get("target_app") or "").strip() == "Music"
        and data.get("search_opened") is False
        and data.get("playback_started") is False
        and data.get("outcome") == "partial"
        and data.get("user_action_required") is False
    )
    if not base_partial:
        return False
    if status == "not_found":
        return bool(
            data.get("background_safe") is True
            and data.get("foreground_action_taken") is False
        )
    if status not in {"playback_unverified", "catalog_playback_unverified"}:
        return False
    return bool(
        data.get("playback_state_unverified") is True
        or data.get("track_identity_verified") is False
        or data.get("catalog_dispatch_verified") is False
    )


def _without_failed_optional_apple_music_alias_evidence(
    facts: list[_DesktopFact],
    partial_fact: _DesktopFact | None,
) -> list[_DesktopFact]:
    """Drop only failures from the runtime-owned optional alias-evidence seam.

    A background-safe Apple Music miss is already a truthful terminal partial.
    Failure to collect optional web evidence must not overwrite that result, but
    the exception is deliberately bound to the exact runtime-generated request
    identity, query, ordering, and (for extraction) its successful search.
    """

    if partial_fact is None:
        return facts
    return [
        fact
        for fact in facts
        if not _is_failed_optional_apple_music_alias_evidence(
            fact,
            facts=facts,
            partial_fact=partial_fact,
        )
    ]


def _is_failed_optional_apple_music_alias_evidence(
    fact: _DesktopFact,
    *,
    facts: list[_DesktopFact],
    partial_fact: _DesktopFact,
) -> bool:
    if fact.position <= partial_fact.position or not _tool_failed(fact):
        return False
    if str(fact.payload.get("source") or "").strip() != "runtime_internal_recovery":
        return False
    partial_query = _bound_background_apple_music_partial_query(partial_fact)
    if not partial_query:
        return False

    if fact.tool == "browser.search":
        return _is_bound_apple_music_alias_search(
            fact,
            partial_fact=partial_fact,
            partial_query=partial_query,
        )
    if fact.tool != "browser.extract_text":
        return False
    if (
        str(fact.payload.get("planning_reason") or "").strip()
        != "apple_music_alias_evidence_extract"
    ):
        return False
    extract_call_id = str(fact.payload.get("tool_call_id") or "").strip()
    extract_match = _APPLE_MUSIC_ALIAS_EXTRACT_CALL_ID.fullmatch(extract_call_id)
    if extract_match is None or _runtime_receipt_input(fact) != {}:
        return False
    iteration = extract_match.group("iteration")
    scope = extract_match.group("scope") or ""
    return any(
        candidate.position < fact.position
        and _is_bound_apple_music_alias_search(
            candidate,
            partial_fact=partial_fact,
            partial_query=partial_query,
            expected_iteration=iteration,
            expected_scope=scope,
            require_success=True,
        )
        for candidate in facts
    )


def _is_bound_apple_music_alias_search(
    fact: _DesktopFact,
    *,
    partial_fact: _DesktopFact,
    partial_query: str,
    expected_iteration: str = "",
    expected_scope: str | None = None,
    require_success: bool = False,
) -> bool:
    if fact.tool != "browser.search" or fact.position <= partial_fact.position:
        return False
    if str(fact.payload.get("source") or "").strip() != "runtime_internal_recovery":
        return False
    if (
        str(fact.payload.get("planning_reason") or "").strip()
        != "apple_music_alias_evidence_search"
    ):
        return False
    call_id = str(fact.payload.get("tool_call_id") or "").strip()
    call_match = _APPLE_MUSIC_ALIAS_SEARCH_CALL_ID.fullmatch(call_id)
    if call_match is None:
        return False
    if expected_iteration and call_match.group("iteration") != expected_iteration:
        return False
    if expected_scope is not None and (call_match.group("scope") or "") != expected_scope:
        return False
    if _runtime_receipt_input(fact) != {
        "query": f"{partial_query} Apple Music English title"
    }:
        return False
    if not require_success:
        return True
    return bool(
        fact.result.get("ok") is True
        and not _tool_failed(fact)
        and not _permission_warning(fact)
    )


def _bound_background_apple_music_partial_query(fact: _DesktopFact) -> str:
    if not _is_trusted_background_library_not_found_fact(fact):
        return ""
    data = fact.result.get("data")
    if not isinstance(data, Mapping):
        return ""
    data_query = str(data.get("query") or "").strip()
    input_payload = _runtime_receipt_input(fact)
    input_query = (
        str(input_payload.get("query") or "").strip()
        if isinstance(input_payload, Mapping)
        else ""
    )
    if not data_query or data_query != input_query:
        return ""
    return data_query


def _facts_share_trusted_partial_receipt(
    authoritative: _DesktopFact,
    candidate: _DesktopFact,
) -> bool:
    """Match only independently trusted, input-identical receipt projections.

    Upstream callers may reuse a primary ID, while nested result/data IDs may
    be supplied by a provider. Records that lack the complete runtime mirror
    fingerprint therefore fail closed unless they are the same fact.
    """

    if authoritative is candidate:
        return True
    authoritative_id = str(
        authoritative.payload.get("tool_call_id") or ""
    ).strip()
    candidate_id = str(candidate.payload.get("tool_call_id") or "").strip()
    if not authoritative_id or not candidate_id:
        return False
    authoritative_input = _runtime_receipt_input(authoritative)
    candidate_input = _runtime_receipt_input(candidate)
    both_background_library_miss = bool(
        _is_trusted_background_library_not_found_fact(authoritative)
        and _is_trusted_background_library_not_found_fact(candidate)
    )
    both_user_action_handoff = bool(
        _is_trusted_partial_user_action_required_fact(authoritative)
        and _is_trusted_partial_user_action_required_fact(candidate)
    )
    return bool(
        authoritative_id == candidate_id
        and authoritative.tool
        and authoritative.tool == candidate.tool
        and (both_background_library_miss or both_user_action_handoff)
        and authoritative_input is not None
        and authoritative_input == candidate_input
        and _fact_execution_scopes_compatible(authoritative, candidate)
    )


def _runtime_receipt_input(fact: _DesktopFact) -> dict[str, Any] | None:
    for key in ("input_preview", "input"):
        value = fact.payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return None


def _has_runtime_local_tool_broker_provenance(result: Mapping[str, Any]) -> bool:
    provenance = result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
    if not isinstance(provenance, Mapping):
        return False
    return (
        provenance.get("source") == RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        and provenance.get("version") == RUNTIME_EXECUTION_PROVENANCE_VERSION
    )


def _desktop_facts(events: list[dict[str, Any]]) -> list[_DesktopFact]:
    facts: list[_DesktopFact] = []
    for index, event in enumerate(events):
        event_type = str(event.get("event_type") or event.get("event") or "").strip()
        nested = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        payload = {**dict(nested), **event}
        tool = _first_text(
            event.get("tool"),
            event.get("tool_name"),
            nested.get("tool"),
            nested.get("tool_name"),
            event.get("detail"),
        )
        # Some planner events historically use the ``agent.desktop`` namespace
        # for every runtime tool. An explicit non-desktop tool must therefore
        # win over that broad event name; otherwise workspace/network actions
        # get incorrectly subjected to desktop post-condition rules.
        if not _is_desktop_tool(tool) and not (
            not tool and event_type.startswith("agent.desktop.")
        ):
            continue
        result = _result_payload(event, nested)
        steps = nested.get("steps") if isinstance(nested.get("steps"), list) else event.get("steps")
        if isinstance(steps, list):
            for step in steps:
                if not isinstance(step, Mapping):
                    continue
                step_tool = _first_text(step.get("tool"), step.get("tool_name"))
                if not _is_desktop_tool(step_tool):
                    continue
                step_result = (
                    dict(step.get("result"))
                    if isinstance(step.get("result"), Mapping)
                    else dict(step.get("output_preview"))
                    if isinstance(step.get("output_preview"), Mapping)
                    else {}
                )
                # Aggregate steps are lifecycle copies of scoped runtime facts.
                # Fill only missing scope so an explicit conflicting child stays
                # conflicting and is rejected by the normal correlation fences.
                step_payload = {
                    key: payload[key]
                    for key in _AGGREGATE_STEP_INHERITED_SCOPE_KEYS
                    if not _aggregate_step_declares_scope(step, key)
                    and str(payload.get(key) or "").strip()
                }
                step_payload.update(dict(step))
                step_fact = _DesktopFact(
                    index=index,
                    position=len(facts),
                    event_type=event_type,
                    tool=step_tool,
                    payload=step_payload,
                    result=step_result,
                )
                if _aggregate_step_repeats_prior_action(step_fact, facts):
                    step_payload[_AGGREGATE_ACTION_COPY_KEY] = True
                facts.append(step_fact)
        facts.append(
            _DesktopFact(
                index=index,
                position=len(facts),
                event_type=event_type,
                tool=tool,
                payload=payload,
                result=result,
            )
        )
    return facts


def _aggregate_step_declares_scope(step: Mapping[str, Any], key: str) -> bool:
    """Detect child-owned scope before projecting an aggregate parent scope."""

    pending: list[Mapping[str, Any]] = [step]
    seen: set[int] = set()
    while pending:
        source = pending.pop()
        source_id = id(source)
        if source_id in seen:
            continue
        seen.add(source_id)
        if str(source.get(key) or "").strip():
            return True
        pending.extend(
            nested
            for nested_key in _AGGREGATE_STEP_NESTED_FACT_KEYS
            for nested in [source.get(nested_key)]
            if isinstance(nested, Mapping)
        )
    return False


def _aggregate_step_repeats_prior_action(
    candidate: _DesktopFact,
    prior_facts: list[_DesktopFact],
) -> bool:
    """Identify only aggregate action copies backed by an earlier runtime fact."""

    if (
        not _requires_postcondition_evidence(candidate)
        or not _successful_aggregate_action_copy_candidate(candidate)
    ):
        return False
    candidate_steps = _fact_primary_step_ids(candidate)
    return any(
        prior.index < candidate.index
        and prior.tool == candidate.tool
        and _requires_postcondition_evidence(prior)
        and _successful_aggregate_action_copy_candidate(prior)
        and _fact_execution_scopes_compatible(prior, candidate)
        and _aggregate_action_copy_identity_matches(
            prior,
            candidate,
            candidate_steps=candidate_steps,
        )
        for prior in prior_facts
    )


def _successful_aggregate_action_copy_candidate(fact: _DesktopFact) -> bool:
    """Never erase a later failure, denial, permission warning, or verifier veto."""

    return bool(
        fact.result.get("ok") is True
        and not _verification_failed(fact)
        and not _permission_warning(fact)
        and not _tool_failed(fact)
    )


def _aggregate_action_copy_identity_matches(
    prior: _DesktopFact,
    candidate: _DesktopFact,
    *,
    candidate_steps: set[str],
) -> bool:
    """Bind copies to strong call IDs, using step identity only for legacy facts."""

    strong_identity_present = False
    for key in ("request_id", "tool_call_id"):
        prior_values = _fact_identity_values(prior, key)
        candidate_values = _fact_identity_values(candidate, key)
        if prior_values or candidate_values:
            strong_identity_present = True
            if not (
                len(prior_values) == 1
                and len(candidate_values) == 1
                and prior_values == candidate_values
            ):
                return False
    if strong_identity_present:
        return True
    return bool(candidate_steps.intersection(_fact_primary_step_ids(prior)))


def _effective_latest_desktop_attempt_facts(
    facts: list[_DesktopFact],
) -> list[_DesktopFact]:
    scoped = _latest_desktop_attempt_facts(facts)
    effective: list[_DesktopFact] = []
    for index, fact in enumerate(scoped):
        if _fact_failure_superseded(fact, scoped[index + 1 :]):
            continue
        effective.append(fact)
    return effective


def _desktop_facts_for_run(
    facts: list[_DesktopFact],
    *,
    run_id: str,
) -> list[_DesktopFact]:
    """Discard explicitly foreign run facts before terminal evaluation."""

    clean_run_id = str(run_id or "").strip()
    if not clean_run_id:
        return facts
    if not any(
        clean_run_id in _fact_identity_values(fact, "run_id")
        for fact in facts
    ):
        # Retain legacy histories that predate explicit run correlation.
        return facts
    return [
        fact
        for fact in facts
        if not _fact_identity_values(fact, "run_id")
        or clean_run_id in _fact_identity_values(fact, "run_id")
    ]


def _fact_failure_superseded(
    fact: _DesktopFact,
    later_facts: list[_DesktopFact],
) -> bool:
    failed = _verification_failed(fact) or _permission_failed(fact) or _tool_failed(fact)
    if not failed:
        return False
    requires_same_tool = fact.result.get("ok") is False or fact.event_type in (
        _FAILED_TOOL_EVENT_TYPES
    )
    for candidate in later_facts:
        if (
            candidate.index == fact.index
            and candidate.event_type == "agent.desktop.intent_completed"
            and isinstance(candidate.payload.get("steps"), list)
        ):
            # The aggregate parent is appended after its nested steps but is
            # not a later execution.  It cannot erase an explicit failure from
            # a child in that same durable event.  Other later lifecycle facts
            # retain their established supersession semantics.
            continue
        if candidate.result.get("ok") is not True:
            continue
        if (
            _verification_failed(candidate)
            or _permission_warning(candidate)
            or _tool_failed(candidate)
        ):
            continue
        if requires_same_tool and candidate.tool != fact.tool:
            continue
        if _facts_correlated(fact, candidate):
            return True
    return False


def _latest_desktop_attempt_facts(facts: list[_DesktopFact]) -> list[_DesktopFact]:
    intent_facts = [
        fact
        for fact in facts
        if fact.event_type == "agent.desktop.intent_completed"
    ]
    if not intent_facts:
        return facts
    # Nested steps and their parent summary share the same event index.  The
    # parent is appended last and carries attempt-level metadata that legacy
    # step payloads may omit, so use the last fact when indices tie.
    latest_intent = max(
        enumerate(intent_facts),
        key=lambda item: (item[1].index, item[0]),
    )[1]
    attempt_ids = _fact_attempt_ids(latest_intent)
    previous_intent_indexes = [
        fact.index
        for fact in intent_facts
        if fact.index < latest_intent.index
    ]
    attempt_boundary = max(previous_intent_indexes, default=-1) + 1
    scoped: list[_DesktopFact] = []
    for fact in facts:
        if fact.index < attempt_boundary:
            continue
        fact_attempt_ids = _fact_attempt_ids(fact)
        if attempt_ids and fact_attempt_ids and attempt_ids.isdisjoint(fact_attempt_ids):
            continue
        if _fact_explicit_scope_conflicts(latest_intent, fact):
            continue
        scoped.append(fact)
    return scoped


def _fact_explicit_scope_conflicts(
    anchor: _DesktopFact,
    candidate: _DesktopFact,
) -> bool:
    for key in ("run_id", "decision_id", "plan_id"):
        anchor_values = _fact_identity_values(anchor, key)
        candidate_values = _fact_identity_values(candidate, key)
        if (
            anchor_values
            and candidate_values
            and anchor_values.isdisjoint(candidate_values)
        ):
            return True
    return False


def _fact_attempt_ids(fact: _DesktopFact) -> set[str]:
    values: set[str] = set()
    for source in _fact_sources(fact):
        for key in _ATTEMPT_ID_KEYS:
            value = str(source.get(key) or "").strip()
            if value:
                values.add(f"{key}:{value}")
    return values


def _result_payload(
    event: Mapping[str, Any],
    nested: Mapping[str, Any],
) -> dict[str, Any]:
    for source in (event, nested):
        for key in ("result", "output_preview", "output"):
            value = source.get(key)
            if isinstance(value, Mapping):
                return dict(value)
    return {}


def _verification_failed(fact: _DesktopFact) -> bool:
    status = _fact_status(fact)
    return bool(
        fact.result.get("verification_failed") is True
        or fact.payload.get("verification_failed") is True
        or status == "verification_failed"
        or any(
            source.get(key) is False
            for source in _fact_sources(fact)
            for key in _POSTCONDITION_FALSE_KEYS
        )
        or any(_looks_like_verification_blocker(item) for item in _blocking_conditions(fact))
    )


def _permission_failed(fact: _DesktopFact) -> bool:
    if not _permission_warning(fact):
        return False
    return fact.result.get("ok") is False or _fact_status(fact) in {
        "blocked",
        "denied",
        "failed",
        "permission_required",
        "unavailable",
    }


def _permission_warning(fact: _DesktopFact) -> bool:
    return bool(
        fact.event_type == "agent.desktop.permission_recovery"
        or fact.result.get("permission_error") is True
        or fact.payload.get("permission_error") is True
        or _nonempty(fact.result.get("permission_targets"))
        or _nonempty(fact.payload.get("permission_targets"))
        or _nonempty(fact.result.get("missing_permissions"))
        or _nonempty(fact.payload.get("missing_permissions"))
        or _fact_status(fact) == "permission_required"
        or any(_looks_like_permission_blocker(item) for item in _blocking_conditions(fact))
    )


def _permission_warning_relevant_to_actions(
    fact: _DesktopFact,
    actions: list[_DesktopFact],
) -> bool:
    if not _permission_warning(fact):
        return False
    affected_tools = _fact_text_list_values(fact, "affected_tools")
    if affected_tools:
        return any(
            _permission_affected_tool_matches_action(affected, action.tool)
            for affected in affected_tools
            for action in actions
            if action.tool
        )
    return _permission_preflight_relevant_to_actions(fact, actions)


def _tool_failed(fact: _DesktopFact) -> bool:
    return bool(
        fact.event_type in _FAILED_TOOL_EVENT_TYPES
        or fact.result.get("ok") is False
        or _fact_status(fact) in _FAILED_STATUSES
        or _blocking_conditions(fact)
    )


def _soft_runtime_readiness_pending_fact(fact: _DesktopFact) -> bool:
    if not fact.event_type.endswith("desktop.intent_unavailable"):
        return False
    # Import lazily to avoid widening the agent-runtime import cycle through
    # the Yachiyo compatibility package during module initialization.
    from apps.shell.yachiyo_agent.runtime_execution import (
        RUNTIME_EXECUTION_READINESS_DEFERRED,
        runtime_execution_readiness_state,
    )

    return (
        runtime_execution_readiness_state({**fact.payload, **fact.result})
        == RUNTIME_EXECUTION_READINESS_DEFERRED
    )


def _execution_outcome_observed(fact: _DesktopFact) -> bool:
    if fact.result or fact.event_type in _TERMINAL_DESKTOP_EVENT_TYPES:
        return True
    if _fact_status(fact) in {*_FAILED_STATUSES, "completed", "succeeded"}:
        return True
    return bool(
        _blocking_conditions(fact)
        or fact.payload.get("permission_error") is True
        or _nonempty(fact.payload.get("permission_targets"))
    )


def _requires_postcondition_evidence(fact: _DesktopFact) -> bool:
    # Task/todo/checkpoint events are projections of an execution fact, not a
    # second desktop action. Treating them as actions creates ghost
    # postconditions after the authoritative tool call has already verified.
    if _is_non_action_projection_fact(fact):
        return False
    # These tools can prove that input was dispatched, not that an arbitrary
    # application effect occurred.  A successful receipt is terminal when the
    # product summary says "sent/dispatched"; generic UI inspection must not
    # turn it into an unverified side effect, including for legacy planner rows
    # that still carried requires_post_action_verification=True.
    if _is_successful_dispatch_receipt(fact):
        return False
    # A safe-shortcut result is only a keystroke receipt.  Even legacy or
    # spoofed rows that cleared the planner flag still require semantic
    # postcondition evidence before the task can complete.
    if is_semantic_safe_shortcut(fact.tool) or is_semantic_safe_key(fact.tool):
        return True
    if fact.tool in _POSTCONDITION_ACTION_TOOLS or fact.tool.startswith("media."):
        return True
    runtime_stage = _first_text(
        fact.payload.get("runtime_stage"),
        fact.result.get("runtime_stage"),
    )
    runtime_role = _first_text(
        fact.payload.get("runtime_role"),
        fact.result.get("runtime_role"),
    )
    if (
        fact.tool in _VERIFICATION_TOOLS
        or runtime_stage == "verify"
        or runtime_role == "verify_result"
    ):
        return False
    return any(
        source.get("requires_post_action_verification") is True
        or _nonempty(source.get("verification_targets"))
        or _nonempty(source.get("task_verification_targets"))
        for source in _fact_sources(fact)
    )


def _is_successful_dispatch_receipt(fact: _DesktopFact) -> bool:
    input_payload = (
        fact.payload.get("input_preview")
        if isinstance(fact.payload.get("input_preview"), Mapping)
        else fact.payload.get("input")
        if isinstance(fact.payload.get("input"), Mapping)
        else {}
    )
    return bool(
        fact.tool in _DISPATCH_RECEIPT_TOOLS
        and not is_semantic_safe_shortcut(fact.tool, input_payload)
        and fact.result.get("ok") is True
        and not _tool_failed(fact)
        and not _permission_failed(fact)
    )


def _is_non_action_projection_fact(fact: _DesktopFact) -> bool:
    return bool(
        fact.payload.get(_AGGREGATE_ACTION_COPY_KEY) is True
        or (
            fact.event_type == "agent.desktop.intent_completed"
            and isinstance(fact.payload.get("steps"), list)
        )
        or fact.event_type in _NON_ACTION_LIFECYCLE_EVENT_TYPES
        or fact.event_type == "agent.desktop.permission_preflight"
        or fact.event_type.startswith(
            ("agent.task.", "desktop.provider_execution.", "desktop.provider_session.")
        )
    )


def _has_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact | None = None,
) -> bool:
    key_action = action if action is not None else fact
    if is_semantic_safe_key(key_action.tool) and fact is key_action:
        # A mutation's own success/verified flags cannot attest its UI effect.
        return False
    if fact.result.get("ok") is not True or _verification_failed(fact):
        return False
    if _permission_warning(fact):
        return False
    sources = _fact_sources(fact)
    if any(source.get("playback_state_unverified") is True for source in sources):
        return False
    media_action = (
        action
        if action is not None and action.tool.startswith("media.")
        else fact
        if fact.tool.startswith("media.")
        else None
    )
    if media_action is not None:
        return _media_postcondition_evidence(fact, action=media_action)
    app_management_action = (
        action
        if action is not None and action.tool in _APP_MANAGEMENT_POSTCONDITIONS
        else fact
        if action is None and fact.tool in _APP_MANAGEMENT_POSTCONDITIONS
        else None
    )
    if app_management_action is not None:
        return _app_management_postcondition_evidence(
            fact,
            action=app_management_action,
        )
    content_action = (
        action
        if action is not None and _is_content_mutating_action(action)
        else fact
        if action is None and _is_content_mutating_action(fact)
        else None
    )
    if content_action is not None:
        return _content_mutation_postcondition_evidence(
            fact,
            action=content_action,
        )
    if (
        action is not None
        and is_semantic_safe_shortcut(action.tool)
        and _safe_shortcut_action(action) in _COPY_SAFE_SHORTCUT_ACTIONS
    ):
        return _copy_shortcut_postcondition_evidence(fact, action=action)
    if (
        action is not None
        and fact is not action
        and fact.payload.get("source") == "runtime_native_postcondition_receipt"
    ):
        return bool(
            _native_receipt_verification_projection_matches(action, fact)
            and not _observed_app_identity_conflicts(action, fact)
        )
    for source in sources:
        if any(source.get(key) is True for key in _POSTCONDITION_TRUE_KEYS):
            return not _observed_app_identity_conflicts(action, fact)
    runtime_stage = _first_text(
        fact.payload.get("runtime_stage"),
        fact.result.get("runtime_stage"),
    )
    runtime_role = _first_text(
        fact.payload.get("runtime_role"),
        fact.result.get("runtime_role"),
    )
    if not (
        fact.tool in _VERIFICATION_TOOLS
        or runtime_stage == "verify"
        or runtime_role == "verify_result"
    ):
        return False
    return _has_structured_postcondition_evidence(
        fact,
        action=action,
    )


def _app_management_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact,
) -> bool:
    """Accept only native, action-scoped app-management receipts."""

    if fact is not action:
        return _native_receipt_verification_projection_matches(action, fact)
    if fact.tool != action.tool:
        return False
    if _observed_app_identity_conflicts(action, fact):
        return False
    contract = _APP_MANAGEMENT_POSTCONDITIONS.get(action.tool)
    if not contract:
        return False
    sources = _fact_sources(fact)
    status_key = str(contract["status_key"])
    statuses = {
        str(source.get(status_key) or "").strip().lower().replace("-", "_")
        for source in sources
        if str(source.get(status_key) or "").strip()
    }
    if statuses.intersection(_UNVERIFIED_APP_MANAGEMENT_STATUSES):
        return False
    verified_keys = {
        str(value)
        for value in contract["verified_keys"]
    }
    if any(
        source.get(key) is False
        for source in sources
        for key in verified_keys
    ):
        return False
    success_statuses = {
        str(value)
        for value in contract["success_statuses"]
    }
    if statuses.intersection(success_statuses):
        verified = True
    else:
        verified = any(
            source.get(key) is True
            for source in sources
            for key in {*verified_keys, "postcondition_verified"}
        )
    if not verified:
        return False
    target_input_key = str(contract.get("target_input_key") or "").strip()
    if not target_input_key:
        return True
    expected_target = _first_text(
        *(
            source.get(target_input_key)
            for source in _fact_sources(action)
        )
    )
    if not expected_target:
        return False
    observed_keys = {
        str(value)
        for value in contract.get("observed_keys") or set()
    }
    observed_targets = {
        _normalize_content_value(source.get(key))
        for source in sources
        for key in observed_keys
        if _normalize_content_value(source.get(key))
    }
    normalized_expected = _normalize_content_value(expected_target)
    return bool(
        normalized_expected
        and any(normalized_expected in observed for observed in observed_targets)
    )


def _native_receipt_verification_projection_matches(
    action: _DesktopFact,
    evidence: _DesktopFact,
) -> bool:
    """Validate the Runtime's internal projection of an earlier native receipt."""

    if evidence.payload.get("source") != "runtime_native_postcondition_receipt":
        return False
    if evidence.result.get("ok") is not True:
        return False
    if evidence.result.get("postcondition_verified") is not True:
        return False
    if evidence.result.get("verification_satisfied_by_native_receipt") is not True:
        return False
    if not _fact_execution_scopes_compatible(action, evidence):
        return False
    if not _native_receipt_source_identity_matches(
        action,
        evidence,
        action_key="request_id",
        evidence_key="source_request_id",
    ):
        return False
    if not _native_receipt_source_identity_matches(
        action,
        evidence,
        action_key="tool_call_id",
        evidence_key="source_tool_call_id",
    ):
        return False
    if _first_text(evidence.result.get("source_tool")) != action.tool:
        return False
    action_step_id = _first_text(
        action.payload.get("step_id"),
        action.payload.get("planner_step_id"),
    )
    source_step_id = _first_text(
        evidence.result.get("source_step_id"),
        evidence.payload.get("source_step_id"),
    )
    if not action_step_id or source_step_id != action_step_id:
        return False
    action_plan_id = _first_text(action.payload.get("plan_id"))
    evidence_plan_id = _first_text(evidence.payload.get("plan_id"))
    if action_plan_id and evidence_plan_id != action_plan_id:
        return False
    if action.tool == "app.focus_window":
        expected_title = _first_text(
            *(
                source.get("title_contains")
                for source in _fact_sources(action)
            )
        )
        observed_title = _first_text(
            *(
                _first_text(
                    source.get("window_title"),
                    source.get("matched_window_title"),
                )
                for source in _fact_sources(action)
            )
        )
        if not expected_title or not observed_title:
            return False
        if _normalize_content_value(expected_title) not in _normalize_content_value(
            observed_title
        ):
            return False
    return True


def _native_receipt_source_identity_matches(
    action: _DesktopFact,
    evidence: _DesktopFact,
    *,
    action_key: str,
    evidence_key: str,
) -> bool:
    """Require exact native-receipt binding unless both legacy IDs are absent."""

    action_values = _fact_identity_values(action, action_key)
    evidence_values = _fact_identity_values(evidence, evidence_key)
    if not action_values and not evidence_values:
        return True
    return bool(
        len(action_values) == 1
        and len(evidence_values) == 1
        and action_values == evidence_values
    )


def _media_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact,
) -> bool:
    """Validate media postconditions against the requested control action."""

    sources = _fact_sources(fact)
    if any(source.get("playback_state_unverified") is True for source in sources):
        return False
    if any(source.get("playback_ok") is False for source in sources):
        return False
    if _observed_app_identity_conflicts(action, fact):
        return False
    if fact is not action and not _media_evidence_targets_expected_app(action, fact):
        return False

    requested_action = _media_control_action(action)
    player_state = _first_text(
        *[source.get("player_state") for source in sources]
    ).lower().replace("-", "_").replace(" ", "_")
    track = _first_text(*[source.get("track") for source in sources])

    if action.tool == "media.apple_music_play":
        catalog_sources = [
            source
            for source in sources
            if "catalog_match_verified" in source
        ]
        return bool(
            player_state == "playing"
            and track
            and any(
                source.get("track_identity_verified") is True
                for source in sources
            )
            and any(source.get("playback_started") is True for source in sources)
            and (
                not catalog_sources
                or all(
                    source.get("catalog_match_verified") is True
                    for source in catalog_sources
                )
            )
            and not any(
                source.get("foreground_action_taken") is True
                for source in sources
            )
        )

    if requested_action == "play":
        if player_state == "playing":
            return True
        # The combined Apple Music operation delegates to the same native
        # control and promotes its successful receipt to ``playback_ok``.
        # A track is required as concrete playback evidence; a fallback media
        # key marks ``playback_state_unverified`` and is rejected above.
        if fact is action and action.tool == "media.apple_music_open_and_play":
            return bool(
                track
                and any(source.get("playback_ok") is True for source in sources)
            )
        return False
    if requested_action == "pause":
        return player_state in {"paused", "stopped"}
    if requested_action == "stop":
        return player_state in {"paused", "stopped", "not_running"}
    if requested_action == "toggle":
        return player_state in {"playing", "paused", "stopped"}
    if requested_action in {"next", "previous"}:
        if fact is action:
            # Dedicated app controls return only after the player accepted the
            # requested command. A later status read, however, cannot prove a
            # track changed merely because some track is currently visible.
            return bool(track or player_state in {"playing", "paused", "stopped"})
        return any(
            source.get(key) is True
            for source in sources
            for key in ("track_changed", "postcondition_verified", "verified")
        )
    if requested_action == "status":
        return bool(player_state and player_state != "unknown") or any(
            isinstance(source.get("running"), bool)
            for source in sources
        )
    return bool(track or (player_state and player_state != "unknown"))


def _media_evidence_targets_expected_app(
    action: _DesktopFact,
    evidence: _DesktopFact,
) -> bool:
    """Reject later media observations that cannot identify the target app."""

    expected = _expected_app_tokens(action) or _intrinsic_media_app_tokens(action)
    if not expected:
        return True
    observed = _observed_app_tokens(evidence) or _intrinsic_media_app_tokens(evidence)
    return bool(observed and _app_token_sets_match(expected, observed))


def _intrinsic_media_app_tokens(fact: _DesktopFact) -> set[str]:
    if fact.tool.startswith("media.apple_music"):
        return {_canonical_app_token("Music")}
    return set()


def _media_control_action(fact: _DesktopFact) -> str:
    known_actions = {
        "next",
        "pause",
        "play",
        "previous",
        "status",
        "stop",
        "toggle",
    }
    for source in _fact_sources(fact):
        for key in (
            "control",
            "requested_control",
            "media_control",
            "requested_action",
            "action",
        ):
            value = _first_text(source.get(key)).lower()
            if value in known_actions:
                return value
    if fact.tool in {
        "media.apple_music_play",
        "media.apple_music_open_and_play",
        "media.music_app_open_and_play",
    }:
        return "play"
    if fact.tool == "media.apple_music_status":
        return "status"
    return ""


def _latest_postcondition_evidence_matches(
    action: _DesktopFact,
    facts: list[_DesktopFact],
) -> bool:
    observations = [
        evidence
        for evidence in facts
        if _postcondition_observation_relevant(action, evidence)
    ]
    if not observations:
        return False
    if _direct_action_has_verified_postcondition(action):
        matched = not any(
            _observation_contradicts_direct_postcondition(action, evidence)
            for evidence in observations
            if evidence is not action
        )
    else:
        matched = _postcondition_evidence_matches(action, observations[-1])
    if not matched:
        return False
    return _container_preparation_content_dependencies_verified(action, facts)


def _blocking_outcome_facts(
    facts: list[_DesktopFact],
    required_actions: list[_DesktopFact],
) -> list[_DesktopFact]:
    successful_permission_diagnostic = any(
        _is_successful_permission_diagnostic_result(fact) for fact in facts
    )
    dispatch_receipts = [
        fact for fact in facts if _is_successful_dispatch_receipt(fact)
    ]
    return [
        fact
        for fact in facts
        if (
            not fact.event_type.startswith("agent.task.")
            and not (
                successful_permission_diagnostic
                and _is_advisory_permission_diagnostic_fact(fact)
            )
            and _permission_preflight_relevant_to_actions(fact, required_actions)
            and not any(
                _verified_direct_action_shields_degraded_observation(action, fact)
                for action in required_actions
            )
            and not any(
                _dispatch_receipt_shields_generic_verification(receipt, fact)
                for receipt in dispatch_receipts
            )
        )
    ]


def _dispatch_receipt_shields_generic_verification(
    receipt: _DesktopFact,
    observation: _DesktopFact,
) -> bool:
    if observation is receipt or not _is_post_action_verification_fact(observation):
        return False
    return _facts_correlated(receipt, observation)


def _is_post_action_verification_fact(fact: _DesktopFact) -> bool:
    if fact.event_type.endswith("desktop.intent_unverified"):
        return True
    runtime_stage = _first_text(
        fact.payload.get("runtime_stage"),
        fact.result.get("runtime_stage"),
    )
    runtime_role = _first_text(
        fact.payload.get("runtime_role"),
        fact.result.get("runtime_role"),
    )
    sources = {
        str(source.get("source") or "").strip()
        for source in _fact_sources(fact)
    }
    return bool(
        runtime_stage == "verify"
        or runtime_role == "verify_result"
        or sources.intersection(
            {
                "runtime_native_postcondition_receipt",
                "runtime_post_action_auto_verify",
                "runtime_verification",
            }
        )
    )


def _is_successful_permission_diagnostic_result(fact: _DesktopFact) -> bool:
    """Return whether a permission diagnostic successfully reported readiness facts.

    Missing permissions and runtime blockers are the payload of this diagnostic,
    not evidence that the diagnostic call itself failed.  An explicit failed
    result or failed tool event remains authoritative.
    """

    return bool(
        is_successful_permission_diagnostic_result(fact.tool, fact.result)
        and fact.event_type not in _FAILED_TOOL_EVENT_TYPES
    )


def _is_advisory_permission_diagnostic_fact(fact: _DesktopFact) -> bool:
    if not is_permission_diagnostic_tool(fact.tool):
        return False
    if fact.event_type in _FAILED_TOOL_EVENT_TYPES or fact.result.get("ok") is False:
        return False
    if fact.result.get("ok") is True and not is_successful_permission_diagnostic_result(
        fact.tool,
        fact.result,
    ):
        return False
    return _fact_status(fact) not in {
        "cancelled",
        "canceled",
        "denied",
        "error",
        "failed",
        "rejected",
        "skipped",
        "unavailable",
        "verification_failed",
    }


def _permission_preflight_relevant_to_actions(
    fact: _DesktopFact,
    actions: list[_DesktopFact],
) -> bool:
    """Keep advisory permission facts only when they can block an action."""

    if fact.event_type != "agent.desktop.permission_preflight":
        return True
    action_tools = {action.tool for action in actions if action.tool}
    if not action_tools:
        return False

    permission_targets = _fact_text_list_values(fact, "permission_targets")
    scoped_prefixes = [
        _SCOPED_PERMISSION_TARGET_TOOL_PREFIXES[target]
        for target in permission_targets
        if target in _SCOPED_PERMISSION_TARGET_TOOL_PREFIXES
    ]
    if permission_targets and len(scoped_prefixes) == len(permission_targets):
        return any(
            tool.startswith(prefix)
            for prefixes in scoped_prefixes
            for prefix in prefixes
            for tool in action_tools
        )

    affected_tools = _fact_text_list_values(fact, "affected_tools")
    if affected_tools:
        return any(
            _permission_affected_tool_matches_action(affected, action_tool)
            for affected in affected_tools
            for action_tool in action_tools
        )
    # Older projections did not preserve capability or affected-tool detail.
    # Unknown scope stays conservative instead of silently dismissing a real
    # permission blocker.
    return True


def _fact_text_list_values(fact: _DesktopFact, key: str) -> list[str]:
    values: list[str] = []
    for source in _fact_sources(fact):
        raw = source.get(key)
        if isinstance(raw, str):
            candidates = [raw]
        elif isinstance(raw, Iterable) and not isinstance(raw, (bytes, Mapping)):
            candidates = list(raw)
        else:
            continue
        for candidate in candidates:
            clean = str(candidate or "").strip()
            if clean and clean not in values:
                values.append(clean)
    return values


def _permission_affected_tool_matches_action(
    affected_tool: str,
    action_tool: str,
) -> bool:
    if affected_tool == action_tool:
        return True
    if affected_tool.startswith("app.open_and_"):
        return action_tool in {"app.open", "desktop.open_app"}
    if affected_tool.startswith("app.focus_and_"):
        return action_tool in {"app.focus", "desktop.focus_app"}
    return False


def _verified_direct_action_shields_degraded_observation(
    action: _DesktopFact,
    observation: _DesktopFact,
) -> bool:
    return bool(
        observation is not action
        and observation.result.get("ok") is True
        and _direct_action_has_verified_postcondition(action)
        and (
            _postcondition_observation_relevant(action, observation)
            or _correlated_foreground_observation_for_verified_media(
                action,
                observation,
            )
        )
        and not _observation_contradicts_direct_postcondition(action, observation)
    )


def _direct_action_has_verified_postcondition(action: _DesktopFact) -> bool:
    sources = _fact_sources(action)
    if action.tool in {"app.open", "desktop.open_app"}:
        return any(source.get("launch_verified") is True for source in sources)
    if action.tool == "app.focus":
        return any(
            source.get("focus_verified") is True
            or source.get("foreground_ready") is True
            for source in sources
        )
    if action.tool.startswith("media."):
        return _has_postcondition_evidence(action, action=action)
    if action.tool in _APP_MANAGEMENT_POSTCONDITIONS:
        return _has_postcondition_evidence(action, action=action)
    return False


def _observation_contradicts_direct_postcondition(
    action: _DesktopFact,
    observation: _DesktopFact,
) -> bool:
    if action.tool in _APP_MANAGEMENT_POSTCONDITIONS:
        # show/hide/minimize return their own action-scoped native receipt.
        # Generic foreground/running-app observations neither prove nor
        # contradict that receipt; only a correlated receipt for the same
        # management action may invalidate it.
        if observation.tool != action.tool:
            return False
        if _observed_app_identity_conflicts(action, observation):
            return True
        return not _has_postcondition_evidence(
            observation,
            action=observation,
        )
    if _unrelated_foreground_observation_for_background_media(action, observation):
        sources = _fact_sources(observation)
        return any(
            source.get("playback_ok") is False
            or source.get("playback_state_unverified") is True
            for source in sources
        )
    if (
        _observed_app_identity_conflicts(action, observation)
    ):
        return True
    sources = _fact_sources(observation)
    if any(source.get("verification_failed") is True for source in sources):
        return True
    if action.tool in {"app.open", "desktop.open_app"}:
        if any(source.get("launch_verified") is False for source in sources):
            return True
    elif action.tool == "app.focus":
        if any(
            source.get("focus_verified") is False
            or source.get("foreground_ready") is False
            for source in sources
        ):
            return True
    elif action.tool.startswith("media."):
        if any(
            source.get("playback_ok") is False
            or source.get("playback_state_unverified") is True
            for source in sources
        ):
            return True
    return any(
        source.get(key) is False
        for source in sources
        for key in (
            "postcondition_verified",
            "target_reached",
            "target_visible",
            "verified",
        )
    )


def _unrelated_foreground_observation_for_background_media(
    action: _DesktopFact,
    observation: _DesktopFact,
) -> bool:
    return bool(
        action.tool.startswith("media.")
        and observation.tool in _VERIFICATION_TOOLS
    )


def _correlated_foreground_observation_for_verified_media(
    action: _DesktopFact,
    observation: _DesktopFact,
) -> bool:
    return bool(
        observation.position >= action.position
        and _unrelated_foreground_observation_for_background_media(
            action,
            observation,
        )
        and _facts_correlated(action, observation)
    )


def _postcondition_observation_relevant(
    action: _DesktopFact,
    evidence: _DesktopFact,
) -> bool:
    if evidence.position < action.position:
        return False
    if evidence is not action and _is_non_action_projection_fact(evidence):
        # Aggregate task/intent projections repeat the last action result after
        # its real verifier. They are lifecycle summaries, not a newer desktop
        # observation, and must not displace correlated verification evidence.
        return False
    if not _is_postcondition_observation_candidate(evidence):
        return False
    if action is evidence:
        return True
    if (
        evidence.payload.get("source") == "runtime_native_postcondition_receipt"
        and not _native_receipt_verification_projection_matches(action, evidence)
    ):
        return False
    action_attempts = _fact_attempt_ids(action)
    evidence_attempts = _fact_attempt_ids(evidence)
    if (
        action_attempts
        and evidence_attempts
        and action_attempts.isdisjoint(evidence_attempts)
    ):
        return False
    action_links = _fact_link_ids(action)
    evidence_links = _fact_link_ids(evidence)
    if action_links.intersection(evidence_links):
        return True
    return _facts_correlated(action, evidence)


def _is_postcondition_observation_candidate(fact: _DesktopFact) -> bool:
    if fact.result.get("ok") is not True:
        return False
    sources = _fact_sources(fact)
    if any(
        source.get(key) is True
        for source in sources
        for key in {*_POSTCONDITION_TRUE_KEYS, *_CONTENT_POSTCONDITION_KEYS}
    ):
        return True
    if (
        fact.tool.startswith("media.")
        or fact.tool in _APP_MANAGEMENT_POSTCONDITIONS
        or fact.tool in _VERIFICATION_TOOLS
        or fact.tool == "clipboard.read"
    ):
        return True
    return _first_text(
        fact.payload.get("runtime_stage"),
        fact.result.get("runtime_stage"),
    ) == "verify" or _first_text(
        fact.payload.get("runtime_role"),
        fact.result.get("runtime_role"),
    ) == "verify_result"


def _postcondition_evidence_matches(
    action: _DesktopFact,
    evidence: _DesktopFact,
) -> bool:
    if not _has_postcondition_evidence(evidence, action=action):
        return False
    if action is evidence:
        return True
    if evidence.position <= action.position:
        return False
    if evidence.payload.get("source") == "runtime_native_postcondition_receipt":
        # The native receipt projection is already bound to the exact source
        # request, call, tool, step, and plan above.  Do not make that stronger
        # identity depend on generic target text: a verifier can legitimately
        # retain the planner's dynamic app placeholder after the source action
        # has resolved it to a concrete application.
        return _native_receipt_verification_projection_matches(action, evidence)
    return _facts_correlated(action, evidence)


def _safe_shortcut_action(fact: _DesktopFact) -> str:
    for parent in (fact.payload, fact.result):
        for key in ("input_preview", "input", "data"):
            source = parent.get(key)
            if not isinstance(source, Mapping):
                continue
            action = str(
                source.get("shortcut_action")
                or source.get("requested_action")
                or source.get("action")
                or ""
            ).strip().lower()
            if action:
                return action
    return ""


def _copy_shortcut_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact,
) -> bool:
    """Accept clipboard content only when it is linked to the copy action."""

    if fact is action:
        return any(
            source.get("postcondition_verified") is True
            or source.get("verified") is True
            for source in _fact_sources(fact)
        )
    if fact.tool != "clipboard.read" or fact.result.get("ok") is not True:
        return False
    if not _facts_correlated(action, fact):
        return False
    return any(
        isinstance(source.get("text"), str) and bool(source.get("text"))
        for source in _fact_sources(fact)
    )


def _content_mutation_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact,
) -> bool:
    """Require semantic evidence that intended content reached the target UI.

    A same-app window proves only that the container exists.  It does not
    prove that a type/draft action placed its intended content there.  Content
    anchors are derived transiently from the already-recorded action input and
    are never added to events or returned to callers.
    """

    if _observed_app_identity_conflicts(action, fact):
        return False
    sources = _fact_sources(fact)
    if any(
        source.get(key) is False
        for source in sources
        for key in _CONTENT_POSTCONDITION_KEYS
    ):
        return False
    if any(
        source.get(key) is True
        for source in sources
        for key in _CONTENT_POSTCONDITION_KEYS
    ):
        return True
    if fact is action:
        return False

    runtime_stage = _first_text(
        fact.payload.get("runtime_stage"),
        fact.result.get("runtime_stage"),
    )
    runtime_role = _first_text(
        fact.payload.get("runtime_role"),
        fact.result.get("runtime_role"),
    )
    if not (
        fact.tool in _VERIFICATION_TOOLS
        or runtime_stage == "verify"
        or runtime_role == "verify_result"
    ):
        return False
    # App/window identity alone is never semantic evidence for typed content.
    if fact.tool == "desktop.active_window":
        return False

    elements = _observed_ui_elements(fact)
    text_values = _observed_ui_text_values(fact)
    if not (elements or text_values):
        return False
    expected_apps = _expected_app_tokens(action) or _declared_app_tokens(fact)
    if expected_apps and not _observed_app_tokens(fact):
        return False
    expected_values = _expected_content_values(action)
    editable_values = _observed_editable_content_values(elements)
    if any(
        expected == observed
        for expected in expected_values
        for observed in editable_values
    ):
        return True
    anchors = _expected_content_anchors(action)
    if not anchors:
        return False
    observed_values = _observed_content_values(
        elements=elements,
        text_values=text_values,
    )
    return any(
        anchor in observed
        for anchor in anchors
        for observed in observed_values
    )


def _is_content_mutating_action(fact: _DesktopFact) -> bool:
    if fact.tool in _CONTENT_MUTATION_TOOLS:
        return True
    return any(
        str(source.get("runtime_role") or "").strip()
        in _CONTENT_MUTATION_RUNTIME_ROLES
        for source in _fact_sources(fact)
    )


def _expected_content_anchors(action: _DesktopFact) -> set[str]:
    anchors: set[str] = set()
    for expected in _expected_content_values(action):
        anchors.update(_safe_content_anchors(expected))
    return anchors


def _expected_content_values(action: _DesktopFact) -> set[str]:
    excluded = {
        normalized
        for source in _fact_sources(action)
        for key in ("app_name", "expected_app_name", "target_app_name")
        for normalized in [_normalize_content_value(source.get(key))]
        if normalized
    }
    values: set[str] = set()
    for source in _fact_sources(action):
        for key in ("body", "content", "message", "text"):
            raw_value = source.get(key)
            if not isinstance(raw_value, str) or not raw_value.strip():
                continue
            for segment in (raw_value, *raw_value.splitlines()):
                normalized = _normalize_content_value(segment)
                if normalized:
                    values.add(normalized)
    return {value for value in values if value not in excluded}


def _safe_content_anchors(value: str) -> set[str]:
    normalized = _normalize_content_value(value)
    minimum_length = 4 if re.search(r"[\u3400-\u9fff]", normalized) else 8
    if len(normalized) < minimum_length:
        return set()
    anchor_width = 24
    if len(normalized) <= anchor_width:
        return {normalized}
    middle_start = max(0, (len(normalized) - anchor_width) // 2)
    return {
        normalized[:anchor_width],
        normalized[middle_start : middle_start + anchor_width],
        normalized[-anchor_width:],
    }


def _observed_content_values(
    *,
    elements: list[Mapping[str, Any]],
    text_values: set[str],
) -> set[str]:
    values = {
        normalized
        for value in text_values
        for normalized in [_normalize_content_value(value)]
        if normalized
    }
    for element in elements:
        for key in (
            "description",
            "label",
            "name",
            "text",
            "title",
            "value",
        ):
            normalized = _normalize_content_value(element.get(key))
            if normalized:
                values.add(normalized)
    return values


def _observed_editable_content_values(
    elements: list[Mapping[str, Any]],
) -> set[str]:
    values: set[str] = set()
    for element in elements:
        role = _normalize_content_value(element.get("role"))
        editable = element.get("editable") is True or any(
            token in role
            for token in ("textfield", "textarea", "searchfield", "textbox", "textview")
        )
        if not editable:
            continue
        for key in ("text", "value"):
            normalized = _normalize_content_value(element.get(key))
            if normalized:
                values.add(normalized)
    return values


def _normalize_content_value(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"[_\W]+", "", value.casefold(), flags=re.UNICODE)


def _container_preparation_content_dependencies_verified(
    action: _DesktopFact,
    facts: list[_DesktopFact],
) -> bool:
    """Bind inferred container verification to its semantic content action."""

    if not _is_container_preparation_action(action):
        return True
    preparation_step_ids = _fact_primary_step_ids(action)
    if not preparation_step_ids:
        return True
    latest_dependents: dict[tuple[str, ...], _DesktopFact] = {}
    for candidate in facts:
        if candidate.position <= action.position:
            continue
        if candidate.result.get("ok") is not True:
            continue
        if _is_non_action_projection_fact(candidate):
            continue
        if not _is_content_mutating_action(candidate):
            continue
        if preparation_step_ids.isdisjoint(_fact_dependency_step_ids(candidate)):
            continue
        if not any(
            _postcondition_observation_relevant(candidate, evidence)
            for evidence in facts
        ):
            # Aggregate/projection payloads can repeat an already-executed
            # content step after its verifier.  They are not a newer attempt
            # and must not hide the canonical action that the verifier saw.
            continue
        candidate_step_ids = _fact_primary_step_ids(candidate)
        identity = tuple(sorted(candidate_step_ids)) or (f"position:{candidate.position}",)
        latest_dependents[identity] = candidate
    return all(
        _latest_postcondition_evidence_matches(candidate, facts)
        for candidate in latest_dependents.values()
    )


def _is_container_preparation_action(fact: _DesktopFact) -> bool:
    return any(
        str(source.get("action") or "").strip() in _CONTAINER_PREPARATION_ACTIONS
        for source in _fact_sources(fact)
    )


def _fact_primary_step_ids(fact: _DesktopFact) -> set[str]:
    return {
        value
        for source in (fact.payload, fact.result)
        for key in ("planner_step_id", "step_id")
        for value in [str(source.get(key) or "").strip()]
        if value
    }


def _fact_dependency_step_ids(fact: _DesktopFact) -> set[str]:
    values: set[str] = set()
    for source in _fact_sources(fact):
        dependencies = source.get("depends_on")
        if not isinstance(dependencies, Iterable) or isinstance(
            dependencies,
            (str, bytes, Mapping),
        ):
            continue
        values.update(
            str(value or "").strip()
            for value in dependencies
            if str(value or "").strip()
        )
    return values


def _has_structured_postcondition_evidence(
    fact: _DesktopFact,
    *,
    action: _DesktopFact | None,
) -> bool:
    if _observed_app_identity_conflicts(action, fact):
        return False
    observed_apps = _observed_app_tokens(fact)
    expected_apps = _expected_app_tokens(action) or _declared_app_tokens(fact)
    tool_name = str(fact.tool or "").strip()
    if tool_name == "desktop.active_window":
        if expected_apps and not observed_apps:
            return False
        return bool(observed_apps or _observed_window_tokens(fact))

    elements = _observed_ui_elements(fact)
    text_values = _observed_ui_text_values(fact)
    has_ui_content = bool(elements or text_values or _positive_ui_count(fact))
    if tool_name in {"desktop.ui_elements", "desktop.read_ui"}:
        if not has_ui_content:
            return False
        if expected_apps and not observed_apps:
            return False
        return _ui_evidence_matches_expected_target(
            action,
            fact,
            elements=elements,
            text_values=text_values,
        )

    if has_ui_content:
        if expected_apps and not observed_apps:
            return False
        return _ui_evidence_matches_expected_target(
            action,
            fact,
            elements=elements,
            text_values=text_values,
        )
    if expected_apps and not observed_apps:
        return False
    return bool(observed_apps or _observed_window_tokens(fact))


def _observed_app_identity_conflicts(
    action: _DesktopFact | None,
    evidence: _DesktopFact,
) -> bool:
    expected = _expected_app_tokens(action) or _declared_app_tokens(evidence)
    observed = _observed_app_tokens(evidence)
    return bool(expected and observed and not _app_token_sets_match(expected, observed))


def _expected_app_tokens(fact: _DesktopFact | None) -> set[str]:
    if fact is None:
        return set()
    sources: list[Mapping[str, Any]] = [fact.payload]
    for key in ("input", "input_preview", "action_target", "verification_target"):
        value = fact.payload.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    return _app_tokens_from_sources(sources)


def _declared_app_tokens(fact: _DesktopFact) -> set[str]:
    sources: list[Mapping[str, Any]] = [fact.payload]
    for key in ("input", "input_preview", "verification_target"):
        value = fact.payload.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    return _app_tokens_from_sources(sources)


def _observed_app_tokens(fact: _DesktopFact) -> set[str]:
    sources: list[Mapping[str, Any]] = [fact.result]
    data = fact.result.get("data")
    if isinstance(data, Mapping):
        sources.append(data)
    return _app_tokens_from_sources(
        sources,
        keys=("active_app_name", "observed_app_name", "frontmost_app", "app_name"),
    )


def _app_tokens_from_sources(
    sources: Iterable[Mapping[str, Any]],
    *,
    keys: tuple[str, ...] = (
        "app_name",
        "target_app_name",
        "expected_app_name",
    ),
) -> set[str]:
    return {
        _canonical_app_token(value)
        for source in sources
        for key in keys
        for value in [str(source.get(key) or "").strip().casefold()]
        if value
    }


def _canonical_app_token(value: str) -> str:
    compact = compact_app_alias(value)
    canonical = APP_ALIASES.get(compact, value)
    return compact_app_alias(canonical)


def _app_token_sets_match(left: set[str], right: set[str]) -> bool:
    return bool(left.intersection(right))


def _observed_window_tokens(fact: _DesktopFact) -> set[str]:
    sources: list[Mapping[str, Any]] = [fact.result]
    data = fact.result.get("data")
    if isinstance(data, Mapping):
        sources.append(data)
    return {
        value
        for source in sources
        for key in ("window_title", "title", "active_window_title")
        for value in [str(source.get(key) or "").strip().casefold()]
        if value
    }


def _observed_ui_elements(fact: _DesktopFact) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    for source in _structured_result_sources(fact):
        elements = source.get("elements")
        if isinstance(elements, list):
            values.extend(item for item in elements if isinstance(item, Mapping))
    return values


def _observed_ui_text_values(fact: _DesktopFact) -> set[str]:
    values: set[str] = set()
    for source in _structured_result_sources(fact):
        for key in ("text", "visible_text", "content", "extracted_text"):
            value = str(source.get(key) or "").strip().casefold()
            if value:
                values.add(value)
    return values


def _structured_result_sources(fact: _DesktopFact) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = [fact.result]
    data = fact.result.get("data")
    if isinstance(data, Mapping):
        sources.append(data)
        nested_ui = data.get("ui_elements")
        if isinstance(nested_ui, Mapping):
            sources.append(nested_ui)
            nested_data = nested_ui.get("data")
            if isinstance(nested_data, Mapping):
                sources.append(nested_data)
    return sources


def _positive_ui_count(fact: _DesktopFact) -> bool:
    for source in _structured_result_sources(fact):
        for key in ("count", "element_count", "text_item_count"):
            value = source.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                return True
    return False


def _ui_evidence_matches_expected_target(
    action: _DesktopFact | None,
    evidence: _DesktopFact,
    *,
    elements: list[Mapping[str, Any]],
    text_values: set[str],
) -> bool:
    expected = _expected_ui_tokens(evidence) or _expected_ui_tokens(action)
    if not expected:
        return True
    observed = set(text_values)
    for element in elements:
        for key in ("name", "label", "title", "text", "value", "description", "identifier"):
            value = str(element.get(key) or "").strip().casefold()
            if value:
                observed.add(value)
    return bool(observed and _target_sets_match(expected, observed))


def _expected_ui_tokens(fact: _DesktopFact | None) -> set[str]:
    if fact is None:
        return set()
    target_sources: list[Mapping[str, Any]] = [fact.payload]
    for key in ("verification_target", "action_target"):
        value = fact.payload.get(key)
        if isinstance(value, Mapping):
            target_sources.append(value)
    input_sources: list[Mapping[str, Any]] = []
    for key in ("input", "input_preview"):
        value = fact.payload.get(key)
        if isinstance(value, Mapping):
            input_sources.append(value)
    target_values = {
        value
        for source in target_sources
        for key in (
            "target",
            "target_label",
            "target_search_text",
            "ui_target",
            "label",
            "name",
            "selector",
        )
        for value in [str(source.get(key) or "").strip().casefold()]
        if value
    }
    input_values = {
        value
        for source in input_sources
        for key in (
            "target",
            "target_label",
            "target_search_text",
            "ui_target",
            "label",
            "selector",
        )
        for value in [str(source.get(key) or "").strip().casefold()]
        if value
    }
    return target_values | input_values


def _facts_correlated(left: _DesktopFact, right: _DesktopFact) -> bool:
    if not _fact_execution_scopes_compatible(left, right):
        return False
    left_attempts = _fact_attempt_ids(left)
    right_attempts = _fact_attempt_ids(right)
    if left_attempts and right_attempts and left_attempts.isdisjoint(right_attempts):
        return False
    left_link_ids = _fact_link_ids(left)
    right_link_ids = _fact_link_ids(right)
    if left_link_ids and right_link_ids and left_link_ids.isdisjoint(right_link_ids):
        return False
    left_targets = _fact_target_tokens(left)
    right_targets = _fact_target_tokens(right)
    if left_targets and right_targets and not _target_sets_match(
        left_targets,
        right_targets,
    ):
        return False
    if left.index == right.index:
        return True
    if left_link_ids.intersection(right_link_ids):
        return True
    if left_targets and right_targets:
        return _target_sets_match(left_targets, right_targets)
    if not left_targets and not right_targets:
        return bool(left_attempts.intersection(right_attempts))
    return False


def _fact_execution_scopes_compatible(
    left: _DesktopFact,
    right: _DesktopFact,
) -> bool:
    """Fence evidence to its plan while retaining strongly-linked legacy rows."""

    strong_identity = bool(
        _fact_attempt_ids(left).intersection(_fact_attempt_ids(right))
        or _fact_request_ids(left).intersection(_fact_request_ids(right))
    )
    for key in ("run_id", "decision_id", "plan_id"):
        left_values = _fact_identity_values(left, key)
        right_values = _fact_identity_values(right, key)
        if left_values and right_values:
            if left_values.isdisjoint(right_values):
                return False
            continue
        if (left_values or right_values) and not strong_identity:
            return False
    return True


def _fact_identity_values(fact: _DesktopFact, key: str) -> set[str]:
    return {
        value
        for source in _fact_sources(fact)
        for value in [str(source.get(key) or "").strip()]
        if value
    }


def _fact_request_ids(fact: _DesktopFact) -> set[str]:
    return {
        value
        for source in _fact_sources(fact)
        for key in (
            "request_id",
            "source_request_id",
            "tool_call_id",
            "source_tool_call_id",
        )
        for value in [str(source.get(key) or "").strip()]
        if value
    }


def _fact_link_ids(fact: _DesktopFact) -> set[str]:
    values: set[str] = set()
    sources: list[Mapping[str, Any]] = list(_fact_sources(fact))
    for parent in (fact.result, fact.payload):
        for key in (
            "action_target",
            "verification_target",
            "verification_evidence",
            "desktop_loop",
        ):
            nested = parent.get(key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    for source in sources:
        for key in (
            "request_id",
            "source_request_id",
            "replan_request_id",
            "step_id",
            "planner_step_id",
            "source_step_id",
        ):
            value = str(source.get(key) or "").strip()
            if value:
                values.add(value)
        for key in (
            "depends_on",
            "verified_step_ids",
            "verification_target_step_ids",
        ):
            raw_values = source.get(key)
            if not isinstance(raw_values, Iterable) or isinstance(
                raw_values,
                (str, bytes, Mapping),
            ):
                continue
            values.update(
                str(value or "").strip()
                for value in raw_values
                if str(value or "").strip()
            )
    return values


def _fact_target_tokens(fact: _DesktopFact) -> set[str]:
    values: set[str] = set()
    for source in _fact_sources(fact):
        for key in (
            "app_name",
            "target_app_name",
            "expected_app_name",
            "target",
            "target_label",
            "target_search_text",
            "ui_target",
        ):
            value = str(source.get(key) or "").strip().casefold()
            if value:
                values.add(value)
    return values


def _target_sets_match(left: set[str], right: set[str]) -> bool:
    return any(
        left_value == right_value
        or left_value in right_value
        or right_value in left_value
        for left_value in left
        for right_value in right
    )


def _fact_sources(fact: _DesktopFact) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = [fact.result, fact.payload]
    for parent in (fact.result, fact.payload):
        for key in ("data", "input", "input_preview", "metadata"):
            nested = parent.get(key)
            if isinstance(nested, Mapping):
                sources.append(nested)
    return tuple(sources)


def _blocking_conditions(fact: _DesktopFact) -> tuple[str, ...]:
    values: list[str] = []
    for source in _fact_sources(fact):
        for key in ("blocking_condition", "blocking_conditions"):
            value = source.get(key)
            if isinstance(value, str):
                values.extend(part.strip() for part in value.split(",") if part.strip())
            elif isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
                values.extend(str(item).strip() for item in value if str(item).strip())
    return tuple(dict.fromkeys(values))


def _looks_like_permission_blocker(value: str) -> bool:
    lowered = value.lower()
    return any(
        token in lowered
        for token in (
            "accessibility",
            "automation",
            "permission",
            "screen_capture",
            "screen_recording",
        )
    )


def _looks_like_verification_blocker(value: str) -> bool:
    lowered = value.lower()
    return any(
        token in lowered
        for token in (
            "focus_unavailable",
            "foreground",
            "target_not",
            "unverified",
            "verification",
        )
    )


def _fact_status(fact: _DesktopFact) -> str:
    return _first_text(
        fact.result.get("status"),
        fact.payload.get("status"),
    ).lower()


def _is_desktop_tool(tool: str) -> bool:
    return bool(tool) and tool.startswith(_DESKTOP_TOOL_PREFIXES)


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _nonempty(value: Any) -> bool:
    return value not in (None, "", [], {}, ())
