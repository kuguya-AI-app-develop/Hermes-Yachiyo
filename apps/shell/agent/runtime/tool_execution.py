"""Tool call execution coordinator for Agent runtime."""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from apps.shell.agent.runtime.app_aliases import (
    APP_ALIASES,
    GENERIC_APP_ALIAS_COMPACTS,
    compact_app_alias,
)
from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.desktop_execution_providers import (
    LOCAL_DESKTOP_PROVIDER_ID,
    LOCAL_DESKTOP_PROVIDER_KIND,
    default_desktop_execution_provider_registry,
    desktop_execution_route_allows_provider_execution,
    desktop_execution_route_payload,
    desktop_execution_route_requires_provider,
)
from apps.shell.agent.runtime.desktop_provider_session_events import (
    desktop_provider_session_public_event,
)
from apps.shell.agent.runtime.dispatch_semantics import (
    exact_native_dispatch_receipt_matches,
    has_exact_native_dispatch_contract,
    has_intrinsic_native_postcondition_contract,
    intrinsic_native_postcondition_state,
    intrinsic_native_postcondition_target_matches,
    is_semantic_safe_key,
    is_semantic_safe_shortcut,
)
from apps.shell.agent.runtime.errors import (
    AgentApprovalRequired,
    AgentDirectOutcomeUnverified,
    AgentRuntimeError,
    AgentWorkspaceBoundaryError,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_replan_base_event_type as _runtime_replan_base_event_type,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_replan_request_event_payload as _runtime_replan_event_payload,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_replan_request_event_type as _runtime_replan_event_type,
)
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_runtime import (
    _runtime_execution_provider_identity,
)
from apps.shell.agent.runtime.input_bindings import (
    InputBindingResolutionError,
    context_binding_unresolved_result,
    has_explicit_input_bindings,
    resolve_tool_request_input_bindings,
    resolve_workspace_file_selection,
)
from apps.shell.agent.runtime.recovery_identity import (
    ensure_recovery_action_identity,
    recovery_action_identity,
    recovery_request_repeats_stalled_discovery,
)
from apps.shell.agent.runtime.recovery_lineage import (
    RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY,
    trusted_recovery_trace_fields,
)
from apps.shell.agent.runtime.recovery_policies import background_window_source
from apps.shell.agent.runtime.replan_deferred import (
    materialized_deferred_items,
    safe_deferred_continuation_request,
)
from apps.shell.agent.runtime.task_progress import (
    append_task_progress_events_for_tool_result,
    append_task_progress_events_for_tool_start,
)
from apps.shell.agent.runtime.tool_capabilities import (
    capability_ids_for_tool,
)
from apps.shell.agent.runtime.tool_outcomes import from_tool_result
from apps.shell.agent.runtime.tool_requests import (
    ensure_tool_call_id,
    normalize_tool_request_input,
)
from apps.shell.agent.runtime.verification_receipts import (
    APP_WINDOW_PRESENT_PREDICATE,
    EXACT_CLIPBOARD_CONTENT_PRESENT_PREDICATE,
    EXACT_FILE_CONTENT_PRESENT_PREDICATE,
    EXACT_FILE_READBACK_VERIFIER_TOOLS,
    EXACT_PASTED_CONTENT_PRESENT_PREDICATE,
    EXACT_SUBMIT_DISPATCH_PREDICATE,
    EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
    RUNTIME_PRIVATE_VERIFICATION_AUTHORITY,
    RUNTIME_PRIVATE_VERIFICATION_CONTEXT_KEY,
    RUNTIME_PRIVATE_VERIFICATION_CONTEXT_VERSION,
    declared_workspace_output_path,
    normalized_workspace_relative_path,
)
from apps.shell.yachiyo_agent.capability_registry import capability_recovery_tools
from apps.shell.yachiyo_agent.desktop_execution_policy import (
    daily_entrypoint_desktop_execution_policy,
    desktop_execution_route_decision,
    sandbox_desktop_provider_status,
)
from apps.shell.yachiyo_agent.desktop_execution_policy import (
    desktop_execution_policy_mode as _public_desktop_execution_policy_mode,
)
from apps.shell.yachiyo_agent.isolated_provider_session import (
    start_isolated_desktop_provider_session,
)
from apps.shell.yachiyo_agent.policy import desktop_tool_execution_mode_for_input
from packages.security import redact_api_error_text

_TOOL_REQUEST_TRACE_TEXT_KEYS = (
    "tool_call_id",
    "source_tool_call_id",
    "request_id",
    "source_request_id",
    "source_approval_id",
    "source",
    "planning_reason",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "step_id",
    "planner_step_id",
    "source_step_id",
    "capability_id",
    "capability_title",
    "capability_status",
    "capability_reason",
    "core_id",
    "workspace_id",
    "task_id",
    "run_id",
    "run_group_id",
    "group_run_id",
    "group_id",
    "workflow_id",
    "workflow_run_id",
    "workflow_node_id",
    "workflow_node_label",
    "replan_request_id",
    "replan_recovery_action_id",
    "replan_recovery_identity",
    "replan_trigger",
    "recovery_link_kind",
    "recovery_action",
    "recovery_source_tool",
    "recovery_suggested_tool",
    "risk_level",
    "policy_reason",
    "target_app_name",
    "target_app_query",
    "target_search_text",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
    "control_action",
    "api_route",
    "diagnostic_route",
    "goal_contract_id",
    "goal_criterion_id",
    "goal_subgoal_id",
    "materialization_binding_id",
    "materialized_content_sha256",
    "recovery_scope_id",
)

_TOOL_REQUEST_TRACE_BOOL_KEYS = (
    "requires_observation",
    "requires_post_action_verification",
    "recovery_context_trusted",
    "root_goal_unchanged",
    "observation_only",
    "goal_completion_authority",
)

_TOOL_REQUEST_TRACE_LIST_KEYS = (
    "capability_selected_tools",
    "capability_planned_step_ids",
    "replan_triggers",
    "replan_signal_ids",
)

_TOOL_REQUEST_TRACE_MAPPING_KEYS = (
    "followup_target",
    "action_target",
    "workspace_file_resolution",
    "observation_evidence",
    "observation_retry",
    "desktop_execution_policy",
    "desktop_execution_route",
    "desktop_provider_session",
    "sandbox_provider",
    "sandbox_desktop_provider",
)

_TOOL_REQUEST_TRACE_MAPPING_LIST_KEYS = (
    "input_bindings",
    "verification_targets",
    "task_verification_targets",
)


_ARTIFACT_BODY_TEXT_TOOLS = {
    "app.focus_and_safe_type_text",
    "app.open_and_safe_type_text",
    "app.focus_and_type_into_ui_element",
    "app.open_and_type_into_ui_element",
    "desktop.safe_type_text",
    "desktop.type",
    "desktop.type_text",
    "desktop.type_into_ui_element",
}

_ARTIFACT_BODY_SOURCES = {
    "analysis_artifact",
    "analysis_result",
    "artifact",
    "artifact_content",
    "data_analysis",
    "report_artifact",
    "research_artifact",
}

_ARTIFACT_BODY_TEXT_LIMIT = 20000

_MAX_PRIVATE_VERIFICATION_RECEIPTS = 256
_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY = object()
_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_REQUEST_KEY = (
    "_runtime_private_clipboard_source_request"
)
_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_MAX_CHARS = 12000
_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_MAX_TOOL_AGE = 16
_RUNTIME_PRIVATE_CLIPBOARD_PASTE_MAX_TOOL_AGE = 8
_RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_AUTHORITY = object()
_RUNTIME_PRIVATE_EXACT_FILE_READBACK_AUTHORITY = object()
_RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY = object()
_RUNTIME_PRIVATE_SUBMIT_REVALIDATION_AUTHORITY = object()
_RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY = (
    "_runtime_private_prepared_submit_context"
)
_RUNTIME_PRIVATE_EXACT_SUBMIT_RESULT_KEY = "_runtime_private_exact_submit_result"
RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY = (
    "_runtime_prepared_submit_receipt"
)
RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_REQUEST_KEY = (
    "_runtime_private_exact_submit_receipt"
)
_RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY = (
    "_runtime_private_exact_file_readback"
)
_RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_VERSION = 1
_EXACT_SUBMIT_DISPATCH_PREDICATE = EXACT_SUBMIT_DISPATCH_PREDICATE
_EXACT_SUBMIT_DISPATCH_ACTIONS = frozenset({"send", "confirm"})
_EXACT_FILE_READBACK_SOURCE_TOOLS = frozenset({"terminal.run", "python.run"})

_CLIPBOARD_PASTE_TOOLS = frozenset(
    {
        "desktop.safe_shortcut",
        "desktop.shortcut",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
    }
)

_EDITABLE_UI_ROLES = frozenset(
    {
        "combobox",
        "searchfield",
        "textarea",
        "textfield",
        "textview",
    }
)
_TRUSTED_APP_WINDOW_RECEIPT_TOOLS = frozenset({"app.open", "desktop.open_app"})
_TRUSTED_EXACT_TYPED_CONTENT_RECEIPT_TOOLS = frozenset(
    {
        "app.open_and_type_into_ui_element",
        "desktop.type_into_ui_element",
    }
)
_EXACT_TYPED_CONTENT_OBSERVATION_TOOLS = frozenset(_ARTIFACT_BODY_TEXT_TOOLS)

_DESKTOP_PROVIDER_SESSION_CONTROL_SOURCES = {
    "agent_studio_group_replan_recovery",
    "agent_studio_replan_recovery",
    "yachiyo_chat_replan_recovery",
}

_INPUT_PREVIEW_TRACE_KEYS = (
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "step_id",
    "planner_step_id",
    "capability_id",
    "capability_title",
    "capability_status",
    "capability_reason",
    "core_id",
    "workspace_id",
    "task_id",
    "run_id",
    "run_group_id",
    "group_run_id",
    "group_id",
    "workflow_id",
    "workflow_run_id",
    "workflow_node_id",
    "workflow_node_label",
    "replan_request_id",
    "replan_trigger",
    "risk_level",
    "policy_reason",
    "target_app_name",
    "target_app_query",
    "target_search_text",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
    "requires_observation",
    "requires_post_action_verification",
    "capability_selected_tools",
    "capability_planned_step_ids",
    "replan_triggers",
    "replan_signal_ids",
)

_ARTIFACT_CONTEXT_KEYS = (
    "source",
    "planning_reason",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "step_id",
    "planner_step_id",
    "capability_id",
    "capability_title",
    "capability_status",
    "capability_reason",
    "capability_selected_tools",
    "capability_planned_step_ids",
    "core_id",
    "workspace_id",
    "task_id",
    "run_group_id",
    "group_run_id",
    "group_id",
    "workflow_id",
    "workflow_run_id",
    "workflow_node_id",
    "workflow_node_label",
    "replan_request_id",
    "replan_trigger",
    "risk_level",
    "policy_reason",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
)


def _default_allows_tool(tool_name: str, allowed_tools: list[str]) -> bool:
    return tool_name in set(str(tool or "").strip() for tool in allowed_tools)


def _tool_result_with_runtime_execution_provenance(
    tool_result: dict[str, Any],
    *,
    local_broker_executed: bool,
) -> dict[str, Any]:
    result = dict(tool_result)
    result.pop(RUNTIME_EXECUTION_PROVENANCE_KEY, None)
    if local_broker_executed:
        result[RUNTIME_EXECUTION_PROVENANCE_KEY] = {
            "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
            "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
        }
    return result


def _tool_result_with_runtime_submit_dispatch_identity(
    tool_name: str,
    tool_input: Mapping[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    """Project an exact Return dispatch without claiming message delivery."""

    if str(tool_name or "").strip() != "desktop.submit_foreground":
        return tool_result
    requested_action = str(tool_input.get("action") or "").strip().casefold()
    data = (
        tool_result.get("data")
        if isinstance(tool_result.get("data"), Mapping)
        else {}
    )
    if (
        requested_action not in _EXACT_SUBMIT_DISPATCH_ACTIONS
        or tool_result.get("ok") is not True
        or tool_result.get("approval_required")
        or str(tool_result.get("action") or "").strip()
        != "desktop.submit_foreground"
        or str(data.get("submit_action") or "").strip().casefold()
        != requested_action
        or str(data.get("key") or "").strip().casefold() != "return"
        or not isinstance(data.get("modifiers"), list)
        or bool(_string_list(data.get("modifiers")))
    ):
        return tool_result
    return {**dict(tool_result), "submitted_action": requested_action}


def _tool_request_trace_payload(tool_request: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in _TOOL_REQUEST_TRACE_TEXT_KEYS:
        value = str(tool_request.get(key) or "").strip()
        if value:
            payload[key] = value
    for key in _TOOL_REQUEST_TRACE_BOOL_KEYS:
        value = tool_request.get(key)
        if isinstance(value, bool):
            payload[key] = value
    for key in _TOOL_REQUEST_TRACE_LIST_KEYS:
        values = _string_list(tool_request.get(key))
        if values:
            payload[key] = values
    for key in _TOOL_REQUEST_TRACE_MAPPING_KEYS:
        value = tool_request.get(key)
        if isinstance(value, Mapping) and value:
            if key == "desktop_provider_session":
                payload[key] = _public_desktop_provider_session(value)
            else:
                payload[key] = dict(value)
    for key in _TOOL_REQUEST_TRACE_MAPPING_LIST_KEYS:
        values = tool_request.get(key)
        if isinstance(values, list):
            mappings = [dict(value) for value in values if isinstance(value, Mapping)]
            if mappings:
                payload[key] = mappings
    if payload.get("source") in {
        "runtime_internal_recovery",
        "runtime_replan_recovery",
    }:
        payload["visibility"] = "internal"
    return payload


def _authoritative_tool_trace_payload(
    tool_request: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    payload = _tool_request_trace_payload(tool_request)
    clean_run_id = str(run_id or "").strip()
    if clean_run_id:
        payload["run_id"] = clean_run_id
    payload["actor"] = "native_runtime"
    payload["visibility"] = "internal"
    payload["execution_authority"] = "runtime_tool_executor"
    return payload


def _canonical_outcome_recovery_contract(
    tool_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Project only bounded, non-sensitive recovery facts to the sidecar."""

    contract: dict[str, Any] = {}
    completion_impact = str(tool_result.get("completion_impact") or "").strip()
    if tool_result.get("blocked_by_user_goal") is True:
        completion_impact = "continue_without_tool"
    if completion_impact in {"continue_without_tool", "report_refusal"}:
        contract["completion_impact"] = completion_impact
    suggested_tools = _string_list(tool_result.get("suggested_tool"))
    if suggested_tools:
        contract["suggested_tools"] = suggested_tools
    return contract


def _is_desktop_provider_session_start_control(
    tool_name: str,
    tool_request: Mapping[str, Any],
) -> bool:
    control_action = str(tool_request.get("control_action") or "").strip()
    return (
        str(tool_name or "").strip() == "desktop.provider_session.start"
        and control_action == "desktop_provider_session.start"
    )


def _control_action_allows_tool(tool_name: str, tool_request: Mapping[str, Any]) -> bool:
    if not _is_desktop_provider_session_start_control(tool_name, tool_request):
        return False
    source = str(tool_request.get("source") or "").strip()
    if source not in _DESKTOP_PROVIDER_SESSION_CONTROL_SOURCES:
        return False
    return any(
        str(tool_request.get(key) or "").strip()
        for key in ("replan_recovery_action_id", "action_id", "replan_request_id")
    )


def _desktop_provider_session_start_request(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    request: dict[str, Any] = {}
    for key in ("host", "provider_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            request[key] = value
    port = payload.get("port")
    if isinstance(port, int):
        request["port"] = port
    elif isinstance(port, str) and port.strip():
        try:
            request["port"] = int(port.strip())
        except ValueError:
            pass
    tools = _string_list(payload.get("tools")) or _string_list(payload.get("tool_names"))
    if tools:
        request["tools"] = tools
    for key in (
        "requires_real_virtual_desktop_backend",
        "require_real_virtual_desktop_backend",
        "real_virtual_desktop_backend_required",
    ):
        value = _optional_bool(payload.get(key))
        if value is not None:
            request[key] = value
    return request


def _public_desktop_provider_session(session: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in (
        "ok",
        "status",
        "running",
        "started",
        "stopped",
        "pid",
        "provider_id",
        "url",
        "source",
        "needed",
        "reason",
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "keyboard_mouse_capture_supported",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "blocking_conditions",
        "provider_manifest_evidence",
        "provider_conformance",
    ):
        value = session.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    for key in ("request_ids", "tool_names"):
        values = _string_list(session.get(key))
        if values:
            payload[key] = values
    return payload


def _desktop_provider_session_start_control_result(
    tool_request: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    request = _desktop_provider_session_start_request(payload)
    try:
        session = start_isolated_desktop_provider_session(request)
    except Exception as exc:
        session = {
            "ok": False,
            "status": "start_failed",
            "running": False,
            "error": redact_api_error_text(exc),
            "source": "isolated_provider_session_manager",
        }
    public_session = _public_desktop_provider_session(session)
    if "error" in session:
        public_session["error"] = redact_api_error_text(session.get("error"))
    provider_id = str(public_session.get("provider_id") or "local-isolated-desktop")
    ok = bool(public_session.get("ok", True)) and str(
        public_session.get("status") or ""
    ) != "start_failed"
    status = str(public_session.get("status") or ("running" if ok else "start_failed"))
    return {
        "ok": ok,
        "tool": "desktop.provider_session.start",
        "status": status,
        "control_action": "desktop_provider_session.start",
        "desktop_provider_session": public_session,
        "provider_id": provider_id,
        "running": bool(public_session.get("running")),
        "started": bool(public_session.get("started")),
        "summary": (
            f"Isolated desktop provider is {status}: {provider_id}"
            if ok
            else f"Failed to start isolated desktop provider: {provider_id}"
        ),
        "source": str(tool_request.get("source") or "runtime_tool_request_runner"),
    }


def _desktop_provider_session_control_event(
    tool_result: Mapping[str, Any],
) -> tuple[str, str, dict[str, Any]] | None:
    control_action = str(tool_result.get("control_action") or "").strip()
    tool_name = str(tool_result.get("tool") or "").strip()
    if (
        control_action != "desktop_provider_session.start"
        and tool_name != "desktop.provider_session.start"
    ):
        return None
    session = tool_result.get("desktop_provider_session")
    if not isinstance(session, Mapping) or not session:
        return None
    payload = {
        "desktop_provider_session": dict(session),
        "control_action": "desktop_provider_session.start",
        "tool": "desktop.provider_session.start",
    }
    if bool(tool_result.get("ok")) is False or str(session.get("status") or "") == "start_failed":
        return (
            "desktop.provider_session.failed",
            "Isolated desktop provider start failed",
            payload,
        )
    if bool(session.get("started")):
        return (
            "desktop.provider_session.started",
            "Isolated desktop provider started",
            payload,
        )
    if bool(session.get("running")):
        return (
            "desktop.provider_session.ready",
            "Isolated desktop provider already running",
            payload,
        )
    return None


def _desktop_provider_session_required_event(
    tool_name: str,
    tool_result: Mapping[str, Any],
    input_preview: Any,
) -> tuple[str, str, dict[str, Any]] | None:
    action = _desktop_provider_session_start_recovery_action(tool_result)
    if action is None:
        return None
    route = _first_mapping(tool_result.get("desktop_execution_route"))
    sandbox_provider = _first_mapping(
        tool_result.get("sandbox_provider"),
        tool_result.get("sandbox_desktop_provider"),
    )
    action_input = action.get("input") if isinstance(action.get("input"), Mapping) else {}
    provider_id = (
        str(action_input.get("provider_id") or "").strip()
        or str(route.get("selected_provider_id") or "").strip()
        or str(sandbox_provider.get("provider_id") or "").strip()
        or "local-isolated-desktop"
    )
    tool_names = (
        _string_list(action_input.get("tool_names"))
        or _string_list(action_input.get("tools"))
        or _string_list(tool_name)
    )
    session = {
        "ok": True,
        "status": "required",
        "running": False,
        "started": False,
        "needed": True,
        "auto_start": False,
        "provider_id": provider_id,
        "reason": str(
            tool_result.get("blocking_condition")
            or action_input.get("reason")
            or "isolated_provider_required"
        ),
        "tool_names": tool_names,
        "source": "runtime_tool_execution_policy",
        "desktop_session_kind": str(
            route.get("desktop_session_kind")
            or sandbox_provider.get("desktop_session_kind")
            or "isolated_desktop"
        ),
        "desktop_session_isolated": True,
        "foreground_takeover_required": False,
        "keyboard_mouse_capture_supported": bool(
            sandbox_provider.get("keyboard_mouse_capture_supported")
            or route.get("keyboard_mouse_capture_supported")
        ),
    }
    event = desktop_provider_session_public_event(
        session,
        payload_context={
            "tool": tool_name,
            "blocked_tool": tool_name,
            "input_preview": input_preview if isinstance(input_preview, dict) else {},
            "desktop_execution_route": dict(route),
            "sandbox_provider": dict(sandbox_provider),
            "recovery_actions": _mapping_list(tool_result.get("recovery_actions")),
        },
        redact=redact_api_error_text,
    )
    if event is None:
        return None
    return event["event_type"], event["detail"], event["payload"]


def _desktop_provider_session_start_recovery_action(
    tool_result: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    for action in _mapping_list(tool_result.get("recovery_actions")):
        if str(action.get("tool") or "").strip() == "desktop.provider_session.start":
            return action
    return None


def _append_desktop_provider_session_required_event(
    *,
    timeline: list[dict[str, Any]],
    timeline_factory: Callable[..., dict[str, Any]],
    append_run_event: Callable[[str, str, dict[str, Any]], Any],
    run_id: str,
    tool_name: str,
    runtime_skip: Mapping[str, Any],
    input_preview: Any,
    trace_payload: Mapping[str, Any],
) -> None:
    event = _desktop_provider_session_required_event(
        tool_name,
        runtime_skip,
        input_preview,
    )
    if event is None:
        return
    event_type, detail, event_payload = event
    event_payload = _event_payload_with_trace_context(event_payload, dict(trace_payload))
    timeline.append(timeline_factory(event_type, detail, **event_payload))
    if run_id:
        append_run_event(run_id, event_type, event_payload)


def _input_preview_with_trace_payload(
    input_preview: Any,
    trace_payload: dict[str, Any],
) -> Any:
    return input_preview


def _artifact_context_from_trace_payload(trace_payload: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in _ARTIFACT_CONTEXT_KEYS:
        value = trace_payload.get(key)
        if value in (None, "", [], {}):
            continue
        context[key] = value
    return context


def _artifact_with_context(
    artifact: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not context:
        return artifact
    enriched = dict(artifact)
    for key, value in context.items():
        enriched.setdefault(key, value)
    return enriched


def _event_payload_with_artifact_context(
    payload: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not context:
        return payload
    enriched = dict(payload)
    for key, value in context.items():
        enriched.setdefault(key, value)
    artifact = enriched.get("artifact")
    if isinstance(artifact, dict):
        nested_artifact = dict(artifact)
        for key, value in context.items():
            nested_artifact.setdefault(key, value)
        enriched["artifact"] = nested_artifact
    return enriched


def _event_payload_with_trace_context(
    payload: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    if not context:
        return payload
    enriched = dict(payload)
    for key, value in context.items():
        enriched.setdefault(key, value)
    input_preview = enriched.get("input_preview")
    if isinstance(input_preview, dict):
        preview = dict(input_preview)
        for key, value in context.items():
            preview.setdefault(key, value)
        enriched["input_preview"] = preview
    return enriched


def _tool_result_requests_user_recovery(result: dict[str, Any]) -> bool:
    if not isinstance(result, dict):
        return False
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return bool(
        result.get("permission_error")
        or data.get("permission_error")
        or result.get("blocked_by_runtime_readiness")
        or result.get("blocked_by_app_resolution")
        or result.get("blocked_by_file_resolution")
        or result.get("blocked_by_desktop_execution_policy")
        or result.get("blocked_by_desktop_execution_provider")
        or result.get("recovery_actions")
        or data.get("recovery_actions")
        or result.get("permission_targets")
        or data.get("permission_targets")
    )


def _tool_result_failed_verification(result: Mapping[str, Any]) -> bool:
    if not isinstance(result, Mapping):
        return False
    if result.get("verification_failed") is True:
        return True
    if result.get("verification_passed") is False:
        return True
    status = str(
        result.get("verification_status") or result.get("status") or result.get("reason") or ""
    ).strip().lower()
    if status == "verification_failed":
        return True
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if data.get("verification_failed") is True:
        return True
    if data.get("verification_passed") is False:
        return True
    data_status = str(
        data.get("verification_status") or data.get("status") or data.get("reason") or ""
    ).strip().lower()
    return data_status == "verification_failed"


_APPROVAL_DEPENDENCY_SUCCESS_STATUSES = {
    "completed",
    "succeeded",
    "success",
    "verified",
}

_APPROVAL_PREACTION_OBSERVATION_TOOLS = frozenset(
    {
        "desktop.inspect_app",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.active_window",
    }
)
_APPROVAL_TRUSTED_OBSERVATION_SOURCES = frozenset(
    {
        "runtime_planner",
        "runtime_verification",
        "runtime_native_postcondition_receipt",
    }
)

_RUNTIME_PRIVATE_APPROVAL_OBSERVATION_RECEIPTS_KEY = (
    "_runtime_pre_approval_observation_receipts"
)
_RUNTIME_PRIVATE_APPROVAL_OBSERVATION_RECEIPT_VERSION = 1

_APPROVAL_DEPENDENCY_TEXT_EQUIVALENTS: tuple[tuple[str, ...], ...] = (
    ("搜索框", "搜尋框", "查找框", "搜索栏", "search", "search field", "search box"),
    ("搜索", "搜尋", "查找", "查詢", "查询", "search", "find"),
    ("输入框", "輸入框", "文本框", "文字框", "text field", "input", "input field", "text input"),
    ("发送", "發送", "send", "submit"),
    ("确认", "確認", "confirm", "ok"),
    ("按钮", "按鈕", "button"),
    ("链接", "連結", "link"),
)


def _approval_dependency_block_result(
    tool_request: Mapping[str, Any],
    tool_requests: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    *,
    run_id: str = "",
) -> dict[str, Any]:
    dependency_step_ids = _string_list(tool_request.get("depends_on"))
    if not dependency_step_ids:
        return {}
    decision_id = str(tool_request.get("decision_id") or "").strip()
    dependency_statuses = {
        step_id: _approval_dependency_status(
            step_id,
            tool_requests,
            timeline,
            decision_id=decision_id,
            approval_request=tool_request,
            run_id=run_id,
        )
        for step_id in dependency_step_ids
    }
    blocked_statuses = {
        step_id: status
        for step_id, status in dependency_statuses.items()
        if status not in _APPROVAL_DEPENDENCY_SUCCESS_STATUSES
    }
    if not blocked_statuses:
        return {}
    tool_name = str(tool_request.get("tool") or tool_request.get("tool_name") or "").strip()
    blocked_step_ids = list(blocked_statuses)
    result: dict[str, Any] = {
        "ok": False,
        "tool": tool_name,
        "action": tool_name,
        "status": "blocked",
        "error": "approval_dependency_unverified",
        "reason": "approval_dependency_unverified",
        "summary": (
            f"未请求 {tool_name} 审批：前置步骤未成功完成"
            f"（{', '.join(blocked_step_ids)}）。"
        ),
        "blocked_by_approval_dependency": True,
        "blocking_condition": "approval_dependency_unverified",
        "blocking_conditions": ["approval_dependency_unverified"],
        "dependency_step_ids": dependency_step_ids,
        "dependency_statuses": dependency_statuses,
        "blocked_dependency_step_ids": blocked_step_ids,
        "retryable": False,
    }
    for status in ("missing", "skipped", "failed", "unverified"):
        result[f"{status}_dependency_step_ids"] = [
            step_id
            for step_id, dependency_status in blocked_statuses.items()
            if dependency_status == status
        ]
    return result


def _approval_dependency_status(
    step_id: str,
    tool_requests: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    *,
    decision_id: str,
    approval_request: Mapping[str, Any],
    run_id: str,
) -> str:
    dependency_request = _approval_dependency_request(
        step_id,
        tool_requests,
        decision_id=decision_id,
    )
    expected_tool = str(
        (dependency_request or {}).get("tool")
        or (dependency_request or {}).get("tool_name")
        or ""
    ).strip()
    expected_ui_target, expected_ui_role = _approval_dependency_expected_ui_target(
        approval_request,
        dependency_request,
    )
    semantic_ui_observation = bool(
        (dependency_request or {}).get("requires_observation")
        and expected_tool
        in {"desktop.inspect_app", "desktop.ui_elements", "desktop.read_ui"}
        and (expected_ui_target or expected_ui_role)
    )
    requires_verification = bool(
        (dependency_request or {}).get("requires_post_action_verification")
        or semantic_ui_observation
    )
    if requires_verification:
        return _approval_verified_dependency_status(
            step_id,
            dependency_request,
            timeline,
            decision_id=decision_id,
            approval_request=approval_request,
            run_id=run_id,
        )
    request_status = str((dependency_request or {}).get("status") or "").strip().lower()
    if request_status in _APPROVAL_DEPENDENCY_SUCCESS_STATUSES:
        status = request_status
    elif request_status == "skipped":
        status = "skipped"
    elif request_status in {"blocked", "failed", "cancelled", "canceled"}:
        status = "failed"
    else:
        status = "unverified" if dependency_request is not None else "missing"

    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        event_payload = _approval_dependency_event_payload(event)
        event_step_id = str(
            event.get("step_id")
            or event_payload.get("step_id")
            or event.get("planner_step_id")
            or event_payload.get("planner_step_id")
            or ""
        ).strip()
        if event_step_id != step_id:
            continue
        event_decision_id = str(
            event.get("decision_id") or event_payload.get("decision_id") or ""
        ).strip()
        if decision_id and event_decision_id != decision_id:
            continue
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        event_tool = str(
            event.get("tool")
            or event_payload.get("tool")
            or event.get("detail")
            or ""
        ).strip()
        verified_by_step_id = str(
            event.get("verified_by_step_id")
            or event_payload.get("verified_by_step_id")
            or ""
        ).strip()
        is_verification_update = bool(verified_by_step_id)
        if expected_tool and event_tool and event_tool != expected_tool:
            if not is_verification_update or status != "unverified":
                continue
        if event_type.endswith(
            ("agent.tool.call", "agent.tool.skipped", "agent.tool.failed")
        ) and not _approval_dependency_action_identity_matches(
            dependency_request,
            event,
            event_payload,
        ):
            continue
        event_result = _approval_dependency_event_result(event, event_payload)
        event_status = str(
            event.get("status") or event_payload.get("status") or ""
        ).strip().lower()
        if event_type.endswith("agent.tool.skipped"):
            status = "skipped"
            continue
        if event_type.endswith("agent.tool.failed"):
            status = "failed"
            continue
        if event_type.endswith(("agent.task.todo.updated", "agent.task.checkpoint.updated")):
            if event_status in _APPROVAL_DEPENDENCY_SUCCESS_STATUSES:
                if not is_verification_update or status == "unverified":
                    status = event_status
            elif event_status == "skipped":
                status = "skipped"
            elif event_status in {"blocked", "failed", "cancelled", "canceled"}:
                verification_status = str(
                    event.get("verification_status")
                    or event_payload.get("verification_status")
                    or ""
                ).strip().lower()
                status = (
                    "unverified"
                    if verification_status in {"unverified", "verification_failed"}
                    else "failed"
                )
            elif event_status:
                status = "unverified"
            continue
        if not event_type.endswith("agent.tool.call"):
            continue
        status = _approval_dependency_tool_result_status(
            event_result,
            requires_verification=requires_verification,
        )
    return status


def _approval_dependency_request(
    step_id: str,
    tool_requests: list[dict[str, Any]],
    *,
    decision_id: str,
) -> Mapping[str, Any] | None:
    for request in tool_requests:
        if not isinstance(request, Mapping):
            continue
        request_step_id = str(
            request.get("step_id") or request.get("planner_step_id") or ""
        ).strip()
        if request_step_id != step_id:
            continue
        if str(request.get("replan_request_id") or "").strip():
            continue
        request_decision_id = str(request.get("decision_id") or "").strip()
        if decision_id and request_decision_id != decision_id:
            continue
        return request
    return None


def _approval_dependency_event_payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _approval_dependency_event_result(
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
) -> Mapping[str, Any]:
    result = event.get("result")
    if not isinstance(result, Mapping):
        result = event_payload.get("result")
    return result if isinstance(result, Mapping) else {}


def _approval_dependency_action_identity_matches(
    dependency_request: Mapping[str, Any] | None,
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
) -> bool:
    if not dependency_request:
        return True
    expected_identity_found = False
    for key in ("tool_call_id", "request_id"):
        expected = str(dependency_request.get(key) or "").strip()
        if not expected:
            continue
        expected_identity_found = True
        actual = str(event.get(key) or event_payload.get(key) or "").strip()
        if actual:
            return expected == actual
    if not expected_identity_found:
        return True
    # Decision-scoped execution always emits an identity; an identity-free event
    # cannot prove that it belongs to the selected dependency request.
    return not str(dependency_request.get("decision_id") or "").strip()


def _approval_dependency_requires_strong_observation_identity(
    dependency_request: Mapping[str, Any] | None,
    approval_request: Mapping[str, Any],
    *,
    run_id: str,
) -> bool:
    """Return whether a planner-owned observation has a complete trust contract.

    Legacy callers that never received Runtime request identities keep their
    compatibility behavior.  Production planner requests always carry these
    fields; once present, none may be omitted or downgraded when an observation
    unlocks a foreground mutation approval.
    """

    if not _approval_dependency_has_complete_runtime_identity(
        dependency_request,
        approval_request,
        run_id=run_id,
    ):
        return False
    dependency_tool = str(
        dependency_request.get("tool") or dependency_request.get("tool_name") or ""
    ).strip()
    if dependency_tool not in _APPROVAL_PREACTION_OBSERVATION_TOOLS:
        return False
    return True


def _approval_dependency_has_complete_runtime_identity(
    dependency_request: Mapping[str, Any] | None,
    approval_request: Mapping[str, Any],
    *,
    run_id: str,
) -> bool:
    if not dependency_request or not str(run_id or "").strip():
        return False
    required_dependency_fields = (
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "step_id",
        "request_id",
        "tool_call_id",
    )
    required_approval_fields = (
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "step_id",
        "request_id",
        "tool_call_id",
    )
    return bool(all(
        str(dependency_request.get(key) or "").strip()
        for key in required_dependency_fields
    ) and all(
        str(approval_request.get(key) or "").strip()
        for key in required_approval_fields
    ))


def _approval_correlated_active_window_identity_matches(
    dependency_request: Mapping[str, Any] | None,
    approval_request: Mapping[str, Any],
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    timeline: list[dict[str, Any]],
    run_id: str,
) -> bool:
    if not dependency_request:
        return False
    if not _approval_dependency_has_complete_runtime_identity(
        dependency_request,
        approval_request,
        run_id=run_id,
    ):
        # An active-window observation is authorization evidence for the
        # effectful approval that follows.  Legacy/incomplete requests cannot
        # prove which Runtime run, plan, request, and tool call produced that
        # evidence, so they must never unlock the approval path.
        return False
    if _approval_intrinsic_correlated_native_receipt_matches(
        dependency_request,
        approval_request,
        event,
        event_payload,
        result,
        timeline=timeline,
        run_id=run_id,
    ):
        return True
    expected_run_id = str(run_id or "").strip()
    for key in ("decision_id", "plan_id", "tool_plan_id"):
        expected = str(dependency_request.get(key) or "").strip()
        if str(approval_request.get(key) or "").strip() != expected:
            return False
        if str(event.get(key) or event_payload.get(key) or "").strip() != expected:
            return False
    if str(event.get("run_id") or event_payload.get("run_id") or "").strip() != (
        expected_run_id
    ):
        return False
    source_step_id = str(
        event.get("source_step_id") or event_payload.get("source_step_id") or ""
    ).strip()
    expected_step_id = str(
        dependency_request.get("step_id")
        or dependency_request.get("planner_step_id")
        or ""
    ).strip()
    if not expected_step_id or source_step_id != expected_step_id:
        return False
    for event_key, request_key in (
        ("source_request_id", "request_id"),
        ("source_tool_call_id", "tool_call_id"),
    ):
        actual = str(event.get(event_key) or event_payload.get(event_key) or "").strip()
        expected = str(dependency_request.get(request_key) or "").strip()
        if not expected or actual != expected:
            return False
    if str(event.get("actor") or event_payload.get("actor") or "").strip() != (
        "native_runtime"
    ):
        return False
    if str(
        event.get("execution_authority")
        or event_payload.get("execution_authority")
        or ""
    ).strip() != "runtime_tool_executor":
        return False
    if str(event.get("visibility") or event_payload.get("visibility") or "").strip() != (
        "internal"
    ):
        return False
    if str(event.get("source") or event_payload.get("source") or "").strip() not in (
        _APPROVAL_TRUSTED_OBSERVATION_SOURCES
    ):
        return False
    if _approval_observation_provider_identity(
        result,
        expected_tool="desktop.active_window",
    ) is None:
        return False
    return _approval_dependency_active_window_identity_verified(
        result,
        dependency_request,
        approval_request=approval_request,
        event_tool="desktop.active_window",
    )


def _approval_intrinsic_correlated_native_receipt_matches(
    dependency_request: Mapping[str, Any],
    approval_request: Mapping[str, Any],
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    timeline: list[dict[str, Any]],
    run_id: str,
) -> bool:
    """Authenticate one intrinsic receipt through its exact source action."""

    expected_tool = str(
        dependency_request.get("tool")
        or dependency_request.get("tool_name")
        or ""
    ).strip()
    expected_step_id = str(
        dependency_request.get("step_id")
        or dependency_request.get("planner_step_id")
        or ""
    ).strip()
    expected_request_id = str(dependency_request.get("request_id") or "").strip()
    expected_tool_call_id = str(
        dependency_request.get("tool_call_id") or ""
    ).strip()
    expected_plan_id = str(dependency_request.get("plan_id") or "").strip()
    expected_tool_plan_id = str(
        dependency_request.get("tool_plan_id") or ""
    ).strip()
    clean_run_id = str(run_id or "").strip()
    if not all(
        (
            expected_tool,
            expected_step_id,
            expected_request_id,
            expected_tool_call_id,
            expected_plan_id,
            expected_tool_plan_id,
            clean_run_id,
        )
    ):
        return False
    for key, expected in (
        ("decision_id", str(dependency_request.get("decision_id") or "").strip()),
        ("plan_id", expected_plan_id),
        ("tool_plan_id", expected_tool_plan_id),
    ):
        if not expected or str(approval_request.get(key) or "").strip() != expected:
            return False
        if str(event.get(key) or event_payload.get(key) or "").strip() != expected:
            return False
    if (
        str(event.get("run_id") or event_payload.get("run_id") or "").strip()
        != clean_run_id
        or str(event.get("source") or event_payload.get("source") or "").strip()
        != "runtime_native_postcondition_receipt"
        or str(event.get("actor") or event_payload.get("actor") or "").strip()
        != "native_runtime"
        or str(
            event.get("execution_authority")
            or event_payload.get("execution_authority")
            or ""
        ).strip()
        != "runtime_tool_executor"
        or str(event.get("visibility") or event_payload.get("visibility") or "").strip()
        != "internal"
        or not str(event.get("request_id") or event_payload.get("request_id") or "").strip()
        or not str(
            event.get("tool_call_id")
            or event_payload.get("tool_call_id")
            or ""
        ).strip()
        or result.get("ok") is not True
        or result.get("postcondition_verified") is not True
        or result.get("verification_satisfied_by_native_receipt") is not True
        or str(result.get("action") or "").strip() != "desktop.active_window"
        or str(result.get("source_tool") or "").strip() != expected_tool
        or str(result.get("source_step_id") or "").strip() != expected_step_id
        or str(result.get("source_request_id") or "").strip()
        != expected_request_id
        or str(result.get("source_tool_call_id") or "").strip()
        != expected_tool_call_id
    ):
        return False
    for field, expected in (
        ("source_step_id", expected_step_id),
        ("source_request_id", expected_request_id),
        ("source_tool_call_id", expected_tool_call_id),
    ):
        if str(event.get(field) or event_payload.get(field) or "").strip() != expected:
            return False

    verified_state = str(
        result.get("verified_observed_state")
        or (
            result.get("data", {}).get("verified_observed_state")
            if isinstance(result.get("data"), Mapping)
            else ""
        )
        or ""
    ).strip()
    claimed_provider = (
        str(result.get("provider_kind") or "").strip(),
        str(result.get("provider_id") or "").strip(),
    )
    if not verified_state or not all(claimed_provider):
        return False

    for source_event in reversed(timeline):
        if not isinstance(source_event, Mapping):
            continue
        source_payload = _approval_dependency_event_payload(source_event)
        if str(
            source_event.get("event")
            or source_event.get("event_type")
            or ""
        ).strip() != "agent.tool.call":
            continue
        source_result = _approval_dependency_event_result(
            source_event,
            source_payload,
        )
        source_input = (
            source_event.get("input_preview")
            if isinstance(source_event.get("input_preview"), Mapping)
            else source_payload.get("input_preview")
            if isinstance(source_payload.get("input_preview"), Mapping)
            else {}
        )
        source_target = _first_mapping(
            source_event.get("action_target"),
            source_payload.get("action_target"),
        )
        if (
            str(source_event.get("run_id") or source_payload.get("run_id") or "").strip()
            != clean_run_id
            or str(
                source_event.get("decision_id")
                or source_payload.get("decision_id")
                or ""
            ).strip()
            != str(dependency_request.get("decision_id") or "").strip()
            or str(source_event.get("plan_id") or source_payload.get("plan_id") or "").strip()
            != expected_plan_id
            or str(
                source_event.get("tool_plan_id")
                or source_payload.get("tool_plan_id")
                or ""
            ).strip()
            != expected_tool_plan_id
            or str(source_event.get("step_id") or source_payload.get("step_id") or "").strip()
            != expected_step_id
            or str(
                source_event.get("request_id")
                or source_payload.get("request_id")
                or ""
            ).strip()
            != expected_request_id
            or str(
                source_event.get("tool_call_id")
                or source_payload.get("tool_call_id")
                or ""
            ).strip()
            != expected_tool_call_id
            or str(
                source_event.get("tool")
                or source_payload.get("tool")
                or source_event.get("detail")
                or ""
            ).strip()
            != expected_tool
            or str(source_event.get("actor") or source_payload.get("actor") or "").strip()
            != "native_runtime"
            or str(
                source_event.get("execution_authority")
                or source_payload.get("execution_authority")
                or ""
            ).strip()
            != "runtime_tool_executor"
            or str(
                source_event.get("visibility")
                or source_payload.get("visibility")
                or ""
            ).strip()
            != "internal"
        ):
            continue
        source_provider = _trusted_runtime_execution_provider_identity(
            source_event,
            source_result,
        )
        intrinsic_state = intrinsic_native_postcondition_state(
            expected_tool,
            source_input,
            source_result,
        )
        return bool(
            all(source_provider)
            and source_provider == claimed_provider
            and intrinsic_state == verified_state
            and intrinsic_native_postcondition_target_matches(
                expected_tool,
                source_input,
                source_target,
            )
        )
    return False


def _approval_preaction_observation_identity_matches(
    dependency_request: Mapping[str, Any] | None,
    approval_request: Mapping[str, Any],
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    run_id: str,
) -> bool:
    if not dependency_request:
        return False
    expected_run_id = str(run_id or "").strip()
    if not expected_run_id:
        return False
    for key in ("decision_id", "plan_id", "tool_plan_id"):
        dependency_value = str(dependency_request.get(key) or "").strip()
        approval_value = str(approval_request.get(key) or "").strip()
        actual_value = str(event.get(key) or event_payload.get(key) or "").strip()
        if not dependency_value or approval_value != dependency_value:
            return False
        if actual_value != dependency_value:
            return False
    exact_fields = {
        "run_id": expected_run_id,
        "step_id": str(
            dependency_request.get("step_id")
            or dependency_request.get("planner_step_id")
            or ""
        ).strip(),
        "request_id": str(dependency_request.get("request_id") or "").strip(),
        "tool_call_id": str(dependency_request.get("tool_call_id") or "").strip(),
    }
    if any(not value for value in exact_fields.values()):
        return False
    for key, expected in exact_fields.items():
        actual = str(event.get(key) or event_payload.get(key) or "").strip()
        if actual != expected:
            return False
    expected_tool = str(
        dependency_request.get("tool") or dependency_request.get("tool_name") or ""
    ).strip()
    actual_tool = str(
        event.get("tool")
        or event_payload.get("tool")
        or event.get("detail")
        or ""
    ).strip()
    if not expected_tool or actual_tool != expected_tool:
        return False
    if str(event.get("actor") or event_payload.get("actor") or "").strip() != (
        "native_runtime"
    ):
        return False
    if str(
        event.get("execution_authority")
        or event_payload.get("execution_authority")
        or ""
    ).strip() != "runtime_tool_executor":
        return False
    if str(event.get("visibility") or event_payload.get("visibility") or "").strip() != (
        "internal"
    ):
        return False
    source = str(event.get("source") or event_payload.get("source") or "").strip()
    if source not in _APPROVAL_TRUSTED_OBSERVATION_SOURCES:
        return False
    return _approval_observation_provider_identity(result, expected_tool=expected_tool) is not None


def _approval_observation_provider_identity(
    result: Mapping[str, Any],
    *,
    expected_tool: str,
) -> tuple[str, str] | None:
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return None
    provider = (
        result.get("desktop_execution_provider")
        if isinstance(result.get("desktop_execution_provider"), Mapping)
        else {}
    )
    route = (
        result.get("desktop_execution_route")
        if isinstance(result.get("desktop_execution_route"), Mapping)
        else {}
    )
    evidence = (
        result.get("desktop_execution_evidence")
        if isinstance(result.get("desktop_execution_evidence"), Mapping)
        else {}
    )
    provider_id = str(provider.get("provider_id") or "").strip()
    provider_kind = str(provider.get("provider_kind") or "").strip()
    provenance = (
        result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
        if isinstance(result.get(RUNTIME_EXECUTION_PROVENANCE_KEY), Mapping)
        else {}
    )
    local_broker_receipt = bool(
        provenance.get("source") == RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        and provenance.get("version") == RUNTIME_EXECUTION_PROVENANCE_VERSION
    )
    if not provider_id and not provider_kind and local_broker_receipt:
        provider_id = RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        provider_kind = "runtime_local"
    elif not provider_id or not provider_kind:
        return None
    if provider:
        if str(route.get("selected_provider_id") or "").strip() != provider_id:
            return None
        if str(route.get("selected_provider_kind") or "").strip() != provider_kind:
            return None
    provider_receipt = bool(
        result.get("desktop_execution_provider_routed") is True
        and evidence.get("ok") is True
        and str(evidence.get("tool") or "").strip() == str(expected_tool or "").strip()
        and str(evidence.get("provider_id") or "").strip() == provider_id
        and str(evidence.get("provider_kind") or "").strip() == provider_kind
    )
    if not (local_broker_receipt or provider_receipt):
        return None
    return provider_id, provider_kind


def _approval_verified_dependency_status(
    step_id: str,
    dependency_request: Mapping[str, Any] | None,
    timeline: list[dict[str, Any]],
    *,
    decision_id: str,
    approval_request: Mapping[str, Any],
    run_id: str,
) -> str:
    expected_tool = str(
        (dependency_request or {}).get("tool")
        or (dependency_request or {}).get("tool_name")
        or ""
    ).strip()
    request_status = str((dependency_request or {}).get("status") or "").strip().lower()
    action_status = (
        "skipped"
        if request_status == "skipped"
        else "failed"
        if request_status in {"blocked", "failed", "cancelled", "canceled"}
        else ""
    )
    semantic_verification_succeeded = False

    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        event_payload = _approval_dependency_event_payload(event)
        event_decision_id = str(
            event.get("decision_id") or event_payload.get("decision_id") or ""
        ).strip()
        if decision_id and event_decision_id != decision_id:
            continue
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        event_step_id = str(
            event.get("step_id")
            or event_payload.get("step_id")
            or event.get("planner_step_id")
            or event_payload.get("planner_step_id")
            or ""
        ).strip()
        event_tool = str(
            event.get("tool")
            or event_payload.get("tool")
            or event.get("detail")
            or ""
        ).strip()
        event_result = _approval_dependency_event_result(event, event_payload)
        event_status = str(
            event.get("status") or event_payload.get("status") or ""
        ).strip().lower()

        if event_step_id == step_id:
            is_action_event = event_type.endswith(
                ("agent.tool.call", "agent.tool.skipped", "agent.tool.failed")
            )
            if is_action_event:
                if expected_tool and event_tool and event_tool != expected_tool:
                    continue
                if not _approval_dependency_action_identity_matches(
                    dependency_request,
                    event,
                    event_payload,
                ):
                    continue
                if (
                    expected_tool in _APPROVAL_PREACTION_OBSERVATION_TOOLS
                    and bool((dependency_request or {}).get("requires_observation"))
                    and _approval_dependency_requires_strong_observation_identity(
                        dependency_request,
                        approval_request,
                        run_id=run_id,
                    )
                    and not _approval_preaction_observation_identity_matches(
                        dependency_request,
                        approval_request,
                        event,
                        event_payload,
                        event_result,
                        run_id=run_id,
                    )
                ):
                    continue
            if event_type.endswith("agent.tool.skipped"):
                action_status = "skipped"
                semantic_verification_succeeded = False
                continue
            if event_type.endswith("agent.tool.failed"):
                action_status = "failed"
                semantic_verification_succeeded = False
                continue
            if event_type.endswith(("agent.task.todo.updated", "agent.task.checkpoint.updated")):
                if event_status == "skipped":
                    action_status = "skipped"
                elif event_status in {"blocked", "failed", "cancelled", "canceled"}:
                    verification_status = str(
                        event.get("verification_status")
                        or event_payload.get("verification_status")
                        or ""
                    ).strip().lower()
                    action_status = (
                        ""
                        if verification_status in {"unverified", "verification_failed"}
                        else "failed"
                    )
                # Completed todo/checkpoint rows are projections, not execution evidence.
                continue
            if event_type.endswith("agent.tool.call"):
                action_result_status = _approval_dependency_tool_result_status(
                    event_result,
                    requires_verification=False,
                )
                if action_result_status == "completed":
                    action_status = "completed"
                    requires_independent_active_window = bool(
                        (dependency_request or {}).get(
                            "requires_post_action_verification"
                        )
                        and expected_tool
                        in {
                            "app.open",
                            "desktop.open_app",
                            "app.focus",
                            "desktop.focus_app",
                            "app.focus_window",
                        }
                    )
                    semantic_verification_succeeded = bool(
                        not requires_independent_active_window
                        and _approval_dependency_semantic_verification_succeeded(
                            event_result,
                            dependency_request,
                            approval_request=approval_request,
                            event=event,
                            event_payload=event_payload,
                            event_tool=event_tool,
                        )
                    )
                elif action_result_status == "failed":
                    action_status = "failed"
                    semantic_verification_succeeded = False
                else:
                    action_status = ""
                    semantic_verification_succeeded = False
                continue

        if not event_type.endswith("agent.tool.call"):
            continue
        if not _approval_dependency_verifier_correlates(
            step_id,
            dependency_request,
            event,
            event_payload,
        ):
            continue
        if (
            event_tool == "desktop.active_window"
            and not _approval_correlated_active_window_identity_matches(
                dependency_request,
                approval_request,
                event,
                event_payload,
                event_result,
                timeline=timeline,
                run_id=run_id,
            )
        ):
            continue
        intrinsic_native_receipt_verified = bool(
            event_tool == "desktop.active_window"
            and dependency_request
            and _approval_intrinsic_correlated_native_receipt_matches(
                dependency_request,
                approval_request,
                event,
                event_payload,
                event_result,
                timeline=timeline,
                run_id=run_id,
            )
        )
        semantic_verification_succeeded = intrinsic_native_receipt_verified or (
            _approval_dependency_semantic_verification_succeeded(
                event_result,
                dependency_request,
                approval_request=approval_request,
                event=event,
                event_payload=event_payload,
                event_tool=event_tool,
            )
        )

    if action_status == "skipped":
        return "skipped"
    if action_status == "failed":
        return "failed"
    if action_status == "completed" and semantic_verification_succeeded:
        return "verified"
    return "unverified"


def _approval_dependency_verifier_correlates(
    step_id: str,
    dependency_request: Mapping[str, Any] | None,
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
) -> bool:
    event_tool = str(
        event.get("tool")
        or event_payload.get("tool")
        or event.get("detail")
        or ""
    ).strip()
    runtime_stage = str(
        event.get("runtime_stage") or event_payload.get("runtime_stage") or ""
    ).strip()
    runtime_role = str(
        event.get("runtime_role") or event_payload.get("runtime_role") or ""
    ).strip()
    source = str(
        event.get("source") or event_payload.get("source") or ""
    ).strip()
    native_receipt = source == "runtime_native_postcondition_receipt"
    declared_verifier_identity = any(
        bool(
            str(candidate.get("source_request_id") or "").strip()
            or str(candidate.get("source_step_id") or "").strip()
            or _string_list(candidate.get("depends_on"))
            or _mapping_list(candidate.get("verification_targets"))
            or _mapping_list(candidate.get("task_verification_targets"))
        )
        for candidate in (event, event_payload)
    )
    if (
        event_tool not in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
        or not (
            runtime_stage == "verify"
            or runtime_role == "verify_result"
            or native_receipt
            or declared_verifier_identity
        )
    ):
        # A mutation awaiting approval can depend on an observation step, but
        # that dependency edge does not make the mutation a verifier.  Letting
        # it participate here can overwrite an already valid semantic receipt.
        return False
    if native_receipt and (
        str(event.get("actor") or event_payload.get("actor") or "").strip()
        != "native_runtime"
        or str(
            event.get("execution_authority")
            or event_payload.get("execution_authority")
            or ""
        ).strip()
        != "runtime_tool_executor"
        or str(
            event.get("visibility") or event_payload.get("visibility") or ""
        ).strip()
        != "internal"
    ):
        return False
    expected_request_id = str((dependency_request or {}).get("request_id") or "").strip()
    source_request_id = str(
        event.get("source_request_id") or event_payload.get("source_request_id") or ""
    ).strip()
    if expected_request_id and source_request_id:
        if expected_request_id != source_request_id:
            return False
        return True

    for source in (event, event_payload):
        source_step_id = str(source.get("source_step_id") or "").strip()
        if source_step_id == step_id:
            return True
        if step_id in _string_list(source.get("depends_on")):
            return True
        for key in ("verification_targets", "task_verification_targets"):
            if any(
                str(target.get("step_id") or "").strip() == step_id
                for target in _mapping_list(source.get(key))
            ):
                return True
        action_target = _first_mapping(source.get("action_target"))
        if step_id in _string_list(action_target.get("verified_step_ids")):
            return True
    return False


_APPROVAL_DEPENDENCY_CONTENT_VERIFICATION_KEYS = (
    "content_match_verified",
    "content_verified",
    "content_visible_verified",
    "draft_content_verified",
    "typed_content_verified",
)

_APPROVAL_DEPENDENCY_GENERAL_VERIFICATION_KEYS = (
    *_APPROVAL_DEPENDENCY_CONTENT_VERIFICATION_KEYS,
    "focus_verified",
    "foreground_ready",
    "launch_verified",
    "postcondition_verified",
    "target_reached",
    "target_visible",
    "verification_passed",
    "verified",
)

_APPROVAL_DEPENDENCY_TARGET_VERIFICATION_KEYS = (
    "expected_target_focused",
    "target_match_verified",
    "target_reached",
    "target_visible",
    "ui_target_verified",
)


def _approval_dependency_semantic_verification_succeeded(
    result: Mapping[str, Any],
    dependency_request: Mapping[str, Any] | None,
    *,
    approval_request: Mapping[str, Any],
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    event_tool: str,
) -> bool:
    if not isinstance(result, Mapping) or result.get("ok") is not True:
        return False
    if _tool_result_failed_verification(result):
        return False
    expected_tool = str(
        (dependency_request or {}).get("tool")
        or (dependency_request or {}).get("tool_name")
        or ""
    ).strip()
    if _approval_dependency_is_clipboard_paste(dependency_request):
        return _approval_dependency_exact_paste_receipt_succeeded(
            result,
            dependency_request,
            event=event,
            event_payload=event_payload,
            event_tool=event_tool,
        )
    if _approval_dependency_is_clipboard_copy(dependency_request):
        return _approval_dependency_exact_copy_receipt_succeeded(
            result,
            dependency_request,
            event=event,
            event_payload=event_payload,
            event_tool=event_tool,
        )
    if is_semantic_safe_shortcut(expected_tool):
        # The shortcut executor/provider cannot attest its own semantic UI
        # effect.  Only dedicated exact copy/paste observation receipts above
        # may unlock a dependent approval.
        return False
    content_mutation = expected_tool in _ARTIFACT_BODY_TEXT_TOOLS
    expected_target, expected_role = _approval_dependency_expected_ui_target(
        approval_request,
        dependency_request,
    )
    if content_mutation:
        verification_keys = _APPROVAL_DEPENDENCY_CONTENT_VERIFICATION_KEYS
    elif expected_target or expected_role:
        verification_keys = _APPROVAL_DEPENDENCY_TARGET_VERIFICATION_KEYS
    else:
        verification_keys = _APPROVAL_DEPENDENCY_GENERAL_VERIFICATION_KEYS
    sources = _approval_dependency_structured_result_sources(result)
    if any(source.get(key) is False for source in sources for key in verification_keys):
        return False
    if str(event_tool or "").strip() == "desktop.active_window":
        return bool(
            not content_mutation
            and not (expected_target or expected_role)
            and _approval_dependency_active_window_identity_verified(
                result,
                dependency_request,
                approval_request=approval_request,
                event_tool=event_tool,
            )
        )
    if any(source.get(key) is True for source in sources for key in verification_keys):
        return True
    if content_mutation:
        expected_text = _approval_dependency_expected_content(dependency_request)
        return bool(
            expected_text
            and event_tool
            in {
                "desktop.inspect_app",
                "desktop.ui_elements",
                "desktop.read_ui",
                "desktop.verify",
            }
            and _approval_dependency_observed_content_matches(result, expected_text)
        )
    if expected_target or expected_role:
        return bool(
            event_tool
            in {
                "desktop.inspect_app",
                "desktop.ui_elements",
                "desktop.read_ui",
                "desktop.verify",
            }
            and _approval_dependency_ui_app_identity_verified(
                result,
                dependency_request,
                approval_request=approval_request,
            )
            and _approval_dependency_ui_observation_matches_target(
                result,
                expected_target=expected_target,
                expected_role=expected_role,
            )
        )
    if event_tool in {"desktop.ui_elements", "desktop.read_ui"}:
        return _approval_dependency_ui_observation_has_content(result)
    return False


def _approval_dependency_is_clipboard_paste(
    dependency_request: Mapping[str, Any] | None,
) -> bool:
    return _is_clipboard_paste_request(dependency_request)


def _is_clipboard_paste_request(
    request: Mapping[str, Any] | None,
) -> bool:
    if not request:
        return False
    tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
    if tool_name not in _CLIPBOARD_PASTE_TOOLS:
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return str(payload.get("action") or "").strip().lower() == "paste"


def _approval_dependency_is_clipboard_copy(
    dependency_request: Mapping[str, Any] | None,
) -> bool:
    if not dependency_request:
        return False
    tool_name = str(
        dependency_request.get("tool") or dependency_request.get("tool_name") or ""
    ).strip()
    if tool_name not in {"desktop.safe_shortcut", "desktop.shortcut"}:
        return False
    payload = (
        dependency_request.get("input")
        if isinstance(dependency_request.get("input"), Mapping)
        else {}
    )
    return str(payload.get("action") or "").strip().lower() == "copy"


def _approval_dependency_exact_copy_receipt_succeeded(
    result: Mapping[str, Any],
    dependency_request: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    event_tool: str,
) -> bool:
    if str(event_tool or "").strip() != "clipboard.read":
        return False
    if str(event.get("source") or event_payload.get("source") or "").strip() != (
        "runtime_native_postcondition_receipt"
    ):
        return False
    if str(event.get("actor") or event_payload.get("actor") or "").strip() != (
        "native_runtime"
    ):
        return False
    if str(
        event.get("execution_authority")
        or event_payload.get("execution_authority")
        or ""
    ).strip() != "runtime_tool_executor":
        return False
    expected_step_id = str(
        dependency_request.get("step_id")
        or dependency_request.get("planner_step_id")
        or ""
    ).strip()
    expected_request_id = str(dependency_request.get("request_id") or "").strip()
    expected_tool_call_id = str(
        dependency_request.get("tool_call_id") or ""
    ).strip()
    if not all((expected_step_id, expected_request_id, expected_tool_call_id)):
        return False
    content_sha256 = str(result.get("content_sha256") or "").strip().lower()
    return bool(
        result.get("ok") is True
        and result.get("postcondition_verified") is True
        and result.get("verification_satisfied_by_native_receipt") is True
        and result.get("clipboard_source_verified") is True
        and str(result.get("source_step_id") or "").strip() == expected_step_id
        and str(result.get("source_request_id") or "").strip() == expected_request_id
        and str(result.get("source_tool_call_id") or "").strip()
        == expected_tool_call_id
        and len(content_sha256) == 64
        and all(char in "0123456789abcdef" for char in content_sha256)
    )


def _approval_dependency_exact_paste_receipt_succeeded(
    result: Mapping[str, Any],
    dependency_request: Mapping[str, Any],
    *,
    event: Mapping[str, Any],
    event_payload: Mapping[str, Any],
    event_tool: str,
) -> bool:
    """Accept paste only from an exact, Runtime-owned source/readback receipt."""

    if str(event_tool or "").strip() not in {
        "desktop.inspect_app",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.verify",
    }:
        return False
    if str(event.get("source") or event_payload.get("source") or "").strip() != (
        "runtime_native_postcondition_receipt"
    ):
        return False
    if str(event.get("actor") or event_payload.get("actor") or "").strip() != (
        "native_runtime"
    ):
        return False
    if str(
        event.get("execution_authority")
        or event_payload.get("execution_authority")
        or ""
    ).strip() != "runtime_tool_executor":
        return False
    if str(event.get("visibility") or event_payload.get("visibility") or "").strip() != (
        "internal"
    ):
        return False
    expected_step_id = str(
        dependency_request.get("step_id")
        or dependency_request.get("planner_step_id")
        or ""
    ).strip()
    expected_request_id = str(dependency_request.get("request_id") or "").strip()
    expected_tool_call_id = str(
        dependency_request.get("tool_call_id") or ""
    ).strip()
    expected_tool = str(
        dependency_request.get("tool") or dependency_request.get("tool_name") or ""
    ).strip()
    if not all(
        (
            expected_step_id,
            expected_request_id,
            expected_tool_call_id,
            expected_tool,
        )
    ):
        return False
    if str(event.get("source_step_id") or event_payload.get("source_step_id") or "").strip() != (
        expected_step_id
    ):
        return False
    if str(
        event.get("source_request_id")
        or event_payload.get("source_request_id")
        or result.get("source_request_id")
        or ""
    ).strip() != expected_request_id:
        return False
    if str(
        event.get("source_tool_call_id")
        or event_payload.get("source_tool_call_id")
        or result.get("source_tool_call_id")
        or ""
    ).strip() != expected_tool_call_id:
        return False
    if str(result.get("source_tool") or "").strip() != expected_tool:
        return False
    content_sha256 = str(result.get("content_sha256") or "").strip().lower()
    clipboard_source_request_id = str(
        result.get("clipboard_source_request_id") or ""
    ).strip()
    clipboard_source_tool_call_id = str(
        result.get("clipboard_source_tool_call_id") or ""
    ).strip()
    clipboard_source_step_id = str(
        result.get("clipboard_source_step_id") or ""
    ).strip()
    result_run_id = str(result.get("run_id") or "").strip()
    result_plan_id = str(result.get("plan_id") or "").strip()
    result_provider_kind = str(result.get("provider_kind") or "").strip()
    result_provider_id = str(result.get("provider_id") or "").strip()
    expected_app_name = _approval_dependency_request_app_name(dependency_request)
    observed_app_name = str(result.get("observed_app_name") or "").strip()
    target_app_name = str(result.get("target_app_name") or "").strip()
    return bool(
        result.get("ok") is True
        and result.get("postcondition_verified") is True
        and result.get("verification_satisfied_by_native_receipt") is True
        and result.get("clipboard_source_verified") is True
        and result.get("target_ui_readback_verified") is True
        and result.get("target_ui_editable_verified") is True
        and str(result.get("verification_predicate_kind") or "").strip()
        == EXACT_PASTED_CONTENT_PRESENT_PREDICATE
        and len(content_sha256) == 64
        and all(char in "0123456789abcdef" for char in content_sha256)
        and str(result.get("paste_request_id") or "").strip()
        == expected_request_id
        and result_run_id
        and result_plan_id
        and str(result.get("clipboard_source_run_id") or "").strip()
        == result_run_id
        and str(result.get("clipboard_source_plan_id") or "").strip()
        == result_plan_id
        and result_provider_kind
        and result_provider_id
        and str(
            result.get("clipboard_source_provider_kind") or ""
        ).strip()
        == result_provider_kind
        and str(result.get("clipboard_source_provider_id") or "").strip()
        == result_provider_id
        and clipboard_source_step_id
        and clipboard_source_request_id
        and clipboard_source_tool_call_id
        and isinstance(result.get("content_length"), int)
        and not isinstance(result.get("content_length"), bool)
        and int(result.get("content_length") or 0) > 0
        and isinstance(result.get("content_byte_length"), int)
        and not isinstance(result.get("content_byte_length"), bool)
        and int(result.get("content_byte_length") or 0) > 0
        and (
            not expected_app_name
            or (
                observed_app_name
                and target_app_name
                and _app_lookups_same_identity(
                    expected_app_name,
                    observed_app_name,
                )
                and _app_lookups_same_identity(
                    expected_app_name,
                    target_app_name,
                )
            )
        )
    )


def _approval_dependency_active_window_identity_verified(
    result: Mapping[str, Any],
    dependency_request: Mapping[str, Any] | None,
    *,
    approval_request: Mapping[str, Any],
    event_tool: str,
) -> bool:
    """Accept only a correlated, post-action active-window identity receipt."""

    if str(event_tool or "").strip() != "desktop.active_window":
        return False
    reported_action = str(result.get("action") or result.get("tool") or "").strip()
    if reported_action != "desktop.active_window":
        return False
    expected_app_name = _approval_dependency_request_app_name(
        dependency_request or {}
    )
    approval_app_name = _approval_dependency_request_app_name(approval_request)
    if not (
        expected_app_name
        and approval_app_name
        and _app_lookups_same_identity(expected_app_name, approval_app_name)
    ):
        return False
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    observed_app_name = str(
        data.get("active_app_name")
        or data.get("app_name")
        or data.get("frontmost_app")
        or ""
    ).strip()
    if not _app_lookups_same_identity(expected_app_name, observed_app_name):
        return False
    expected_window_title = (
        _approval_dependency_request_window_title(approval_request)
        or _approval_dependency_request_window_title(dependency_request or {})
    )
    if not expected_window_title:
        return True
    observed_window_title = str(
        data.get("window_title") or data.get("title") or ""
    ).strip()
    return bool(
        observed_window_title
        and _approval_dependency_semantic_token(observed_window_title)
        == _approval_dependency_semantic_token(expected_window_title)
    )


def _approval_dependency_ui_app_identity_verified(
    result: Mapping[str, Any],
    dependency_request: Mapping[str, Any] | None,
    *,
    approval_request: Mapping[str, Any],
) -> bool:
    dependency_app = _approval_dependency_request_app_name(dependency_request or {})
    approval_app = _approval_dependency_request_app_name(approval_request)
    if dependency_app and approval_app and not _app_lookups_same_identity(
        dependency_app,
        approval_app,
    ):
        return False
    expected_app = approval_app or dependency_app
    if not expected_app:
        return True
    observed_app = ""
    for source in _approval_dependency_structured_result_sources(result):
        observed_app = str(
            source.get("active_app_name")
            or source.get("app_name")
            or source.get("frontmost_app")
            or ""
        ).strip()
        if observed_app:
            break
    return bool(observed_app and _app_lookups_same_identity(expected_app, observed_app))


def _approval_dependency_request_app_name(
    request: Mapping[str, Any],
) -> str:
    """Read the app identity from executable input or planner-owned context.

    Foreground submit/close tools intentionally have no ``app_name`` argument;
    their app scope is carried by the correlated action target/desktop loop.
    Both the dependency and approval contexts are still required to agree
    before an active-window observation can unlock approval.
    """

    raw_input = request.get("input")
    sources: list[Mapping[str, Any]] = [
        raw_input if isinstance(raw_input, Mapping) else {},
    ]
    for key in (
        "action_target",
        "desktop_loop",
        "observation_evidence",
        "verification_target",
    ):
        value = request.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    for source in sources:
        app_name = str(
            source.get("app_name")
            or source.get("target_app_name")
            or source.get("expected_app_name")
            or ""
        ).strip()
        if app_name:
            return app_name
    return str(request.get("target_app_name") or "").strip()


def _approval_dependency_request_window_title(request: Mapping[str, Any]) -> str:
    raw_input = request.get("input")
    sources: list[Mapping[str, Any]] = [
        raw_input if isinstance(raw_input, Mapping) else {},
    ]
    for key in ("action_target", "desktop_loop", "verification_target"):
        value = request.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    for source in sources:
        title = str(
            source.get("window_title")
            or source.get("expected_window_title")
            or ""
        ).strip()
        if title:
            return title
    return ""


def _approval_dependency_structured_result_sources(
    result: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    sources: list[Mapping[str, Any]] = [result]
    data = result.get("data")
    if isinstance(data, Mapping):
        sources.append(data)
        ui_elements = data.get("ui_elements")
        if isinstance(ui_elements, Mapping):
            sources.append(ui_elements)
            nested_data = ui_elements.get("data")
            if isinstance(nested_data, Mapping):
                sources.append(nested_data)
    return sources


def _approval_dependency_expected_content(
    dependency_request: Mapping[str, Any] | None,
) -> str:
    raw_input = (
        dependency_request.get("input")
        if dependency_request and isinstance(dependency_request.get("input"), Mapping)
        else {}
    )
    for key in ("text", "content", "body", "value", "artifact_body", "draft"):
        value = _approval_dependency_semantic_token(raw_input.get(key))
        if value:
            return value
    return ""


def _approval_dependency_observed_content_matches(
    result: Mapping[str, Any],
    expected_text: str,
) -> bool:
    editable_values: set[str] = set()
    for source in _approval_dependency_structured_result_sources(result):
        elements = source.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, Mapping):
                continue
            role = _approval_dependency_semantic_token(element.get("role"))
            editable = element.get("editable") is True or any(
                token in role
                for token in ("textfield", "textarea", "searchfield", "textbox", "textview")
            )
            if not editable:
                continue
            for key in ("text", "value"):
                normalized = _approval_dependency_semantic_token(element.get(key))
                if normalized:
                    editable_values.add(normalized)
    if expected_text in editable_values:
        return True
    anchors = _approval_dependency_safe_content_anchors(expected_text)
    return any(anchor in observed for anchor in anchors for observed in editable_values)


def _approval_dependency_safe_content_anchors(value: str) -> set[str]:
    minimum_length = 4 if any("\u3400" <= char <= "\u9fff" for char in value) else 8
    if len(value) < minimum_length:
        return set()
    anchor_width = 24
    if len(value) <= anchor_width:
        return {value}
    middle_start = max(0, (len(value) - anchor_width) // 2)
    return {
        value[:anchor_width],
        value[middle_start : middle_start + anchor_width],
        value[-anchor_width:],
    }


def _approval_dependency_expected_ui_target(
    approval_request: Mapping[str, Any],
    dependency_request: Mapping[str, Any] | None,
) -> tuple[str, str]:
    sources: list[Mapping[str, Any]] = []
    for request in (approval_request, dependency_request or {}):
        raw_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        sources.extend((raw_input, request))
    target = next(
        (
            str(source.get(key) or "").strip()
            for source in sources
            for key in ("target", "target_label", "ui_target", "selector")
            if str(source.get(key) or "").strip()
        ),
        "",
    )
    role = next(
        (
            str(source.get(key) or "").strip()
            for source in sources
            for key in ("role_filter", "target_role")
            if str(source.get(key) or "").strip()
        ),
        "",
    )
    return target, role


def _approval_dependency_ui_observation_matches_target(
    result: Mapping[str, Any],
    *,
    expected_target: str,
    expected_role: str,
) -> bool:
    return bool(
        _approval_dependency_matching_ui_element(
            result,
            expected_target=expected_target,
            expected_role=expected_role,
        )
    )


def _approval_dependency_matching_ui_element(
    result: Mapping[str, Any],
    *,
    expected_target: str,
    expected_role: str,
) -> Mapping[str, Any] | None:
    target_tokens = _approval_dependency_semantic_candidates(expected_target)
    role_token = _approval_dependency_semantic_token(expected_role)
    candidates: list[Mapping[str, Any]] = []
    for source in _approval_dependency_structured_result_sources(result):
        elements = source.get("elements")
        if not isinstance(elements, list):
            continue
        for element in elements:
            if not isinstance(element, Mapping) or element.get("enabled") is False:
                continue
            element_role = _approval_dependency_semantic_token(
                f"{element.get('role') or ''} {element.get('subrole') or ''}"
            )
            if role_token and role_token not in element_role:
                continue
            candidates.append(element)
            if not target_tokens:
                return element
            observed_tokens = {
                token
                for key in (
                    "name",
                    "label",
                    "title",
                    "text",
                    "value",
                    "description",
                    "identifier",
                    "help",
                )
                for token in [_approval_dependency_semantic_token(element.get(key))]
                if token
            }
            if any(
                target == observed or target in observed or observed in target
                for target in target_tokens
                for observed in observed_tokens
                if len(target) >= 2 and len(observed) >= 2
            ):
                return element
    if target_tokens and _approval_dependency_target_is_ordinal(expected_target):
        return candidates[0] if candidates else None
    return None


def _tool_request_with_preapproval_observed_ui_target(
    tool_request: dict[str, Any],
    tool_requests: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    tool_name = str(tool_request.get("tool") or tool_request.get("tool_name") or "").strip()
    if tool_name not in {
        "desktop.click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_click_ui_element",
    }:
        return tool_request
    dependency_ids = _string_list(tool_request.get("depends_on"))
    if len(dependency_ids) != 1:
        return tool_request
    dependency_request = _approval_dependency_request(
        dependency_ids[0],
        tool_requests,
        decision_id=str(tool_request.get("decision_id") or "").strip(),
    )
    if not _approval_dependency_requires_strong_observation_identity(
        dependency_request,
        tool_request,
        run_id=run_id,
    ):
        return tool_request
    payload = tool_request.get("input") if isinstance(tool_request.get("input"), Mapping) else {}
    expected_target = str(payload.get("target") or "").strip()
    expected_role = str(payload.get("role_filter") or "").strip()
    if not expected_target:
        return tool_request
    for event in reversed(timeline):
        if not isinstance(event, Mapping):
            continue
        event_payload = _approval_dependency_event_payload(event)
        event_result = _approval_dependency_event_result(event, event_payload)
        if not _approval_preaction_observation_identity_matches(
            dependency_request,
            tool_request,
            event,
            event_payload,
            event_result,
            run_id=run_id,
        ):
            continue
        element = _approval_dependency_matching_ui_element(
            event_result,
            expected_target=expected_target,
            expected_role=expected_role,
        )
        if not element:
            continue
        observed_target = next(
            (
                str(element.get(key) or "").strip()
                for key in ("name", "label", "title", "text", "description", "identifier")
                if str(element.get(key) or "").strip()
            ),
            "",
        )
        if not observed_target:
            return tool_request
        if _approval_dependency_semantic_token(observed_target) == (
            _approval_dependency_semantic_token(expected_target)
        ):
            return tool_request
        resolved_input = dict(payload)
        resolved_input["target"] = observed_target
        return {**tool_request, "input": resolved_input}
    return tool_request


def _approval_dependency_target_is_ordinal(value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    return any(
        token in normalized
        for token in ("第一个", "第1个", "第一项", "first", "top result")
    )


def _approval_dependency_ui_observation_has_content(result: Mapping[str, Any]) -> bool:
    for source in _approval_dependency_structured_result_sources(result):
        elements = source.get("elements")
        if isinstance(elements, list) and any(isinstance(item, Mapping) for item in elements):
            return True
        for key in ("text", "visible_text", "content", "extracted_text"):
            value = source.get(key)
            if isinstance(value, list) and any(str(item or "").strip() for item in value):
                return True
            if not isinstance(value, list) and str(value or "").strip():
                return True
        count = source.get("count")
        if isinstance(count, int) and count > 0:
            return True
    return False


def _approval_dependency_semantic_token(value: Any) -> str:
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _approval_dependency_semantic_candidates(value: Any) -> set[str]:
    token = _approval_dependency_semantic_token(value)
    if not token:
        return set()
    candidates = {token}
    for equivalents in _APPROVAL_DEPENDENCY_TEXT_EQUIVALENTS:
        normalized = {
            item
            for item in (
                _approval_dependency_semantic_token(equivalent)
                for equivalent in equivalents
            )
            if item
        }
        if token in normalized:
            candidates.update(normalized)
            continue
        if any(item in token for item in normalized if len(item) >= 2):
            candidates.update(normalized)
    return candidates


def _approval_dependency_tool_result_status(
    result: Mapping[str, Any],
    *,
    requires_verification: bool,
) -> str:
    if not isinstance(result, Mapping) or result.get("approval_required"):
        return "unverified"
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if _tool_result_failed_verification(result) or any(
        source.get("playback_state_unverified") is True
        or source.get("postcondition_verified") is False
        or source.get("content_verified") is False
        for source in (result, data)
    ):
        return "unverified"
    if result.get("ok") is not True:
        return "failed"
    if not requires_verification:
        return "completed"
    if any(
        source.get(key) is True
        for source in (result, data)
        for key in (
            "verification_passed",
            "postcondition_verified",
            "content_verified",
            "verified",
        )
    ):
        return "verified"
    return "unverified"


def _tool_result_with_verification_failure_status(
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(tool_result, dict):
        return tool_result
    if tool_result.get("approval_required"):
        return tool_result
    if not _tool_result_failed_verification(tool_result):
        return tool_result
    enriched = dict(tool_result)
    enriched["verification_failed"] = True
    enriched.setdefault("verification_passed", False)
    data = enriched.get("data") if isinstance(enriched.get("data"), Mapping) else {}
    if data:
        enriched["data"] = {**data, "verification_failed": True}
        enriched["data"].setdefault("verification_passed", False)
    return enriched


def _desktop_execution_policy_skip_result(
    tool_name: str,
    tool_request: Mapping[str, Any],
    input_preview: Any,
) -> dict[str, Any] | None:
    policy = _desktop_execution_policy_from_request(tool_request)
    policy_mode = _desktop_execution_policy_mode(policy)
    execution_mode = desktop_tool_execution_mode_for_input(
        tool_name,
        input_preview if isinstance(input_preview, Mapping) else {},
    )
    execution_payload = execution_mode.model_dump(mode="json")
    sandbox_required_by_policy = _desktop_execution_policy_requires_sandbox(
        policy,
        execution_payload,
    )

    sandbox_provider = sandbox_desktop_provider_status(tool_request)
    route_decision = (
        desktop_execution_route_payload(tool_request)
        or desktop_execution_route_decision(
            tool_name,
            policy=policy,
            execution_mode=execution_payload,
            metadata=tool_request,
        )
    )
    route_blocks_execution = _desktop_execution_route_blocks_execution(route_decision)
    background_provider_selected = str(
        route_decision.get("selected_provider_kind")
        or route_decision.get("provider_kind")
        or sandbox_provider.get("provider_kind")
        or ""
    ).strip() == "background_desktop"
    if policy_mode == "allow" and not sandbox_required_by_policy and not route_blocks_execution:
        return None
    if (
        policy_mode == "preview_input"
        and not route_blocks_execution
        and not _desktop_execution_policy_blocks_input_tool(
            tool_name,
            policy,
            execution_payload,
            input_preview if isinstance(input_preview, Mapping) else {},
        )
    ):
        return None
    if not (
        bool(execution_mode.foreground_control)
        or bool(execution_mode.keyboard_mouse_capture)
        or route_blocks_execution
    ):
        return None
    if desktop_execution_route_allows_provider_execution(route_decision):
        return None
    route_blockers = [
        str(item).strip()
        for item in route_decision.get("blocking_conditions", [])
        if str(item or "").strip()
    ]
    if route_blocks_execution:
        blocking_condition = (
            route_blockers[0]
            if route_blockers
            else "desktop_execution_provider_required"
        )
        status = str(route_decision.get("status") or "provider_required")
    elif policy_mode == "allow" and sandbox_required_by_policy:
        blocking_condition = (
            route_blockers[0]
            if route_blockers
            else "desktop_execution_sandbox_required"
        )
        status = str(route_decision.get("status") or "provider_required")
    else:
        blocking_condition = (
            "desktop_execution_handoff_required"
            if policy_mode == "handoff"
            else "desktop_execution_preview_required"
        )
        status = "handoff_required" if policy_mode == "handoff" else "preview_required"
    capability_mismatch = bool(
        blocking_condition == "desktop_execution_provider_tool_unavailable"
        or status in {"provider_tool_unavailable", "sandbox_tool_not_supported"}
    )
    recovery_actions = (
        []
        if capability_mismatch
        else _desktop_execution_policy_recovery_actions(
            tool_name,
            tool_request,
            policy=policy,
            policy_mode=policy_mode,
            execution_mode=execution_payload,
            sandbox_provider=sandbox_provider,
            route_decision=route_decision,
        )
    )
    recommended_tools = (
        []
        if capability_mismatch
        else [
            "screen.capture",
            "desktop.active_window",
            "desktop.ui_elements",
        ]
    )
    if any(
        str(action.get("tool") or "").strip() == "desktop.provider_session.start"
        for action in recovery_actions
    ):
        recommended_tools = ["desktop.provider_session.start", *recommended_tools]
    return {
        "ok": False,
        "tool": tool_name,
        "status": (
            "provider_capability_mismatch" if capability_mismatch else status
        ),
        "error": (
            "desktop_execution_provider_tool_unavailable"
            if capability_mismatch
            else "desktop_execution_policy_blocked"
        ),
        "summary": (
            "The selected desktop provider is available but cannot execute this "
            "tool; the Runtime must choose another allowed capability."
            if capability_mismatch
            else
            "Background desktop control is not ready; the action was paused without "
            "taking over the user's desktop."
            if background_provider_selected
            else "Desktop foreground execution was blocked by the runtime execution policy."
        ),
        "blocked_by_desktop_execution_policy": True,
        "blocked_by_desktop_execution_provider": capability_mismatch,
        "desktop_provider_capability_mismatch": capability_mismatch,
        **(
            {
                "retryable": True,
                "replan_allowed": True,
                "retry_with_alternative_capability": True,
                "replan_reason": "selected_provider_capability_mismatch",
            }
            if capability_mismatch
            else {}
        ),
        "blocking_condition": blocking_condition,
        "blocking_conditions": [blocking_condition],
        "desktop_execution_policy": policy,
        "desktop_execution_mode": execution_payload,
        "desktop_execution_route": route_decision,
        "execution_mode": str(execution_payload.get("mode") or ""),
        "foreground_control": bool(execution_payload.get("foreground_control")),
        "keyboard_mouse_capture": bool(
            execution_payload.get("keyboard_mouse_capture")
        ),
        "sandbox_recommended": bool(execution_payload.get("sandbox_recommended")),
        "sandbox_provider": sandbox_provider,
        "user_handoff_recommended": (
            not capability_mismatch
            and (policy_mode == "handoff" or background_provider_selected)
        ),
        "input_preview": input_preview if isinstance(input_preview, dict) else {},
        "recommended_tools": recommended_tools,
        "recovery_actions": recovery_actions,
        "hint": (
            "Replan with an allowed tool supported by the selected provider; do not "
            "fall back to the user's foreground desktop."
            if capability_mismatch
            else
            "Install or authorize the background control component, then retry. "
            "Foreground control remains available only after explicit approval."
            if background_provider_selected
            else "Switch the run to supervised_live, continue in Agent Studio, or let the "
            "user perform the foreground step manually."
        ),
    }


def _desktop_foreground_session_notice_payload(
    tool_name: str,
    tool_request: Mapping[str, Any],
    input_preview: Any,
) -> dict[str, Any] | None:
    route = desktop_execution_route_payload(tool_request)
    if not isinstance(route, Mapping) or not bool(route.get("can_execute")):
        return None
    if bool(route.get("user_foreground_takeover_risk")) is not True:
        return None
    policy = _desktop_execution_policy_from_request(tool_request)
    return {
        "tool": tool_name,
        "status": "foreground_session_routed",
        "summary": (
            "Desktop action is executable, but it uses the user's foreground "
            "desktop session; keyboard/mouse input remains sandbox-gated."
        ),
        "input_preview": input_preview if isinstance(input_preview, dict) else {},
        "desktop_execution_policy": policy,
        "desktop_execution_route": dict(route),
        "selected_provider_kind": str(route.get("selected_provider_kind") or ""),
        "selected_provider_id": str(route.get("selected_provider_id") or ""),
        "foreground_takeover_required": bool(
            route.get("foreground_takeover_required")
        ),
        "requires_user_foreground_session": bool(
            route.get("requires_user_foreground_session")
        ),
        "user_foreground_takeover_risk": True,
        "mitigation": (
            "Use an isolated desktop provider for keyboard/mouse workflows, or "
            "continue in Agent Studio supervised live when foreground takeover is intended."
        ),
    }


def _append_desktop_foreground_session_notice(
    *,
    timeline: list[dict[str, Any]],
    timeline_factory: Callable[..., dict[str, Any]],
    append_run_event: Callable[[str, str, dict[str, Any]], Any],
    run_id: str,
    tool_name: str,
    tool_request: Mapping[str, Any],
    input_preview: Any,
    trace_payload: Mapping[str, Any],
) -> None:
    notice = _desktop_foreground_session_notice_payload(
        tool_name,
        tool_request,
        input_preview,
    )
    if notice is None:
        return
    payload = {**dict(trace_payload), **notice}
    timeline.append(
        timeline_factory(
            "agent.tool.foreground_session_notice",
            tool_name,
            **payload,
        )
    )
    if run_id:
        append_run_event(
            run_id,
            "agent.tool.foreground_session_notice",
            payload,
        )


def _desktop_execution_policy_requires_sandbox(
    policy: Mapping[str, Any],
    execution_payload: Mapping[str, Any],
) -> bool:
    if not isinstance(policy, Mapping):
        return False
    requires_keyboard_mouse_sandbox = bool(
        policy.get("require_sandbox_for_keyboard_mouse")
    ) and bool(execution_payload.get("keyboard_mouse_capture"))
    avoids_foreground_takeover = bool(
        policy.get("avoid_user_foreground_takeover")
    ) and (
        bool(execution_payload.get("foreground_control"))
        or bool(execution_payload.get("keyboard_mouse_capture"))
    )
    return requires_keyboard_mouse_sandbox or avoids_foreground_takeover


def _desktop_execution_route_blocks_execution(route_decision: Mapping[str, Any]) -> bool:
    if not isinstance(route_decision, Mapping) or not route_decision:
        return False
    if bool(route_decision.get("can_execute")):
        return False
    status = str(route_decision.get("status") or "").strip()
    if status in {
        "provider_required",
        "provider_tool_unavailable",
        "sandbox_adapter_required",
        "sandbox_desktop_session_required",
        "sandbox_keyboard_mouse_provider_required",
        "sandbox_tool_not_supported",
        "real_virtual_desktop_provider_required",
    }:
        return True
    provider_blockers = {
        "desktop_execution_provider_unavailable",
        "desktop_execution_provider_tool_unavailable",
        "sandbox_desktop_provider_required",
        "sandbox_desktop_adapter_required",
        "sandbox_desktop_session_required",
        "sandbox_keyboard_mouse_provider_required",
        "isolated_desktop_provider_required",
        "loopback_desktop_backend",
        "real_virtual_desktop_backend_required",
    }
    return any(
        str(item or "").strip() in provider_blockers
        for item in route_decision.get("blocking_conditions", [])
    )


def _tool_request_with_desktop_execution_route(
    tool_name: str,
    tool_request: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(tool_request)
    policy = _desktop_execution_policy_from_request(payload)
    probe_background_provider = bool(policy.get("prefer_background_desktop"))
    explicit_route = desktop_execution_route_payload(payload)
    if explicit_route:
        if (
            probe_background_provider
            and _background_route_should_refresh_for_execution(explicit_route)
        ):
            return _tool_request_with_probed_background_route(
                tool_name,
                payload,
                policy=policy,
            )
        route_override = _desktop_execution_route_safety_override(
            tool_name,
            payload,
            explicit_route,
        )
        if route_override:
            payload["desktop_execution_route"] = dict(route_override)
        payload = _tool_request_with_sandbox_provider_session_context(payload)
        return payload
    if not policy:
        return payload
    if probe_background_provider:
        return _tool_request_with_probed_background_route(
            tool_name,
            payload,
            policy=policy,
        )
    raw_input = payload.get("input") if isinstance(payload.get("input"), Mapping) else {}
    execution_mode = desktop_tool_execution_mode_for_input(
        tool_name,
        raw_input,
    ).model_dump(mode="json")
    route_decision = desktop_execution_route_decision(
        tool_name,
        policy=policy,
        execution_mode=execution_mode,
        metadata=payload,
    )
    if route_decision:
        payload["desktop_execution_route"] = dict(route_decision)
        payload = _tool_request_with_sandbox_provider_session_context(
            payload,
            include_default=True,
        )
    return payload


def _background_route_should_refresh_for_execution(route: Mapping[str, Any]) -> bool:
    if (
        str(route.get("selected_provider_kind") or "").strip()
        != "background_desktop"
    ):
        return False
    non_refreshable_blockers = {
        "desktop_execution_provider_tool_unavailable",
        "real_virtual_desktop_backend_required",
        "sandbox_desktop_session_required",
        "sandbox_keyboard_mouse_provider_required",
    }
    blockers = {
        str(item or "").strip()
        for item in route.get("blocking_conditions", [])
        if str(item or "").strip()
    }
    return not bool(blockers & non_refreshable_blockers)


def _tool_request_with_probed_background_route(
    tool_name: str,
    tool_request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Refresh the background provider at execution time without local fallback."""

    payload = dict(tool_request)
    raw_input = payload.get("input") if isinstance(payload.get("input"), Mapping) else {}
    execution_mode = desktop_tool_execution_mode_for_input(
        tool_name,
        raw_input,
    ).model_dump(mode="json")
    # A planner snapshot is evidence that this provider was selected, not a
    # live execution credential. Passing the passive snapshot back as an
    # explicit provider makes sandbox_desktop_provider_status() trust it and
    # silently skips the Cua health probe. Remove only cached provider values;
    # the selected background route and policy remain fail-closed authority.
    decision_metadata = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "desktop_execution_provider",
            "desktop_sandbox_provider",
            "sandbox_desktop_provider",
            "sandbox_provider",
        }
    }
    decision_metadata["desktop_provider_health_probe"] = True
    selected_route = desktop_execution_route_payload(payload)
    if (
        str(selected_route.get("selected_provider_kind") or "").strip()
        == "background_desktop"
    ):
        # Preserve the planner's provider choice for read-only observations.
        # ``prefer_background_desktop`` alone governs foreground actions; the
        # explicit selected route is the authority that this specific
        # observation also belongs to the same background target.
        decision_metadata["desktop_provider_route_readonly"] = True
    # Resolve liveness once, then feed that exact snapshot into route
    # selection.  This keeps the route and executable provider binding
    # consistent without issuing two health requests at the execution edge.
    sandbox_provider = sandbox_desktop_provider_status(
        decision_metadata,
        probe_health=True,
    )
    decision_metadata["sandbox_provider"] = sandbox_provider
    route_decision = desktop_execution_route_decision(
        tool_name,
        policy=policy,
        execution_mode=execution_mode,
        metadata=decision_metadata,
    )
    if route_decision:
        payload["desktop_execution_route"] = dict(route_decision)
        payload["sandbox_provider"] = sandbox_provider
        payload = _tool_request_with_sandbox_provider_session_context(payload)
    return payload


def _model_authored_desktop_request_needs_runtime_policy(
    tool_name: str,
    tool_request: Mapping[str, Any],
) -> bool:
    protocol = str(tool_request.get("protocol") or "").strip()
    if protocol not in {"json_fallback", "tool_calls"}:
        return False
    if str(tool_request.get("source") or "").strip() == "runtime_internal_recovery":
        # The parser strips runtime-owned fields from model-authored requests.
        # Only a validated, allowlisted recovery adapter can attach this marker.
        return False
    clean_tool = str(tool_name or "").strip()
    raw_input = tool_request.get("input")
    input_payload = dict(raw_input) if isinstance(raw_input, Mapping) else {}
    execution_mode = desktop_tool_execution_mode_for_input(
        clean_tool,
        input_payload,
    )
    desktop_affecting = clean_tool.startswith(("app.", "desktop.", "media.")) or any(
        (
            bool(execution_mode.foreground_control),
            bool(execution_mode.keyboard_mouse_capture),
            bool(execution_mode.sandbox_recommended),
        )
    )
    if not desktop_affecting:
        return False
    return not bool(_desktop_execution_policy_from_request(tool_request))


def _tool_request_with_sandbox_provider_session_context(
    tool_request: Mapping[str, Any],
    *,
    include_default: bool = False,
) -> dict[str, Any]:
    payload = dict(tool_request)
    sandbox_provider = _first_mapping(
        payload.get("sandbox_provider"),
        payload.get("sandbox_desktop_provider"),
        payload.get("desktop_sandbox_provider"),
    )
    if sandbox_provider:
        session = _first_mapping(payload.get("desktop_provider_session"))
        payload["sandbox_provider"] = _sandbox_provider_with_session_context(
            sandbox_provider,
            session,
        )
        return payload
    if include_default:
        payload.setdefault("sandbox_provider", sandbox_desktop_provider_status(payload))
    return payload


def _sandbox_provider_with_session_context(
    sandbox_provider: Mapping[str, Any],
    session: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(sandbox_provider)
    if str(payload.get("provider_kind") or "").strip() == LOCAL_DESKTOP_PROVIDER_KIND:
        return payload
    for key in (
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "keyboard_mouse_capture_supported",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "provider_contract",
        "provider_manifest_evidence",
        "provider_conformance",
    ):
        value = session.get(key)
        if payload.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            payload[key] = value
    return payload


def _desktop_execution_route_safety_override(
    tool_name: str,
    tool_request: Mapping[str, Any],
    route: Mapping[str, Any],
) -> dict[str, Any]:
    if not desktop_execution_route_allows_provider_execution(route):
        return {}
    policy = _desktop_execution_policy_from_request(tool_request)
    if not policy:
        return {}
    if not _tool_request_has_desktop_provider_state(tool_request):
        return {}
    raw_input = tool_request.get("input")
    raw_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
    execution_mode = desktop_tool_execution_mode_for_input(
        tool_name,
        raw_input,
    ).model_dump(mode="json")
    route_decision = desktop_execution_route_decision(
        tool_name,
        policy=policy,
        execution_mode=execution_mode,
        metadata=tool_request,
    )
    if _desktop_execution_route_blocks_execution(route_decision):
        return route_decision
    if not desktop_execution_route_allows_provider_execution(route_decision):
        return route_decision
    return {}


def _tool_request_has_desktop_provider_state(tool_request: Mapping[str, Any]) -> bool:
    return bool(
        _first_mapping(
            tool_request.get("sandbox_provider"),
            tool_request.get("sandbox_desktop_provider"),
            tool_request.get("desktop_sandbox_provider"),
            tool_request.get("desktop_provider_session"),
        )
    )


def _desktop_execution_policy_recovery_actions(
    tool_name: str,
    tool_request: Mapping[str, Any],
    *,
    policy: Mapping[str, Any],
    policy_mode: str,
    execution_mode: Mapping[str, Any],
    sandbox_provider: Mapping[str, Any],
    route_decision: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_input = tool_request.get("input")
    tool_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
    action_target = tool_request.get("action_target")
    observation_evidence = tool_request.get("observation_evidence")
    observation_retry = tool_request.get("observation_retry")
    supervised_policy = {
        "mode": "supervised_live",
        "allow_live_foreground": True,
        "source": "desktop_execution_policy_recovery",
        "reason": "User selected an Agent Studio supervised live retry.",
    }
    sandbox_policy = {
        "mode": "sandbox_preferred",
        "source": "desktop_execution_policy_recovery",
        "reason": "Prefer a sandbox desktop/session before touching the real foreground desktop.",
    }
    sandbox_blockers = [
        str(item).strip()
        for item in route_decision.get("blocking_conditions", [])
        if str(item or "").strip()
    ] or [
        str(item).strip()
        for item in sandbox_provider.get("blocking_conditions", [])
        if str(item or "").strip()
    ] or ["sandbox_desktop_provider_required"]
    manual_metadata = {
        "runtime_replan_auto_start_eligible": False,
        "runtime_replan_auto_start_reason": "desktop_execution_policy_requires_supervision",
        "runtime_replan_auto_start_blockers": [
            "desktop_execution_policy",
            *(
                ["keyboard_mouse_capture"]
                if bool(execution_mode.get("keyboard_mouse_capture"))
                else []
            ),
            *(
                ["foreground_control"]
                if bool(execution_mode.get("foreground_control"))
                else []
            ),
        ],
        "desktop_execution_policy": supervised_policy,
        "desktop_execution_route": dict(route_decision),
        "blocked_desktop_execution_policy": dict(policy),
        "blocked_desktop_execution_policy_mode": policy_mode,
        "desktop_execution_mode": dict(execution_mode),
    }
    actions: list[dict[str, Any]] = [
        *_desktop_execution_provider_session_start_recovery_actions(
            tool_name,
            tool_request,
            route_decision=route_decision,
            sandbox_provider=sandbox_provider,
            desktop_policy=policy,
            sandbox_blockers=sandbox_blockers,
        ),
        {
            "label": "Inspect active desktop state",
            "tool": "desktop.active_window",
            "input": {},
            "permission_target": "desktop_observation",
            "risk_level": "low",
            "planning_reason": "desktop_execution_policy_observation_recovery",
            "recovery_action_kind": "observe_desktop_state",
            "metadata": {
                "runtime_replan_auto_start_eligible": True,
                "desktop_execution_policy": dict(policy),
                "blocked_tool": tool_name,
            },
        },
        {
            "label": "Inspect visible desktop controls",
            "tool": "desktop.ui_elements",
            "input": {},
            "permission_target": "desktop_observation",
            "risk_level": "low",
            "planning_reason": "desktop_execution_policy_observation_recovery",
            "recovery_action_kind": "observe_desktop_controls",
            "metadata": {
                "runtime_replan_auto_start_eligible": True,
                "desktop_execution_policy": dict(policy),
                "blocked_tool": tool_name,
            },
        },
    ]
    if bool(execution_mode.get("sandbox_recommended")):
        actions.append(
            {
                "label": "Prepare sandbox desktop handoff",
                "tool": "screen.capture",
                "input": {"reason": "sandbox_desktop_handoff"},
                "permission_target": "sandbox_desktop",
                "risk_level": "low",
                "planning_reason": "desktop_execution_policy_sandbox_handoff",
                "recovery_action_kind": "sandbox_desktop_handoff",
                "desktop_execution_policy": sandbox_policy,
                "desktop_execution_route": dict(route_decision),
                "sandbox_provider": dict(sandbox_provider),
                "deferred_continuation": [
                    {
                        "tool": tool_name,
                        "input": tool_input,
                        "desktop_execution_policy": sandbox_policy,
                        "desktop_execution_route": dict(route_decision),
                        "sandbox_provider": dict(sandbox_provider),
                        "planning_reason": "desktop_execution_policy_sandbox_deferred_tool",
                        "source": "desktop_execution_policy_recovery",
                    }
                ],
                "metadata": {
                    "runtime_replan_auto_start_eligible": False,
                    "runtime_replan_auto_start_reason": "sandbox_desktop_handoff_required",
                    "runtime_replan_auto_start_blockers": sandbox_blockers,
                    "desktop_execution_policy": sandbox_policy,
                    "desktop_execution_route": dict(route_decision),
                    "sandbox_provider": dict(sandbox_provider),
                    "blocked_desktop_execution_policy": dict(policy),
                    "blocked_desktop_execution_policy_mode": policy_mode,
                    "desktop_execution_mode": dict(execution_mode),
                    "sandbox_desktop_handoff": True,
                    "sandbox_original_tool": tool_name,
                    "sandbox_original_input": tool_input,
                },
            }
        )
    actions.append(
        {
            "label": "Continue in Agent Studio supervised live",
            "tool": tool_name,
            "input": tool_input,
            "permission_target": "desktop_foreground_execution",
            "risk_level": _desktop_execution_policy_recovery_risk(execution_mode),
            "approval_required": True,
            "planning_reason": "desktop_execution_policy_supervised_live_recovery",
            "recovery_action_kind": "supervised_live_retry",
            "desktop_execution_policy": supervised_policy,
            "metadata": manual_metadata,
        }
    )
    if isinstance(action_target, Mapping) and action_target:
        actions[-1]["action_target"] = dict(action_target)
    if isinstance(observation_evidence, Mapping) and observation_evidence:
        actions[-1]["observation_evidence"] = dict(observation_evidence)
    if isinstance(observation_retry, Mapping) and observation_retry:
        actions[-1]["observation_retry"] = dict(observation_retry)
    return actions


def _desktop_execution_provider_session_start_recovery_actions(
    tool_name: str,
    tool_request: Mapping[str, Any],
    *,
    route_decision: Mapping[str, Any],
    sandbox_provider: Mapping[str, Any],
    desktop_policy: Mapping[str, Any],
    sandbox_blockers: list[str],
) -> list[dict[str, Any]]:
    clean_tool = str(tool_name or "").strip()
    if not clean_tool or not _desktop_execution_policy_should_offer_session_start(
        route_decision,
        sandbox_provider,
    ):
        return []
    raw_input = tool_request.get("input")
    tool_input = dict(raw_input) if isinstance(raw_input, Mapping) else {}
    provider_id = (
        str(route_decision.get("selected_provider_id") or "").strip()
        or str(sandbox_provider.get("provider_id") or "").strip()
        or "local-isolated-desktop"
    )
    requires_real_backend = bool(
        {
            "loopback_desktop_backend",
            "real_virtual_desktop_backend_required",
        }
        & set(sandbox_blockers)
    ) or str(route_decision.get("status") or "").strip() == (
        "real_virtual_desktop_provider_required"
    )
    deferred_request = {
        "tool": clean_tool,
        "input": dict(tool_input),
        "desktop_execution_policy": {
            **dict(desktop_policy),
            "mode": str(desktop_policy.get("mode") or "sandbox_preferred"),
            "prefer_isolated_desktop": True,
            "avoid_user_foreground_takeover": True,
            "require_sandbox_for_keyboard_mouse": True,
            "source": "desktop_execution_policy_recovery",
        },
        "planning_reason": "desktop_execution_policy_retry_after_session_start",
        "source": "desktop_execution_policy_recovery",
    }
    for key in (
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "step_id",
        "planner_step_id",
        "capability_id",
        "target_capability_id",
        "runtime_stage",
        "runtime_role",
    ):
        value = str(tool_request.get(key) or "").strip()
        if value:
            deferred_request[key] = value
    return [
        {
            "label": "Start isolated desktop provider",
            "tool": "desktop.provider_session.start",
            "input": {
                "provider_id": provider_id,
                "tools": [clean_tool],
                "tool_names": [clean_tool],
                **(
                    {"requires_real_virtual_desktop_backend": True}
                    if requires_real_backend
                    else {}
                ),
                "reason": "desktop_execution_policy_requires_isolated_provider",
                "diagnostic_route": "/yachiyo/studio/tools",
                "api_route": "/yachiyo/studio/tools/desktop-provider/session/start",
            },
            "permission_target": "isolated_desktop_provider",
            "risk_level": "medium",
            "approval_required": True,
            "approval_status": "pending",
            "planning_reason": "desktop_execution_policy_provider_session_recovery",
            "recovery_action_kind": "desktop_provider_session_start",
            "deferred_tool": clean_tool,
            "deferred_input": dict(tool_input),
            "deferred_continuation": [deferred_request],
            "metadata": {
                "runtime_retry_source": "desktop_provider_session",
                **(
                    {"requires_real_virtual_desktop_backend": True}
                    if requires_real_backend
                    else {}
                ),
                "runtime_replan_auto_start_eligible": False,
                "runtime_replan_auto_start_reason": "desktop_provider_session_start_requires_approval",
                "runtime_replan_auto_start_blockers": [
                    "approval_required",
                    *sandbox_blockers,
                ],
                "desktop_execution_route": dict(route_decision),
                "sandbox_provider": dict(sandbox_provider),
                "sandbox_original_tool": clean_tool,
                "sandbox_original_input": dict(tool_input),
            },
        }
    ]


def _desktop_execution_policy_should_offer_session_start(
    route_decision: Mapping[str, Any],
    sandbox_provider: Mapping[str, Any],
) -> bool:
    provider_kind = str(
        route_decision.get("selected_provider_kind")
        or route_decision.get("provider_kind")
        or sandbox_provider.get("provider_kind")
        or ""
    ).strip()
    if provider_kind == "background_desktop":
        return False
    route_status = str(route_decision.get("status") or "").strip()
    route_blockers = {
        str(item).strip()
        for item in route_decision.get("blocking_conditions", [])
        if str(item or "").strip()
    }
    provider_blockers = {
        str(item).strip()
        for item in sandbox_provider.get("blocking_conditions", [])
        if str(item or "").strip()
    }
    if route_status in {
        "provider_required",
        "sandbox_adapter_required",
        "sandbox_desktop_session_required",
        "sandbox_keyboard_mouse_provider_required",
        "real_virtual_desktop_provider_required",
    }:
        return True
    return bool(
        (route_blockers | provider_blockers)
        & {
            "sandbox_desktop_provider_required",
            "sandbox_desktop_adapter_required",
            "sandbox_desktop_session_required",
            "sandbox_keyboard_mouse_provider_required",
            "isolated_desktop_provider_required",
            "loopback_desktop_backend",
            "real_virtual_desktop_backend_required",
        }
    )


def _desktop_execution_policy_recovery_risk(
    execution_mode: Mapping[str, Any],
) -> str:
    if bool(execution_mode.get("keyboard_mouse_capture")):
        return "high"
    if bool(execution_mode.get("foreground_control")):
        return "medium"
    return "low"


def _desktop_execution_policy_blocks_input_tool(
    tool_name: str,
    policy: Mapping[str, Any],
    execution_mode: Mapping[str, Any],
    input_preview: Mapping[str, Any] | None = None,
) -> bool:
    if not bool(execution_mode.get("keyboard_mouse_capture")):
        return False
    clean_tool = str(tool_name or "").strip()
    if clean_tool.startswith("media.") and policy.get("allow_media_control") is not False:
        return False
    if _desktop_execution_policy_allows_low_risk_safe_shortcut(
        clean_tool,
        input_preview,
    ):
        return False
    return True


def _desktop_execution_policy_allows_low_risk_safe_shortcut(
    tool_name: str,
    input_preview: Mapping[str, Any] | None,
) -> bool:
    if tool_name not in {
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "desktop.safe_shortcut",
    }:
        return False
    payload = input_preview if isinstance(input_preview, Mapping) else {}
    action = str(payload.get("action") or "").strip()
    return action in {"new_document", "new_note", "new_task"}


def _desktop_execution_policy_from_request(
    tool_request: Mapping[str, Any],
) -> dict[str, Any]:
    for key in (
        "desktop_execution_policy",
        "yachiyo_desktop_execution_policy",
        "desktop_interaction_policy",
    ):
        raw = tool_request.get(key)
        if isinstance(raw, Mapping):
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            return {"mode": raw.strip()}
    metadata = tool_request.get("metadata")
    if isinstance(metadata, Mapping):
        return _desktop_execution_policy_from_request(metadata)
    return {}


def _desktop_execution_policy_mode(policy: Mapping[str, Any]) -> str:
    return _public_desktop_execution_policy_mode(policy)


def _tool_result_with_runtime_recovery_defaults(
    tool_name: str,
    tool_request: Mapping[str, Any],
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(tool_result, dict):
        return tool_result
    if tool_result.get("approval_required"):
        return tool_result
    if _tool_request_has_runtime_recovery_metadata(tool_request):
        return tool_result
    if tool_result.get("ok") is not False and not _tool_result_failed_verification(tool_result):
        return tool_result
    fallback_tools = _runtime_default_replan_fallback_tools(tool_name)
    if not fallback_tools:
        fallback_tools = _runtime_capability_replan_fallback_tools(tool_request)
    recovery_actions = _runtime_default_replan_recovery_actions(
        tool_name,
        raw_input,
        fallback_tools,
    )
    if not fallback_tools and not recovery_actions:
        return tool_result
    enriched = dict(tool_result)
    if fallback_tools:
        enriched["recommended_tools"] = _string_list([
            *_string_list(enriched.get("recommended_tools")),
            *fallback_tools,
        ])
    if recovery_actions:
        enriched["recovery_actions"] = _dedupe_runtime_replan_recovery_actions([
            *_mapping_list(enriched.get("recovery_actions")),
            *recovery_actions,
        ])
    return enriched


def _tool_request_has_runtime_recovery_metadata(tool_request: Mapping[str, Any]) -> bool:
    return bool(
        _string_list(tool_request.get("replan_signal_ids"))
        or _string_list(tool_request.get("replan_triggers"))
        or _string_list(tool_request.get("fallback_tools"))
        or _mapping_list(tool_request.get("recovery_actions"))
        or _first_mapping(tool_request.get("observation_retry"))
        or bool(tool_request.get("requires_observation"))
        or bool(tool_request.get("requires_post_action_verification"))
    )


def _normalized_app_lookup(value: Any) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _canonical_app_lookup(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean:
        return ""
    compact = compact_app_alias(clean)
    canonical = (
        clean
        if compact in GENERIC_APP_ALIAS_COMPACTS
        else str(APP_ALIASES.get(compact) or clean).strip()
    )
    return _normalized_app_lookup(canonical)


def _is_generic_app_lookup(value: Any) -> bool:
    return compact_app_alias(str(value or "").strip()) in GENERIC_APP_ALIAS_COMPACTS


_SELECTED_DESKTOP_APP_NAME = "<selected app from desktop.list_apps>"
_SELECTED_RUNNING_DESKTOP_APP_NAME = "<selected app from desktop.running_apps>"
_DESKTOP_APP_SELECTION_SOURCE = "desktop.list_apps"
_DESKTOP_RUNNING_APP_SELECTION_SOURCE = "desktop.running_apps"
_DESKTOP_APP_SELECTION_SOURCES = {
    _DESKTOP_APP_SELECTION_SOURCE,
    _DESKTOP_RUNNING_APP_SELECTION_SOURCE,
}
_SELECTED_WORKSPACE_FILE_PATH = "<selected file from workspace.list>"
_SELECTED_WORKSPACE_FILES_PATH = "<selected files from workspace.list>"
_SELECTED_WORKSPACE_FILE_PATHS = {
    _SELECTED_WORKSPACE_FILE_PATH,
    _SELECTED_WORKSPACE_FILES_PATH,
}


def _app_lookups_related(left: Any, right: Any) -> bool:
    clean_left = _normalized_app_lookup(left)
    clean_right = _normalized_app_lookup(right)
    if not clean_left or not clean_right:
        return False
    if clean_left == clean_right:
        return True
    canonical_left = _canonical_app_lookup(left)
    canonical_right = _canonical_app_lookup(right)
    if canonical_left and canonical_left == canonical_right:
        return True
    shorter, longer = (
        (clean_left, clean_right)
        if len(clean_left) <= len(clean_right)
        else (clean_right, clean_left)
    )
    return f" {shorter} " in f" {longer} "


def _app_lookups_same_identity(left: Any, right: Any) -> bool:
    clean_left = _normalized_app_lookup(left)
    clean_right = _normalized_app_lookup(right)
    if not clean_left or not clean_right:
        return False
    if clean_left == clean_right:
        return True
    canonical_left = _canonical_app_lookup(left)
    canonical_right = _canonical_app_lookup(right)
    return bool(canonical_left and canonical_left == canonical_right)


def _apps_from_list_apps_result(result: dict[str, Any]) -> list[Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for container in (data, result):
        apps = container.get("apps")
        if isinstance(apps, list):
            return apps
        matches = container.get("matches")
        if isinstance(matches, list):
            return matches
    return []


def _desktop_app_selection_source(value: Any) -> str:
    clean = str(value or "").strip()
    if clean in _DESKTOP_APP_SELECTION_SOURCES:
        return clean
    return ""


def _selected_desktop_app_placeholder_source(app_name: Any) -> str:
    clean = str(app_name or "").strip()
    if clean == _SELECTED_RUNNING_DESKTOP_APP_NAME:
        return _DESKTOP_RUNNING_APP_SELECTION_SOURCE
    if clean == _SELECTED_DESKTOP_APP_NAME:
        return _DESKTOP_APP_SELECTION_SOURCE
    return ""


def _app_match_score(app: dict[str, Any]) -> int | None:
    for key in ("match_score", "score", "app_resolution_score"):
        value = app.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _app_candidate_name(app: Mapping[str, Any]) -> str:
    return str(
        app.get("name")
        or app.get("app_name")
        or app.get("resolved_app_name")
        or ""
    ).strip()


def _app_candidate_path(app: Mapping[str, Any]) -> str:
    return str(app.get("path") or app.get("resolved_app_path") or "").strip()


def _contains_non_ascii(value: Any) -> bool:
    return any(ord(char) > 127 for char in str(value or ""))


def _app_match_is_high_confidence(app: dict[str, Any], query: str) -> bool:
    score = _app_match_score(app)
    if score is not None and score < 80:
        return False
    clean_query = _normalized_app_lookup(query)
    clean_name = _normalized_app_lookup(_app_candidate_name(app))
    if (
        clean_query
        and clean_name
        and clean_query != clean_name
        and _contains_non_ascii(query)
        and clean_name.endswith(clean_query)
    ):
        return False
    return True


def _best_match_from_list_apps_result(result: dict[str, Any]) -> dict[str, Any] | None:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    for container in (data, result):
        best_match = container.get("best_match")
        if isinstance(best_match, dict):
            return best_match
        resolution = container.get("resolution")
        if isinstance(resolution, dict) and _app_candidate_name(resolution):
            return {
                "name": _app_candidate_name(resolution),
                "path": _app_candidate_path(resolution),
                "match_score": resolution.get("app_resolution_score"),
                "match_confidence": resolution.get("app_resolution_confidence"),
                "match_reason": resolution.get("app_resolution_reason"),
                "matched_name": resolution.get("app_resolution_matched_name"),
                "matched_name_source": resolution.get("app_resolution_matched_name_source"),
                "matched_capability": resolution.get("app_resolution_matched_capability"),
            }
    return None


def _discovered_app_name_for_query(
    timeline: list[dict[str, Any]],
    query: str,
    *,
    source_tool: str = _DESKTOP_APP_SELECTION_SOURCE,
) -> str:
    clean_query = _normalized_app_lookup(query)
    if not clean_query:
        return ""
    clean_source_tool = _desktop_app_selection_source(source_tool) or _DESKTOP_APP_SELECTION_SOURCE
    for event in reversed(timeline):
        if event.get("event") != "agent.tool.call":
            continue
        if str(event.get("detail") or "") != clean_source_tool:
            continue
        input_preview = event.get("input_preview") if isinstance(event.get("input_preview"), dict) else {}
        if (
            clean_source_tool == _DESKTOP_APP_SELECTION_SOURCE
            and not _app_lookups_related(input_preview.get("query"), clean_query)
        ):
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if clean_source_tool == _DESKTOP_RUNNING_APP_SELECTION_SOURCE:
            running_match = _best_running_app_match_for_query(result, query)
            if running_match:
                return _app_candidate_name(running_match)
            continue
        best_match = _best_match_from_list_apps_result(result)
        if best_match is not None and _app_match_is_high_confidence(best_match, query):
            app_name = str(best_match.get("name") or "").strip()
            if app_name:
                return app_name
        discovered_apps = [
            app
            for app in _apps_from_list_apps_result(result)
            if isinstance(app, dict) and _app_candidate_name(app)
        ]
        for app in discovered_apps:
            app_name = _app_candidate_name(app)
            if _normalized_app_lookup(app_name) == clean_query:
                return app_name
        for app in discovered_apps:
            if not _app_match_is_high_confidence(app, query):
                continue
            app_name = _app_candidate_name(app)
            if _app_lookups_related(app_name, clean_query):
                return app_name
        for app in discovered_apps:
            score = _app_match_score(app)
            if score is not None and score >= 80 and _app_match_is_high_confidence(app, query):
                return _app_candidate_name(app)
    return ""


def _latest_selected_app_candidate(
    timeline: list[dict[str, Any]],
    *,
    source_tool: str = _DESKTOP_APP_SELECTION_SOURCE,
) -> dict[str, Any]:
    clean_source_tool = (
        _desktop_app_selection_source(source_tool) or _DESKTOP_APP_SELECTION_SOURCE
    )
    for event in reversed(timeline):
        if event.get("event") != "agent.tool.call":
            continue
        if str(event.get("detail") or "") != clean_source_tool:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        candidate = _selected_app_candidate_from_result(
            result,
            source_tool=clean_source_tool,
        )
        if candidate:
            return candidate
    return {}


def _selected_app_candidate_from_result(
    result: dict[str, Any],
    *,
    source_tool: str,
) -> dict[str, Any]:
    best_match = _best_match_from_list_apps_result(result)
    if isinstance(best_match, dict) and _app_candidate_name(best_match):
        return best_match
    apps = [
        app
        for app in _apps_from_list_apps_result(result)
        if isinstance(app, dict) and _app_candidate_name(app)
    ]
    if source_tool == _DESKTOP_RUNNING_APP_SELECTION_SOURCE:
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        frontmost_name = str(
            data.get("frontmost") or result.get("frontmost") or ""
        ).strip()
        if frontmost_name:
            for app in apps:
                if _normalized_app_lookup(
                    _app_candidate_name(app)
                ) == _normalized_app_lookup(frontmost_name):
                    return {
                        **app,
                        "match_reason": (
                            app.get("match_reason") or "frontmost_running_app"
                        ),
                    }
        for app in apps:
            if bool(app.get("frontmost")):
                return {
                    **app,
                    "match_reason": (
                        app.get("match_reason") or "frontmost_running_app"
                    ),
                }
    if len(apps) == 1:
        return apps[0]
    return {}


def _best_running_app_match_for_query(
    result: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    running_apps = [
        app
        for app in _apps_from_list_apps_result(result)
        if isinstance(app, dict) and _app_candidate_name(app)
    ]
    if not running_apps:
        return {}
    clean_query = _normalized_app_lookup(query)
    for app in running_apps:
        app_name = _app_candidate_name(app)
        if _normalized_app_lookup(app_name) == clean_query:
            return {**app, "match_score": app.get("match_score", 100)}
    installed_candidates = _installed_app_candidates_for_query(query)
    for candidate in installed_candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_name = _app_candidate_name(candidate)
        if not candidate_name or not _app_match_is_high_confidence(dict(candidate), query):
            continue
        for app in running_apps:
            if _app_lookups_related(_app_candidate_name(app), candidate_name):
                return {**dict(candidate), **dict(app), "name": _app_candidate_name(app)}
    for app in running_apps:
        app_name = _app_candidate_name(app)
        if _app_lookups_related(app_name, clean_query):
            return {**app, "match_score": app.get("match_score", 80)}
    return {}


def _installed_app_candidates_for_query(query: str) -> list[dict[str, Any]]:
    try:
        from apps.shell.agent.tools import desktop

        candidates = desktop._installed_app_match_candidates(query)  # type: ignore[attr-defined]
    except Exception:
        return []
    if not isinstance(candidates, list):
        return []
    return [candidate for candidate in candidates if isinstance(candidate, dict)]


def _tool_request_with_discovered_app_name(
    tool_request: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    resolution = _tool_request_app_name_resolution(tool_request, timeline)
    return _tool_request_with_app_name_resolution(tool_request, resolution)


def _tool_request_with_app_name_resolution(
    tool_request: dict[str, Any],
    resolution: dict[str, str],
) -> dict[str, Any]:
    if not resolution:
        return tool_request
    tool_name = str(tool_request.get("tool") or "").strip()
    if not _tool_request_input_accepts_app_name_resolution(tool_name):
        return {
            **tool_request,
            "input_resolution": resolution,
        }
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    resolved_input = {
        **raw_input,
        "app_name": str(resolution.get("resolved_app_name") or "").strip(),
    }
    if (
        _selected_desktop_app_placeholder_source(raw_input.get("app_name"))
        or _desktop_app_selection_source(raw_input.get("selection_source"))
    ):
        resolved_input.pop("selection_source", None)
        resolved_input.pop("query", None)
    return {
        **tool_request,
        "input_resolution": resolution,
        "input": resolved_input,
    }


def _tool_request_existing_app_name_resolution(
    tool_request: dict[str, Any],
) -> dict[str, str]:
    resolution = (
        tool_request.get("input_resolution")
        if isinstance(tool_request.get("input_resolution"), dict)
        else {}
    )
    if not resolution:
        return {}
    if (
        str(resolution.get("field") or "").strip() == "app_name"
        or str(resolution.get("resolved_app_name") or "").strip()
    ):
        app_resolution_keys = {
            "field",
            "requested_app_name",
            "resolved_app_name",
            "source_tool",
            "app_resolution_score",
            "app_resolution_confidence",
            "app_resolution_reason",
            "app_resolution_matched_name",
            "app_resolution_matched_name_source",
            "app_resolution_matched_capability",
            "resolved_app_path",
            "tool",
        }
        app_resolution = {
            str(key): str(value)
            for key, value in resolution.items()
            if key in app_resolution_keys and key != "field" and value is not None
        }
        app_resolution["field"] = "app_name"
        return app_resolution
    return {}


def _tool_request_workspace_file_resolution(
    tool_request: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    if not _tool_request_uses_selected_workspace_file(raw_input):
        return {}
    lineage_values = (
        str(tool_request.get("run_id") or "").strip(),
        str(tool_request.get("plan_id") or "").strip(),
        str(
            tool_request.get("step_id")
            or tool_request.get("planner_step_id")
            or ""
        ).strip(),
        str(tool_request.get("tool_call_id") or "").strip(),
    )
    has_authoritative_lineage = all(lineage_values)
    if has_authoritative_lineage:
        try:
            trusted = resolve_workspace_file_selection(
                tool_request,
                timeline,
                run_id=str(tool_request.get("run_id") or "").strip(),
            )
        except InputBindingResolutionError:
            return {}
        receipt = trusted.receipt.to_payload()
        resolution: dict[str, Any] = {
            "field": _selected_workspace_file_field(raw_input) or "path",
            "requested_path": trusted.receipt.requested_path,
            "resolved_path": trusted.resolved_path,
            "resolved_file_count": len(trusted.resolved_paths),
            "source_tool": trusted.receipt.source_tool_name,
            "source_path": trusted.receipt.source_scope,
            "source_step_id": trusted.receipt.source_step_id,
            "source_tool_call_id": trusted.receipt.source_tool_call_id,
            "source_plan_id": trusted.receipt.plan_id,
            "workspace_file_resolution": receipt,
        }
        if trusted.receipt.requested_path == _SELECTED_WORKSPACE_FILES_PATH:
            resolution["resolved_paths"] = list(trusted.resolved_paths)
        names = [PurePosixPath(path).name for path in trusted.resolved_paths]
        if names:
            resolution["resolved_file_name"] = names[0]
            if len(names) > 1:
                resolution["resolved_file_names"] = names
        if trusted.receipt.selection:
            resolution["selection"] = trusted.receipt.selection
        return resolution
    has_planned_lineage = bool(
        lineage_values[1]
        or _string_list(tool_request.get("depends_on"))
    )
    if has_planned_lineage:
        # A partially projected planned request is not an old unplanned call.
        # Falling back here would let it select an unrelated discovery event.
        return {}
    field = _selected_workspace_file_field(raw_input) or "path"
    candidate = _selected_workspace_file_from_timeline(raw_input, timeline)
    if not candidate:
        return {}
    requested_path = str(
        raw_input.get(field)
        or raw_input.get("path")
        or raw_input.get("target_path")
        or _SELECTED_WORKSPACE_FILE_PATH
    ).strip()
    if _tool_request_uses_selected_workspace_files(raw_input):
        candidates = _selected_workspace_files_from_timeline(raw_input, timeline)
        paths = [str(item.get("path") or "").strip() for item in candidates]
        paths = [path for path in paths if path]
        if not paths:
            return {}
        resolution = {
            "field": field,
            "requested_path": requested_path,
            "resolved_path": paths[0],
            "resolved_paths": paths,
            "resolved_file_count": len(paths),
            "source_tool": str(candidates[0].get("source_tool") or "workspace.list").strip(),
        }
        source_path = str(candidates[0].get("source_path") or "").strip()
        if source_path:
            resolution["source_path"] = source_path
        names = [
            str(item.get("name") or "").strip()
            for item in candidates
            if str(item.get("name") or "").strip()
        ]
        if names:
            resolution["resolved_file_names"] = names
            resolution["resolved_file_name"] = names[0]
        selection = str(raw_input.get("selection") or "").strip()
        if selection:
            resolution["selection"] = selection
        return resolution
    resolution = {
        "field": field,
        "requested_path": requested_path,
        "resolved_path": str(candidate.get("path") or "").strip(),
        "source_tool": str(candidate.get("source_tool") or "workspace.list").strip(),
    }
    source_path = str(candidate.get("source_path") or "").strip()
    if source_path:
        resolution["source_path"] = source_path
    entry_name = str(candidate.get("name") or "").strip()
    if entry_name:
        resolution["resolved_file_name"] = entry_name
    selection = str(raw_input.get("selection") or "").strip()
    if selection:
        resolution["selection"] = selection
    return resolution


def _tool_request_with_workspace_file_resolution(
    tool_request: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    if not resolution:
        return tool_request
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    resolved_path = str(resolution.get("resolved_path") or "").strip()
    resolved_paths = [
        str(path or "").strip()
        for path in resolution.get("resolved_paths", [])
        if str(path or "").strip()
    ] if isinstance(resolution.get("resolved_paths"), list) else []
    if not resolved_path and not resolved_paths:
        return tool_request
    field = str(resolution.get("field") or "path").strip() or "path"
    resolved_input = dict(raw_input)
    if resolved_paths:
        resolved_input.pop(field, None)
        resolved_input["paths"] = resolved_paths
    else:
        resolved_input[field] = resolved_path
        if field == "target_path":
            resolved_input.setdefault("path", resolved_path)
    if str(resolved_input.get("path") or "").strip() in _SELECTED_WORKSPACE_FILE_PATHS:
        if resolved_paths:
            resolved_input.pop("path", None)
        else:
            resolved_input["path"] = resolved_path
    if str(resolved_input.get("target_path") or "").strip() in _SELECTED_WORKSPACE_FILE_PATHS:
        if resolved_paths:
            resolved_input.pop("target_path", None)
        else:
            resolved_input["target_path"] = resolved_path
    if str(resolved_input.get("selection_source") or "").strip() in {
        "workspace.list",
        "file.search",
        "fs.find_files",
    }:
        for key in (
            "selection_source",
            "source_scope",
            "source_path",
            "selection",
            "selection_hint",
            "pattern",
            "file_type",
        ):
            resolved_input.pop(key, None)
    existing_resolution = (
        tool_request.get("input_resolution")
        if isinstance(tool_request.get("input_resolution"), dict)
        else {}
    )
    merged_resolution = {**existing_resolution, **resolution}
    if str(existing_resolution.get("resolved_app_name") or "").strip():
        merged_resolution = {
            **existing_resolution,
            "file_field": str(resolution.get("field") or "").strip(),
            "requested_path": str(resolution.get("requested_path") or "").strip(),
            "resolved_path": resolved_path,
            "file_resolution_source_tool": str(resolution.get("source_tool") or "").strip(),
        }
        for key in ("source_path", "resolved_file_name", "selection"):
            value = str(resolution.get(key) or "").strip()
            if value:
                merged_resolution[key] = value
        resolved_names = resolution.get("resolved_file_names")
        if isinstance(resolved_names, list) and resolved_names:
            merged_resolution["resolved_file_names"] = [
                str(name or "").strip() for name in resolved_names if str(name or "").strip()
            ]
        if resolved_paths:
            merged_resolution["resolved_paths"] = resolved_paths
            merged_resolution["resolved_file_count"] = len(resolved_paths)
    updated_request = {
        **tool_request,
        "input_resolution": merged_resolution,
        "input": resolved_input,
    }
    receipt = resolution.get("workspace_file_resolution")
    if not isinstance(receipt, Mapping) or not receipt:
        return updated_request
    action_target = (
        dict(tool_request.get("action_target"))
        if isinstance(tool_request.get("action_target"), Mapping)
        else {}
    )
    requested_path = str(receipt.get("requested_path") or "").strip()
    if requested_path:
        action_target["expected_path"] = requested_path
    action_target["resolution_required"] = True
    action_target["workspace_file_resolution"] = dict(receipt)
    if resolved_paths:
        action_target.pop("path", None)
        action_target["paths"] = resolved_paths
    else:
        action_target["path"] = resolved_path
    return {
        **updated_request,
        "action_target": action_target,
        "workspace_file_resolution": dict(receipt),
    }


def _tool_request_uses_selected_workspace_file(raw_input: dict[str, Any]) -> bool:
    if _selected_workspace_file_field(raw_input):
        return True
    if str(raw_input.get("selection_source") or "").strip() != "workspace.list":
        return False
    path_value = str(
        raw_input.get("path") or raw_input.get("target_path") or raw_input.get("file_path") or ""
    ).strip()
    return not path_value or (path_value.startswith("<") and path_value.endswith(">"))


def _selected_workspace_file_field(raw_input: dict[str, Any]) -> str:
    for field in ("path", "target_path", "file_path"):
        if str(raw_input.get(field) or "").strip() in _SELECTED_WORKSPACE_FILE_PATHS:
            return field
    return ""


def _tool_request_uses_selected_workspace_files(raw_input: dict[str, Any]) -> bool:
    field = _selected_workspace_file_field(raw_input)
    if not field:
        return False
    return str(raw_input.get(field) or "").strip() == _SELECTED_WORKSPACE_FILES_PATH


def _tool_request_artifact_body_resolution(
    tool_request: dict[str, Any],
    broker: Any,
    artifacts: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    tool_name = str(tool_request.get("tool") or "").strip()
    if tool_name not in _ARTIFACT_BODY_TEXT_TOOLS:
        return {}
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    if str(raw_input.get("text") or "").strip():
        return {}
    body_source = str(raw_input.get("body_source") or "").strip()
    if body_source not in _ARTIFACT_BODY_SOURCES:
        return {}
    artifact_path = _artifact_body_path(raw_input, artifacts)
    if not artifact_path:
        return {}
    content, metadata = _read_broker_text_artifact(broker, artifact_path)
    if not content:
        return {}
    return {
        "_resolved_text": content,
        "field": "text",
        "body_source": body_source,
        "artifact_path": artifact_path,
        "source_tool": _artifact_source_tool(artifact_path, artifacts) or "data.analyze",
        **metadata,
    }


def _tool_request_with_artifact_body_resolution(
    tool_request: dict[str, Any],
    resolution: dict[str, Any],
) -> dict[str, Any]:
    content = str(resolution.get("_resolved_text") or "")
    if not content:
        return tool_request
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    resolved_input = {**raw_input, "text": content}
    existing_resolution = (
        tool_request.get("input_resolution")
        if isinstance(tool_request.get("input_resolution"), dict)
        else {}
    )
    public_resolution = _public_artifact_body_resolution(resolution)
    if str(existing_resolution.get("resolved_app_name") or "").strip():
        merged_resolution = {
            **existing_resolution,
            "text_field": "text",
            "text_body_source": str(public_resolution.get("body_source") or "").strip(),
            "text_artifact_path": str(public_resolution.get("artifact_path") or "").strip(),
            "text_source_tool": str(public_resolution.get("source_tool") or "").strip(),
        }
        for key in ("resolved_text_bytes", "resolved_text_truncated"):
            value = public_resolution.get(key)
            if value not in (None, "", [], {}):
                merged_resolution[key] = value
    else:
        merged_resolution = {**existing_resolution, **public_resolution}
    return {
        **tool_request,
        "input_resolution": merged_resolution,
        "input": resolved_input,
    }


def _public_artifact_body_resolution(resolution: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in resolution.items()
        if not str(key).startswith("_") and value not in (None, "", [], {})
    }


def _artifact_body_path(
    raw_input: Mapping[str, Any],
    artifacts: list[dict[str, Any]] | None,
) -> str:
    direct_path = str(raw_input.get("artifact_path") or raw_input.get("path") or "").strip()
    if direct_path:
        return direct_path
    for artifact in reversed(artifacts or []):
        if not isinstance(artifact, Mapping):
            continue
        path = str(artifact.get("path") or artifact.get("artifact_path") or "").strip()
        if not path:
            continue
        kind = str(artifact.get("kind") or "").strip().lower()
        mime_type = str(artifact.get("mime_type") or "").strip().lower()
        if kind in {"markdown", "text", "report", "csv"} or mime_type.startswith("text/"):
            return path
    return ""


def _artifact_source_tool(
    artifact_path: str,
    artifacts: list[dict[str, Any]] | None,
) -> str:
    clean_path = str(artifact_path or "").strip()
    if not clean_path:
        return ""
    for artifact in reversed(artifacts or []):
        if not isinstance(artifact, Mapping):
            continue
        path = str(artifact.get("path") or artifact.get("artifact_path") or "").strip()
        if path != clean_path:
            continue
        return str(artifact.get("source_tool") or "").strip()
    return ""


def _read_broker_text_artifact(
    broker: Any,
    artifact_path: str,
) -> tuple[str, dict[str, Any]]:
    root_value = getattr(broker, "artifact_root", None)
    if root_value is None:
        return "", {}
    rel_path = Path(str(artifact_path or "").strip())
    if rel_path.is_absolute() or any(part == ".." for part in rel_path.parts):
        return "", {}
    root = Path(root_value).resolve()
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return "", {}
    if not target.is_file():
        return "", {}
    content = target.read_text(encoding="utf-8", errors="replace")
    truncated = False
    if len(content) > _ARTIFACT_BODY_TEXT_LIMIT:
        content = content[:_ARTIFACT_BODY_TEXT_LIMIT].rstrip()
        truncated = True
    metadata: dict[str, Any] = {
        "resolved_text_bytes": len(content.encode("utf-8")),
    }
    if truncated:
        metadata["resolved_text_truncated"] = True
    return content.strip(), metadata


def _selected_workspace_file_from_timeline(
    raw_input: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, str]:
    for event in reversed(timeline):
        if event.get("event") != "agent.tool.call":
            continue
        source_tool = str(event.get("detail") or "").strip()
        if source_tool not in {"workspace.list", "file.search", "fs.find_files"}:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False:
            continue
        input_preview = event.get("input_preview") if isinstance(event.get("input_preview"), dict) else {}
        if not _workspace_file_selection_event_matches(raw_input, input_preview, result):
            continue
        source_path = _workspace_file_source_path(input_preview, result)
        entries = _workspace_file_entries_from_result(result)
        entry = _select_workspace_file_entry(entries, raw_input, source_path)
        if not entry:
            continue
        path = _workspace_file_entry_path(entry, source_path)
        if not path:
            continue
        return {
            "path": path,
            "name": str(entry.get("name") or "").strip(),
            "source_tool": source_tool,
            "source_path": source_path,
        }
    return {}


def _selected_workspace_files_from_timeline(
    raw_input: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> list[dict[str, str]]:
    for event in reversed(timeline):
        if event.get("event") != "agent.tool.call":
            continue
        source_tool = str(event.get("detail") or "").strip()
        if source_tool not in {"workspace.list", "file.search", "fs.find_files"}:
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if result.get("ok") is False:
            continue
        input_preview = event.get("input_preview") if isinstance(event.get("input_preview"), dict) else {}
        if not _workspace_file_selection_event_matches(raw_input, input_preview, result):
            continue
        source_path = _workspace_file_source_path(input_preview, result)
        entries = _workspace_file_entries_from_result(result)
        selected_entries = _select_workspace_file_entries(entries, raw_input, source_path)
        candidates: list[dict[str, str]] = []
        for entry in selected_entries:
            path = _workspace_file_entry_path(entry, source_path)
            if not path:
                continue
            candidates.append(
                {
                    "path": path,
                    "name": str(entry.get("name") or "").strip(),
                    "source_tool": source_tool,
                    "source_path": source_path,
                }
            )
        if candidates:
            return candidates
    return []


def _workspace_file_selection_event_matches(
    raw_input: dict[str, Any],
    input_preview: dict[str, Any],
    result: dict[str, Any],
) -> bool:
    source_scope = str(
        raw_input.get("source_scope")
        or raw_input.get("source_path")
        or raw_input.get("directory")
        or ""
    ).strip()
    if source_scope:
        event_path = _workspace_file_source_path(input_preview, result)
        if _normalized_workspace_path(event_path) != _normalized_workspace_path(source_scope):
            return False
    pattern = str(raw_input.get("pattern") or "").strip()
    event_pattern = str(input_preview.get("pattern") or "").strip()
    if pattern and event_pattern and pattern != event_pattern:
        return False
    file_type = str(raw_input.get("source_kind") or raw_input.get("file_type") or "").strip()
    event_file_type = str(input_preview.get("file_type") or "").strip()
    if file_type and event_file_type and file_type != event_file_type:
        return False
    return True


def _workspace_file_source_path(
    input_preview: dict[str, Any],
    result: dict[str, Any],
) -> str:
    return str(result.get("path") or input_preview.get("path") or ".").strip() or "."


def _workspace_file_entries_from_result(result: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for container in (result, result.get("data") if isinstance(result.get("data"), dict) else {}):
        if not isinstance(container, dict):
            continue
        for key in ("entries", "files", "matches", "results"):
            raw_entries = container.get(key)
            if not isinstance(raw_entries, list):
                continue
            entries.extend(entry for entry in raw_entries if isinstance(entry, dict))
    return entries


def _select_workspace_file_entry(
    entries: list[dict[str, Any]],
    raw_input: dict[str, Any],
    source_path: str,
) -> dict[str, Any]:
    if _tool_request_uses_selected_workspace_files(raw_input):
        selected = _select_workspace_file_entries(entries, raw_input, source_path)
        return selected[0] if selected else {}
    files = _workspace_file_entry_files(entries, source_path)
    if not files:
        return {}
    selection = str(raw_input.get("selection") or raw_input.get("selection_hint") or "").casefold()
    if any(token in selection for token in ("最近", "最新", "latest", "newest", "recent")):
        with_mtime = [
            (entry, _workspace_file_entry_mtime(entry))
            for entry in files
            if _workspace_file_entry_mtime(entry) is not None
        ]
        if with_mtime:
            return max(with_mtime, key=lambda item: item[1] or 0)[0]
        return {}
    if any(token in selection for token in ("最后", "last")):
        return files[-1]
    if any(token in selection for token in ("第一个", "第1个", "first", "top")):
        return files[0]
    if len(files) == 1:
        return files[0]
    return {}


def _select_workspace_file_entries(
    entries: list[dict[str, Any]],
    raw_input: dict[str, Any],
    source_path: str,
) -> list[dict[str, Any]]:
    files = _workspace_file_entry_files(entries, source_path)
    if not files:
        return []
    selection = str(raw_input.get("selection") or raw_input.get("selection_hint") or "").casefold()
    if any(token in selection for token in ("所有", "全部", "all", "every", "each")):
        return files
    if any(token in selection for token in ("多个", "多份", "multiple", "several", "比较", "对比", "compare")):
        return files
    return files if _tool_request_uses_selected_workspace_files(raw_input) else []


def _workspace_file_entry_files(
    entries: list[dict[str, Any]],
    source_path: str,
) -> list[dict[str, Any]]:
    files = [
        entry
        for entry in entries
        if _workspace_file_entry_path(entry, source_path)
        and str(entry.get("type") or entry.get("kind") or "file").strip() not in {
            "dir",
            "directory",
        }
    ]
    return files


def _workspace_file_entry_mtime(entry: dict[str, Any]) -> float | None:
    for key in ("mtime", "modified_at", "last_modified", "mtime_ns"):
        value = entry.get(key)
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _workspace_file_entry_path(entry: dict[str, Any], source_path: str) -> str:
    for key in ("path", "relative_path", "relpath", "display_path"):
        path = str(entry.get(key) or "").strip()
        if path:
            return _normalized_workspace_path(path)
    name = str(entry.get("name") or "").strip()
    if not name:
        return ""
    base = _normalized_workspace_path(source_path)
    if not base or base == ".":
        return name
    return f"{base.rstrip('/')}/{name}"


def _normalized_workspace_path(value: Any) -> str:
    path = str(value or "").strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path.rstrip("/") or "."


def _tool_request_input_accepts_app_name_resolution(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    if clean_tool.startswith("app."):
        return True
    return clean_tool in {
        "desktop.open_app",
        "desktop.focus_app",
        "desktop.show_app",
        "desktop.hide_app",
        "desktop.quit_app",
        "desktop.inspect_app",
        "desktop.list_windows",
        "desktop.windows",
        "desktop.verify",
        "desktop.read_ui",
        "desktop.ui_elements",
        "desktop.open_path_with_app",
        "app.open_path_with_app",
        "media.music_app_open_and_play",
        "media.music_app_control",
    }


def _tool_request_with_open_path_app_input(
    tool_request: dict[str, Any],
    tool_name: str,
) -> dict[str, Any]:
    if tool_name not in {"desktop.open_path_with_app", "app.open_path_with_app"}:
        return tool_request
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    app_name = str(raw_input.get("app_name") or "").strip()
    if not app_name or _selected_desktop_app_placeholder_source(app_name):
        return tool_request
    if _desktop_app_selection_source(raw_input.get("selection_source")):
        return tool_request
    path = str(raw_input.get("path") or raw_input.get("target_path") or "").strip()
    if not path or (path.startswith("<") and path.endswith(">")):
        return tool_request
    return {
        **tool_request,
        "input": {
            "app_name": app_name,
            "path": path,
        },
    }


def _tool_request_with_verification_target(
    tool_request: dict[str, Any],
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    if not target:
        return tool_request
    if str(tool_request.get("tool") or "").strip() != "desktop.active_window":
        return tool_request
    if isinstance(tool_request.get("verification_target"), dict):
        return tool_request
    app_name = str(target.get("app_name") or "").strip()
    if not app_name:
        return tool_request
    return {
        **tool_request,
        "verification_target": {
            "app_name": app_name,
            **(
                {"source_tool": str(target.get("source_tool") or "").strip()}
                if str(target.get("source_tool") or "").strip()
                else {}
            ),
        },
    }


_FOREGROUND_APP_CONTEXT_TOOLS = {
    "desktop.inspect_app",
    "desktop.list_windows",
    "desktop.windows",
    "desktop.read_ui",
    "desktop.ui_elements",
    "desktop.verify",
}


def _tool_request_with_foreground_app_context(
    tool_request: dict[str, Any],
    target: dict[str, Any] | None,
) -> dict[str, Any]:
    if not target:
        return tool_request
    if str(tool_request.get("foreground_app_context") or "").strip() == "current_app":
        return tool_request
    tool_name = str(tool_request.get("tool") or "").strip()
    if tool_name not in _FOREGROUND_APP_CONTEXT_TOOLS:
        return tool_request
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    if str(raw_input.get("app_name") or "").strip():
        return tool_request
    if _desktop_app_selection_source(raw_input.get("selection_source")):
        return tool_request
    app_name = str(target.get("app_name") or "").strip()
    if not app_name:
        return tool_request
    return {
        **tool_request,
        "input": {**raw_input, "app_name": app_name},
    }


def _tool_request_app_name_resolution(
    tool_request: dict[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, str]:
    raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
    raw_app_name = str(raw_input.get("app_name") or "").strip()
    selection_source = _desktop_app_selection_source(raw_input.get("selection_source"))
    selected_app_query = str(raw_input.get("query") or "").strip()
    placeholder_source = _selected_desktop_app_placeholder_source(raw_app_name)
    source_tool = selection_source or placeholder_source or _DESKTOP_APP_SELECTION_SOURCE
    uses_selected_app_placeholder = (
        bool(placeholder_source)
        or bool(selection_source)
    )
    requested_app_name = (
        selected_app_query
        if uses_selected_app_placeholder and selected_app_query
        else raw_app_name
    )
    selected_candidate: dict[str, Any] = {}
    if uses_selected_app_placeholder and not selected_app_query:
        selected_candidate = _latest_selected_app_candidate(
            timeline,
            source_tool=source_tool,
        )
        discovered_app_name = _app_candidate_name(selected_candidate)
    else:
        discovered_app_name = _discovered_app_name_for_query(
            timeline,
            requested_app_name,
            source_tool=source_tool,
        )
    if not discovered_app_name:
        if (
            selection_source == _DESKTOP_APP_SELECTION_SOURCE
            and raw_app_name
            and not placeholder_source
            and not _is_generic_app_lookup(raw_app_name)
            and not _is_generic_app_lookup(selected_app_query)
            and (
                not selected_app_query
                or _app_lookups_same_identity(raw_app_name, selected_app_query)
            )
        ):
            return {
                "tool": str(tool_request.get("tool") or "").strip(),
                "field": "app_name",
                "requested_app_name": requested_app_name or raw_app_name,
                "resolved_app_name": raw_app_name,
                "source_tool": selection_source,
                "app_resolution_confidence": "explicit",
                "app_resolution_reason": "explicit_app_name_without_selection_evidence",
            }
        return {}
    if (
        not uses_selected_app_placeholder
        and _normalized_app_lookup(discovered_app_name) == _normalized_app_lookup(
            requested_app_name
        )
    ):
        return {}
    evidence = (
        _discovered_app_best_match_evidence(selected_candidate)
        if selected_candidate
        else _discovered_app_resolution_evidence(
            timeline,
            requested_app_name,
            discovered_app_name,
            source_tool=source_tool,
        )
    )
    if selected_candidate and "app_resolution_reason" not in evidence:
        evidence["app_resolution_reason"] = f"latest_{source_tool}_selection"
    return {
        "tool": str(tool_request.get("tool") or "").strip(),
        "field": "app_name",
        "requested_app_name": requested_app_name,
        "resolved_app_name": discovered_app_name,
        "source_tool": source_tool,
        **evidence,
    }


def _discovered_app_resolution_evidence(
    timeline: list[dict[str, Any]],
    requested_app_name: str,
    resolved_app_name: str,
    *,
    source_tool: str = _DESKTOP_APP_SELECTION_SOURCE,
) -> dict[str, str]:
    clean_requested = _normalized_app_lookup(requested_app_name)
    clean_resolved = _normalized_app_lookup(resolved_app_name)
    if not clean_requested or not clean_resolved:
        return {}
    clean_source_tool = _desktop_app_selection_source(source_tool) or _DESKTOP_APP_SELECTION_SOURCE
    for event in reversed(timeline):
        if event.get("event") != "agent.tool.call":
            continue
        if str(event.get("detail") or "") != clean_source_tool:
            continue
        input_preview = event.get("input_preview") if isinstance(event.get("input_preview"), dict) else {}
        if (
            clean_source_tool == _DESKTOP_APP_SELECTION_SOURCE
            and not _app_lookups_related(input_preview.get("query"), clean_requested)
        ):
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if clean_source_tool == _DESKTOP_RUNNING_APP_SELECTION_SOURCE:
            running_match = _best_running_app_match_for_query(result, requested_app_name)
            if (
                isinstance(running_match, dict)
                and _normalized_app_lookup(_app_candidate_name(running_match)) == clean_resolved
            ):
                return _discovered_app_best_match_evidence(running_match)
            continue
        best_match = _best_match_from_list_apps_result(result)
        if (
            isinstance(best_match, dict)
            and _normalized_app_lookup(_app_candidate_name(best_match)) == clean_resolved
        ):
            return _discovered_app_best_match_evidence(best_match)
        for app in _apps_from_list_apps_result(result):
            if not isinstance(app, dict):
                continue
            if _normalized_app_lookup(_app_candidate_name(app)) != clean_resolved:
                continue
            return _discovered_app_best_match_evidence(app)
    return {}


def _discovered_app_best_match_evidence(app: Mapping[str, Any]) -> dict[str, str]:
    evidence: dict[str, str] = {}
    score = _app_match_score(dict(app))
    if score is not None:
        evidence["app_resolution_score"] = str(score)
    confidence = str(
        app.get("match_confidence")
        or app.get("confidence")
        or app.get("app_resolution_confidence")
        or ""
    ).strip()
    if confidence:
        evidence["app_resolution_confidence"] = confidence
    reason = str(
        app.get("match_reason")
        or app.get("reason")
        or app.get("app_resolution_reason")
        or ""
    ).strip()
    if reason:
        evidence["app_resolution_reason"] = reason
    matched_name = str(
        app.get("matched_name")
        or app.get("app_resolution_matched_name")
        or ""
    ).strip()
    if matched_name:
        evidence["app_resolution_matched_name"] = matched_name
    matched_name_source = str(
        app.get("matched_name_source")
        or app.get("app_resolution_matched_name_source")
        or ""
    ).strip()
    if matched_name_source:
        evidence["app_resolution_matched_name_source"] = matched_name_source
    matched_capability = str(
        app.get("matched_capability")
        or app.get("app_resolution_matched_capability")
        or ""
    ).strip()
    if matched_capability:
        evidence["app_resolution_matched_capability"] = matched_capability
    path = _app_candidate_path(app)
    if path:
        evidence["resolved_app_path"] = path
    return evidence


def _input_preview_with_app_name_resolution(
    input_preview: Any,
    resolution: dict[str, str],
) -> Any:
    if not resolution:
        return input_preview
    preview = dict(input_preview) if isinstance(input_preview, dict) else {}
    requested_app_name = str(resolution.get("requested_app_name") or "").strip()
    resolved_app_name = str(resolution.get("resolved_app_name") or "").strip()
    source_tool = str(resolution.get("source_tool") or "").strip()
    if (
        requested_app_name
        and resolved_app_name
        and _normalized_app_lookup(requested_app_name)
        == _normalized_app_lookup(resolved_app_name)
    ):
        current_app_name = str(preview.get("app_name") or "").strip()
        if (
            not current_app_name
            or _normalized_app_lookup(current_app_name)
            == _normalized_app_lookup(resolved_app_name)
        ):
            preview["app_name"] = resolved_app_name
            return preview
    if resolved_app_name:
        preview.setdefault("app_name", resolved_app_name)
        preview.setdefault("resolved_app_name", resolved_app_name)
    if requested_app_name:
        preview.setdefault("requested_app_name", requested_app_name)
    if source_tool:
        preview.setdefault("app_resolution_source", source_tool)
    for key in (
        "app_resolution_score",
        "app_resolution_confidence",
        "app_resolution_reason",
        "app_resolution_matched_name",
        "app_resolution_matched_name_source",
        "app_resolution_matched_capability",
        "resolved_app_path",
    ):
        value = str(resolution.get(key) or "").strip()
        if value:
            preview.setdefault(key, value)
    return preview


def _tool_event_input_preview(tool_name: str, input_preview: Any) -> Any:
    if str(tool_name or "").strip() not in {"desktop.ui_elements", "desktop.read_ui"}:
        return input_preview
    if not isinstance(input_preview, dict):
        return input_preview
    trace_scope_keys = {
        "app_name",
        "selection_source",
        "app_selection_source",
        "query",
        "requested_app_name",
        "resolved_app_name",
        "resolved_app_path",
        "app_resolution_source",
        "app_resolution_score",
        "app_resolution_confidence",
        "app_resolution_reason",
        "app_resolution_matched_name",
        "app_resolution_matched_name_source",
        "app_resolution_matched_capability",
    }
    if input_preview and all(key in trace_scope_keys for key in input_preview):
        return {}
    return input_preview


def _tool_result_artifact(tool_name: str, tool_result: dict[str, Any]) -> dict[str, Any] | None:
    if not tool_result.get("ok"):
        return None
    if tool_name == "artifact.write":
        return {"kind": "tool_artifact", **tool_result}
    raw_artifact = tool_result.get("artifact")
    if not isinstance(raw_artifact, dict):
        return None
    artifact = {"kind": "tool_artifact", "source_tool": tool_name, **raw_artifact}
    if not artifact.get("source_tool"):
        artifact["source_tool"] = tool_name
    return artifact


def _tool_result_extra_artifacts(
    tool_name: str,
    tool_result: dict[str, Any],
    primary_artifact: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    raw_artifacts = tool_result.get("artifacts")
    if not isinstance(raw_artifacts, list):
        return []
    primary_path = str((primary_artifact or {}).get("path") or "")
    artifacts: list[dict[str, Any]] = []
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        artifact = {"kind": "tool_artifact", "source_tool": tool_name, **raw_artifact}
        if not artifact.get("source_tool"):
            artifact["source_tool"] = tool_name
        if primary_path and str(artifact.get("path") or "") == primary_path:
            continue
        if artifact not in artifacts:
            artifacts.append(artifact)
    return artifacts


def _tool_result_with_desktop_provider_session_context(
    tool_request: Mapping[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    session = tool_request.get("desktop_provider_session")
    if not isinstance(session, Mapping) or not session:
        return tool_result
    public_session = _public_desktop_provider_session(session)
    if not public_session or isinstance(
        tool_result.get("desktop_provider_session"),
        Mapping,
    ):
        return tool_result
    return {**tool_result, "desktop_provider_session": public_session}


def _desktop_provider_execution_event(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    input_preview: Any,
) -> tuple[str, str, dict[str, Any]] | None:
    if not bool(tool_result.get("desktop_execution_provider_routed")):
        return None
    provider = _first_mapping(tool_result.get("desktop_execution_provider"))
    if (
        not provider
        or provider.get("adapter_registered") is False
        or bool(tool_result.get("blocked_by_desktop_execution_provider"))
    ):
        return None
    route = _first_mapping(
        tool_result.get("desktop_execution_route"),
        tool_request.get("desktop_execution_route"),
    )
    sandbox_provider = _first_mapping(
        tool_result.get("sandbox_provider"),
        tool_request.get("sandbox_provider"),
        tool_request.get("sandbox_desktop_provider"),
        tool_request.get("desktop_sandbox_provider"),
    )
    payload: dict[str, Any] = {
        "tool": tool_name,
        "ok": bool(tool_result.get("ok", True)),
        "status": str(tool_result.get("status") or ""),
        "input_preview": input_preview if isinstance(input_preview, dict) else {},
    }
    if provider:
        payload["desktop_execution_provider"] = dict(provider)
    if route:
        payload["desktop_execution_route"] = dict(route)
    if sandbox_provider:
        payload["sandbox_provider"] = dict(sandbox_provider)
    session = _first_mapping(
        tool_result.get("desktop_provider_session"),
        tool_request.get("desktop_provider_session"),
    )
    if session:
        public_session = _public_desktop_provider_session(session)
        if public_session:
            payload["desktop_provider_session"] = public_session
    return (
        "desktop.provider_execution.routed",
        "Desktop tool routed through desktop provider",
        payload,
    )


_BROKER_APPROVAL_POLICY_EXCEPTIONS = {
    "file.organize",
    "terminal.run",
    "workspace.write_patch",
}


def _pre_execution_approval_required_result(
    tool_name: str,
    tool_request: Mapping[str, Any],
    broker: Any,
    *,
    approved: bool,
) -> dict[str, Any] | None:
    if approved:
        return None
    request_requires_approval = bool(tool_request.get("approval_required"))
    broker_approvals = getattr(broker, "approvals", None)
    broker_requires_approval = (
        isinstance(broker_approvals, Mapping)
        and bool(broker_approvals.get(tool_name))
        and tool_name not in _BROKER_APPROVAL_POLICY_EXCEPTIONS
    )
    if not request_requires_approval and not broker_requires_approval:
        return None
    validate_request = getattr(broker, "validate_tool_request", None)
    if callable(validate_request):
        request_input = tool_request.get("input")
        validate_request(tool_name, dict(request_input) if isinstance(request_input, Mapping) else {})
    policy_reason = str(
        tool_request.get("policy_reason")
        or tool_request.get("approval_reason")
        or (
            "当前工具策略要求人工确认后再执行。"
            if broker_requires_approval
            else "This planned tool request requires approval before execution."
        )
    ).strip()
    result: dict[str, Any] = {
        "ok": False,
        "approval_required": True,
        "tool": tool_name,
        "status": "approval_required",
        "policy_reason": policy_reason,
    }
    for key in ("risk_level", "plugin_id", "approval_id", "step_id", "planner_step_id"):
        value = tool_request.get(key)
        if value not in (None, "", [], {}):
            result[key] = value
    return result


def _private_verification_receipt_from_terminal_action(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    terminal_event: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Capture only a provider-routed terminal action with a decidable goal."""

    clean_run_id = str(run_id or "").strip()
    clean_tool = str(tool_name or "").strip()
    source_tool_call_id = str(tool_request.get("tool_call_id") or "").strip()
    source_step_id = str(
        tool_request.get("step_id") or tool_request.get("planner_step_id") or ""
    ).strip()
    if (
        not clean_run_id
        or not source_tool_call_id
        or not source_step_id
        or tool_result.get("ok") is not True
        or tool_result.get("approval_required")
        or str(terminal_event.get("event") or "").strip() != "agent.tool.call"
        or str(terminal_event.get("detail") or "").strip() != clean_tool
        or str(terminal_event.get("tool_call_id") or "").strip()
        != source_tool_call_id
    ):
        return {}
    event_result = terminal_event.get("result")
    if not isinstance(event_result, Mapping) or dict(event_result) != dict(tool_result):
        return {}
    provider_kind, provider_id = _trusted_terminal_provider_identity(tool_result)
    if provider_kind != "background_desktop" or not provider_id:
        return {}
    target = _trusted_agent_owned_target(tool_result)
    if not target:
        return {}
    predicate: dict[str, Any] = {}
    if clean_tool in _TRUSTED_APP_WINDOW_RECEIPT_TOOLS:
        sources = _structured_result_sources(tool_result)
        launch_kept_in_background = any(
            source.get("self_activation_suppressed") is True
            or source.get("launch_reused") is True
            for source in sources
        )
        if not launch_kept_in_background or not str(target.get("app_name") or ""):
            return {}
        predicate = {
            "kind": APP_WINDOW_PRESENT_PREDICATE,
            "app_name": str(target["app_name"]),
        }
    elif clean_tool in _TRUSTED_EXACT_TYPED_CONTENT_RECEIPT_TOOLS:
        raw_input = (
            tool_request.get("input")
            if isinstance(tool_request.get("input"), Mapping)
            else {}
        )
        expected_text = raw_input.get("text")
        grounded_element = tool_result.get("grounded_element")
        if (
            not isinstance(expected_text, str)
            or not expected_text
            or len(expected_text) > _ARTIFACT_BODY_TEXT_LIMIT
            or tool_result.get("action_dispatched") is not True
            or not isinstance(grounded_element, Mapping)
            or _runtime_positive_int(grounded_element.get("pid"))
            != target["pid"]
            or _runtime_positive_int(grounded_element.get("window_id"))
            != target["window_id"]
        ):
            return {}
        text_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
        materialization_binding_id = str(
            tool_request.get("materialization_binding_id") or ""
        ).strip()
        materialized_content_sha256 = str(
            tool_request.get("materialized_content_sha256") or ""
        ).strip()
        if (materialization_binding_id or materialized_content_sha256) and (
            not materialization_binding_id
            or materialized_content_sha256 != text_sha256
        ):
            return {}
        predicate = {
            "kind": EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
            "expected_text": expected_text,
            "text_sha256": text_sha256,
        }
    else:
        return {}
    receipt = {
        "run_id": clean_run_id,
        "plan_id": str(tool_request.get("plan_id") or "").strip(),
        "tool_plan_id": str(tool_request.get("tool_plan_id") or "").strip(),
        "source_tool_call_id": source_tool_call_id,
        "source_step_id": source_step_id,
        "source_tool": clean_tool,
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "target": target,
        "predicate": predicate,
    }
    if clean_tool in _TRUSTED_EXACT_TYPED_CONTENT_RECEIPT_TOOLS:
        materialization_binding_id = str(
            tool_request.get("materialization_binding_id") or ""
        ).strip()
        materialized_content_sha256 = str(
            tool_request.get("materialized_content_sha256") or ""
        ).strip()
        if materialization_binding_id:
            receipt["materialization_binding_id"] = materialization_binding_id
            receipt["materialized_content_sha256"] = materialized_content_sha256
    return receipt


def _verification_plan_identity_matches(
    verifier_request: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    for key in ("plan_id", "tool_plan_id"):
        verifier_value = str(verifier_request.get(key) or "").strip()
        receipt_value = str(receipt.get(key) or "").strip()
        if (verifier_value or receipt_value) and verifier_value != receipt_value:
            return False
    return True


def _private_verification_plan_identity_matches(
    verifier_request: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    """Require a concrete shared plan before a private receipt can authorize verify."""

    verifier_plan_id = str(verifier_request.get("plan_id") or "").strip()
    receipt_plan_id = str(receipt.get("plan_id") or "").strip()
    if not verifier_plan_id or verifier_plan_id != receipt_plan_id:
        return False
    verifier_tool_plan_id = str(
        verifier_request.get("tool_plan_id") or ""
    ).strip()
    receipt_tool_plan_id = str(receipt.get("tool_plan_id") or "").strip()
    if (verifier_tool_plan_id or receipt_tool_plan_id) and (
        not verifier_tool_plan_id
        or not receipt_tool_plan_id
        or verifier_tool_plan_id != receipt_tool_plan_id
    ):
        return False
    verifier_binding_id = str(
        verifier_request.get("materialization_binding_id") or ""
    ).strip()
    receipt_binding_id = str(receipt.get("materialization_binding_id") or "").strip()
    verifier_content_sha256 = str(
        verifier_request.get("materialized_content_sha256") or ""
    ).strip()
    receipt_content_sha256 = str(
        receipt.get("materialized_content_sha256") or ""
    ).strip()
    if any(
        (
            verifier_binding_id,
            receipt_binding_id,
            verifier_content_sha256,
            receipt_content_sha256,
        )
    ) and (
        not verifier_binding_id
        or not receipt_binding_id
        or verifier_binding_id != receipt_binding_id
        or not verifier_content_sha256
        or not receipt_content_sha256
        or verifier_content_sha256 != receipt_content_sha256
    ):
        return False
    return True


def _matching_terminal_action_event(
    timeline: list[dict[str, Any]],
    receipt: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    source_tool_call_id = str(receipt.get("source_tool_call_id") or "").strip()
    source_step_id = str(receipt.get("source_step_id") or "").strip()
    source_tool = str(receipt.get("source_tool") or "").strip()
    for event in reversed(timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if str(event.get("tool_call_id") or "").strip() != source_tool_call_id:
            continue
        if str(event.get("detail") or event.get("tool") or "").strip() != source_tool:
            return None
        event_step_id = str(
            event.get("step_id") or event.get("planner_step_id") or ""
        ).strip()
        if event_step_id != source_step_id:
            return None
        if not _verification_plan_identity_matches(event, receipt):
            return None
        for identity_key in (
            "materialization_binding_id",
            "materialized_content_sha256",
        ):
            receipt_identity = str(receipt.get(identity_key) or "").strip()
            if receipt_identity and str(event.get(identity_key) or "").strip() != (
                receipt_identity
            ):
                return None
        result = event.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            return None
        provider_kind, provider_id = _trusted_terminal_provider_identity(result)
        if (
            provider_kind != str(receipt.get("provider_kind") or "").strip()
            or provider_id != str(receipt.get("provider_id") or "").strip()
            or not _same_private_verification_target(
                _trusted_agent_owned_target(result),
                receipt.get("target"),
            )
        ):
            return None
        return event
    return None


def _trusted_terminal_provider_identity(
    result: Mapping[str, Any],
) -> tuple[str, str]:
    provider = (
        result.get("desktop_execution_provider")
        if isinstance(result.get("desktop_execution_provider"), Mapping)
        else {}
    )
    if (
        result.get("desktop_execution_provider_routed") is not True
        or provider.get("adapter_registered") is not True
    ):
        return "", ""
    return (
        str(provider.get("provider_kind") or "").strip(),
        str(provider.get("provider_id") or "").strip(),
    )


def _trusted_agent_owned_target(result: Mapping[str, Any]) -> dict[str, Any]:
    sources = _structured_result_sources(result)
    if not any(source.get("agent_owned_target") is True for source in sources):
        return {}
    evidence = (
        result.get("desktop_execution_provider_evidence")
        if isinstance(result.get("desktop_execution_provider_evidence"), Mapping)
        else {}
    )
    verification_evidence = (
        result.get("verification_evidence")
        if isinstance(result.get("verification_evidence"), Mapping)
        else {}
    )
    verification_target = (
        verification_evidence.get("target")
        if isinstance(verification_evidence.get("target"), Mapping)
        else {}
    )
    grounded_element = (
        result.get("grounded_element")
        if isinstance(result.get("grounded_element"), Mapping)
        else {}
    )
    identity_sources = [*sources, evidence, verification_target, grounded_element]
    target_identity = next(
        (
            (pid, window_id)
            for source in identity_sources
            for pid, window_id in [
                (
                    _runtime_positive_int(source.get("pid")),
                    _runtime_positive_int(source.get("window_id")),
                )
            ]
            if pid is not None and window_id is not None
        ),
        (None, None),
    )
    pid, window_id = target_identity
    if pid is None or window_id is None:
        return {}
    app_name = next(
        (
            str(source.get(key) or "").strip()
            for source in sources
            for key in ("app_name", "name", "resolved_app_name")
            if str(source.get(key) or "").strip()
        ),
        "",
    )
    return {
        "pid": pid,
        "window_id": window_id,
        "agent_owned_target": True,
        **({"app_name": app_name} if app_name else {}),
    }


def _structured_result_sources(result: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    return [result, data]


def _same_private_verification_target(first: Any, second: Any) -> bool:
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return False
    first_app = compact_app_alias(str(first.get("app_name") or "").strip())
    second_app = compact_app_alias(str(second.get("app_name") or "").strip())
    first_pid = _runtime_positive_int(first.get("pid"))
    second_pid = _runtime_positive_int(second.get("pid"))
    first_window_id = _runtime_positive_int(first.get("window_id"))
    second_window_id = _runtime_positive_int(second.get("window_id"))
    return bool(
        first.get("agent_owned_target") is True
        and second.get("agent_owned_target") is True
        and first_pid is not None
        and first_pid == second_pid
        and first_window_id is not None
        and first_window_id == second_window_id
        and (
            first_app == second_app
            if first_app or second_app
            else True
        )
    )


def _runtime_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


class RuntimeToolCallExecutor:
    """Executes one tool call while preserving policy, budget, and event gates."""

    def __init__(
        self,
        *,
        normalize_tool_name: Callable[[Any], str],
        input_preview: Callable[[Any], Any],
        run_budget: Callable[[str, list[dict[str, Any]]], Any],
        validate_tool_payload: Callable[[str, dict[str, Any]], None],
        limit_tool_result: Callable[[dict[str, Any]], dict[str, Any]],
        timeline_factory: Callable[..., dict[str, Any]],
        tool_call_events: Any,
        trace_events: Any,
        append_run_event: Callable[[str, str, dict[str, Any]], Any],
        allows_tool: Callable[[str, list[str]], bool] | None = None,
        desktop_provider_registry: Any | None = None,
        execution_lease_checker: Callable[[str], None] | None = None,
    ) -> None:
        self._normalize_tool_name = normalize_tool_name
        self._input_preview = input_preview
        self._run_budget = run_budget
        self._validate_tool_payload = validate_tool_payload
        self._limit_tool_result = limit_tool_result
        self._timeline = timeline_factory
        self._tool_call_events = tool_call_events
        self._trace_events = trace_events
        self._append_run_event = append_run_event
        self._allows_tool = allows_tool or _default_allows_tool
        self._desktop_provider_registry = (
            desktop_provider_registry or default_desktop_execution_provider_registry()
        )
        self._execution_lease_checker = execution_lease_checker
        self._trusted_verification_receipts: dict[
            tuple[str, str], dict[str, Any]
        ] = {}
        self._trusted_verification_receipts_lock = threading.RLock()
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._trusted_verification_receipts_lock:
            self._trusted_verification_receipts.clear()
        close = getattr(self._desktop_provider_registry, "close", None)
        if callable(close):
            close()

    def execute(
        self,
        tool_request: dict[str, Any],
        allowed_tools: list[str],
        broker: Any,
        timeline: list[dict[str, Any]],
        *,
        artifacts: list[dict[str, Any]] | None = None,
        approved: bool = False,
        run_id: str = "",
        budget: Any = None,
    ) -> dict[str, Any]:
        self._assert_execution_lease(run_id)
        # The lifecycle id belongs to the caller-visible request so every
        # event emitted for this request can be correlated after execution.
        # Runtime-only authority is still attached only to the private copy
        # below and never written back to model-authored input.
        ensure_tool_call_id(tool_request)
        tool_request = dict(tool_request)
        private_prepared_submit_context = tool_request.pop(
            _RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY,
            None,
        )
        tool_request.pop(RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY, None)
        tool_request.pop(RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_REQUEST_KEY, None)
        tool_request.pop(_RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY, None)
        private_recovery_context = tool_request.pop(
            RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY,
            None,
        )
        # A serialized/model-authored marker is never authority.  It is added
        # back only after the process-private object and every lineage field
        # have been checked below.
        tool_request.pop("recovery_context_trusted", None)
        # Clipboard source authority is consumed only by the trusted request
        # runner after this call returns.  Never forward the process-private
        # object to a provider, broker, persisted trace, or model projection.
        tool_request.pop(_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_REQUEST_KEY, None)
        # The model may author a tool request, but only this trusted executor
        # frame can bind it to a live Runtime run. Providers must never derive
        # PID/session capabilities from model-controlled task/core identifiers.
        tool_request.pop("_runtime_execution_scope", None)
        # Reserved for a future receipt-bound verifier context.  Until the
        # runtime can inject it from a trusted prior execution receipt, strip
        # every caller/model-authored copy so public source ids remain
        # observational only.
        tool_request.pop(RUNTIME_PRIVATE_VERIFICATION_CONTEXT_KEY, None)
        trusted_run_id = str(run_id or "").strip()
        if trusted_run_id:
            tool_request["_runtime_execution_scope"] = {
                "run_id": trusted_run_id,
            }
        tool_name = self._normalize_tool_name(tool_request.get("tool"))
        if not (
            tool_name == "desktop.submit_foreground"
            and isinstance(private_prepared_submit_context, Mapping)
            and private_prepared_submit_context.get("_authority")
            is _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY
            and str(private_prepared_submit_context.get("run_id") or "").strip()
            == trusted_run_id
            and str(
                private_prepared_submit_context.get("submit_step_id") or ""
            ).strip()
            == _runtime_request_step_id(tool_request)
            and str(
                private_prepared_submit_context.get("submit_request_id") or ""
            ).strip()
            == str(tool_request.get("request_id") or "").strip()
            and str(
                private_prepared_submit_context.get("submit_tool_call_id") or ""
            ).strip()
            == str(tool_request.get("tool_call_id") or "").strip()
        ):
            private_prepared_submit_context = None
        if tool_name == "desktop.verify":
            # Source identities on a serialized verifier request are claims,
            # not authority.  The executor rebinds them below only when one
            # process-private receipt matches the verifier dependency graph.
            tool_request.pop("source_tool_call_id", None)
            tool_request.pop("source_step_id", None)
        tool_request.update(
            trusted_recovery_trace_fields(
                tool_name,
                tool_request,
                private_recovery_context,
                run_id=trusted_run_id,
            )
        )
        if _model_authored_desktop_request_needs_runtime_policy(
            tool_name,
            tool_request,
        ):
            model_policy = daily_entrypoint_desktop_execution_policy(
                surface="agent_runtime"
            )
            model_policy.update(
                {
                    "require_background_provider_for_local_fallback_tools": True,
                    "source": "model_authored_agent_runtime",
                    "reason": (
                        "Model-authored desktop actions must not select a local "
                        "fallback that can contend with the user's foreground."
                    ),
                }
            )
            tool_request["desktop_execution_policy"] = model_policy
        tool_request = _tool_request_with_desktop_execution_route(tool_name, tool_request)
        bind_owned_provider = getattr(
            self._desktop_provider_registry,
            "bind_tool_request_to_owned_provider",
            None,
        )
        if callable(bind_owned_provider):
            tool_request = bind_owned_provider(tool_name, tool_request)
        verification_context = self._private_verification_context_for_request(
            tool_name,
            tool_request,
            timeline,
            run_id=trusted_run_id,
        )
        if verification_context:
            tool_request[RUNTIME_PRIVATE_VERIFICATION_CONTEXT_KEY] = (
                verification_context
            )
            tool_request["source_tool_call_id"] = verification_context[
                "source_tool_call_id"
            ]
            tool_request["source_step_id"] = verification_context[
                "source_step_id"
            ]
        payload = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
        input_preview = self._input_preview(payload)
        input_resolution = (
            tool_request.get("input_resolution")
            if isinstance(tool_request.get("input_resolution"), dict)
            else {}
        )
        trace_payload = _authoritative_tool_trace_payload(
            tool_request,
            run_id=run_id,
        )
        input_preview = _input_preview_with_app_name_resolution(input_preview, input_resolution)
        input_preview = _input_preview_with_trace_payload(input_preview, trace_payload)
        input_preview = _tool_event_input_preview(tool_name, input_preview)
        budget = budget or self._run_budget(run_id, timeline)
        trusted_control_action = _control_action_allows_tool(tool_name, tool_request)
        if not self._allows_tool(tool_name, allowed_tools) and not trusted_control_action:
            budget.claim_tool_call(tool_name)
            timeline.append(
                self._timeline(
                    "agent.tool.denied",
                    tool_name,
                    input_preview=input_preview,
                    **trace_payload,
                )
            )
            self._tool_call_events.denied(
                run_id,
                tool_name,
                input_preview,
                trace=trace_payload,
            )
            raise AgentRuntimeError(f"Agent 试图调用未授权工具：{tool_name}")
        self._tool_call_events.requested(
            run_id,
            tool_name,
            input_preview,
            approved=approved,
            trace=trace_payload,
        )
        if not trusted_control_action:
            try:
                self._validate_tool_payload(tool_name, payload)
            except AgentRuntimeError as exc:
                self._tool_call_events.failed(
                    run_id,
                    tool_name,
                    input_preview,
                    approved=approved,
                    pre_validation=True,
                    error=exc,
                    trace=trace_payload,
                )
                raise
        runtime_skip = (
            None
            if trusted_control_action
            else _desktop_execution_policy_skip_result(
                tool_name,
                tool_request,
                input_preview,
            )
        )
        if runtime_skip is None and not trusted_control_action:
            runtime_skip = _broker_tool_precondition_failure(broker, tool_name)
        if runtime_skip is not None:
            budget.claim_tool_call(tool_name)
            self._tool_call_events.result(
                run_id,
                tool_name,
                input_preview,
                runtime_skip,
                approved=approved,
                trace=trace_payload,
            )
            timeline.append(
                self._timeline(
                    "agent.tool.skipped",
                    tool_name,
                    input_preview=input_preview,
                    result=runtime_skip,
                    **trace_payload,
                )
            )
            if run_id:
                self._append_run_event(
                    run_id,
                    "agent.tool.skipped",
                    {
                        "tool": tool_name,
                        "input_preview": input_preview,
                        "result": runtime_skip,
                        **trace_payload,
                    },
                )
            _append_desktop_provider_session_required_event(
                timeline=timeline,
                timeline_factory=self._timeline,
                append_run_event=self._append_run_event,
                run_id=run_id,
                tool_name=tool_name,
                runtime_skip=runtime_skip,
                input_preview=input_preview,
                trace_payload=trace_payload,
            )
            return runtime_skip
        _append_desktop_foreground_session_notice(
            timeline=timeline,
            timeline_factory=self._timeline,
            append_run_event=self._append_run_event,
            run_id=run_id,
            tool_name=tool_name,
            tool_request=tool_request,
            input_preview=input_preview,
            trace_payload=trace_payload,
        )
        budget.claim_tool_call(
            tool_name,
            terminal_execution=tool_name in {"terminal.run", "python.run"} and approved,
        )
        self._tool_call_events.started(
            run_id,
            tool_name,
            input_preview,
            approved=approved,
            trace=trace_payload,
        )
        timeline.append(
            self._timeline(
                "agent.tool.started",
                tool_name,
                input_preview=input_preview,
                status="running",
                **trace_payload,
            )
        )
        append_task_progress_events_for_tool_start(
            tool_request={**tool_request, "tool": tool_name},
            timeline=timeline,
            timeline_factory=self._timeline,
            append_run_event=self._append_run_event,
            run_id=run_id,
        )
        self._assert_execution_lease(run_id)
        local_broker_executed = False
        private_exact_submit_result: dict[str, Any] = {}
        try:
            tool_result = _pre_execution_approval_required_result(
                tool_name,
                tool_request,
                broker,
                approved=approved,
            )
            if tool_result is None:
                if private_prepared_submit_context is not None:
                    atomic_submit = getattr(
                        broker,
                        "runtime_exact_submit_foreground",
                        None,
                    )
                    if not callable(atomic_submit):
                        tool_result = {
                            "ok": False,
                            "action": tool_name,
                            "status": "blocked",
                            "reason": "exact_submit_atomic_adapter_unavailable",
                            "error": "exact_submit_atomic_adapter_unavailable",
                            "summary": (
                                "Submit was not dispatched because the Runtime "
                                "could not atomically revalidate its prepared target."
                            ),
                            "retryable": False,
                        }
                    else:
                        pre_holder: dict[str, Any] = {}
                        post_holder: dict[str, Any] = {}

                        def validate_pre(snapshot: Mapping[str, Any]) -> bool:
                            validated = (
                                _private_prepared_submit_snapshot_revalidation(
                                    private_prepared_submit_context,
                                    snapshot,
                                    run_id=trusted_run_id,
                                    phase="pre",
                                    provider_kind=LOCAL_DESKTOP_PROVIDER_KIND,
                                    provider_id=LOCAL_DESKTOP_PROVIDER_ID,
                                )
                            )
                            pre_holder.clear()
                            pre_holder.update(validated)
                            return bool(validated)

                        def observe_post(snapshot: Mapping[str, Any]) -> None:
                            observed = (
                                _private_prepared_submit_snapshot_revalidation(
                                    private_prepared_submit_context,
                                    snapshot,
                                    run_id=trusted_run_id,
                                    phase="post",
                                    provider_kind=LOCAL_DESKTOP_PROVIDER_KIND,
                                    provider_id=LOCAL_DESKTOP_PROVIDER_ID,
                                )
                            )
                            post_holder.clear()
                            post_holder.update(observed)

                        tool_result = atomic_submit(
                            str(payload.get("action") or "").strip(),
                            validate_pre=validate_pre,
                            observe_post=observe_post,
                        )
                        local_broker_executed = True
                        if pre_holder and post_holder:
                            private_exact_submit_result = {
                                "pre_revalidation": dict(pre_holder),
                                "post_revalidation": dict(post_holder),
                            }
                elif trusted_control_action:
                    tool_result = _desktop_provider_session_start_control_result(
                        tool_request,
                        payload,
                    )
                else:
                    tool_result = self._desktop_provider_registry.execute_if_routed(
                        tool_name,
                        payload,
                        tool_request=tool_request,
                        broker=broker,
                        approved=approved,
                    )
                if tool_result is None:
                    tool_result = broker.call(tool_name, payload, approved=approved)
                    local_broker_executed = True
        except AgentRuntimeError as exc:
            self._assert_execution_lease(run_id)
            if not tool_name.startswith("workspace."):
                self._tool_call_events.failed(
                    run_id,
                    tool_name,
                    input_preview,
                    approved=approved,
                    error=exc,
                    trace=trace_payload,
                )
                raise
            terminal_hint = (
                " If the required target is outside the configured workspace, "
                "use terminal.run and wait for approval."
                if "terminal.run" in allowed_tools
                else ""
            )
            boundary_refusal = isinstance(exc, AgentWorkspaceBoundaryError)
            tool_result = {
                "ok": False,
                "tool": tool_name,
                "error": redact_api_error_text(exc),
                "hint": (
                    "Workspace tools only accept relative paths within the configured Default Workdir. "
                    "Use a valid relative path and do not retry the same invalid path."
                    f"{terminal_hint}"
                ),
                **(
                    {"suggested_tool": "terminal.run"}
                    if "terminal.run" in allowed_tools
                    else {}
                ),
                **(
                    {
                        "reason": "workspace_boundary_refusal",
                        "policy_refusal": True,
                        "completion_impact": "report_refusal",
                        "retryable": False,
                    }
                    if boundary_refusal
                    else {}
                ),
            }
        self._assert_execution_lease(run_id)
        tool_result = self._limit_tool_result(tool_result)
        tool_result = _tool_result_with_desktop_provider_session_context(
            tool_request,
            tool_result,
        )
        tool_result = _tool_result_with_active_window_verification_target(
            tool_name,
            tool_result,
            (
                tool_request.get("verification_target")
                if isinstance(tool_request.get("verification_target"), dict)
                else {}
            ),
        )
        tool_result = _tool_result_with_verification_failure_status(tool_result)
        tool_result = _tool_result_with_runtime_execution_provenance(
            tool_result,
            local_broker_executed=local_broker_executed,
        )
        tool_result = _tool_result_with_runtime_submit_dispatch_identity(
            tool_name,
            payload,
            tool_result,
        )
        tool_result = _tool_result_with_trusted_exact_dispatch(
            tool_name,
            tool_request,
            tool_result,
            run_id=trusted_run_id,
        )
        self._tool_call_events.result(
            run_id,
            tool_name,
            input_preview,
            tool_result,
            approved=approved,
            trace=trace_payload,
        )
        terminal_event = self._timeline(
            "agent.tool.call",
            tool_name,
            input_preview=input_preview,
            result=tool_result,
            approved=bool(approved),
            **trace_payload,
        )
        timeline.append(terminal_event)
        self._remember_private_verification_receipt(
            tool_name,
            tool_request,
            tool_result,
            terminal_event,
            run_id=trusted_run_id,
        )
        provider_execution_event = _desktop_provider_execution_event(
            tool_name,
            tool_request,
            tool_result,
            input_preview,
        )
        if provider_execution_event is not None:
            event_type, detail, event_payload = provider_execution_event
            event_payload = _event_payload_with_trace_context(event_payload, trace_payload)
            timeline.append(self._timeline(event_type, detail, **event_payload))
            if run_id:
                self._append_run_event(run_id, event_type, event_payload)
        if run_id:
            self._tool_call_events.agent_tool_call(
                run_id,
                tool_name,
                input_preview,
                tool_result,
                approved=approved,
                trace=trace_payload,
            )
            trace_event = self._trace_events.memory_skill_trace_event(
                tool_name,
                input_preview,
                tool_result,
            )
            if trace_event is not None:
                self._append_run_event(
                    run_id,
                    trace_event["event_type"],
                    _event_payload_with_trace_context(
                        trace_event["payload"],
                        _artifact_context_from_trace_payload(trace_payload),
                    ),
                )
            provider_session_event = _desktop_provider_session_control_event(tool_result)
            if provider_session_event is not None:
                event_type, detail, event_payload = provider_session_event
                event_payload = _event_payload_with_trace_context(event_payload, trace_payload)
                timeline.append(self._timeline(event_type, detail, **event_payload))
                self._append_run_event(run_id, event_type, event_payload)
        artifact = _tool_result_artifact(tool_name, tool_result)
        extra_artifacts = _tool_result_extra_artifacts(tool_name, tool_result, artifact)
        artifact_context = _artifact_context_from_trace_payload(trace_payload)
        if artifact is not None:
            artifact = _artifact_with_context(artifact, artifact_context)
        extra_artifacts = [
            _artifact_with_context(extra_artifact, artifact_context)
            for extra_artifact in extra_artifacts
        ]
        if artifact is not None and artifacts is not None:
            if artifact not in artifacts:
                artifacts.append(artifact)
        for extra_artifact in extra_artifacts:
            if artifacts is not None and extra_artifact not in artifacts:
                artifacts.append(extra_artifact)
        if artifact is not None and run_id:
            self._append_run_event(
                run_id,
                "artifact.created",
                _event_payload_with_artifact_context(
                    self._trace_events.artifact_created_payload(
                        tool_result,
                        run_id=run_id,
                        source_tool=tool_name,
                    ),
                    artifact_context,
                ),
            )
        if run_id:
            for extra_artifact in extra_artifacts:
                self._append_run_event(
                    run_id,
                    "artifact.created",
                    _event_payload_with_artifact_context(
                        self._trace_events.artifact_created_payload(
                            {"ok": True, "artifact": extra_artifact},
                            run_id=run_id,
                            source_tool=tool_name,
                        ),
                        artifact_context,
                    ),
                )
        if (
            private_exact_submit_result
            and str(tool_result.get("submitted_action") or "").strip()
        ):
            return {
                **tool_result,
                _RUNTIME_PRIVATE_EXACT_SUBMIT_RESULT_KEY: (
                    private_exact_submit_result
                ),
            }
        return tool_result

    def _assert_execution_lease(self, run_id: str) -> None:
        if self._execution_lease_checker is not None:
            self._execution_lease_checker(run_id)

    def _remember_private_verification_receipt(
        self,
        tool_name: str,
        tool_request: Mapping[str, Any],
        tool_result: Mapping[str, Any],
        terminal_event: Mapping[str, Any],
        *,
        run_id: str,
    ) -> None:
        receipt = _private_verification_receipt_from_terminal_action(
            tool_name,
            tool_request,
            tool_result,
            terminal_event,
            run_id=run_id,
        )
        source_tool_call_id = str(receipt.get("source_tool_call_id") or "").strip()
        if not receipt or not run_id or not source_tool_call_id:
            return
        key = (run_id, source_tool_call_id)
        with self._trusted_verification_receipts_lock:
            self._trusted_verification_receipts.pop(key, None)
            self._trusted_verification_receipts[key] = receipt
            while len(self._trusted_verification_receipts) > (
                _MAX_PRIVATE_VERIFICATION_RECEIPTS
            ):
                oldest_key = next(iter(self._trusted_verification_receipts))
                self._trusted_verification_receipts.pop(oldest_key, None)

    def _private_verification_context_for_request(
        self,
        tool_name: str,
        tool_request: Mapping[str, Any],
        timeline: list[dict[str, Any]],
        *,
        run_id: str,
    ) -> dict[str, Any]:
        if tool_name != "desktop.verify" or not run_id:
            return {}
        target_step_ids = _postcondition_verifier_target_step_ids(tool_request)
        if not target_step_ids:
            return {}
        route = desktop_execution_route_payload(tool_request)
        provider_kind = str(route.get("selected_provider_kind") or "").strip()
        provider_id = str(route.get("selected_provider_id") or "").strip()
        if not provider_kind or not provider_id:
            return {}
        with self._trusted_verification_receipts_lock:
            receipts = [
                dict(receipt)
                for (receipt_run_id, _source_call_id), receipt in (
                    self._trusted_verification_receipts.items()
                )
                if receipt_run_id == run_id and isinstance(receipt, Mapping)
            ]
        candidates = [
            receipt
            for receipt in receipts
            if str(receipt.get("run_id") or "").strip() == run_id
            and str(receipt.get("source_step_id") or "").strip()
            in target_step_ids
            and _private_verification_plan_identity_matches(
                tool_request,
                receipt,
            )
            and provider_kind
            == str(receipt.get("provider_kind") or "").strip()
            and provider_id == str(receipt.get("provider_id") or "").strip()
            and _matching_terminal_action_event(timeline, receipt) is not None
        ]
        if len(candidates) != 1:
            return {}
        receipt = candidates[0]
        source_tool_call_id = str(
            receipt.get("source_tool_call_id") or ""
        ).strip()
        source_step_id = str(receipt.get("source_step_id") or "").strip()
        if not source_tool_call_id or not source_step_id:
            return {}
        return {
            "version": RUNTIME_PRIVATE_VERIFICATION_CONTEXT_VERSION,
            "_authority": RUNTIME_PRIVATE_VERIFICATION_AUTHORITY,
            "run_id": run_id,
            "plan_id": str(receipt.get("plan_id") or ""),
            "tool_plan_id": str(receipt.get("tool_plan_id") or ""),
            "source_tool_call_id": source_tool_call_id,
            "source_step_id": source_step_id,
            "source_tool": str(receipt.get("source_tool") or ""),
            "provider_kind": provider_kind,
            "provider_id": provider_id,
            "target": dict(receipt.get("target") or {}),
            "predicate": dict(receipt.get("predicate") or {}),
            **(
                {
                    "materialization_binding_id": str(
                        receipt.get("materialization_binding_id") or ""
                    ),
                    "materialized_content_sha256": str(
                        receipt.get("materialized_content_sha256") or ""
                    ),
                }
                if receipt.get("materialization_binding_id")
                else {}
            ),
        }


def _prepare_runtime_private_clipboard_source_requests(
    tool_requests: list[dict[str, Any]],
) -> None:
    """Mark only an explicit read dependency as a private paste source.

    A bare paste remains unverified.  The Runtime upgrades a ``clipboard.read``
    only when a later paste declares it as a direct dependency inside the same
    concrete plan.  The process-private marker cannot be serialized by a model
    or restored from a public Run event.
    """

    indexed_requests: list[tuple[int, dict[str, Any]]] = []
    for index, request in enumerate(tool_requests):
        if not isinstance(request, dict):
            continue
        request.pop(_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_REQUEST_KEY, None)
        indexed_requests.append((index, request))
    for paste_index, paste_request in indexed_requests:
        if not _is_clipboard_paste_request(paste_request):
            continue
        if paste_request.get("requires_post_action_verification") is not True:
            continue
        paste_plan_id = str(paste_request.get("plan_id") or "").strip()
        paste_step_id = _runtime_request_step_id(paste_request)
        dependencies = set(_string_list(paste_request.get("depends_on")))
        if not paste_plan_id or not paste_step_id or not dependencies:
            continue
        candidates = [
            source_request
            for source_index, source_request in indexed_requests
            if source_index < paste_index
            and str(source_request.get("tool") or "").strip() == "clipboard.read"
            and _runtime_request_step_id(source_request) in dependencies
            and _runtime_request_plan_identity_matches(
                paste_request,
                source_request,
            )
        ]
        if len(candidates) != 1:
            continue
        source_request = candidates[0]
        source_request[_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_REQUEST_KEY] = (
            _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
        )
        raw_input = (
            source_request.get("input")
            if isinstance(source_request.get("input"), Mapping)
            else {}
        )
        source_request["input"] = {
            **dict(raw_input),
            "max_chars": _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_MAX_CHARS,
        }


def _runtime_request_step_id(request: Mapping[str, Any]) -> str:
    return str(
        request.get("step_id") or request.get("planner_step_id") or ""
    ).strip()


def _bind_exact_workspace_file_readback_verifier(
    source_request: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]],
    *,
    run_id: str,
) -> bool:
    """Bind one planned workspace file-read verifier to an exact producer invocation.

    The opaque marker is minted only after the Runner observes source success.
    Public lineage fields remain useful for audit, but cannot independently
    authorize a readback receipt or survive serialized continuation.
    """

    source_tool = str(
        source_request.get("tool") or source_request.get("tool_name") or ""
    ).strip()
    source_stage = str(source_request.get("runtime_stage") or "").strip()
    source_step_id = _runtime_request_step_id(source_request)
    source_request_id = str(source_request.get("request_id") or "").strip()
    source_tool_call_id = str(
        source_request.get("tool_call_id") or ""
    ).strip()
    source_plan_id = str(source_request.get("plan_id") or "").strip()
    clean_run_id = str(run_id or "").strip()
    output_path = declared_workspace_output_path(
        _first_mapping(source_request.get("action_target"))
    )
    if (
        source_tool not in _EXACT_FILE_READBACK_SOURCE_TOOLS
        or source_stage not in {"operate", "produce"}
        or not clean_run_id
        or not source_step_id
        or not source_request_id
        or not source_tool_call_id
        or not source_plan_id
        or not output_path
    ):
        return False

    bindings = {
        "run_id": clean_run_id,
        "plan_id": source_plan_id,
        "source_tool": source_tool,
        "source_step_id": source_step_id,
        "source_request_id": source_request_id,
        "source_tool_call_id": source_tool_call_id,
        "source_output_path": output_path,
        "verification_predicate_kind": EXACT_FILE_CONTENT_PRESENT_PREDICATE,
    }
    candidates: list[dict[str, Any]] = []
    for request in remaining_requests:
        if not isinstance(request, dict):
            continue
        verifier_tool = str(
            request.get("tool") or request.get("tool_name") or ""
        ).strip()
        runtime_stage = str(request.get("runtime_stage") or "").strip()
        runtime_role = str(request.get("runtime_role") or "").strip()
        verifier_step_id = _runtime_request_step_id(request)
        verifier_input = (
            request.get("input")
            if isinstance(request.get("input"), Mapping)
            else {}
        )
        verifier_path = normalized_workspace_relative_path(
            verifier_input.get("path")
        )
        if not (
            verifier_tool in EXACT_FILE_READBACK_VERIFIER_TOOLS
            and (runtime_stage == "verify" or runtime_role == "verify_result")
            and verifier_step_id
            and verifier_step_id != source_step_id
            and str(request.get("request_id") or "").strip()
            and request.get("approval_required") is not True
            and source_step_id in _string_list(request.get("depends_on"))
            and verifier_path == output_path
            and _runtime_request_plan_identity_matches(source_request, request)
        ):
            continue
        verifier_run_id = str(request.get("run_id") or "").strip()
        if verifier_run_id and verifier_run_id != clean_run_id:
            continue
        if any(
            str(request.get(key) or "").strip()
            and str(request.get(key) or "").strip() != expected
            for key, expected in bindings.items()
        ):
            continue
        candidates.append(request)
    if len(candidates) != 1:
        return False
    verifier = candidates[0]
    verifier.update(bindings)
    verifier[_RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY] = (
        _RUNTIME_PRIVATE_EXACT_FILE_READBACK_AUTHORITY
    )
    return True


def _runtime_request_plan_identity_matches(
    first: Mapping[str, Any],
    second: Mapping[str, Any],
) -> bool:
    first_plan_id = str(first.get("plan_id") or "").strip()
    second_plan_id = str(second.get("plan_id") or "").strip()
    if not first_plan_id or first_plan_id != second_plan_id:
        return False
    for key in ("decision_id", "tool_plan_id"):
        first_value = str(first.get(key) or "").strip()
        second_value = str(second.get(key) or "").strip()
        if (first_value or second_value) and (
            not first_value
            or not second_value
            or first_value != second_value
        ):
            return False
    return True


def _private_clipboard_source_receipt_from_result(
    source_request: Mapping[str, Any],
    source_result: Mapping[str, Any],
    *,
    run_id: str,
    tool_sequence: int,
) -> dict[str, Any]:
    if source_request.get(_RUNTIME_PRIVATE_CLIPBOARD_SOURCE_REQUEST_KEY) is not (
        _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
    ):
        return {}
    if str(source_request.get("tool") or "").strip() != "clipboard.read":
        return {}
    clean_run_id = str(run_id or source_request.get("run_id") or "").strip()
    request_run_id = str(source_request.get("run_id") or "").strip()
    plan_id = str(source_request.get("plan_id") or "").strip()
    source_step_id = _runtime_request_step_id(source_request)
    source_request_id = str(source_request.get("request_id") or "").strip()
    source_tool_call_id = str(source_request.get("tool_call_id") or "").strip()
    if (
        not clean_run_id
        or (request_run_id and request_run_id != clean_run_id)
        or not plan_id
        or not source_step_id
        or not source_request_id
        or not source_tool_call_id
        or source_result.get("ok") is not True
        or source_result.get("approval_required")
        or source_result.get("truncated") is True
    ):
        return {}
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        source_request,
        source_result,
    )
    if not provider_kind or not provider_id:
        return {}
    data = (
        source_result.get("data")
        if isinstance(source_result.get("data"), Mapping)
        else {}
    )
    content = data.get("text")
    text_length = data.get("text_length")
    max_chars = data.get("max_chars")
    if (
        not isinstance(content, str)
        or not content
        or data.get("truncated") is not False
        or isinstance(text_length, bool)
        or not isinstance(text_length, int)
        or text_length != len(content)
        or isinstance(max_chars, bool)
        or not isinstance(max_chars, int)
        or max_chars < text_length
    ):
        return {}
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        return {}
    sequence = max(0, int(tool_sequence))
    return {
        "_authority": _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY,
        "run_id": clean_run_id,
        "decision_id": str(source_request.get("decision_id") or "").strip(),
        "plan_id": plan_id,
        "tool_plan_id": str(source_request.get("tool_plan_id") or "").strip(),
        "source_step_id": source_step_id,
        "source_request_id": source_request_id,
        "source_tool_call_id": source_tool_call_id,
        "source_tool": "clipboard.read",
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "content": content,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_length": len(content),
        "content_byte_length": len(encoded),
        "created_tool_sequence": sequence,
        "expires_after_tool_sequence": (
            sequence + _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_MAX_TOOL_AGE
        ),
        "consumed_by_tool_call_id": "",
    }


def _private_clipboard_source_for_paste(
    paste_request: Mapping[str, Any],
    source_receipts: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    tool_sequence: int,
) -> dict[str, Any]:
    if not _is_clipboard_paste_request(paste_request):
        return {}
    clean_run_id = str(run_id or paste_request.get("run_id") or "").strip()
    request_run_id = str(paste_request.get("run_id") or "").strip()
    plan_id = str(paste_request.get("plan_id") or "").strip()
    paste_step_id = _runtime_request_step_id(paste_request)
    paste_request_id = str(paste_request.get("request_id") or "").strip()
    paste_tool_call_id = str(paste_request.get("tool_call_id") or "").strip()
    dependencies = set(_string_list(paste_request.get("depends_on")))
    if (
        not clean_run_id
        or (request_run_id and request_run_id != clean_run_id)
        or not plan_id
        or not paste_step_id
        or not paste_request_id
        or not paste_tool_call_id
        or not dependencies
    ):
        return {}
    candidates = [
        dict(receipt)
        for receipt in source_receipts.values()
        if isinstance(receipt, Mapping)
        and receipt.get("_authority") is _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
        and str(receipt.get("run_id") or "").strip() == clean_run_id
        and str(receipt.get("plan_id") or "").strip() == plan_id
        and str(receipt.get("source_step_id") or "").strip() in dependencies
        and not str(receipt.get("consumed_by_tool_call_id") or "").strip()
        and int(receipt.get("created_tool_sequence") or 0) < int(tool_sequence)
        and int(receipt.get("expires_after_tool_sequence") or -1)
        >= int(tool_sequence)
        and _runtime_request_receipt_optional_identity_matches(
            paste_request,
            receipt,
        )
    ]
    if len(candidates) != 1:
        return {}
    receipt = candidates[0]
    claimed_source_request_id = str(
        paste_request.get("clipboard_source_request_id") or ""
    ).strip()
    claimed_source_tool_call_id = str(
        paste_request.get("clipboard_source_tool_call_id") or ""
    ).strip()
    if claimed_source_request_id and claimed_source_request_id != str(
        receipt.get("source_request_id") or ""
    ).strip():
        return {}
    if claimed_source_tool_call_id and claimed_source_tool_call_id != str(
        receipt.get("source_tool_call_id") or ""
    ).strip():
        return {}
    return receipt


def _runtime_request_receipt_optional_identity_matches(
    request: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> bool:
    for key in ("decision_id", "tool_plan_id"):
        request_value = str(request.get(key) or "").strip()
        receipt_value = str(receipt.get(key) or "").strip()
        if (request_value or receipt_value) and (
            not request_value
            or not receipt_value
            or request_value != receipt_value
        ):
            return False
    return True


def _consume_private_clipboard_source_receipt(
    receipt: Mapping[str, Any],
    source_receipts: dict[str, dict[str, Any]],
    *,
    paste_tool_call_id: str,
) -> None:
    source_tool_call_id = str(receipt.get("source_tool_call_id") or "").strip()
    clean_paste_tool_call_id = str(paste_tool_call_id or "").strip()
    current = source_receipts.get(source_tool_call_id)
    if (
        not source_tool_call_id
        or not clean_paste_tool_call_id
        or not isinstance(current, Mapping)
        or current.get("_authority") is not _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
    ):
        return
    source_receipts[source_tool_call_id] = {
        **dict(current),
        "consumed_by_tool_call_id": clean_paste_tool_call_id,
    }


def _private_clipboard_paste_binding_from_action(
    source_receipt: Mapping[str, Any],
    paste_request: Mapping[str, Any],
    paste_result: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]] | None = None,
    *,
    run_id: str,
    tool_sequence: int,
) -> dict[str, Any]:
    if source_receipt.get("_authority") is not (
        _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
    ):
        return {}
    if (
        not _is_clipboard_paste_request(paste_request)
        or paste_result.get("ok") is not True
        or paste_result.get("approval_required")
        or paste_result.get("permission_error") is True
    ):
        return {}
    clean_run_id = str(run_id or paste_request.get("run_id") or "").strip()
    paste_tool_call_id = str(paste_request.get("tool_call_id") or "").strip()
    paste_request_id = str(paste_request.get("request_id") or "").strip()
    paste_step_id = _runtime_request_step_id(paste_request)
    plan_id = str(paste_request.get("plan_id") or "").strip()
    if not all(
        (clean_run_id, paste_tool_call_id, paste_request_id, paste_step_id, plan_id)
    ):
        return {}
    if (
        str(source_receipt.get("run_id") or "").strip() != clean_run_id
        or str(source_receipt.get("plan_id") or "").strip() != plan_id
        or not _runtime_request_receipt_optional_identity_matches(
            paste_request,
            source_receipt,
        )
    ):
        return {}
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        paste_request,
        paste_result,
    )
    if (
        not provider_kind
        or not provider_id
        or provider_kind != str(source_receipt.get("provider_kind") or "").strip()
        or provider_id != str(source_receipt.get("provider_id") or "").strip()
    ):
        return {}
    target_app_name = _approval_dependency_request_app_name(paste_request)
    target_ui_element = _clipboard_paste_target_ui_element(paste_request)
    target_window = _trusted_ui_window_identity(
        paste_result,
        expected_app_name=target_app_name,
    )
    verifier = _declared_exact_paste_verifier(
        paste_request,
        remaining_requests or [],
    )
    if not target_app_name or not target_ui_element or not target_window or not verifier:
        return {}
    for source in _structured_result_sources(paste_result):
        result_app_name = str(
            source.get("app_name")
            or source.get("active_app_name")
            or source.get("target_app_name")
            or ""
        ).strip()
        if result_app_name and not _app_lookups_same_identity(
            target_app_name,
            result_app_name,
        ):
            return {}
    sequence = max(0, int(tool_sequence))
    return {
        "_authority": _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY,
        "run_id": clean_run_id,
        "decision_id": str(paste_request.get("decision_id") or "").strip(),
        "plan_id": plan_id,
        "tool_plan_id": str(paste_request.get("tool_plan_id") or "").strip(),
        "paste_step_id": paste_step_id,
        "paste_request_id": paste_request_id,
        "paste_tool_call_id": paste_tool_call_id,
        "paste_tool": str(paste_request.get("tool") or "").strip(),
        "target_app_name": target_app_name,
        "target_window": target_window,
        "target_ui_element": target_ui_element,
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "verifier_step_id": _runtime_request_step_id(verifier),
        "verifier_request_id": str(verifier.get("request_id") or "").strip(),
        "verifier_tool_call_id": str(
            verifier.get("tool_call_id") or ""
        ).strip(),
        "clipboard_source_step_id": str(
            source_receipt.get("source_step_id") or ""
        ).strip(),
        "clipboard_source_request_id": str(
            source_receipt.get("source_request_id") or ""
        ).strip(),
        "clipboard_source_tool_call_id": str(
            source_receipt.get("source_tool_call_id") or ""
        ).strip(),
        "content": source_receipt.get("content"),
        "content_sha256": str(source_receipt.get("content_sha256") or "").strip(),
        "content_length": source_receipt.get("content_length"),
        "content_byte_length": source_receipt.get("content_byte_length"),
        "expires_after_tool_sequence": (
            sequence + _RUNTIME_PRIVATE_CLIPBOARD_PASTE_MAX_TOOL_AGE
        ),
    }


def _clipboard_paste_target_ui_element(request: Mapping[str, Any]) -> str:
    raw_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    action_target = (
        request.get("action_target")
        if isinstance(request.get("action_target"), Mapping)
        else {}
    )
    return str(
        raw_input.get("target")
        or raw_input.get("ui_element")
        or raw_input.get("field")
        or action_target.get("target")
        or action_target.get("ui_element")
        or action_target.get("field")
        or ""
    ).strip()


def _declared_exact_paste_verifier(
    paste_request: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bind one exact read-only verifier before retaining clipboard bytes."""

    paste_step_id = _runtime_request_step_id(paste_request)
    paste_request_id = str(paste_request.get("request_id") or "").strip()
    paste_tool_call_id = str(paste_request.get("tool_call_id") or "").strip()
    if not all((paste_step_id, paste_request_id, paste_tool_call_id)):
        return {}
    candidates: list[dict[str, Any]] = []
    for request in remaining_requests:
        if not isinstance(request, dict):
            continue
        verifier_tool = str(
            request.get("tool") or request.get("tool_name") or ""
        ).strip()
        verifier_step_id = _runtime_request_step_id(request)
        if (
            verifier_tool not in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
            or verifier_step_id in {"", paste_step_id}
            or not (
                str(request.get("runtime_stage") or "").strip() == "verify"
                or str(request.get("runtime_role") or "").strip()
                == "verify_result"
            )
            or not _declared_verifier_targets_source_step(
                request,
                paste_step_id,
            )
            or not _runtime_request_plan_identity_matches(
                paste_request,
                request,
            )
        ):
            continue
        claimed_source_fields = {
            "source_step_id": paste_step_id,
            "source_request_id": paste_request_id,
            "source_tool_call_id": paste_tool_call_id,
        }
        if any(
            str(request.get(key) or "").strip() not in {"", expected}
            for key, expected in claimed_source_fields.items()
        ):
            continue
        ensure_tool_call_id(request)
        verifier_request_id = str(request.get("request_id") or "").strip()
        verifier_tool_call_id = str(request.get("tool_call_id") or "").strip()
        if not verifier_request_id or not verifier_tool_call_id:
            continue
        request.update(claimed_source_fields)
        request.setdefault(
            "verification_predicate_kind",
            EXACT_PASTED_CONTENT_PRESENT_PREDICATE,
        )
        candidates.append(request)
    return candidates[0] if len(candidates) == 1 else {}


def _private_clipboard_paste_binding_for_verifier(
    verifier_request: Mapping[str, Any],
    paste_bindings: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
    tool_sequence: int,
) -> dict[str, Any]:
    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    binding = paste_bindings.get(source_tool_call_id)
    if (
        not source_tool_call_id
        or not isinstance(binding, Mapping)
        or binding.get("_authority") is not _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
    ):
        return {}
    clean_run_id = str(run_id or verifier_request.get("run_id") or "").strip()
    if (
        not clean_run_id
        or str(binding.get("run_id") or "").strip() != clean_run_id
        or str(binding.get("paste_tool_call_id") or "").strip()
        != source_tool_call_id
        or int(binding.get("expires_after_tool_sequence") or -1)
        < int(tool_sequence)
        or str(verifier_request.get("plan_id") or "").strip()
        != str(binding.get("plan_id") or "").strip()
        or not _runtime_request_receipt_optional_identity_matches(
            verifier_request,
            binding,
        )
    ):
        return {}
    source_step_id = str(verifier_request.get("source_step_id") or "").strip()
    if source_step_id != str(binding.get("paste_step_id") or "").strip():
        return {}
    verifier_identity = {
        "verifier_step_id": _runtime_request_step_id(verifier_request),
        "verifier_request_id": str(
            verifier_request.get("request_id") or ""
        ).strip(),
        "verifier_tool_call_id": str(
            verifier_request.get("tool_call_id") or ""
        ).strip(),
    }
    if any(not value for value in verifier_identity.values()) or any(
        str(binding.get(key) or "").strip() != value
        for key, value in verifier_identity.items()
    ):
        return {}
    requested_app_name = _approval_dependency_request_app_name(verifier_request)
    target_app_name = str(binding.get("target_app_name") or "").strip()
    if requested_app_name and not _app_lookups_same_identity(
        requested_app_name,
        target_app_name,
    ):
        return {}
    return dict(binding)


def _runtime_owned_exact_submit_provider_identity(
    result: Mapping[str, Any],
) -> tuple[str, str]:
    """Return only executor-minted local or provider-routed identity."""

    provenance = (
        result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
        if isinstance(result.get(RUNTIME_EXECUTION_PROVENANCE_KEY), Mapping)
        else {}
    )
    if (
        provenance.get("source") == RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        and provenance.get("version") == RUNTIME_EXECUTION_PROVENANCE_VERSION
    ):
        # A local executor receipt and a claimed routed-provider receipt are
        # mutually exclusive.  Accepting both would let provider output pick
        # the identity later projected into Goal evidence.
        if result.get("desktop_execution_provider_routed") is True:
            return "", ""
        return LOCAL_DESKTOP_PROVIDER_KIND, LOCAL_DESKTOP_PROVIDER_ID

    provider = (
        result.get("desktop_execution_provider")
        if isinstance(result.get("desktop_execution_provider"), Mapping)
        else {}
    )
    identity = _approval_observation_provider_identity(
        result,
        expected_tool="desktop.submit_foreground",
    )
    if (
        identity is None
        or result.get("desktop_execution_provider_routed") is not True
        or provider.get("adapter_registered") is not True
    ):
        return "", ""
    provider_id, provider_kind = identity
    return str(provider_kind or "").strip(), str(provider_id or "").strip()


def _private_prepared_submit_context_from_observation(
    receipt: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    run_id: str,
    private_clipboard_paste_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    predicate = str(receipt.get("verification_predicate_kind") or "").strip()
    if predicate not in {
        EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
        EXACT_PASTED_CONTENT_PRESENT_PREDICATE,
    }:
        return {}
    clean_run_id = str(run_id or "").strip()
    source_step_id = str(receipt.get("source_step_id") or "").strip()
    source_tool_call_id = str(receipt.get("source_tool_call_id") or "").strip()
    content_sha256 = str(receipt.get("content_sha256") or "").strip().lower()
    target_window = receipt.get("target_window")
    target_ui_identity = receipt.get("target_ui_identity")
    if (
        not clean_run_id
        or not source_step_id
        or not source_tool_call_id
        or len(content_sha256) != 64
        or any(char not in "0123456789abcdef" for char in content_sha256)
        or not isinstance(target_window, Mapping)
        or not isinstance(target_ui_identity, Mapping)
        or not target_ui_identity
    ):
        return {}
    source_events = [
        event
        for event in timeline
        if isinstance(event, Mapping)
        and str(event.get("event") or event.get("event_type") or "").strip()
        == "agent.tool.call"
        and str(event.get("actor") or "").strip() == "native_runtime"
        and str(event.get("execution_authority") or "").strip()
        == "runtime_tool_executor"
        and str(event.get("run_id") or "").strip() == clean_run_id
        and event.get("approval_resume_result_canonical") is not True
        and isinstance(event.get("result"), Mapping)
        and event["result"].get("ok") is True
        and not event["result"].get("approval_required")
        and str(event.get("plan_id") or "").strip()
        == str(receipt.get("plan_id") or "").strip()
        and str(event.get("tool_call_id") or "").strip() == source_tool_call_id
        and str(event.get("step_id") or event.get("planner_step_id") or "").strip()
        == source_step_id
    ]
    if len(source_events) != 1:
        return {}
    source_event = source_events[0]
    expected_text: Any = None
    if (
        isinstance(private_clipboard_paste_binding, Mapping)
        and private_clipboard_paste_binding.get("_authority")
        is _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
        and str(
            private_clipboard_paste_binding.get("paste_tool_call_id") or ""
        ).strip()
        == source_tool_call_id
    ):
        expected_text = private_clipboard_paste_binding.get("content")
    if not isinstance(expected_text, str):
        source_input = (
            source_event.get("input_preview")
            if isinstance(source_event.get("input_preview"), Mapping)
            else {}
        )
        expected_text = source_input.get("text")
    if (
        not isinstance(expected_text, str)
        or not expected_text
        or hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
        != content_sha256
    ):
        return {}
    provider_kind = str(receipt.get("provider_kind") or "").strip()
    provider_id = str(receipt.get("provider_id") or "").strip()
    source_request_id = str(source_event.get("request_id") or "").strip()
    verifier_step_id = _runtime_request_step_id(verifier_request)
    verifier_request_id = str(verifier_request.get("request_id") or "").strip()
    verifier_tool_call_id = str(
        verifier_request.get("tool_call_id") or ""
    ).strip()
    if not all(
        (
            provider_kind,
            provider_id,
            source_request_id,
            verifier_step_id,
            verifier_request_id,
            verifier_tool_call_id,
        )
    ):
        return {}
    return {
        "_authority": _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY,
        "run_id": clean_run_id,
        "decision_id": str(receipt.get("decision_id") or "").strip(),
        "plan_id": str(receipt.get("plan_id") or "").strip(),
        "tool_plan_id": str(verifier_request.get("tool_plan_id") or "").strip(),
        "source_tool": str(receipt.get("source_tool") or "").strip(),
        "source_step_id": source_step_id,
        "source_request_id": source_request_id,
        "source_tool_call_id": source_tool_call_id,
        "source_verifier_step_id": verifier_step_id,
        "source_verifier_request_id": verifier_request_id,
        "source_verifier_tool_call_id": verifier_tool_call_id,
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "target_app_name": str(target_window.get("app_name") or "").strip(),
        "target_window": dict(target_window),
        "target_ui_identity": dict(target_ui_identity),
        "content": expected_text,
        "content_sha256": content_sha256,
    }


def _private_prepared_submit_context_for_request(
    submit_request: Mapping[str, Any],
    contexts: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    if str(submit_request.get("tool") or "").strip() != "desktop.submit_foreground":
        return {}
    dependencies = set(_string_list(submit_request.get("depends_on")))
    candidates = [
        dict(context)
        for source_step_id, context in contexts.items()
        if source_step_id in dependencies
        and isinstance(context, Mapping)
        and context.get("_authority") is _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY
        and str(context.get("run_id") or "").strip() == str(run_id or "").strip()
        and _runtime_request_receipt_optional_identity_matches(
            submit_request,
            context,
        )
        and str(submit_request.get("plan_id") or "").strip()
        == str(context.get("plan_id") or "").strip()
    ]
    if len(candidates) != 1:
        return {}
    context = candidates[0]
    submit_identity = {
        "submit_step_id": _runtime_request_step_id(submit_request),
        "submit_request_id": str(submit_request.get("request_id") or "").strip(),
        "submit_tool_call_id": str(
            submit_request.get("tool_call_id") or ""
        ).strip(),
    }
    if any(not value for value in submit_identity.values()):
        return {}
    return {**context, **submit_identity}


def persisted_prepared_submit_receipt_from_private_context(
    context: Mapping[str, Any],
) -> dict[str, Any]:
    """Serialize facts, never authority, for a private approval envelope."""

    if context.get("_authority") is not _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY:
        return {}
    scalar_keys = (
        "run_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "source_tool",
        "source_step_id",
        "source_request_id",
        "source_tool_call_id",
        "source_verifier_step_id",
        "source_verifier_request_id",
        "source_verifier_tool_call_id",
        "provider_kind",
        "provider_id",
        "target_app_name",
        "content",
        "content_sha256",
        "submit_step_id",
        "submit_request_id",
        "submit_tool_call_id",
    )
    receipt = {
        "version": _RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_VERSION,
        "receipt_kind": "runtime_prepared_submit_receipt",
        **{
            key: str(context.get(key) or "").strip()
            for key in scalar_keys
        },
        "target_window": dict(context.get("target_window") or {}),
        "target_ui_identity": dict(context.get("target_ui_identity") or {}),
    }
    required = (
        "run_id",
        "plan_id",
        "source_step_id",
        "source_request_id",
        "source_tool_call_id",
        "source_verifier_step_id",
        "source_verifier_request_id",
        "source_verifier_tool_call_id",
        "provider_kind",
        "provider_id",
        "target_app_name",
        "content",
        "content_sha256",
        "submit_step_id",
        "submit_request_id",
        "submit_tool_call_id",
    )
    return receipt if all(receipt.get(key) for key in required) else {}


def rehydrate_private_prepared_submit_context(
    tool_request: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    run_id: str,
    goal_contract: Any = None,
) -> dict[str, Any]:
    """Re-mint opaque submit authority from an exact canonical receipt."""

    persisted = tool_request.get(RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY)
    if not isinstance(persisted, Mapping) or (
        persisted.get("version")
        != _RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_VERSION
        or str(persisted.get("receipt_kind") or "").strip()
        != "runtime_prepared_submit_receipt"
        or str(tool_request.get("tool") or "").strip()
        != "desktop.submit_foreground"
    ):
        return {}
    clean_run_id = str(run_id or "").strip()
    exact_request_fields = {
        "run_id": clean_run_id,
        "plan_id": str(tool_request.get("plan_id") or "").strip(),
        "decision_id": str(tool_request.get("decision_id") or "").strip(),
        "tool_plan_id": str(tool_request.get("tool_plan_id") or "").strip(),
        "submit_step_id": _runtime_request_step_id(tool_request),
        "submit_request_id": str(tool_request.get("request_id") or "").strip(),
        "submit_tool_call_id": str(
            tool_request.get("tool_call_id") or ""
        ).strip(),
    }
    if any(not value for value in exact_request_fields.values()) or any(
        str(persisted.get(key) or "").strip() != value
        for key, value in exact_request_fields.items()
    ):
        return {}
    source_step_id = str(persisted.get("source_step_id") or "").strip()
    if source_step_id not in set(_string_list(tool_request.get("depends_on"))):
        return {}
    content = persisted.get("content")
    content_sha256 = str(persisted.get("content_sha256") or "").strip()
    target_window = persisted.get("target_window")
    target_ui_identity = persisted.get("target_ui_identity")
    if (
        not isinstance(content, str)
        or not content
        or hashlib.sha256(content.encode("utf-8")).hexdigest() != content_sha256
        or not isinstance(target_window, Mapping)
        or not isinstance(target_ui_identity, Mapping)
        or not str(target_window.get("app_name") or "").strip()
        or _runtime_positive_int(target_window.get("pid")) is None
        or _runtime_positive_int(target_window.get("window_id")) is None
        or not target_ui_identity
    ):
        return {}
    contract_id = str(getattr(goal_contract, "contract_id", "") or "").strip()
    contract_run_id = str(getattr(goal_contract, "run_id", "") or "").strip()
    request_contract_id = str(tool_request.get("goal_contract_id") or "").strip()
    if (
        not contract_id
        or contract_run_id != clean_run_id
        or (request_contract_id and request_contract_id != contract_id)
    ):
        return {}
    source_verifier_tool_call_id = str(
        persisted.get("source_verifier_tool_call_id") or ""
    ).strip()
    source_verifier_request_id = str(
        persisted.get("source_verifier_request_id") or ""
    ).strip()
    source_verifier_step_id = str(
        persisted.get("source_verifier_step_id") or ""
    ).strip()
    source_request_id = str(persisted.get("source_request_id") or "").strip()
    source_tool_call_id = str(
        persisted.get("source_tool_call_id") or ""
    ).strip()
    candidates: list[Mapping[str, Any]] = []
    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if not (
            str(event.get("event") or event.get("event_type") or "").strip()
            == "agent.tool.call"
            and str(event.get("source") or "").strip()
            == "runtime_native_postcondition_receipt"
            and str(event.get("actor") or "").strip() == "native_runtime"
            and str(event.get("execution_authority") or "").strip()
            == "runtime_tool_executor"
            and str(event.get("visibility") or "").strip() == "internal"
            and str(event.get("execution_mode") or "").strip()
            in {
                "trusted_observation_receipt_projection",
                "native_postcondition_receipt_projection",
            }
            and str(event.get("run_id") or "").strip() == clean_run_id
            and str(event.get("plan_id") or "").strip()
            == exact_request_fields["plan_id"]
            and str(event.get("step_id") or "").strip()
            == source_verifier_step_id
            and str(event.get("request_id") or "").strip()
            == source_verifier_request_id
            and str(event.get("tool_call_id") or "").strip()
            == source_verifier_tool_call_id
            and str(event.get("source_step_id") or "").strip()
            == source_step_id
            and str(event.get("source_request_id") or "").strip()
            == source_request_id
            and str(event.get("source_tool_call_id") or "").strip()
            == source_tool_call_id
            and result.get("ok") is True
            and result.get("postcondition_verified") is True
            and result.get("verification_satisfied_by_native_receipt") is True
            and str(result.get("content_sha256") or "").strip()
            == content_sha256
            and dict(result.get("target_window") or {}) == dict(target_window)
            and dict(result.get("target_ui_identity") or {})
            == dict(target_ui_identity)
            and str(result.get("provider_kind") or "").strip()
            == str(persisted.get("provider_kind") or "").strip()
            and str(result.get("provider_id") or "").strip()
            == str(persisted.get("provider_id") or "").strip()
        ):
            continue
        candidates.append(event)
    if len(candidates) != 1:
        return {}
    criteria = tuple(getattr(goal_contract, "criteria", ()) or ())
    if not any(
        exact_request_fields["submit_step_id"]
        in tuple(getattr(item, "source_step_ids", ()) or ())
        for item in criteria
    ):
        return {}
    return {
        "_authority": _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY,
        **{
            key: value
            for key, value in dict(persisted).items()
            if key not in {"version", "receipt_kind"}
        },
    }


def _prepared_submit_revalidation_request(
    submit_request: Mapping[str, Any],
    prepared_context: Mapping[str, Any],
    *,
    phase: str,
) -> dict[str, Any]:
    clean_phase = "post" if str(phase or "").strip() == "post" else "pre"
    submit_call_id = str(submit_request.get("tool_call_id") or "").strip()
    submit_step_id = _runtime_request_step_id(submit_request)
    return {
        "tool": "desktop.ui_elements",
        "input": {},
        "source": "runtime_internal_submit_revalidation",
        "runtime_stage": "verify",
        "runtime_role": "verify_result",
        "decision_id": str(submit_request.get("decision_id") or "").strip(),
        "plan_id": str(submit_request.get("plan_id") or "").strip(),
        "tool_plan_id": str(submit_request.get("tool_plan_id") or "").strip(),
        "step_id": f"{submit_step_id}:{clean_phase}-submit-revalidation",
        "request_id": f"{submit_call_id}:{clean_phase}:request",
        "tool_call_id": f"{submit_call_id}:{clean_phase}:call",
        "source_step_id": str(prepared_context.get("source_step_id") or ""),
        "source_request_id": str(
            prepared_context.get("source_request_id") or ""
        ),
        "source_tool_call_id": str(
            prepared_context.get("source_tool_call_id") or ""
        ),
        "visibility": "internal",
    }


def _private_prepared_submit_revalidation_from_result(
    prepared_context: Mapping[str, Any],
    observation_request: Mapping[str, Any],
    observation_result: Mapping[str, Any],
    *,
    run_id: str,
    phase: str,
) -> dict[str, Any]:
    if (
        prepared_context.get("_authority")
        is not _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY
        or observation_result.get("ok") is not True
        or observation_result.get("approval_required")
        or observation_result.get("permission_error") is True
    ):
        return {}
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        observation_request,
        observation_result,
    )
    if (
        provider_kind != str(prepared_context.get("provider_kind") or "").strip()
        or provider_id != str(prepared_context.get("provider_id") or "").strip()
    ):
        return {}
    observed_window = _trusted_ui_window_identity(
        observation_result,
        expected_app_name=str(
            prepared_context.get("target_app_name") or ""
        ).strip(),
    )
    if not _same_trusted_ui_window_identity(
        prepared_context.get("target_window"),
        observed_window,
    ):
        return {}
    data = (
        observation_result.get("data")
        if isinstance(observation_result.get("data"), Mapping)
        else {}
    )
    _observed_app, elements = _trusted_ui_observation_elements(data)
    expected_identity = prepared_context.get("target_ui_identity")
    expected_text = prepared_context.get("content")
    matches = [
        element
        for element in elements
        if isinstance(expected_identity, Mapping)
        and _trusted_editable_ui_target_identity(element)
        == dict(expected_identity)
        and isinstance(expected_text, str)
        and element.get("value") == expected_text
    ]
    if len(matches) != 1:
        return {}
    content_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    if content_sha256 != str(
        prepared_context.get("content_sha256") or ""
    ).strip():
        return {}
    return {
        "_authority": _RUNTIME_PRIVATE_SUBMIT_REVALIDATION_AUTHORITY,
        "run_id": str(run_id or "").strip(),
        "phase": "post" if str(phase or "").strip() == "post" else "pre",
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "target_window": dict(observed_window),
        "target_ui_identity": dict(expected_identity),
        "content_sha256": content_sha256,
        "observation_step_id": _runtime_request_step_id(observation_request),
        "observation_request_id": str(
            observation_request.get("request_id") or ""
        ).strip(),
        "observation_tool_call_id": str(
            observation_request.get("tool_call_id") or ""
        ).strip(),
    }


def _private_prepared_submit_snapshot_revalidation(
    prepared_context: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    *,
    run_id: str,
    phase: str,
    provider_kind: str,
    provider_id: str,
) -> dict[str, Any]:
    """Validate a live snapshot taken inside the submit foreground lock."""

    clean_phase = "post" if str(phase or "").strip() == "post" else "pre"
    if prepared_context.get("_authority") is not (
        _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY
    ):
        return {}
    base = {
        "_authority": _RUNTIME_PRIVATE_SUBMIT_REVALIDATION_AUTHORITY,
        "run_id": str(run_id or "").strip(),
        "phase": clean_phase,
        "provider_kind": str(provider_kind or "").strip(),
        "provider_id": str(provider_id or "").strip(),
        "target_window": dict(prepared_context.get("target_window") or {}),
        "target_ui_identity": dict(
            prepared_context.get("target_ui_identity") or {}
        ),
        "content_sha256": str(
            prepared_context.get("content_sha256") or ""
        ).strip(),
        "observation_tool_call_id": (
            f"{str(prepared_context.get('submit_tool_call_id') or '').strip()}"
            f":{clean_phase}:atomic"
        ),
    }
    if snapshot.get("ok") is not True:
        return (
            {**base, "observation_status": "unobservable"}
            if clean_phase == "post"
            else {}
        )
    observed_window = _trusted_ui_window_identity(
        snapshot,
        expected_app_name=str(
            prepared_context.get("target_app_name") or ""
        ).strip(),
    )
    if not _same_trusted_ui_window_identity(
        prepared_context.get("target_window"),
        observed_window,
    ):
        return (
            {**base, "observation_status": "window_drifted"}
            if clean_phase == "post"
            else {}
        )
    data = snapshot.get("data") if isinstance(snapshot.get("data"), Mapping) else {}
    _observed_app, elements = _trusted_ui_observation_elements(data)
    expected_identity = dict(prepared_context.get("target_ui_identity") or {})
    expected_text = prepared_context.get("content")
    if not isinstance(expected_text, str) or not expected_text or not expected_identity:
        return {}
    expected_hash = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    if expected_hash != str(prepared_context.get("content_sha256") or "").strip():
        return {}
    exact_target_elements = [
        element
        for element in elements
        if _trusted_editable_ui_target_identity(element) == expected_identity
    ]
    exact_content_at_target = [
        element
        for element in exact_target_elements
        if element.get("value") == expected_text
    ]
    if clean_phase == "pre":
        if len(exact_target_elements) != 1 or len(exact_content_at_target) != 1:
            return {}
        return {
            **base,
            "target_window": dict(observed_window),
            "observation_status": "exact_prepared_target",
        }
    content_observed_elsewhere = any(
        element.get("value") == expected_text for element in elements
    )
    if len(exact_target_elements) == 1:
        target_value = exact_target_elements[0].get("value")
        post_status = (
            "exact_content_retained"
            if target_value == expected_text
            else "prepared_target_state_changed"
        )
    elif content_observed_elsewhere:
        post_status = "content_observed_after_submit"
    else:
        post_status = "prepared_target_unfocused_or_removed"
    return {
        **base,
        "target_window": dict(observed_window),
        "observation_status": post_status,
    }


def _exact_submit_terminal_event_matches(
    source_request: Mapping[str, Any],
    source_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
    run_id: str,
) -> bool:
    expected = {
        "run_id": str(run_id or "").strip(),
        "decision_id": str(source_request.get("decision_id") or "").strip(),
        "plan_id": str(source_request.get("plan_id") or "").strip(),
        "tool_plan_id": str(source_request.get("tool_plan_id") or "").strip(),
        "step_id": _runtime_request_step_id(source_request),
        "request_id": str(source_request.get("request_id") or "").strip(),
        "tool_call_id": str(source_request.get("tool_call_id") or "").strip(),
    }
    if any(not value for value in expected.values()):
        return False
    matches: list[Mapping[str, Any]] = []
    for event in timeline[max(0, int(tool_timeline_start or 0)) :]:
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if event.get("approval_resume_result_canonical") is True:
            continue
        if str(event.get("detail") or event.get("tool") or "").strip() != (
            "desktop.submit_foreground"
        ):
            continue
        if (
            str(event.get("actor") or "").strip() != "native_runtime"
            or str(event.get("execution_authority") or "").strip()
            != "runtime_tool_executor"
            or str(event.get("visibility") or "").strip() != "internal"
        ):
            continue
        if any(
            str(
                event.get(key)
                or (
                    event.get("planner_step_id")
                    if key == "step_id"
                    else ""
                )
                or ""
            ).strip()
            != value
            for key, value in expected.items()
        ):
            continue
        input_preview = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        source_input = (
            source_request.get("input")
            if isinstance(source_request.get("input"), Mapping)
            else {}
        )
        if str(input_preview.get("action") or "").strip().casefold() != str(
            source_input.get("action") or ""
        ).strip().casefold():
            continue
        event_result = (
            event.get("result") if isinstance(event.get("result"), Mapping) else {}
        )
        if dict(event_result) != dict(source_result):
            continue
        matches.append(event)
    return len(matches) == 1


def _declared_exact_submit_verifier(
    source_request: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]],
) -> dict[str, Any]:
    source_step_id = _runtime_request_step_id(source_request)
    source_request_id = str(source_request.get("request_id") or "").strip()
    source_tool_call_id = str(source_request.get("tool_call_id") or "").strip()
    source_identities = {
        key: str(source_request.get(key) or "").strip()
        for key in ("decision_id", "plan_id", "tool_plan_id")
    }
    if (
        not source_step_id
        or not source_request_id
        or not source_tool_call_id
        or any(not value for value in source_identities.values())
    ):
        return {}
    candidates: list[dict[str, Any]] = []
    for request in remaining_requests:
        if not isinstance(request, dict):
            continue
        verifier_tool = str(
            request.get("tool") or request.get("tool_name") or ""
        ).strip()
        verifier_step_id = _runtime_request_step_id(request)
        if (
            verifier_tool not in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
            or verifier_step_id in {"", source_step_id}
            or str(request.get("verification_predicate_kind") or "").strip()
            != _EXACT_SUBMIT_DISPATCH_PREDICATE
            or not (
                str(request.get("runtime_stage") or "").strip() == "verify"
                or str(request.get("runtime_role") or "").strip()
                == "verify_result"
            )
            or not _declared_verifier_targets_source_step(request, source_step_id)
            or str(request.get("source_tool") or "").strip()
            != "desktop.submit_foreground"
            or str(request.get("source_step_id") or "").strip()
            != source_step_id
            or str(request.get("source_request_id") or "").strip()
            != source_request_id
            or str(request.get("source_tool_call_id") or "").strip()
            != source_tool_call_id
            or any(
                str(request.get(key) or "").strip() != value
                for key, value in source_identities.items()
            )
        ):
            continue
        ensure_tool_call_id(request)
        verifier_request_id = str(request.get("request_id") or "").strip()
        verifier_tool_call_id = str(request.get("tool_call_id") or "").strip()
        if not verifier_request_id or not verifier_tool_call_id:
            continue
        candidates.append(request)
    return candidates[0] if len(candidates) == 1 else {}


def _has_exact_submit_verifier_intent(
    source_request: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]],
) -> bool:
    if str(source_request.get("tool") or "").strip() != "desktop.submit_foreground":
        return False
    source_step_id = _runtime_request_step_id(source_request)
    return any(
        isinstance(request, Mapping)
        and (
            str(request.get("verification_predicate_kind") or "").strip()
            == _EXACT_SUBMIT_DISPATCH_PREDICATE
            or str(request.get("source_tool") or "").strip()
            == "desktop.submit_foreground"
        )
        and (
            not source_step_id
            or _declared_verifier_targets_source_step(request, source_step_id)
            or str(request.get("source_step_id") or "").strip()
            in {"", source_step_id}
        )
        for request in remaining_requests
    )


def _private_exact_submit_dispatch_receipt_from_result(
    source_request: Mapping[str, Any],
    source_result: Mapping[str, Any],
    remaining_requests: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    prepared_context: Mapping[str, Any] | None = None,
    pre_revalidation: Mapping[str, Any] | None = None,
    post_revalidation: Mapping[str, Any] | None = None,
    *,
    tool_timeline_start: int,
    run_id: str,
) -> dict[str, Any]:
    if str(source_request.get("tool") or "").strip() != (
        "desktop.submit_foreground"
    ):
        return {}
    if (
        not isinstance(prepared_context, Mapping)
        or prepared_context.get("_authority")
        is not _RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY
        or not isinstance(pre_revalidation, Mapping)
        or pre_revalidation.get("_authority")
        is not _RUNTIME_PRIVATE_SUBMIT_REVALIDATION_AUTHORITY
        or str(pre_revalidation.get("phase") or "").strip() != "pre"
        or str(pre_revalidation.get("observation_status") or "").strip()
        != "exact_prepared_target"
        or not isinstance(post_revalidation, Mapping)
        or post_revalidation.get("_authority")
        is not _RUNTIME_PRIVATE_SUBMIT_REVALIDATION_AUTHORITY
        or str(post_revalidation.get("phase") or "").strip() != "post"
        or str(post_revalidation.get("observation_status") or "").strip()
        in {"", "window_drifted"}
    ):
        return {}
    source_input = (
        source_request.get("input")
        if isinstance(source_request.get("input"), Mapping)
        else {}
    )
    action = str(source_input.get("action") or "").strip().casefold()
    data = (
        source_result.get("data")
        if isinstance(source_result.get("data"), Mapping)
        else {}
    )
    if (
        action not in _EXACT_SUBMIT_DISPATCH_ACTIONS
        or source_result.get("ok") is not True
        or source_result.get("approval_required")
        or source_result.get("permission_error") is True
        or str(source_result.get("action") or "").strip()
        != "desktop.submit_foreground"
        or str(data.get("submit_action") or "").strip().casefold() != action
        or str(data.get("key") or "").strip().casefold() != "return"
        or not isinstance(data.get("modifiers"), list)
        or bool(_string_list(data.get("modifiers")))
    ):
        return {}
    provider_kind, provider_id = _runtime_owned_exact_submit_provider_identity(
        source_result
    )
    if not provider_kind or not provider_id:
        return {}
    if any(
        str(context.get("provider_kind") or "").strip() != provider_kind
        or str(context.get("provider_id") or "").strip() != provider_id
        for context in (prepared_context, pre_revalidation, post_revalidation)
    ):
        return {}
    for identity_key in ("target_window", "target_ui_identity"):
        prepared_identity = prepared_context.get(identity_key)
        if not isinstance(prepared_identity, Mapping) or any(
            not isinstance(context.get(identity_key), Mapping)
            or dict(context[identity_key]) != dict(prepared_identity)
            for context in (pre_revalidation, post_revalidation)
        ):
            return {}
    content_sha256 = str(
        prepared_context.get("content_sha256") or ""
    ).strip()
    if (
        len(content_sha256) != 64
        or any(char not in "0123456789abcdef" for char in content_sha256)
        or str(pre_revalidation.get("content_sha256") or "").strip()
        != content_sha256
        or str(post_revalidation.get("content_sha256") or "").strip()
        != content_sha256
    ):
        return {}
    submit_identity = {
        "submit_step_id": _runtime_request_step_id(source_request),
        "submit_request_id": str(source_request.get("request_id") or "").strip(),
        "submit_tool_call_id": str(
            source_request.get("tool_call_id") or ""
        ).strip(),
    }
    if any(
        not value
        or str(prepared_context.get(key) or "").strip() != value
        for key, value in submit_identity.items()
    ):
        return {}
    if not _exact_submit_terminal_event_matches(
        source_request,
        source_result,
        timeline,
        tool_timeline_start=tool_timeline_start,
        run_id=run_id,
    ):
        return {}
    verifier = _declared_exact_submit_verifier(
        source_request,
        remaining_requests,
    )
    if not verifier:
        return {}
    return {
        "_authority": _RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_AUTHORITY,
        "run_id": str(run_id or "").strip(),
        "decision_id": str(source_request.get("decision_id") or "").strip(),
        "plan_id": str(source_request.get("plan_id") or "").strip(),
        "tool_plan_id": str(source_request.get("tool_plan_id") or "").strip(),
        "source_tool": "desktop.submit_foreground",
        "source_step_id": _runtime_request_step_id(source_request),
        "source_request_id": str(source_request.get("request_id") or "").strip(),
        "source_tool_call_id": str(
            source_request.get("tool_call_id") or ""
        ).strip(),
        "provider_kind": provider_kind,
        "provider_id": provider_id,
        "submitted_action": action,
        "prepared_source_step_id": str(
            prepared_context.get("source_step_id") or ""
        ).strip(),
        "prepared_source_request_id": str(
            prepared_context.get("source_request_id") or ""
        ).strip(),
        "prepared_source_tool_call_id": str(
            prepared_context.get("source_tool_call_id") or ""
        ).strip(),
        "target_app_name": str(
            prepared_context.get("target_app_name") or ""
        ).strip(),
        "target_window": dict(prepared_context["target_window"]),
        "target_ui_identity": dict(prepared_context["target_ui_identity"]),
        "content_sha256": content_sha256,
        "pre_revalidation_tool_call_id": str(
            pre_revalidation.get("observation_tool_call_id") or ""
        ).strip(),
        "post_revalidation_tool_call_id": str(
            post_revalidation.get("observation_tool_call_id") or ""
        ).strip(),
        "post_observation_status": str(
            post_revalidation.get("observation_status") or ""
        ).strip(),
        "verifier_step_id": _runtime_request_step_id(verifier),
        "verifier_request_id": str(verifier.get("request_id") or "").strip(),
        "verifier_tool_call_id": str(verifier.get("tool_call_id") or "").strip(),
    }


def _private_exact_submit_dispatch_receipt_for_verifier(
    verifier_request: Mapping[str, Any],
    receipts: Mapping[str, Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    receipt = receipts.get(source_tool_call_id)
    if (
        not source_tool_call_id
        or not isinstance(receipt, Mapping)
        or receipt.get("_authority") is not (
            _RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_AUTHORITY
        )
    ):
        return {}
    exact_fields = {
        "run_id": str(run_id or "").strip(),
        "decision_id": str(verifier_request.get("decision_id") or "").strip(),
        "plan_id": str(verifier_request.get("plan_id") or "").strip(),
        "tool_plan_id": str(verifier_request.get("tool_plan_id") or "").strip(),
        "source_step_id": str(
            verifier_request.get("source_step_id") or ""
        ).strip(),
        "source_request_id": str(
            verifier_request.get("source_request_id") or ""
        ).strip(),
        "source_tool_call_id": source_tool_call_id,
        "verifier_step_id": _runtime_request_step_id(verifier_request),
        "verifier_request_id": str(
            verifier_request.get("request_id") or ""
        ).strip(),
        "verifier_tool_call_id": str(
            verifier_request.get("tool_call_id") or ""
        ).strip(),
    }
    if (
        any(not value for value in exact_fields.values())
        or any(
            str(receipt.get(key) or "").strip() != value
            for key, value in exact_fields.items()
        )
        or str(verifier_request.get("source_tool") or "").strip()
        != "desktop.submit_foreground"
        or str(verifier_request.get("verification_predicate_kind") or "").strip()
        != _EXACT_SUBMIT_DISPATCH_PREDICATE
        or not (
            str(verifier_request.get("runtime_stage") or "").strip() == "verify"
            or str(verifier_request.get("runtime_role") or "").strip()
            == "verify_result"
        )
        or not _declared_verifier_targets_source_step(
            verifier_request,
            str(receipt.get("source_step_id") or "").strip(),
        )
    ):
        return {}
    required_prepared_fields = (
        "prepared_source_step_id",
        "prepared_source_request_id",
        "prepared_source_tool_call_id",
        "target_app_name",
        "content_sha256",
        "pre_revalidation_tool_call_id",
        "post_revalidation_tool_call_id",
        "post_observation_status",
    )
    if (
        any(not str(receipt.get(key) or "").strip() for key in required_prepared_fields)
        or not isinstance(receipt.get("target_window"), Mapping)
        or not isinstance(receipt.get("target_ui_identity"), Mapping)
    ):
        return {}
    route = desktop_execution_route_payload(verifier_request)
    route_kind = str(route.get("selected_provider_kind") or "").strip()
    route_id = str(route.get("selected_provider_id") or "").strip()
    if (route_kind or route_id) and (
        route_kind != str(receipt.get("provider_kind") or "").strip()
        or route_id != str(receipt.get("provider_id") or "").strip()
    ):
        return {}
    return {
        "source_tool": "desktop.submit_foreground",
        "source_step_id": str(receipt["source_step_id"]),
        "source_request_id": str(receipt["source_request_id"]),
        "source_tool_call_id": str(receipt["source_tool_call_id"]),
        "provider_kind": str(receipt["provider_kind"]),
        "provider_id": str(receipt["provider_id"]),
        "verification_predicate_kind": _EXACT_SUBMIT_DISPATCH_PREDICATE,
        "submitted_action": str(receipt["submitted_action"]),
        "verified_observed_state": "submitted",
        "prepared_source_step_id": str(receipt["prepared_source_step_id"]),
        "prepared_source_request_id": str(receipt["prepared_source_request_id"]),
        "prepared_source_tool_call_id": str(
            receipt["prepared_source_tool_call_id"]
        ),
        "target_app_name": str(receipt["target_app_name"]),
        "target_window": dict(receipt["target_window"]),
        "target_ui_identity": dict(receipt["target_ui_identity"]),
        "content_sha256": str(receipt["content_sha256"]),
        "pre_revalidation_tool_call_id": str(
            receipt["pre_revalidation_tool_call_id"]
        ),
        "post_revalidation_tool_call_id": str(
            receipt["post_revalidation_tool_call_id"]
        ),
        "post_observation_status": str(receipt["post_observation_status"]),
    }


def _outcome_coordinator_owns_background_window_recovery(
    tool_name: str,
    tool_result: Mapping[str, Any],
) -> bool:
    """Defer only the exact safe Cua owned-window failure to the Goal loop."""

    outcome = from_tool_result(
        tool_name,
        tool_result,
        capabilities=capability_ids_for_tool(tool_name),
    )
    return background_window_source(outcome) is not None


class RuntimeToolRequestRunner:
    """Runs model-requested tools while preserving pause and projection behavior."""

    def __init__(
        self,
        *,
        normalize_tool_name: Callable[[Any], str],
        input_preview: Callable[[Any], Any],
        run_budget: Callable[[str, list[dict[str, Any]]], Any],
        user_goal_from_messages: Callable[[list[dict[str, Any]]], str],
        goal_disallows_tool: Callable[[str, str], str],
        timeline_factory: Callable[..., dict[str, Any]],
        append_run_event: Callable[[str, str, dict[str, Any]], Any],
        tool_loop_projection: Any,
        pending_approval_builder: Any,
        call_agent_tool: Callable[..., dict[str, Any]],
        execution_lease_checker: Callable[[str], None] | None = None,
    ) -> None:
        self._normalize_tool_name = normalize_tool_name
        self._input_preview = input_preview
        self._run_budget = run_budget
        self._user_goal_from_messages = user_goal_from_messages
        self._goal_disallows_tool = goal_disallows_tool
        self._timeline = timeline_factory
        self._append_run_event = append_run_event
        self._tool_loop_projection = tool_loop_projection
        self._pending_approval_builder = pending_approval_builder
        self._call_agent_tool = call_agent_tool
        self._execution_lease_checker = execution_lease_checker

    def run(
        self,
        tool_requests: list[dict[str, Any]],
        allowed_tools: list[str],
        broker: Any,
        messages: list[dict[str, Any]],
        timeline: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        *,
        next_iteration: int,
        run_id: str = "",
        budget: Any = None,
    ) -> None:
        tool_timeline_start = len(timeline)
        budget = budget or self._run_budget(run_id, timeline)
        user_goal = self._user_goal_from_messages(messages)
        foreground_readiness_blocker: dict[str, Any] | None = None
        active_window_verification_target: dict[str, Any] | None = None
        private_clipboard_source_receipts: dict[str, dict[str, Any]] = {}
        private_clipboard_paste_bindings: dict[str, dict[str, Any]] = {}
        private_prepared_submit_contexts: dict[str, dict[str, Any]] = {}
        private_exact_submit_dispatch_receipts: dict[str, dict[str, Any]] = {}
        tool_sequence = 0
        _prepare_runtime_private_clipboard_source_requests(tool_requests)
        for tool_request in tool_requests:
            # Exact file readback authority is intentionally live-run only.
            # Preserve only the opaque identity attached by an approved
            # source continuation in this process; serialized/model values
            # are stripped before any verifier can execute.
            exact_file_readback_authority = tool_request.pop(
                _RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY,
                None,
            )
            if exact_file_readback_authority is (
                _RUNTIME_PRIVATE_EXACT_FILE_READBACK_AUTHORITY
            ):
                tool_request[_RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY] = (
                    exact_file_readback_authority
                )
            incoming_exact_submit_receipt = tool_request.pop(
                RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_REQUEST_KEY,
                None,
            )
            if (
                isinstance(incoming_exact_submit_receipt, Mapping)
                and incoming_exact_submit_receipt.get("_authority")
                is _RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_AUTHORITY
            ):
                source_tool_call_id = str(
                    incoming_exact_submit_receipt.get("source_tool_call_id") or ""
                ).strip()
                if source_tool_call_id in private_exact_submit_dispatch_receipts:
                    # Ambiguous duplicate capabilities fail closed.  The exact
                    # verifier below performs the full request/receipt binding.
                    private_exact_submit_dispatch_receipts.pop(
                        source_tool_call_id,
                        None,
                    )
                elif source_tool_call_id:
                    private_exact_submit_dispatch_receipts[source_tool_call_id] = (
                        dict(incoming_exact_submit_receipt)
                    )
            normalize_tool_request_input(tool_request)
        permission_preflight_block = _authoritative_permission_preflight_block(
            tool_requests,
            broker,
        )
        if permission_preflight_block is not None:
            blocked_index, blocked_result = permission_preflight_block
            blocked_request = tool_requests[blocked_index]
            if run_id:
                blocked_request["run_id"] = run_id
            else:
                blocked_request.pop("run_id", None)
            ensure_tool_call_id(blocked_request)
            blocked_tool = self._normalize_tool_name(blocked_request.get("tool"))
            blocked_input = (
                blocked_request.get("input")
                if isinstance(blocked_request.get("input"), dict)
                else {}
            )
            trace_payload = _authoritative_tool_trace_payload(
                blocked_request,
                run_id=run_id,
            )
            input_preview = _tool_event_input_preview(
                blocked_tool,
                _input_preview_with_trace_payload(
                    self._input_preview(blocked_input),
                    trace_payload,
                ),
            )
            budget.claim_tool_call(blocked_tool)
            timeline.append(
                self._timeline(
                    "agent.tool.skipped",
                    blocked_tool,
                    input_preview=input_preview,
                    result=blocked_result,
                    status="blocked",
                    **trace_payload,
                )
            )
            if run_id:
                self._append_run_event(
                    run_id,
                    "agent.tool.skipped",
                    {
                        "tool": blocked_tool,
                        "input_preview": input_preview,
                        "result": blocked_result,
                        "status": "blocked",
                        **trace_payload,
                    },
                )
            self._append_tool_result_progress(
                blocked_request,
                tool_name=blocked_tool,
                tool_event_type="agent.tool.skipped",
                tool_result=blocked_result,
                timeline=timeline,
                tool_timeline_start=tool_timeline_start,
                run_id=run_id,
            )
            self._tool_loop_projection.append_tool_result_message(
                messages,
                {**blocked_request, "tool": blocked_tool},
                blocked_result,
            )
            # Permission readiness is a batch-level precondition.  Once an
            # affected composite is detected, no discovery-adjacent app open,
            # focus, input, or verifier from that batch may run and race the
            # user's desktop before authorization is restored.
            return
        for index, tool_request in enumerate(tool_requests):
            if self._execution_lease_checker is not None:
                self._execution_lease_checker(run_id)
            tool_sequence += 1
            # ``run_id`` is Runtime authority, never model/request authority.
            # Persist it on every terminal event so later input bindings can
            # correlate a source to this exact run without accepting a public
            # wrapper or model-authored replacement.
            if run_id:
                tool_request["run_id"] = run_id
            else:
                tool_request.pop("run_id", None)
            ensure_tool_call_id(tool_request)
            bound_input_fields: frozenset[str] = frozenset()
            if has_explicit_input_bindings(tool_request):
                try:
                    binding_resolution = resolve_tool_request_input_bindings(
                        tool_request,
                        timeline,
                        run_id=run_id,
                    )
                except InputBindingResolutionError as error:
                    tool_name = self._normalize_tool_name(tool_request.get("tool"))
                    trace_payload = _authoritative_tool_trace_payload(
                        tool_request,
                        run_id=run_id,
                    )
                    raw_input = (
                        tool_request.get("input")
                        if isinstance(tool_request.get("input"), dict)
                        else {}
                    )
                    input_preview = _tool_event_input_preview(
                        tool_name,
                        _input_preview_with_trace_payload(
                            self._input_preview(raw_input),
                            trace_payload,
                        ),
                    )
                    binding_failure = context_binding_unresolved_result(error)
                    tool_request.setdefault(
                        "replan_triggers",
                        ["context_binding_unresolved"],
                    )
                    trace_payload = _authoritative_tool_trace_payload(
                        tool_request,
                        run_id=run_id,
                    )
                    budget.claim_tool_call(tool_name)
                    timeline.append(
                        self._timeline(
                            "agent.tool.skipped",
                            tool_name,
                            input_preview=input_preview,
                            result=binding_failure,
                            status="blocked",
                            **trace_payload,
                        )
                    )
                    if run_id:
                        self._append_run_event(
                            run_id,
                            "agent.tool.skipped",
                            {
                                "tool": tool_name,
                                "input_preview": input_preview,
                                "result": binding_failure,
                                "status": "blocked",
                                **trace_payload,
                            },
                        )
                    self._append_tool_result_progress(
                        tool_request,
                        tool_name=tool_name,
                        tool_event_type="agent.tool.skipped",
                        tool_result=binding_failure,
                        timeline=timeline,
                        tool_timeline_start=tool_timeline_start,
                        run_id=run_id,
                    )
                    self._tool_loop_projection.append_tool_result_message(
                        messages,
                        {**tool_request, "tool": tool_name},
                        binding_failure,
                    )
                    # A dependent action must never run with a guessed or
                    # model-forged value.  The emitted failure enters the
                    # normal replan loop on the next model iteration.
                    break
                tool_request = {
                    **tool_request,
                    "input": dict(binding_resolution.input),
                }
                tool_requests[index] = tool_request
                bound_input_fields = binding_resolution.bound_top_level_fields
                if binding_resolution.receipts:
                    binding_receipts = [
                        receipt.to_payload()
                        for receipt in binding_resolution.receipts
                    ]
                    resolution_payload = {
                        "tool": self._normalize_tool_name(tool_request.get("tool")),
                        "tool_call_id": str(
                            tool_request.get("tool_call_id") or ""
                        ).strip(),
                        "run_id": run_id,
                        "plan_id": str(tool_request.get("plan_id") or "").strip(),
                        "step_id": str(
                            tool_request.get("step_id")
                            or tool_request.get("planner_step_id")
                            or ""
                        ).strip(),
                        "resolution_kind": "runtime_input_binding",
                        "input_binding_receipts": binding_receipts,
                    }
                    timeline.append(
                        self._timeline(
                            "agent.tool.input_resolved",
                            resolution_payload["tool"],
                            **resolution_payload,
                        )
                    )
                    if run_id:
                        self._append_run_event(
                            run_id,
                            "agent.tool.input_resolved",
                            resolution_payload,
                        )
            exact_submit_receipt = (
                _private_exact_submit_dispatch_receipt_for_verifier(
                    tool_request,
                    private_exact_submit_dispatch_receipts,
                    run_id=run_id,
                )
            )
            verification_receipt = exact_submit_receipt or (
                _native_postcondition_receipt_for_verifier(
                    tool_request,
                    timeline,
                    tool_timeline_start=tool_timeline_start,
                )
            )
            if verification_receipt:
                if exact_submit_receipt:
                    private_exact_submit_dispatch_receipts.pop(
                        str(
                            exact_submit_receipt.get("source_tool_call_id") or ""
                        ).strip(),
                        None,
                    )
                verification_tool = self._normalize_tool_name(tool_request.get("tool"))
                trace_payload = _authoritative_tool_trace_payload(
                    tool_request,
                    run_id=run_id,
                )
                verification_input = (
                    dict(tool_request.get("input"))
                    if isinstance(tool_request.get("input"), Mapping)
                    else {}
                )
                satisfied_result = {
                    "ok": True,
                    "action": verification_tool,
                    "postcondition_verified": True,
                    "verification_satisfied_by_native_receipt": True,
                    **verification_receipt,
                }
                payload = {
                    "tool": verification_tool,
                    "status": "satisfied",
                    **trace_payload,
                    "source": "runtime_native_postcondition_receipt",
                    "reason": "native_postcondition_receipt",
                    "result": satisfied_result,
                }
                timeline.append(
                    self._timeline(
                        "agent.post_action_verification.satisfied",
                        verification_tool,
                        **payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.post_action_verification.satisfied",
                        payload,
                    )
                projected_call = {
                    "tool": verification_tool,
                    "input_preview": verification_input,
                    "result": satisfied_result,
                    **trace_payload,
                    "source": "runtime_native_postcondition_receipt",
                    "execution_mode": "native_postcondition_receipt_projection",
                    "visibility": "internal",
                }
                timeline.append(
                    self._timeline(
                        "agent.tool.call",
                        verification_tool,
                        **projected_call,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.tool.call",
                        projected_call,
                    )
                self._append_tool_result_progress(
                    tool_request,
                    tool_name=verification_tool,
                    tool_event_type="agent.tool.call",
                    tool_result=satisfied_result,
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                self._tool_loop_projection.append_tool_result_message(
                    messages,
                    {**tool_request, "tool": verification_tool},
                    satisfied_result,
                )
                continue
            app_name_resolution = _tool_request_existing_app_name_resolution(tool_request)
            if "app_name" in bound_input_fields:
                app_name_resolution = {}
            elif not app_name_resolution:
                app_name_resolution = _tool_request_app_name_resolution(tool_request, timeline)
            tool_request = _tool_request_with_app_name_resolution(
                tool_request,
                app_name_resolution,
            )
            file_resolution = (
                {}
                if bound_input_fields
                & {"file_path", "path", "paths", "target_path"}
                else _tool_request_workspace_file_resolution(tool_request, timeline)
            )
            tool_request = _tool_request_with_workspace_file_resolution(
                tool_request,
                file_resolution,
            )
            artifact_body_resolution = (
                {}
                if "text" in bound_input_fields
                else _tool_request_artifact_body_resolution(
                    tool_request,
                    broker,
                    artifacts,
                )
            )
            tool_request = _tool_request_with_artifact_body_resolution(
                tool_request,
                artifact_body_resolution,
            )
            artifact_body_resolution = _public_artifact_body_resolution(
                artifact_body_resolution,
            )
            tool_name = self._normalize_tool_name(tool_request.get("tool"))
            tool_request = _tool_request_with_verification_target(
                tool_request,
                active_window_verification_target,
            )
            tool_request = _tool_request_with_foreground_app_context(
                tool_request,
                active_window_verification_target,
            )
            tool_request = _tool_request_with_preapproval_observed_ui_target(
                tool_request,
                tool_requests,
                timeline,
                run_id=run_id,
            )
            tool_request = _tool_request_with_open_path_app_input(tool_request, tool_name)
            tool_request = _tool_request_with_desktop_execution_route(tool_name, tool_request)
            # Keep the authoritative resolved request in the execution list so
            # later dependency checks bind to the exact app/file identity that
            # was actually operated on, rather than the planner placeholder.
            tool_requests[index] = tool_request
            raw_input = (
                tool_request.get("input")
                if isinstance(tool_request.get("input"), dict)
                else {}
            )
            trace_payload = _authoritative_tool_trace_payload(
                tool_request,
                run_id=run_id,
            )
            for resolution in (app_name_resolution, file_resolution, artifact_body_resolution):
                if not resolution:
                    continue
                resolution_payload = {
                    **resolution,
                    "tool": tool_name,
                    "tool_call_id": trace_payload["tool_call_id"],
                }
                timeline.append(
                    self._timeline(
                        "agent.tool.input_resolved",
                        tool_name,
                        **resolution_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.tool.input_resolved",
                        resolution_payload,
                    )
            input_preview = _input_preview_with_app_name_resolution(
                self._input_preview(raw_input),
                app_name_resolution,
            )
            input_preview = _input_preview_with_trace_payload(input_preview, trace_payload)
            input_preview = _tool_event_input_preview(tool_name, input_preview)
            runtime_skip = _desktop_execution_policy_skip_result(
                tool_name,
                tool_request,
                input_preview,
            )
            runtime_skip = runtime_skip or _unresolved_discovered_app_skip_result(
                tool_name,
                raw_input,
                app_name_resolution,
            )
            runtime_skip = runtime_skip or _unresolved_workspace_file_skip_result(
                tool_name,
                raw_input,
                file_resolution,
            )
            runtime_skip = runtime_skip or _broker_tool_precondition_failure(
                broker,
                tool_name,
            )
            provider_route_required = desktop_execution_route_requires_provider(
                desktop_execution_route_payload(tool_request)
            )
            if not provider_route_required and not _broker_requires_approval(broker, tool_name):
                runtime_skip = runtime_skip or _runtime_readiness_skip_result(
                    tool_name,
                    raw_input,
                    foreground_readiness_blocker,
                )
            if runtime_skip is not None:
                budget.claim_tool_call(tool_name)
                timeline.append(
                    self._timeline(
                        "agent.tool.skipped",
                        tool_name,
                        input_preview=input_preview,
                        result=runtime_skip,
                        **trace_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.tool.skipped",
                        {
                            "tool": tool_name,
                            "input_preview": input_preview,
                            "result": runtime_skip,
                            **trace_payload,
                        },
                    )
                replan_payload = self._append_tool_result_progress(
                    tool_request,
                    tool_name=tool_name,
                    tool_event_type="agent.tool.skipped",
                    tool_result=runtime_skip,
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                auto_recovery_enqueued = self._enqueue_runtime_replan_recovery_requests(
                    replan_payload,
                    source_tool_name=tool_name,
                    tool_requests=tool_requests,
                    insert_index=index + 1,
                    allowed_tools=allowed_tools,
                    remaining_requests=tool_requests[index + 1 :],
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                self._tool_loop_projection.append_tool_result_message(
                    messages,
                    {**tool_request, "tool": tool_name},
                    runtime_skip,
                )
                if (
                    _runtime_replan_payload_reports_recovery_failure(replan_payload)
                    and not auto_recovery_enqueued
                ):
                    break
                if _tool_result_requests_user_recovery(runtime_skip) and not auto_recovery_enqueued:
                    break
                continue
            goal_block_reason = self._goal_disallows_tool(user_goal, tool_name)
            if goal_block_reason:
                budget.claim_tool_call(tool_name)
                tool_result = {
                    "ok": False,
                    "blocked_by_user_goal": True,
                    "tool": tool_name,
                    "error": goal_block_reason,
                    "hint": (
                        "Do not ask for approval. Continue with an inline answer "
                        "that follows the user's stated constraint."
                    ),
                }
                timeline.append(
                    self._timeline(
                        "agent.tool.skipped",
                        tool_name,
                        input_preview=input_preview,
                        result=tool_result,
                        **trace_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.tool.skipped",
                        {
                            "tool": tool_name,
                            "input_preview": input_preview,
                            "result": tool_result,
                            **trace_payload,
                        },
                    )
                replan_payload = self._append_tool_result_progress(
                    tool_request,
                    tool_name=tool_name,
                    tool_event_type="agent.tool.skipped",
                    tool_result=tool_result,
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                auto_recovery_enqueued = self._enqueue_runtime_replan_recovery_requests(
                    replan_payload,
                    source_tool_name=tool_name,
                    tool_requests=tool_requests,
                    insert_index=index + 1,
                    allowed_tools=allowed_tools,
                    remaining_requests=tool_requests[index + 1 :],
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                self._tool_loop_projection.append_tool_result_message(
                    messages,
                    {**tool_request, "tool": tool_name},
                    tool_result,
                )
                if (
                    _runtime_replan_payload_reports_recovery_failure(replan_payload)
                    and not auto_recovery_enqueued
                ):
                    break
                continue
            private_clipboard_source = _private_clipboard_source_for_paste(
                tool_request,
                private_clipboard_source_receipts,
                run_id=run_id,
                tool_sequence=tool_sequence,
            )
            if (
                tool_name == "desktop.submit_foreground"
                and tool_request.get("requires_post_action_verification") is True
            ):
                _remaining_requests_include_post_action_verification(
                    tool_requests[index + 1 :],
                    source_tool_name=tool_name,
                    allowed_tools=allowed_tools,
                    source_step_id=str(
                        tool_request.get("step_id")
                        or tool_request.get("planner_step_id")
                        or ""
                    ).strip(),
                    source_request_id=str(
                        tool_request.get("request_id") or ""
                    ).strip(),
                    source_tool_call_id=str(
                        tool_request.get("tool_call_id") or ""
                    ).strip(),
                    source_plan_id=str(
                        tool_request.get("plan_id") or ""
                    ).strip(),
                    source_tool_plan_id=str(
                        tool_request.get("tool_plan_id") or ""
                    ).strip(),
                    verification_predicate_kind=(
                        _EXACT_SUBMIT_DISPATCH_PREDICATE
                    ),
                    bind_source_identity=True,
                )
            exact_submit_verifier = _declared_exact_submit_verifier(
                tool_request,
                tool_requests[index + 1 :],
            )
            exact_submit_verifier_intent = _has_exact_submit_verifier_intent(
                tool_request,
                tool_requests[index + 1 :],
            )
            prepared_submit_context: dict[str, Any] = {}
            pre_submit_revalidation: dict[str, Any] = {}
            post_submit_revalidation: dict[str, Any] = {}
            if exact_submit_verifier_intent:
                prepared_submit_context = (
                    _private_prepared_submit_context_for_request(
                        tool_request,
                        private_prepared_submit_contexts,
                        run_id=run_id,
                    )
                    if exact_submit_verifier
                    else {}
                )
                if not prepared_submit_context:
                    blocked_result = {
                        "ok": False,
                        "tool": tool_name,
                        "action": tool_name,
                        "status": "blocked",
                        "reason": "prepared_submit_target_revalidation_failed",
                        "error": "prepared_submit_target_revalidation_failed",
                        "summary": (
                            "Submit was not dispatched because the prepared app, "
                            "window, editable target, or exact content changed."
                        ),
                        "postcondition_verified": False,
                        "retryable": False,
                    }
                    timeline.append(
                        self._timeline(
                            "agent.tool.skipped",
                            tool_name,
                            input_preview=input_preview,
                            result=blocked_result,
                            status="blocked",
                            **trace_payload,
                        )
                    )
                    if run_id:
                        self._append_run_event(
                            run_id,
                            "agent.tool.skipped",
                            {
                                "tool": tool_name,
                                "input_preview": input_preview,
                                "result": blocked_result,
                                "status": "blocked",
                                **trace_payload,
                            },
                        )
                    self._append_tool_result_progress(
                        tool_request,
                        tool_name=tool_name,
                        tool_event_type="agent.tool.skipped",
                        tool_result=blocked_result,
                        timeline=timeline,
                        tool_timeline_start=tool_timeline_start,
                        run_id=run_id,
                    )
                    self._tool_loop_projection.append_tool_result_message(
                        messages,
                        {**tool_request, "tool": tool_name},
                        blocked_result,
                    )
                    break
                tool_request = {
                    **tool_request,
                    _RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY: (
                        prepared_submit_context
                    ),
                }
                tool_requests[index] = tool_request
            action_timeline_start = len(timeline)
            self._append_tool_start_progress(
                tool_request,
                tool_name=tool_name,
                timeline=timeline,
                run_id=run_id,
            )
            tool_result = self._call_agent_tool(
                tool_request,
                allowed_tools,
                broker,
                timeline,
                artifacts=artifacts,
                run_id=run_id,
                budget=budget,
            )
            private_exact_submit_result = tool_result.pop(
                _RUNTIME_PRIVATE_EXACT_SUBMIT_RESULT_KEY,
                None,
            )
            if exact_submit_verifier and prepared_submit_context:
                # The process-private capability is valid only for this Runner
                # frame.  A pending approval may persist lineage facts, never
                # the opaque authority object itself.  The approval-resume
                # coordinator can re-mint authority only after validating this
                # exact receipt against the canonical Run timeline.
                resumable_tool_request = dict(tool_request)
                resumable_tool_request.pop(
                    _RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY,
                    None,
                )
                if tool_result.get("approval_required"):
                    persisted_receipt = (
                        persisted_prepared_submit_receipt_from_private_context(
                            prepared_submit_context
                        )
                    )
                    if not persisted_receipt:
                        raise AgentRuntimeError(
                            "prepared_submit_receipt_persistence_failed"
                        )
                    resumable_tool_request[
                        RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY
                    ] = persisted_receipt
                tool_request = resumable_tool_request
                tool_requests[index] = resumable_tool_request
            tool_result = _tool_result_with_verification_failure_status(tool_result)
            if (
                exact_submit_verifier
                and prepared_submit_context
                and isinstance(private_exact_submit_result, Mapping)
            ):
                pre_submit_revalidation = dict(
                    private_exact_submit_result.get("pre_revalidation") or {}
                )
                post_submit_revalidation = dict(
                    private_exact_submit_result.get("post_revalidation") or {}
                )
            if exact_submit_verifier and prepared_submit_context:
                private_prepared_submit_contexts.pop(
                    str(prepared_submit_context.get("source_step_id") or "").strip(),
                    None,
                )
            if tool_name == "clipboard.read":
                source_receipt = _private_clipboard_source_receipt_from_result(
                    tool_request,
                    tool_result,
                    run_id=run_id,
                    tool_sequence=tool_sequence,
                )
                source_tool_call_id = str(
                    source_receipt.get("source_tool_call_id") or ""
                ).strip()
                if source_receipt and source_tool_call_id:
                    private_clipboard_source_receipts[source_tool_call_id] = (
                        source_receipt
                    )
            elif private_clipboard_source and not tool_result.get(
                "approval_required"
            ):
                paste_tool_call_id = str(
                    tool_request.get("tool_call_id") or ""
                ).strip()
                _consume_private_clipboard_source_receipt(
                    private_clipboard_source,
                    private_clipboard_source_receipts,
                    paste_tool_call_id=paste_tool_call_id,
                )
                paste_binding = _private_clipboard_paste_binding_from_action(
                    private_clipboard_source,
                    tool_request,
                    tool_result,
                    tool_requests[index + 1 :],
                    run_id=run_id,
                    tool_sequence=tool_sequence,
                )
                if paste_binding and paste_tool_call_id:
                    private_clipboard_paste_bindings[paste_tool_call_id] = (
                        paste_binding
                    )
            trusted_direct_observation = _tool_result_with_trusted_direct_observation(
                tool_name,
                tool_request,
                tool_result,
                run_id=run_id,
            )
            if trusted_direct_observation is not tool_result:
                tool_result = trusted_direct_observation
                _replace_latest_terminal_tool_result(
                    timeline,
                    tool_name=tool_name,
                    tool_call_id=str(tool_request.get("tool_call_id") or ""),
                    tool_result=tool_result,
                )
            if tool_result.get("ok") is True and not tool_result.get(
                "approval_required"
            ):
                _bind_exact_workspace_file_readback_verifier(
                    tool_request,
                    tool_requests[index + 1 :],
                    run_id=run_id,
                )
                _remaining_requests_include_post_action_verification(
                    tool_requests[index + 1 :],
                    source_tool_name=tool_name,
                    allowed_tools=allowed_tools,
                    source_step_id=str(
                        tool_request.get("step_id")
                        or tool_request.get("planner_step_id")
                        or ""
                    ).strip(),
                    source_request_id=str(
                        tool_request.get("request_id") or ""
                    ).strip(),
                    source_tool_call_id=str(
                        tool_request.get("tool_call_id") or ""
                    ).strip(),
                    source_plan_id=str(tool_request.get("plan_id") or "").strip(),
                    source_tool_plan_id=str(
                        tool_request.get("tool_plan_id") or ""
                    ).strip(),
                    verification_predicate_kind=(
                        _post_action_verification_predicate_kind(
                            tool_name,
                            tool_request,
                        )
                    ),
                    bind_source_identity=True,
                )
                exact_submit_receipt = (
                    _private_exact_submit_dispatch_receipt_from_result(
                        tool_request,
                        tool_result,
                        tool_requests[index + 1 :],
                        timeline,
                        prepared_submit_context,
                        pre_submit_revalidation,
                        post_submit_revalidation,
                        tool_timeline_start=action_timeline_start,
                        run_id=run_id,
                    )
                )
                source_tool_call_id = str(
                    exact_submit_receipt.get("source_tool_call_id") or ""
                ).strip()
                if exact_submit_receipt and source_tool_call_id:
                    private_exact_submit_dispatch_receipts[source_tool_call_id] = (
                        exact_submit_receipt
                    )
            private_clipboard_paste_binding = (
                _private_clipboard_paste_binding_for_verifier(
                    tool_request,
                    private_clipboard_paste_bindings,
                    run_id=run_id,
                    tool_sequence=tool_sequence,
                )
            )
            trusted_observation_receipt = (
                _trusted_postcondition_observation_receipt_for_verifier(
                    tool_request,
                    tool_result,
                    timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                    private_clipboard_paste_binding=(
                        private_clipboard_paste_binding
                    ),
                )
            )
            if trusted_observation_receipt:
                prepared_context = (
                    _private_prepared_submit_context_from_observation(
                        trusted_observation_receipt,
                        tool_request,
                        timeline,
                        run_id=run_id,
                        private_clipboard_paste_binding=(
                            private_clipboard_paste_binding
                        ),
                    )
                )
                prepared_source_step_id = str(
                    prepared_context.get("source_step_id") or ""
                ).strip()
                if prepared_context and prepared_source_step_id:
                    private_prepared_submit_contexts[prepared_source_step_id] = (
                        prepared_context
                    )
                if private_clipboard_paste_binding:
                    private_clipboard_paste_bindings.pop(
                        str(
                            private_clipboard_paste_binding.get(
                                "paste_tool_call_id"
                            )
                            or ""
                        ).strip(),
                        None,
                    )
                tool_result = _tool_result_with_trusted_observation_receipt(
                    tool_result,
                    trusted_observation_receipt,
                )
                self._append_trusted_observation_receipt_projection(
                    tool_request,
                    tool_name=tool_name,
                    tool_result=tool_result,
                    receipt=trusted_observation_receipt,
                    timeline=timeline,
                    run_id=run_id,
                )
            else:
                partial_clipboard_readback = (
                    _tool_result_with_unverified_semantic_clipboard_copy_readback(
                        tool_request,
                        tool_result,
                        timeline,
                        tool_timeline_start=tool_timeline_start,
                        run_id=run_id,
                    )
                )
                if partial_clipboard_readback is not tool_result:
                    tool_result = partial_clipboard_readback
                    _replace_latest_terminal_tool_result(
                        timeline,
                        tool_name=tool_name,
                        tool_call_id=str(tool_request.get("tool_call_id") or ""),
                        tool_result=tool_result,
                    )
                else:
                    unverified_paste_readback = (
                        _tool_result_with_unverified_semantic_clipboard_paste_readback(
                            tool_request,
                            tool_result,
                            timeline,
                            tool_timeline_start=tool_timeline_start,
                            run_id=run_id,
                        )
                    )
                    if unverified_paste_readback is not tool_result:
                        tool_result = unverified_paste_readback
                        _replace_latest_terminal_tool_result(
                            timeline,
                            tool_name=tool_name,
                            tool_call_id=str(tool_request.get("tool_call_id") or ""),
                            tool_result=tool_result,
                        )
            if tool_result.get("approval_required"):
                dependency_block = _approval_dependency_block_result(
                    tool_request,
                    tool_requests,
                    timeline,
                    run_id=run_id,
                )
                if dependency_block:
                    blocked_payload = {
                        "tool": tool_name,
                        "input_preview": input_preview,
                        "result": dependency_block,
                        "status": "blocked",
                        **trace_payload,
                    }
                    timeline.append(
                        self._timeline(
                            "agent.tool.skipped",
                            tool_name,
                            input_preview=input_preview,
                            result=dependency_block,
                            status="blocked",
                            **trace_payload,
                        )
                    )
                    if run_id:
                        self._append_run_event(
                            run_id,
                            "agent.tool.skipped",
                            blocked_payload,
                        )
                    self._append_tool_result_progress(
                        tool_request,
                        tool_name=tool_name,
                        tool_event_type="agent.tool.skipped",
                        tool_result=dependency_block,
                        timeline=timeline,
                        tool_timeline_start=tool_timeline_start,
                        run_id=run_id,
                    )
                    self._tool_loop_projection.append_tool_result_message(
                        messages,
                        {**tool_request, "tool": tool_name},
                        dependency_block,
                    )
                    raise AgentDirectOutcomeUnverified(
                        str(dependency_block.get("summary") or "approval dependency unverified"),
                        reason="approval_dependency_unverified",
                        tool_name=tool_name,
                        input_preview=dict(raw_input),
                        tool_call_id=str(trace_payload.get("tool_call_id") or ""),
                    )
                self._append_tool_result_progress(
                    tool_request,
                    tool_name=tool_name,
                    tool_event_type="agent.tool.call",
                    tool_result=tool_result,
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                pending_approval = self._pending_approval_builder.build(
                    tool_request,
                    messages=messages,
                    next_iteration=next_iteration,
                    remaining_tool_requests=tool_requests[index + 1 :],
                )
                for key in ("risk_level", "policy_reason", "plugin_id"):
                    value = str(tool_result.get(key) or "").strip()
                    if value:
                        pending_approval[key] = value
                raise AgentApprovalRequired(pending_approval)
            tool_result = _tool_result_with_runtime_recovery_defaults(
                tool_name,
                tool_request,
                raw_input,
                tool_result,
            )
            fatal_failure = self._tool_loop_projection.fatal_failure_detail(
                tool_name,
                tool_request,
                tool_result,
            )
            if fatal_failure:
                timeline.append(
                    self._timeline(
                        "agent.tool.failed",
                        tool_name,
                        input_preview=input_preview,
                        result=tool_result,
                        status="failed",
                        **trace_payload,
                    )
                )
                self._append_tool_result_progress(
                    tool_request,
                    tool_name=tool_name,
                    tool_event_type="agent.tool.failed",
                    tool_result=tool_result,
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
                raise AgentRuntimeError(fatal_failure)
            self._tool_loop_projection.append_tool_result_message(
                messages,
                tool_request,
                tool_result,
            )
            replan_payload = self._append_tool_result_progress(
                tool_request,
                tool_name=tool_name,
                tool_event_type="agent.tool.call",
                tool_result=tool_result,
                timeline=timeline,
                tool_timeline_start=tool_timeline_start,
                run_id=run_id,
            )
            previous_readiness_blocker = foreground_readiness_blocker
            next_readiness_blocker = _updated_foreground_readiness_blocker(
                foreground_readiness_blocker,
                tool_name,
                raw_input,
                tool_result,
            )
            if previous_readiness_blocker is not None and next_readiness_blocker is None:
                recovered_payload = _foreground_readiness_recovered_payload(
                    previous_readiness_blocker,
                    tool_name,
                    input_preview,
                    tool_result,
                )
                timeline.append(
                    self._timeline(
                        "agent.desktop.readiness_recovered",
                        tool_name,
                        **recovered_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.desktop.readiness_recovered",
                        recovered_payload,
                    )
            foreground_readiness_blocker = next_readiness_blocker
            coordinator_owns_background_window_recovery = (
                _outcome_coordinator_owns_background_window_recovery(
                    tool_name,
                    tool_result,
                )
            )
            auto_recovery_enqueued = False
            if not coordinator_owns_background_window_recovery:
                auto_recovery_enqueued = self._enqueue_runtime_replan_recovery_requests(
                    replan_payload,
                    source_tool_name=tool_name,
                    tool_requests=tool_requests,
                    insert_index=index + 1,
                    allowed_tools=allowed_tools,
                    remaining_requests=tool_requests[index + 1 :],
                    timeline=timeline,
                    tool_timeline_start=tool_timeline_start,
                    run_id=run_id,
                )
            if coordinator_owns_background_window_recovery:
                # This exact owned-background launch failure has a stronger,
                # Goal-bounded recovery in OutcomeLoopCoordinator.  Running
                # the generic replan fallback here would replay app.open and
                # advance dependent observations before the owned window has
                # been materialized and verified.
                break
            if (
                _runtime_replan_payload_reports_recovery_failure(replan_payload)
                and not auto_recovery_enqueued
            ):
                break
            if _tool_result_failed_verification(tool_result) and not auto_recovery_enqueued:
                break
            if _tool_result_requests_user_recovery(tool_result) and not auto_recovery_enqueued:
                if _remaining_request_can_handle_foreground_readiness(
                    foreground_readiness_blocker,
                    tool_requests[index + 1 :],
                ):
                    continue
                break
            deferred_continuation = _provider_session_deferred_continuation_requests(
                tool_name,
                tool_request,
                tool_result,
                remaining_requests=tool_requests[index + 1 :],
            )
            if not deferred_continuation:
                deferred_continuation = _runtime_replan_deferred_continuation_requests(
                    tool_name,
                    tool_request,
                    tool_result,
                    allowed_tools=allowed_tools,
                    remaining_requests=tool_requests[index + 1 :],
                )
            if deferred_continuation:
                tool_requests[index + 1 : index + 1] = deferred_continuation
                enqueued_payload = _deferred_continuation_enqueued_payload(
                    tool_name,
                    deferred_continuation,
                    retry_source=(
                        "desktop_provider_session"
                        if _is_desktop_provider_session_start_control(
                            tool_name,
                            tool_request,
                        )
                        else "runtime_replan_recovery"
                    ),
                )
                timeline.append(
                    self._timeline(
                        "agent.deferred_continuation.enqueued",
                        tool_name,
                        **enqueued_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.deferred_continuation.enqueued",
                        enqueued_payload,
                    )
            if tool_name == "desktop.active_window":
                active_window_verification_target = None
                next_active_window_target = None
            else:
                next_active_window_target = _active_window_target_from_tool_result(
                    tool_name,
                    raw_input,
                    tool_result,
                )
                if next_active_window_target is not None:
                    active_window_verification_target = next_active_window_target
            auto_verify_request = _post_action_verification_request(
                tool_name,
                tool_request,
                tool_result,
                allowed_tools=allowed_tools,
                remaining_requests=tool_requests[index + 1 :],
                active_window_target=next_active_window_target,
                timeline=timeline,
            )
            if auto_verify_request:
                if (
                    auto_verify_request.get("planner_declared_verifier") is True
                    and not _declared_post_action_verifier_already_planned(
                        auto_verify_request,
                        timeline,
                    )
                ):
                    planned_payload = (
                        _declared_post_action_verifier_planned_payload(
                            auto_verify_request
                        )
                    )
                    timeline.append(
                        self._timeline(
                            "agent.desktop.intent_planned",
                            str(auto_verify_request.get("tool") or "").strip(),
                            **planned_payload,
                        )
                    )
                    if run_id:
                        self._append_run_event(
                            run_id,
                            "agent.desktop.intent_planned",
                            planned_payload,
                        )
                tool_requests[index + 1 : index + 1] = [auto_verify_request]
                enqueued_payload = _post_action_verification_enqueued_payload(
                    tool_name,
                    auto_verify_request,
                )
                timeline.append(
                    self._timeline(
                        "agent.post_action_verification.enqueued",
                        tool_name,
                        **enqueued_payload,
                    )
                )
                if run_id:
                    self._append_run_event(
                        run_id,
                        "agent.post_action_verification.enqueued",
                        enqueued_payload,
                    )

    def _append_trusted_observation_receipt_projection(
        self,
        tool_request: Mapping[str, Any],
        *,
        tool_name: str,
        tool_result: Mapping[str, Any],
        receipt: Mapping[str, Any],
        timeline: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        trace_payload = _authoritative_tool_trace_payload(
            tool_request,
            run_id=run_id,
        )
        source_fields = {
            key: value
            for key, value in receipt.items()
            if key
            in {
                "source_tool",
                "source_step_id",
                "source_request_id",
                "source_tool_call_id",
                "provider_kind",
                "provider_id",
                "verified_observed_state",
            }
        }
        current_tool_call_id = str(trace_payload.get("tool_call_id") or "").strip()
        projected_result = {**dict(tool_result), **source_fields}
        for event in reversed(timeline):
            if str(event.get("event") or event.get("event_type") or "").strip() != (
                "agent.tool.call"
            ):
                continue
            if str(event.get("detail") or event.get("tool") or "").strip() != tool_name:
                continue
            if str(event.get("tool_call_id") or "").strip() != current_tool_call_id:
                continue
            # The executor has already produced this terminal event. Upgrade
            # its in-memory projection only after the independent observation
            # passed every lineage/provider predicate; the raw provider event
            # remains persisted and the receipt projection below is appended
            # as a separate auditable event.
            event["result"] = dict(projected_result)
            event["source"] = "runtime_native_postcondition_receipt"
            event["reason"] = "trusted_postcondition_observation"
            event.update(source_fields)
            break
        payload = {
            "tool": tool_name,
            "status": "satisfied",
            **trace_payload,
            "source": "runtime_native_postcondition_receipt",
            "reason": "trusted_postcondition_observation",
            "result": dict(projected_result),
        }
        timeline.append(
            self._timeline(
                "agent.post_action_verification.satisfied",
                tool_name,
                **payload,
            )
        )
        projected_call = {
            "tool": tool_name,
            "input_preview": (
                dict(tool_request.get("input"))
                if isinstance(tool_request.get("input"), Mapping)
                else {}
            ),
            "result": dict(projected_result),
            **trace_payload,
            "source": "runtime_native_postcondition_receipt",
            "reason": "trusted_postcondition_observation",
            "execution_mode": "trusted_observation_receipt_projection",
            "projection_of_tool_call_id": str(
                trace_payload.get("tool_call_id") or ""
            ),
            "visibility": "internal",
            **source_fields,
        }
        if (
            str(receipt.get("verification_predicate_kind") or "").strip()
            == EXACT_FILE_CONTENT_PRESENT_PREDICATE
            and current_tool_call_id
        ):
            # The broker's raw workspace.read event is already durable. Give
            # the Runtime receipt projection its own terminal identity so a
            # replay's first-winner rule cannot discard the later authority.
            projected_call["tool_call_id"] = (
                f"{current_tool_call_id}:exact-file-readback-receipt"
            )
        timeline.append(
            self._timeline(
                "agent.tool.call",
                tool_name,
                **projected_call,
            )
        )
        if run_id:
            self._append_run_event(
                run_id,
                "agent.post_action_verification.satisfied",
                payload,
            )
            self._append_run_event(
                run_id,
                "agent.tool.call",
                projected_call,
            )

    def _append_tool_result_progress(
        self,
        tool_request: dict[str, Any],
        *,
        tool_name: str,
        tool_event_type: str,
        tool_result: dict[str, Any],
        timeline: list[dict[str, Any]],
        tool_timeline_start: int,
        run_id: str,
    ) -> dict[str, Any]:
        traced_request = {**tool_request, "tool": tool_name}
        self._append_canonical_tool_outcome(
            traced_request,
            tool_name=tool_name,
            tool_result=tool_result,
            run_id=run_id,
        )
        tool_event = {
            "event": tool_event_type,
            "detail": tool_name,
            "result": tool_result,
            "run_id": str(run_id or "").strip(),
            "decision_id": str(tool_request.get("decision_id") or "").strip(),
            "plan_id": str(tool_request.get("plan_id") or "").strip(),
            "request_id": str(tool_request.get("request_id") or "").strip(),
            "step_id": str(
                tool_request.get("step_id")
                or tool_request.get("planner_step_id")
                or ""
            ).strip(),
            "tool_call_id": str(tool_request.get("tool_call_id") or "").strip(),
            "source": "native_runtime",
            "visibility": "internal",
        }
        append_task_progress_events_for_tool_result(
            tool_request=traced_request,
            tool_event=tool_event,
            timeline=timeline,
            timeline_factory=self._timeline,
            append_run_event=self._append_run_event,
            run_id=run_id,
        )
        return append_replan_request_event_for_tool_result(
            tool_request=traced_request,
            tool_event=tool_event,
            timeline=timeline,
            timeline_factory=self._timeline,
            append_run_event=self._append_run_event,
            runtime_tool_timeline_start=tool_timeline_start,
            run_id=run_id,
        )

    def _append_canonical_tool_outcome(
        self,
        tool_request: Mapping[str, Any],
        *,
        tool_name: str,
        tool_result: dict[str, Any],
        run_id: str,
    ) -> None:
        """Persist a private sidecar without changing raw tool/model events."""

        if not run_id or not supports_keyword(self._append_run_event, "visibility"):
            return
        trace_payload = _authoritative_tool_trace_payload(
            tool_request,
            run_id=run_id,
        )
        outcome = from_tool_result(
            tool_name,
            tool_result,
            capabilities=capability_ids_for_tool(tool_name),
        )
        payload = {
            **outcome.to_event_payload(),
            **trace_payload,
            **_canonical_outcome_recovery_contract(tool_result),
            "visibility": "internal",
        }
        self._append_run_event(
            run_id,
            "agent.tool.outcome",
            payload,
            visibility="internal",
        )

    def _enqueue_runtime_replan_recovery_requests(
        self,
        replan_payload: Mapping[str, Any] | None,
        *,
        source_tool_name: str,
        tool_requests: list[dict[str, Any]],
        insert_index: int,
        allowed_tools: list[str],
        remaining_requests: list[dict[str, Any]],
        timeline: list[dict[str, Any]],
        tool_timeline_start: int,
        run_id: str,
    ) -> bool:
        recovery_requests = _runtime_replan_auto_recovery_action_requests(
            replan_payload,
            allowed_tools=allowed_tools,
            remaining_requests=remaining_requests,
            timeline=timeline,
            tool_timeline_start=tool_timeline_start,
        )
        if not recovery_requests:
            return False
        for request in recovery_requests:
            ensure_recovery_action_identity(request)
        tool_requests[insert_index:insert_index] = recovery_requests
        enqueued_payload = _deferred_continuation_enqueued_payload(
            source_tool_name,
            recovery_requests,
            retry_source="runtime_replan_recovery",
            replan_payload=replan_payload,
        )
        timeline.append(
            self._timeline(
                "agent.deferred_continuation.enqueued",
                source_tool_name,
                **enqueued_payload,
            )
        )
        if run_id:
            self._append_run_event(
                run_id,
                "agent.deferred_continuation.enqueued",
                enqueued_payload,
            )
        return True

    def _append_tool_start_progress(
        self,
        tool_request: dict[str, Any],
        *,
        tool_name: str,
        timeline: list[dict[str, Any]],
        run_id: str,
    ) -> None:
        append_task_progress_events_for_tool_start(
            tool_request={**tool_request, "tool": tool_name},
            timeline=timeline,
            timeline_factory=self._timeline,
            append_run_event=self._append_run_event,
            run_id=run_id,
        )


def _provider_session_deferred_continuation_requests(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    remaining_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not _is_desktop_provider_session_start_control(tool_name, tool_request):
        return []
    if tool_result.get("ok") is not True:
        return []
    continuation = _mapping_list(tool_request.get("deferred_continuation"))
    if not continuation:
        return []
    session = _first_mapping(tool_result.get("desktop_provider_session"))
    existing_signatures = {
        _deferred_request_signature(request)
        for request in remaining_requests
        if isinstance(request, Mapping)
    }
    requests: list[dict[str, Any]] = []
    for item in continuation:
        request = dict(item)
        continuation_tool = str(request.get("tool") or request.get("tool_name") or "").strip()
        if not continuation_tool:
            continue
        request["tool"] = continuation_tool
        request.pop("tool_name", None)
        request.pop("desktop_execution_route", None)
        request.pop("sandbox_provider", None)
        request.pop("sandbox_desktop_provider", None)
        request.setdefault("source", "desktop_provider_session_deferred_continuation")
        request.setdefault(
            "planning_reason",
            "desktop_provider_session_deferred_continuation",
        )
        request.setdefault("runtime_retry_source", "desktop_provider_session")
        for key in (
            "replan_request_id",
            "replan_recovery_action_id",
            "action_id",
            "decision_id",
            "plan_id",
            "tool_plan_id",
            "capability_id",
            "target_capability_id",
            "runtime_stage",
            "runtime_role",
        ):
            value = tool_request.get(key)
            if key not in request and value not in (None, "", [], {}):
                request[key] = value
        if session:
            request["desktop_provider_session"] = dict(session)
        signature = _deferred_request_signature(request)
        if signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        requests.append(request)
    return requests


def _runtime_replan_auto_recovery_action_requests(
    replan_payload: Mapping[str, Any] | None,
    *,
    allowed_tools: list[str],
    remaining_requests: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    tool_timeline_start: int,
) -> list[dict[str, Any]]:
    if not isinstance(replan_payload, Mapping) or not replan_payload:
        return []
    metadata = (
        replan_payload.get("metadata")
        if isinstance(replan_payload.get("metadata"), Mapping)
        else {}
    )
    if metadata.get("replan_recovery_failed") is True:
        return []
    trigger = str(replan_payload.get("trigger") or "").strip()
    if trigger not in {"tool_failure", "tool_unavailable", "verification_failed"}:
        return []
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    if not allowed:
        return []
    existing_signatures = {
        _deferred_request_signature(request)
        for request in remaining_requests
        if isinstance(request, Mapping)
    }
    source_tool_name = str(replan_payload.get("source_tool_name") or "").strip()
    input_preview = (
        replan_payload.get("input_preview")
        if isinstance(replan_payload.get("input_preview"), Mapping)
        else {}
    )
    if source_tool_name:
        existing_signatures.add(
            _deferred_request_signature(
                {
                    "tool": source_tool_name,
                    "input": dict(input_preview),
                }
            )
        )
    requests: list[dict[str, Any]] = []
    for index, action in enumerate(
        _runtime_replan_payload_recovery_actions(replan_payload),
        start=1,
    ):
        request = _runtime_replan_action_request(
            action,
            replan_payload,
            allowed=allowed,
            action_index=index,
        )
        if not request:
            continue
        if recovery_request_repeats_stalled_discovery(
            request,
            timeline,
            tool_timeline_start=tool_timeline_start,
        ):
            continue
        ensure_recovery_action_identity(request)
        if _runtime_replan_request_already_succeeded(
            request,
            timeline,
            tool_timeline_start=tool_timeline_start,
        ):
            continuations = _runtime_replan_deferred_continuation_requests(
                str(request.get("tool") or "").strip(),
                request,
                {"ok": True},
                allowed_tools=list(allowed),
                remaining_requests=[*remaining_requests, *requests],
            )
            for continuation in continuations:
                signature = _deferred_request_signature(continuation)
                if signature in existing_signatures:
                    continue
                existing_signatures.add(signature)
                ensure_recovery_action_identity(continuation)
                requests.append(continuation)
            continue
        signature = _deferred_request_signature(request)
        if signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        requests.append(request)
    return _dedupe_runtime_replan_recovery_requests(requests)


def _runtime_replan_request_already_succeeded(
    request: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in _RUNTIME_REPLAN_NO_REPEAT_AFTER_SUCCESS_TOOLS:
        return False
    if (
        request.get("recovery_action_kind") == "desktop_target_reacquisition"
        and request.get("allow_repeat_after_success") is True
    ):
        return False
    request_input = (
        request.get("input") if isinstance(request.get("input"), Mapping) else {}
    )
    for event in timeline[max(0, int(tool_timeline_start or 0)) :]:
        if str(event.get("event") or "").strip() != "agent.tool.call":
            continue
        if str(event.get("detail") or "").strip() != tool_name:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if result.get("ok") is not True or result.get("approval_required"):
            continue
        event_input = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        if all(event_input.get(key) == value for key, value in request_input.items()):
            return True
    return False


def _runtime_replan_payload_reports_recovery_failure(
    replan_payload: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(replan_payload, Mapping):
        return False
    metadata = (
        replan_payload.get("metadata")
        if isinstance(replan_payload.get("metadata"), Mapping)
        else {}
    )
    return bool(metadata.get("replan_recovery_failed") is True)


def _runtime_replan_payload_recovery_actions(
    replan_payload: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    metadata = (
        replan_payload.get("metadata")
        if isinstance(replan_payload.get("metadata"), Mapping)
        else {}
    )
    actions = _mapping_list(metadata.get("recovery_actions"))
    if actions:
        return actions
    return _mapping_list(replan_payload.get("recovery_actions"))


def _runtime_replan_action_request(
    action: Mapping[str, Any],
    replan_payload: Mapping[str, Any],
    *,
    allowed: set[str],
    action_index: int,
) -> dict[str, Any]:
    tool_name = str(action.get("tool") or action.get("tool_name") or "").strip()
    if not tool_name or tool_name not in allowed:
        return {}
    if _runtime_replan_action_auto_start_blocked(action):
        return {}
    raw_input = action.get("input") if isinstance(action.get("input"), Mapping) else {}
    request: dict[str, Any] = {
        "protocol": "json_fallback",
        "tool": tool_name,
        "input": dict(raw_input),
        "source": "runtime_replan_recovery",
        "planning_reason": str(
            action.get("planning_reason") or "planner_replan_runtime_recovery_action"
        ).strip(),
        "recovery_action_tool": tool_name,
        "selected": True,
    }
    request_id = str(replan_payload.get("request_id") or "").strip()
    trigger = str(replan_payload.get("trigger") or "").strip()
    if request_id:
        request["replan_request_id"] = request_id
    if trigger:
        request["replan_trigger"] = trigger
    for key, payload_key in (
        ("step_id", "source_step_id"),
        ("planner_step_id", "source_step_id"),
        ("source_step_id", "source_step_id"),
        ("source_tool_name", "source_tool_name"),
        ("capability_id", "target_capability_id"),
        ("target_capability_id", "target_capability_id"),
    ):
        value = str(replan_payload.get(payload_key) or "").strip()
        if value:
            request[key] = value
    action_id = str(action.get("action_id") or action.get("id") or "").strip()
    if not action_id and request_id:
        action_id = f"{request_id}:action:{action_index}:{tool_name}"
    if action_id:
        request["action_id"] = action_id
        request["replan_recovery_action_id"] = action_id
    for key in (
        "label",
        "permission_target",
        "risk_level",
    ):
        value = str(action.get(key) or "").strip()
        if value:
            request["recovery_action_label" if key == "label" else key] = value
    if bool(action.get("approval_required")):
        request["approval_required"] = True
    recovery_action_kind = str(action.get("recovery_action_kind") or "").strip()
    if recovery_action_kind:
        request["recovery_action_kind"] = recovery_action_kind
    if action.get("allow_repeat_after_success") is True:
        request["allow_repeat_after_success"] = True
    action_metadata = (
        action.get("metadata") if isinstance(action.get("metadata"), Mapping) else {}
    )
    payload_metadata = (
        replan_payload.get("metadata")
        if isinstance(replan_payload.get("metadata"), Mapping)
        else {}
    )
    for key in (
        "decision_id",
        "plan_id",
        "core_id",
        "workspace_id",
        "task_id",
        "run_group_id",
        "group_run_id",
        "group_id",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
    ):
        value = str(replan_payload.get(key) or "").strip()
        if value:
            request[key] = value
    for key in ("runtime_doctrine", "runtime_stage", "runtime_role"):
        value = str(action_metadata.get(key) or payload_metadata.get(key) or "").strip()
        if value:
            request[key] = value
    for key in ("replan_triggers", "replan_signal_ids"):
        values = _string_list(action_metadata.get(key) or replan_payload.get(key))
        if key == "replan_triggers" and trigger and trigger not in values:
            values.append(trigger)
        if values:
            request[key] = values
    for key in (
        "action_target",
        "observation_evidence",
        "observation_retry",
        "desktop_execution_policy",
        "desktop_execution_route",
        "sandbox_provider",
        "desktop_provider_session",
        "desktop_loop",
    ):
        value = action.get(key)
        if not isinstance(value, Mapping):
            value = payload_metadata.get(key)
        if not isinstance(value, Mapping):
            value = replan_payload.get(key)
        if isinstance(value, Mapping) and value:
            request[key] = dict(value)
    verification_targets = _mapping_list(action.get("verification_targets"))
    if not verification_targets:
        verification_targets = _mapping_list(action.get("task_verification_targets"))
    if not verification_targets:
        verification_targets = _mapping_list(replan_payload.get("verification_targets"))
    if verification_targets:
        request["verification_targets"] = [dict(target) for target in verification_targets]
        request["task_verification_targets"] = [
            dict(target) for target in verification_targets
        ]
    raw_deferred_items = materialized_deferred_items(action)
    deferred_continuation = _runtime_replan_action_deferred_continuation_requests(
        action,
        replan_payload,
        allowed,
    )
    if raw_deferred_items and len(deferred_continuation) != len(raw_deferred_items):
        return {}
    if deferred_continuation:
        if action_id:
            for deferred_request in deferred_continuation:
                deferred_request.setdefault("action_id", action_id)
                deferred_request.setdefault("replan_recovery_action_id", action_id)
        request["deferred_continuation"] = deferred_continuation
    for key in ("deferred_tool", "deferred_input", "deferred_context"):
        value = action.get(key)
        if value not in (None, "", [], {}):
            request[key] = dict(value) if isinstance(value, Mapping) else value
    return request


def _runtime_replan_action_deferred_continuation_requests(
    action: Mapping[str, Any],
    replan_payload: Mapping[str, Any],
    allowed: set[str],
) -> list[dict[str, Any]]:
    continuation = materialized_deferred_items(action)
    if not continuation:
        return []
    request_id = str(replan_payload.get("request_id") or "").strip()
    trigger = str(replan_payload.get("trigger") or "").strip()
    action_id = str(action.get("action_id") or action.get("id") or "").strip()
    requests: list[dict[str, Any]] = []
    for item in continuation:
        request = safe_deferred_continuation_request(
            item,
            allowed,
            auto_safe_tools=_runtime_replan_deferred_auto_safe_tools(action),
            allow_approved_unsafe=False,
            approved_unsafe_tools=_RUNTIME_REPLAN_MANUAL_APPROVAL_DEFERRED_TOOLS,
        )
        if not request:
            continue
        if request_id:
            request.setdefault("replan_request_id", request_id)
        if trigger:
            request.setdefault("replan_trigger", trigger)
        if action_id:
            request.setdefault("action_id", action_id)
            request.setdefault("replan_recovery_action_id", action_id)
        request.setdefault("source", "runtime_replan_recovery")
        request.setdefault("planning_reason", "planner_replan_deferred_continuation")
        _copy_runtime_replan_context(request, replan_payload)
        requests.append(request)
    return _dedupe_runtime_replan_recovery_requests(requests)


def _runtime_replan_deferred_continuation_requests(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    allowed_tools: list[str],
    remaining_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if _is_desktop_provider_session_start_control(tool_name, tool_request):
        return []
    if tool_result.get("ok") is not True or tool_result.get("approval_required"):
        return []
    request_id = str(tool_request.get("replan_request_id") or "").strip()
    if not request_id:
        return []
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    if not allowed:
        return []
    existing_signatures = {
        _deferred_request_signature(request)
        for request in remaining_requests
        if isinstance(request, Mapping)
    }
    approved_deferred = _runtime_replan_request_approves_deferred_actions(tool_request)
    requests: list[dict[str, Any]] = []
    for item in materialized_deferred_items(tool_request):
        request = safe_deferred_continuation_request(
            item,
            allowed,
            auto_safe_tools=_runtime_replan_deferred_auto_safe_tools(tool_request),
            allow_approved_unsafe=approved_deferred,
            approved_unsafe_tools=_runtime_replan_approved_deferred_tools(item),
        )
        if not request:
            continue
        request.setdefault("replan_request_id", request_id)
        for key in (
            "replan_trigger",
            "action_id",
            "replan_recovery_action_id",
            "source_step_id",
            "source_tool_name",
            "target_capability_id",
            "capability_id",
            "decision_id",
            "plan_id",
            "core_id",
            "workspace_id",
            "task_id",
            "run_group_id",
            "group_run_id",
            "group_id",
            "workflow_id",
            "workflow_run_id",
            "workflow_node_id",
            "workflow_node_label",
            "desktop_execution_policy",
            "desktop_loop",
            "desktop_provider_session",
            "sandbox_desktop_provider",
            "sandbox_provider",
        ):
            value = tool_request.get(key)
            if key not in request and value not in (None, "", [], {}):
                request[key] = dict(value) if isinstance(value, Mapping) else value
        request.setdefault(
            "source",
            str(tool_request.get("source") or "runtime_replan_recovery").strip(),
        )
        request.setdefault("planning_reason", "planner_replan_deferred_continuation")
        signature = _deferred_request_signature(request)
        if signature in existing_signatures:
            continue
        existing_signatures.add(signature)
        requests.append(request)
    return _dedupe_runtime_replan_recovery_requests(requests)


_RUNTIME_REPLAN_MANUAL_APPROVAL_DEFERRED_TOOLS = {
    "app.focus_and_click_ui_element",
    "app.focus_and_type_into_ui_element",
    "app.open_and_click_ui_element",
    "app.open_and_type_into_ui_element",
    "desktop.click",
    "desktop.click_ui_element",
    "desktop.safe_click",
    "desktop.safe_type_text",
    "desktop.type",
    "desktop.type_into_ui_element",
    "desktop.type_text",
}

_RUNTIME_REPLAN_APPROVED_VERIFICATION_OBSERVATION_TOOLS = {
    "browser.screenshot",
    "desktop.read_ui",
    "desktop.ui_elements",
    "screen.capture",
}


def _runtime_replan_approved_deferred_tools(
    item: Mapping[str, Any],
) -> set[str]:
    tools = set(_RUNTIME_REPLAN_MANUAL_APPROVAL_DEFERRED_TOOLS)
    if (
        str(item.get("runtime_stage") or "").strip() == "verify"
        or str(item.get("runtime_role") or "").strip() == "verify_result"
    ):
        # A verifier already enclosed by an explicitly approved recovery chain
        # may observe its postcondition.  This does not make the same sensitive
        # observation eligible as a standalone automatic recovery action.
        tools.update(_RUNTIME_REPLAN_APPROVED_VERIFICATION_OBSERVATION_TOOLS)
    return tools

_RUNTIME_REPLAN_INTERNAL_PERMISSION_TARGETS = {
    "app_launch",
    "runtime_observation",
}


def _runtime_replan_request_approves_deferred_actions(
    tool_request: Mapping[str, Any],
) -> bool:
    source = str(tool_request.get("source") or "").strip()
    if source in {"agent_studio_replan_recovery", "yachiyo_chat_replan_recovery"}:
        return True
    return bool(tool_request.get("approved_by_replan_recovery_action"))


def _runtime_replan_action_auto_start_blocked(action: Mapping[str, Any]) -> bool:
    permission_target = str(action.get("permission_target") or "").strip().casefold()
    if (
        permission_target
        and permission_target not in _RUNTIME_REPLAN_INTERNAL_PERMISSION_TARGETS
    ):
        return True
    metadata = action.get("metadata") if isinstance(action.get("metadata"), Mapping) else {}
    explicit = metadata.get("runtime_replan_auto_start_eligible")
    if isinstance(explicit, bool):
        return not explicit
    explicit = metadata.get("auto_start_eligible")
    if isinstance(explicit, bool):
        return not explicit
    blockers = metadata.get("runtime_replan_auto_start_blockers")
    if isinstance(blockers, list) and blockers:
        return True
    blockers = metadata.get("auto_start_blockers")
    if isinstance(blockers, list) and blockers:
        return True
    return bool(
        _runtime_replan_recovery_action_auto_start_context(action).get("eligible")
        is not True
    )


def _copy_runtime_replan_context(
    request: dict[str, Any],
    replan_payload: Mapping[str, Any],
) -> None:
    for key in (
        "decision_id",
        "plan_id",
        "core_id",
        "workspace_id",
        "task_id",
        "run_group_id",
        "group_run_id",
        "group_id",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "source_step_id",
        "source_tool_name",
        "target_capability_id",
        "capability_id",
    ):
        value = replan_payload.get(key)
        if key not in request and value not in (None, "", [], {}):
            request[key] = dict(value) if isinstance(value, Mapping) else value
    for key in ("action_target", "observation_evidence", "observation_retry"):
        if isinstance(request.get(key), Mapping) and request.get(key):
            continue
        value = replan_payload.get(key)
        if isinstance(value, Mapping) and value:
            request[key] = dict(value)
    metadata = (
        replan_payload.get("metadata")
        if isinstance(replan_payload.get("metadata"), Mapping)
        else {}
    )
    for key in (
        "desktop_execution_policy",
        "desktop_loop",
        "desktop_provider_session",
        "sandbox_desktop_provider",
        "sandbox_provider",
    ):
        if isinstance(request.get(key), Mapping) and request.get(key):
            continue
        value = replan_payload.get(key)
        if not isinstance(value, Mapping):
            value = metadata.get(key)
        if isinstance(value, Mapping) and value:
            request[key] = dict(value)


def _post_action_verification_request(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    allowed_tools: list[str],
    remaining_requests: list[dict[str, Any]],
    active_window_target: Mapping[str, Any] | None,
    timeline: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if tool_result.get("ok") is not True or tool_result.get("approval_required"):
        return {}
    if bool(tool_request.get("requires_post_action_verification")) is not True:
        return {}
    if str(tool_request.get("runtime_stage") or "").strip() == "verify":
        return {}
    source_step_id = str(
        tool_request.get("step_id") or tool_request.get("planner_step_id") or ""
    ).strip()
    source_request_id = str(tool_request.get("request_id") or "").strip()
    source_tool_call_id = str(tool_request.get("tool_call_id") or "").strip()
    raw_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    semantic_clipboard_copy = bool(
        tool_name in {"desktop.safe_shortcut", "desktop.shortcut"}
        and str(raw_input.get("action") or "").strip().lower() == "copy"
    )
    semantic_clipboard_paste = bool(
        tool_name in _CLIPBOARD_PASTE_TOOLS
        and str(raw_input.get("action") or "").strip().lower() == "paste"
    )
    declared_exact_dispatch_verifier = (
        _trusted_declared_exact_dispatch_verifier(
            tool_name,
            tool_request,
            tool_result,
            allowed_tools=allowed_tools,
            timeline=timeline,
        )
    )
    verification_tool = (
        str(declared_exact_dispatch_verifier.get("tool") or "").strip()
        if declared_exact_dispatch_verifier
        else "clipboard.read"
        if semantic_clipboard_copy and "clipboard.read" in allowed_tools
        else next(
            (
                candidate
                for candidate in (
                    "desktop.ui_elements",
                    "desktop.read_ui",
                    "desktop.verify",
                )
                if semantic_clipboard_paste and candidate in allowed_tools
            ),
            "",
        )
        if semantic_clipboard_paste
        else _post_action_verification_tool(
            tool_name,
            allowed_tools=allowed_tools,
        )
    )
    if not verification_tool:
        verification_tool = _trusted_exact_dispatch_projection_verifier_tool(
            tool_name,
            tool_request,
            tool_result,
        )
    if not verification_tool:
        return {}
    if not semantic_clipboard_copy and _remaining_requests_include_post_action_verification(
        remaining_requests,
        source_tool_name=tool_name,
        allowed_tools=allowed_tools,
        source_step_id=source_step_id,
        source_request_id=source_request_id,
        source_tool_call_id=source_tool_call_id,
        source_plan_id=str(tool_request.get("plan_id") or "").strip(),
        source_tool_plan_id=str(tool_request.get("tool_plan_id") or "").strip(),
        verification_predicate_kind=_post_action_verification_predicate_kind(
            tool_name,
            tool_request,
        ),
        bind_source_identity=True,
    ):
        return {}
    app_name = _post_action_verification_app_name(
        tool_request,
        tool_result,
        active_window_target=active_window_target,
    )
    if _tool_can_change_active_app(tool_name) and not app_name:
        # App-scoped verification must bind to the app identity reported by
        # the successful action result. Falling back to a pre-resolution alias
        # or selection placeholder can re-run discovery against the wrong app.
        return {}
    if not semantic_clipboard_copy and _upgrade_declared_planned_verifier(
        remaining_requests,
        verification_tool=verification_tool,
        verification_input=_post_action_verification_input(
            verification_tool,
            app_name,
        ),
        source_tool_name=tool_name,
        source_tool_request=tool_request,
        app_name=app_name,
    ):
        return {}
    request: dict[str, Any] = {
        "tool": verification_tool,
        "input": _post_action_verification_input(verification_tool, app_name),
        "source_tool": tool_name,
        "source": "runtime_post_action_auto_verify",
        "planning_reason": "runtime_post_action_auto_verify",
        "runtime_stage": "verify",
        "runtime_role": "verify_result",
        "approval_required": False,
        "requires_observation": True,
        "requires_post_action_verification": False,
        "observation_retry": {
            "from_tool": verification_tool,
            "tool": verification_tool,
            "reason": "verification_failed",
        },
    }
    if source_step_id:
        declared_verifier_step_id = str(
            declared_exact_dispatch_verifier.get("step_id") or ""
        ).strip()
        request["step_id"] = (
            declared_verifier_step_id
            or f"{source_step_id}:runtime-verify"
        )
        request["planner_step_id"] = request["step_id"]
        request["source_step_id"] = source_step_id
        request["depends_on"] = [source_step_id]
        if declared_verifier_step_id:
            request["planner_declared_verifier"] = True
            request["planner_verifier_tool"] = verification_tool
            declared_capability_id = str(
                declared_exact_dispatch_verifier.get("capability_id") or ""
            ).strip()
            if declared_capability_id:
                request["capability_id"] = declared_capability_id
            source_planning_reason = str(
                tool_request.get("planning_reason") or ""
            ).strip()
            if source_planning_reason:
                request["planning_reason"] = source_planning_reason
    if source_request_id:
        request["source_request_id"] = source_request_id
        if request.get("step_id"):
            request["request_id"] = (
                f"{source_request_id}:verify:{request['step_id']}:"
                f"{verification_tool}"
            )
    if source_tool_call_id:
        request["source_tool_call_id"] = source_tool_call_id
    source_approval_id = str(tool_request.get("approval_id") or "").strip()
    if source_approval_id:
        request["source_approval_id"] = source_approval_id
    verification_target = _post_action_verification_target(
        tool_request,
        source_step_id=source_step_id,
    )
    if verification_target:
        request["verification_targets"] = [verification_target]
        request["task_verification_targets"] = [verification_target]
    if app_name:
        request["verification_target"] = {"app_name": app_name, "source_tool": tool_name}
    desktop_loop = _post_action_verification_desktop_loop(
        tool_request,
        verification_tool=verification_tool,
        app_name=app_name,
        source_step_id=source_step_id,
    )
    if desktop_loop:
        request["desktop_loop"] = desktop_loop
    _copy_post_action_verification_context(tool_request, request)
    return request


def _trusted_exact_dispatch_projection_verifier_tool(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
) -> str:
    """Return an internal verifier only for an executor-trusted exact dispatch."""

    raw_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else (
            tool_request.get("input_preview")
            if isinstance(tool_request.get("input_preview"), Mapping)
            else {}
        )
    )
    required_identity = (
        str(tool_request.get("run_id") or "").strip(),
        str(tool_request.get("plan_id") or "").strip(),
        str(
            tool_request.get("step_id")
            or tool_request.get("planner_step_id")
            or ""
        ).strip(),
        str(tool_request.get("request_id") or "").strip(),
        str(tool_request.get("tool_call_id") or "").strip(),
    )
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        tool_request,
        tool_result,
    )
    if (
        not has_exact_native_dispatch_contract(tool_name)
        or tool_result.get("native_dispatch_verified") is not True
        or tool_result.get("postcondition_verified") is not True
        or str(tool_result.get("verified_observed_state") or "").strip()
        != "fulfilled"
        or not all(required_identity)
        or not provider_kind
        or not provider_id
        or str(tool_result.get("verification_provider_kind") or "").strip()
        != provider_kind
        or str(tool_result.get("verification_provider_id") or "").strip()
        != provider_id
        or not exact_native_dispatch_receipt_matches(
            tool_name,
            raw_input,
            tool_result,
        )
    ):
        return ""
    # The Runner consumes this request through
    # ``_native_postcondition_receipt_for_verifier`` before policy/tool
    # execution.  It therefore creates an auditable verifier-linked projection
    # without treating the source dispatch event itself as Goal evidence.
    return "desktop.verify"


def _trusted_declared_exact_dispatch_verifier(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    allowed_tools: Iterable[str],
    timeline: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Bind a trusted app/path receipt to its unique declared verifier.

    Capability-discovery plans can execute their resolved app/path action in a
    second Runner batch, where the planner-declared verifier is no longer in
    ``remaining_requests``.  Reconstructing a verifier name from convention
    would let an untrusted request mint completion authority, so this bridge
    accepts only the exact verifier declared by both the run's GoalContract
    and the matching Runtime plan step.
    """

    clean_tool = str(tool_name or "").strip()
    native_quit = bool(
        clean_tool == "app.quit"
        and intrinsic_native_postcondition_state(
            clean_tool,
            _first_mapping(tool_request.get("input")),
            tool_result,
        ) == "fulfilled"
    )
    if not native_quit and (
        clean_tool not in {"app.open_path_with_app", "desktop.open_path_with_app"}
        or not _trusted_exact_dispatch_projection_verifier_tool(
            clean_tool, tool_request, tool_result,
        )
    ):
        return {}

    run_id = str(tool_request.get("run_id") or "").strip()
    contract_id = str(tool_request.get("goal_contract_id") or "").strip()
    criterion_id = str(tool_request.get("goal_criterion_id") or "").strip()
    plan_id = str(tool_request.get("plan_id") or "").strip()
    decision_id = str(tool_request.get("decision_id") or "").strip()
    source_step_id = str(
        tool_request.get("step_id")
        or tool_request.get("planner_step_id")
        or ""
    ).strip()
    capability_id = str(tool_request.get("capability_id") or "").strip()
    if not all(
        (
            run_id,
            contract_id,
            criterion_id,
            plan_id,
            decision_id,
            source_step_id,
            capability_id,
        )
    ):
        return {}

    events = [event for event in timeline if isinstance(event, Mapping)]
    contract_candidates: list[Mapping[str, Any]] = []
    for raw_event in events:
        event_type, payload = _runtime_timeline_event_payload(raw_event)
        if event_type != "agent.goal.contract":
            continue
        contract = (
            payload.get("goal_contract")
            if isinstance(payload.get("goal_contract"), Mapping)
            else {}
        )
        if (
            str(payload.get("run_id") or "").strip() != run_id
            or str(contract.get("run_id") or "").strip() != run_id
            or str(payload.get("contract_id") or "").strip() != contract_id
            or str(contract.get("contract_id") or "").strip() != contract_id
            or str(contract.get("source") or "").strip() != "goal_contract"
        ):
            continue
        contract_candidates.append(contract)
    if len(contract_candidates) != 1:
        return {}

    criteria = _mapping_list(contract_candidates[0].get("criteria"))
    matching_criteria = [
        criterion
        for criterion in criteria
        if str(criterion.get("criterion_id") or "").strip() == criterion_id
    ]
    if len(matching_criteria) != 1:
        return {}
    criterion = matching_criteria[0]
    verifier_step_ids = tuple(
        dict.fromkeys(_string_list(criterion.get("verifier_step_ids")))
    )
    required_capabilities = _string_list(
        criterion.get("required_capabilities")
    )
    expected = _first_mapping(criterion.get("expected"))
    if (
        criterion.get("effectful") is not True
        or source_step_id not in _string_list(criterion.get("source_step_ids"))
        or len(verifier_step_ids) != 1
        or capability_id not in required_capabilities
        or str(expected.get("state") or "").strip() != "fulfilled"
    ):
        return {}

    if native_quit:
        target = _first_mapping(expected.get("target"))
        requested_app = str(_first_mapping(tool_request.get("input")).get("app_name") or "").strip()
        if target.get("action") != "quit_app" or target.get("app_name") != requested_app:
            return {}

    verifier_step_id = verifier_step_ids[0]
    allowed = {str(item or "").strip() for item in allowed_tools}
    plan_candidates: list[dict[str, Any]] = []
    for raw_event in events:
        event_type, payload = _runtime_timeline_event_payload(raw_event)
        flat_native_step = bool(
            native_quit and event_type == "agent.desktop.intent_planned"
            and payload.get("source") == "runtime_verification"
            and payload.get("runtime_stage") == "verify"
            and payload.get("runtime_role") == "verify_result"
        )
        if event_type != "agent.plan.step" and not flat_native_step:
            continue
        step = payload if flat_native_step else (
            payload.get("step") if isinstance(payload.get("step"), Mapping) else {}
        )
        verifier_tool = str(step.get("tool_name") or step.get("tool") or "").strip()
        execution_mode = (
            step.get("desktop_execution_mode") if flat_native_step
            else step.get("execution_mode")
            if isinstance(step.get("execution_mode"), Mapping)
            else {}
        )
        if not (
            (flat_native_step or str(payload.get("source") or "").strip() == "runtime_planner")
            and str(payload.get("plan_id") or "").strip() == plan_id
            and str(payload.get("decision_id") or "").strip() == decision_id
            and str(step.get("step_id") or "").strip() == verifier_step_id
            and source_step_id in _string_list(step.get("depends_on"))
            and step.get("approval_required") is False
            and verifier_tool in allowed
            and (
                verifier_tool in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
                or (native_quit and verifier_tool == "desktop.running_apps")
            )
            and str(execution_mode.get("mode") or "").strip()
            == "read_only_observation"
            and execution_mode.get("keyboard_mouse_capture") is False
        ):
            continue
        if flat_native_step:
            source_plans = []
            expected_target = _first_mapping(expected.get("target"))
            requested_app = str(_first_mapping(tool_request.get("input")).get("app_name") or "").strip()
            if not requested_app or expected_target.get("action") != "quit_app" or expected_target.get("app_name") != requested_app:
                continue
            for source_event in events:
                source_type, source_payload = _runtime_timeline_event_payload(source_event)
                source_input = _first_mapping(source_payload.get("input_preview"))
                if (
                    source_type == "agent.desktop.intent_planned"
                    and source_payload.get("source") == "runtime_planner"
                    and source_payload.get("tool") == "app.quit"
                    and source_payload.get("plan_id") == plan_id
                    and source_payload.get("decision_id") == decision_id
                    and source_payload.get("step_id") == source_step_id
                    and source_payload.get("capability_id") == capability_id
                    and source_input.get("app_name") == requested_app
                    and _first_mapping(source_payload.get("action_target")) == expected_target
                ):
                    source_plans.append(source_payload)
            if len(source_plans) != 1:
                continue
        # The quit adapter already made an independent same-app running
        # observation. Preserve the Goal's verifier identity even when an
        # approval-resume batch no longer carries its declared inventory step.
        observer_tool = (
            _post_action_verification_tool(clean_tool, allowed_tools=list(allowed))
            if native_quit and verifier_tool == "desktop.running_apps"
            else verifier_tool
        )
        if not observer_tool:
            continue
        plan_candidates.append(
            {
                "tool": observer_tool,
                "step_id": verifier_step_id,
                "capability_id": str(step.get("capability_id") or "").strip(),
                "execution_mode": dict(execution_mode),
            }
        )
    if len(plan_candidates) != 1:
        return {}
    return plan_candidates[0]


def _runtime_timeline_event_payload(
    event: Mapping[str, Any],
) -> tuple[str, Mapping[str, Any]]:
    payload = (
        event.get("payload")
        if isinstance(event.get("payload"), Mapping)
        else event
    )
    event_type = str(
        event.get("event")
        or event.get("event_type")
        or payload.get("event")
        or payload.get("event_type")
        or ""
    ).strip()
    return event_type, payload


_EXACT_PATH_DISPATCH_RECEIPT_TOOLS = frozenset(
    {
        "app.open_path_with_app",
        "desktop.open_path",
        "desktop.open_path_with_app",
        "desktop.reveal_path",
    }
)


def _native_postcondition_receipt_for_verifier(
    verifier_request: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
) -> dict[str, Any]:
    runtime_stage = str(verifier_request.get("runtime_stage") or "").strip()
    runtime_role = str(verifier_request.get("runtime_role") or "").strip()
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    if not verifier_tool or not (
        runtime_stage == "verify" or runtime_role == "verify_result"
    ):
        return {}
    target_step_ids = _postcondition_verifier_target_step_ids(verifier_request)
    if not target_step_ids:
        return {}
    verifier_plan_id = str(verifier_request.get("plan_id") or "").strip()
    event_groups: list[list[dict[str, Any]]] = [timeline[tool_timeline_start:]]
    if verifier_plan_id and tool_timeline_start > 0:
        # Direct-plan requests may be executed in more than one runner batch.
        # In that case the operate receipt precedes this batch, but the stable
        # plan id and step dependency still bind it to this verifier.
        event_groups.append(timeline[:tool_timeline_start])
    for events in event_groups:
        for event in reversed(events):
            if not isinstance(event, Mapping):
                continue
            if (
                str(event.get("event") or event.get("event_type") or "").strip()
                != "agent.tool.call"
            ):
                continue
            if not _postcondition_receipt_execution_scope_matches(
                verifier_request,
                event,
            ):
                continue
            event_input = (
                event.get("input_preview")
                if isinstance(event.get("input_preview"), Mapping)
                else {}
            )
            event_step_id = str(
                event.get("step_id")
                or event.get("planner_step_id")
                or event_input.get("step_id")
                or event_input.get("planner_step_id")
                or ""
            ).strip()
            if event_step_id not in target_step_ids:
                continue
            result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
            action_tool = str(event.get("detail") or event.get("tool") or "").strip()
            if action_tool == "desktop.submit_foreground":
                # A generic mutation acknowledgement cannot prove that Return
                # was dispatched to the exact prepared app/window/editable
                # target.  Submit completion is accepted only through the
                # process-private exact dispatch receipt path above.
                return {}
            if not _trusted_native_postcondition_verifier_for_action(
                action_tool,
                verifier_request,
            ):
                return {}
            if not _native_action_receipt_verifies_postcondition(
                action_tool,
                result,
                action_input=event_input,
            ):
                return {}
            receipt = {
                "source_tool": action_tool,
                "source_step_id": event_step_id,
            }
            source_tool_call_id = str(
                event.get("tool_call_id")
                or event_input.get("tool_call_id")
                or ""
            ).strip()
            if source_tool_call_id and str(
                verifier_request.get("source_tool_call_id") or ""
            ).strip() == source_tool_call_id:
                receipt["source_tool_call_id"] = source_tool_call_id
            intrinsic_state = intrinsic_native_postcondition_state(
                action_tool,
                event_input,
                result,
            )
            if intrinsic_state:
                source_request_id = str(event.get("request_id") or "").strip()
                if (
                    not source_tool_call_id
                    or not source_request_id
                    or str(
                        verifier_request.get("source_request_id") or ""
                    ).strip()
                    != source_request_id
                ):
                    return {}
                # Direct-plan steps can cross runner batches before the
                # verifier has seen the executor-minted call id.  The exact
                # run/plan/step/request match above identifies the persisted
                # terminal action, so bind its authoritative call id here.
                receipt["source_tool_call_id"] = source_tool_call_id
                provider_kind, provider_id = (
                    _trusted_runtime_execution_provider_identity(event, result)
                )
                if not provider_kind or not provider_id:
                    return {}
                receipt.update(
                    {
                        "source_request_id": source_request_id,
                        "provider_kind": provider_kind,
                        "provider_id": provider_id,
                    }
                )
            predicate_kind = str(
                verifier_request.get("verification_predicate_kind") or ""
            ).strip()
            if predicate_kind:
                if predicate_kind != _post_action_verification_predicate_kind(
                    action_tool
                ):
                    return {}
                receipt["verification_predicate_kind"] = predicate_kind
            verified_state = intrinsic_state
            if (
                not verified_state
                and action_tool in _EXACT_PATH_DISPATCH_RECEIPT_TOOLS
                and _trusted_exact_dispatch_projection_verifier_tool(
                    action_tool,
                    event,
                    result,
                )
            ):
                source_request_id = str(event.get("request_id") or "").strip()
                if (
                    not source_tool_call_id
                    or not source_request_id
                    or str(
                        verifier_request.get("source_request_id") or ""
                    ).strip()
                    != source_request_id
                ):
                    return {}
                provider_kind, provider_id = (
                    _trusted_runtime_execution_provider_identity(event, result)
                )
                if not provider_kind or not provider_id:
                    return {}
                receipt.update(
                    {
                        "source_tool_call_id": source_tool_call_id,
                        "source_request_id": source_request_id,
                        "provider_kind": provider_kind,
                        "provider_id": provider_id,
                    }
                )
                verified_state = "fulfilled"
            if verified_state:
                receipt["verified_observed_state"] = verified_state
            elif action_tool in _EXACT_PATH_DISPATCH_RECEIPT_TOOLS:
                # A generic provider acknowledgement for an exact path/app
                # dispatch is never a postcondition receipt.  Returning even
                # lineage-only data would make the Runner mark the verifier
                # satisfied, so fail closed unless the exact trusted branch
                # above produced the expected state.
                return {}
            return receipt
    return {}


def _trusted_postcondition_observation_receipt_for_verifier(
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
    run_id: str,
    private_clipboard_paste_binding: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Bind a real read-only observation to one exact prior mutation.

    Unlike ``_native_postcondition_receipt_for_verifier``, this path never
    treats the mutation's acknowledgement as proof.  It runs after the
    verifier returned and accepts only Runtime-produced terminal events with
    matching run/plan/source-call and execution-provider identities.
    """

    runtime_stage = str(verifier_request.get("runtime_stage") or "").strip()
    runtime_role = str(verifier_request.get("runtime_role") or "").strip()
    if not (runtime_stage == "verify" or runtime_role == "verify_result"):
        return {}
    if (
        verifier_result.get("ok") is not True
        or verifier_result.get("approval_required")
        or verifier_result.get("permission_error") is True
        or verifier_result.get("verification_failed") is True
    ):
        return {}
    clean_run_id = str(run_id or verifier_request.get("run_id") or "").strip()
    verifier_run_id = str(verifier_request.get("run_id") or "").strip()
    plan_id = str(verifier_request.get("plan_id") or "").strip()
    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    source_step_ids = _postcondition_verifier_target_step_ids(verifier_request)
    if (
        not clean_run_id
        or (verifier_run_id and verifier_run_id != clean_run_id)
        or not plan_id
        or not source_tool_call_id
        or not source_step_ids
    ):
        return {}
    verifier_provider = _trusted_runtime_execution_provider_identity(
        verifier_request,
        verifier_result,
    )
    if not all(verifier_provider):
        return {}

    event_groups: list[list[dict[str, Any]]] = [timeline[tool_timeline_start:]]
    if tool_timeline_start > 0:
        event_groups.append(timeline[:tool_timeline_start])
    for events in event_groups:
        for event in reversed(events):
            if not isinstance(event, Mapping):
                continue
            if str(event.get("event") or event.get("event_type") or "").strip() != (
                "agent.tool.call"
            ):
                continue
            if str(event.get("tool_call_id") or "").strip() != source_tool_call_id:
                continue
            source_step_id = str(
                event.get("step_id") or event.get("planner_step_id") or ""
            ).strip()
            if source_step_id not in source_step_ids:
                return {}
            if str(event.get("run_id") or "").strip() != clean_run_id:
                return {}
            if str(event.get("plan_id") or "").strip() != plan_id:
                return {}
            if not _trusted_observation_optional_scope_matches(
                verifier_request,
                event,
                keys=("decision_id", "tool_plan_id"),
            ):
                return {}
            action_result = (
                event.get("result")
                if isinstance(event.get("result"), Mapping)
                else {}
            )
            if (
                action_result.get("ok") is not True
                or action_result.get("approval_required")
                or action_result.get("permission_error") is True
            ):
                return {}
            action_provider = _trusted_runtime_execution_provider_identity(
                event,
                action_result,
            )
            if action_provider != verifier_provider:
                return {}
            action_tool = str(
                event.get("detail") or event.get("tool") or ""
            ).strip()
            observed = _trusted_exact_workspace_file_readback_receipt(
                action_tool,
                event,
                verifier_request,
                verifier_result,
            )
            if not observed:
                observed = _trusted_app_running_observation_receipt(
                action_tool,
                event,
                verifier_request,
                verifier_result,
            )
            if not observed:
                observed = _trusted_local_focus_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                )
            if not observed:
                observed = _trusted_system_volume_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                )
            if not observed:
                observed = _trusted_app_search_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                )
            if not observed:
                observed = _trusted_exact_typed_content_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                )
            if not observed:
                observed = _trusted_exact_pasted_content_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                    private_clipboard_paste_binding,
                )
            if not observed:
                observed = _trusted_exact_clipboard_content_observation_receipt(
                    action_tool,
                    event,
                    verifier_request,
                    verifier_result,
                )
            if not observed:
                return {}
            decision_id = str(event.get("decision_id") or "").strip()
            source_request_id = str(event.get("request_id") or "").strip()
            return {
                "source_tool": action_tool,
                "source_step_id": source_step_id,
                "source_tool_call_id": source_tool_call_id,
                **(
                    {"source_request_id": source_request_id}
                    if source_request_id
                    else {}
                ),
                "run_id": clean_run_id,
                **({"decision_id": decision_id} if decision_id else {}),
                "plan_id": plan_id,
                "provider_kind": verifier_provider[0],
                "provider_id": verifier_provider[1],
                **observed,
            }
    return {}


def _trusted_exact_workspace_file_readback_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Attest exact bytes read from one producer's declared output path."""

    if (
        action_tool not in _EXACT_FILE_READBACK_SOURCE_TOOLS
        or verifier_request.get(_RUNTIME_PRIVATE_EXACT_FILE_READBACK_REQUEST_KEY)
        is not _RUNTIME_PRIVATE_EXACT_FILE_READBACK_AUTHORITY
        or str(
            verifier_request.get("tool")
            or verifier_request.get("tool_name")
            or ""
        ).strip()
        not in EXACT_FILE_READBACK_VERIFIER_TOOLS
        or str(
            verifier_request.get("verification_predicate_kind") or ""
        ).strip()
        != EXACT_FILE_CONTENT_PRESENT_PREDICATE
    ):
        return {}
    source_request_id = str(action_event.get("request_id") or "").strip()
    if (
        not source_request_id
        or str(verifier_request.get("source_request_id") or "").strip()
        != source_request_id
    ):
        return {}
    event_output_path = declared_workspace_output_path(
        _first_mapping(action_event.get("action_target"))
    )
    bound_output_path = normalized_workspace_relative_path(
        verifier_request.get("source_output_path")
    )
    output_path = event_output_path or bound_output_path
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    requested_path = normalized_workspace_relative_path(verifier_input.get("path"))
    observed_path = normalized_workspace_relative_path(verifier_result.get("path"))
    if (
        not bound_output_path
        or (event_output_path and event_output_path != bound_output_path)
        or not output_path
        or requested_path != output_path
        or observed_path != output_path
    ):
        return {}
    verifier_data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    if any(
        source.get(key) is True
        for source in (verifier_result, verifier_data)
        for key in (
            "truncated",
            "content_truncated",
            "output_truncated",
            "decoding_lossy",
        )
    ):
        return {}
    content = verifier_result.get("content")
    if not isinstance(content, str) or not content:
        return {}
    size_bytes = verifier_result.get("size_bytes")
    observed_content_bytes = verifier_result.get("content_bytes")
    if (
        verifier_result.get("truncated") is not False
        or verifier_result.get("decoding_lossy") is not False
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes < 0
        or isinstance(observed_content_bytes, bool)
        or not isinstance(observed_content_bytes, int)
        or observed_content_bytes < 0
    ):
        return {}
    content_bytes = content.encode("utf-8")
    if not (
        len(content_bytes) == observed_content_bytes == size_bytes
    ):
        return {}
    action_result = (
        action_event.get("result")
        if isinstance(action_event.get("result"), Mapping)
        else {}
    )
    action_data = (
        action_result.get("data")
        if isinstance(action_result.get("data"), Mapping)
        else {}
    )
    return_codes = [
        source.get(key)
        for source in (action_result, action_data)
        for key in ("returncode", "exit_code")
        if key in source
    ]
    if (
        not return_codes
        or any(
            isinstance(code, bool) or not isinstance(code, int) or code != 0
            for code in return_codes
        )
        or any(source.get("timed_out") is True for source in (action_result, action_data))
    ):
        return {}
    local_provider = (LOCAL_DESKTOP_PROVIDER_KIND, LOCAL_DESKTOP_PROVIDER_ID)
    if (
        _trusted_runtime_execution_provider_identity(action_event, action_result)
        != local_provider
        or _trusted_runtime_execution_provider_identity(
            verifier_request,
            verifier_result,
        )
        != local_provider
    ):
        return {}
    tool_plan_id = str(action_event.get("tool_plan_id") or "").strip()
    return {
        "verification_predicate_kind": EXACT_FILE_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "fulfilled",
        "observed_path": observed_path,
        "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
        "content_length": len(content_bytes),
        **({"tool_plan_id": tool_plan_id} if tool_plan_id else {}),
    }


def _trusted_app_running_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Prove an app-open postcondition from an exact app-status observation."""

    if action_tool not in _TRUSTED_APP_WINDOW_RECEIPT_TOOLS:
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    if (
        verifier_tool != "desktop.verify"
        or str(verifier_input.get("verification_goal") or "").strip()
        != "app_running"
    ):
        return {}
    predicate_kind = str(
        verifier_request.get("verification_predicate_kind") or ""
    ).strip()
    if predicate_kind and predicate_kind != APP_WINDOW_PRESENT_PREDICATE:
        return {}

    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    action_result = (
        action_event.get("result")
        if isinstance(action_event.get("result"), Mapping)
        else {}
    )
    action_data = (
        action_result.get("data")
        if isinstance(action_result.get("data"), Mapping)
        else {}
    )
    expected_apps = tuple(
        dict.fromkeys(
            str(source.get(key) or "").strip()
            for source in (action_data, action_input)
            for key in (
                "resolved_app_name",
                "app_name",
                "requested_app_name",
                "target_app_name",
            )
            if str(source.get(key) or "").strip()
        )
    )
    requested_app = str(verifier_input.get("app_name") or "").strip()
    if not expected_apps or not requested_app or not any(
        _app_lookups_same_identity(expected_app, requested_app)
        for expected_app in expected_apps
    ):
        return {}

    verifier_data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observation_sources = (verifier_result, verifier_data)
    if any(
        source.get(key) is False
        for source in observation_sources
        for key in ("running", "launch_verified")
    ) or not any(source.get("running") is True for source in observation_sources):
        return {}
    observed_app = str(
        verifier_data.get("resolved_app_name")
        or verifier_data.get("app_name")
        or verifier_result.get("resolved_app_name")
        or verifier_result.get("app_name")
        or ""
    ).strip()
    if (
        not observed_app
        or not _app_lookups_same_identity(requested_app, observed_app)
        or not any(
            _app_lookups_same_identity(expected_app, observed_app)
            for expected_app in expected_apps
        )
    ):
        return {}
    return {
        "verification_predicate_kind": APP_WINDOW_PRESENT_PREDICATE,
        "verified_observed_state": "open",
        "observed_app_name": observed_app,
    }




def _tool_result_with_trusted_observation_receipt(
    tool_result: Mapping[str, Any],
    receipt: Mapping[str, Any],
) -> dict[str, Any]:
    result = dict(tool_result)
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    receipt_payload = dict(receipt)
    result.update(
        {
            "postcondition_verified": True,
            "observation_verified": True,
            "verification_satisfied_by_native_receipt": True,
            "verification_observed_by_tool": True,
            **receipt_payload,
        }
    )
    result["data"] = {
        **dict(data),
        "postcondition_verified": True,
        "observation_verified": True,
        **{
            key: value
            for key, value in receipt_payload.items()
            if key in {"verified_observed_state", "observed_app_name"}
        },
    }
    return result


def _tool_result_with_trusted_direct_observation(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any] | Mapping[str, Any]:
    """Promote a structured read only after validating its full state shape."""

    if (
        tool_name != "system.volume"
        or tool_result.get("ok") is not True
        or tool_result.get("permission_error") is True
        or tool_result.get("approval_required")
        or not str(run_id or "").strip()
        or not str(tool_request.get("plan_id") or "").strip()
    ):
        return tool_result
    raw_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    if str(raw_input.get("action") or "").strip().lower() != "status":
        return tool_result
    data = (
        tool_result.get("data")
        if isinstance(tool_result.get("data"), Mapping)
        else {}
    )
    if _system_volume_level(data) is None or not isinstance(data.get("muted"), bool):
        return tool_result
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        tool_request,
        tool_result,
    )
    if not provider_kind or not provider_id:
        return tool_result
    return _tool_result_with_trusted_observation_receipt(
        tool_result,
        {
            "run_id": str(run_id).strip(),
            "plan_id": str(tool_request.get("plan_id") or "").strip(),
            "provider_kind": provider_kind,
            "provider_id": provider_id,
            "verified_observed_state": "fulfilled",
            "observed_state_kind": "volume_state",
            "observed_volume_level": _system_volume_level(data),
            "observed_muted": bool(data["muted"]),
        },
    )


def _tool_result_with_trusted_exact_dispatch(
    tool_name: str,
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any] | Mapping[str, Any]:
    """Promote only a Runtime-bound exact dispatch schema to verified state."""

    if not has_exact_native_dispatch_contract(tool_name):
        return tool_result
    result = dict(tool_result)
    raw_data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    data = dict(raw_data)
    untrusted_verification_keys = {
        "native_dispatch_verified",
        "observation_verified",
        "postcondition_verified",
        "verification_observed_by_tool",
        "verification_passed",
        "verification_satisfied_by_native_receipt",
        "verified",
        "verified_observed_state",
    }
    for key in untrusted_verification_keys:
        result.pop(key, None)
        data.pop(key, None)
    result["data"] = data
    raw_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    required_identity = (
        str(run_id or "").strip(),
        str(tool_request.get("plan_id") or "").strip(),
        str(
            tool_request.get("step_id")
            or tool_request.get("planner_step_id")
            or ""
        ).strip(),
        str(tool_request.get("request_id") or "").strip(),
        str(tool_request.get("tool_call_id") or "").strip(),
    )
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        tool_request,
        result,
    )
    if (
        not all(required_identity)
        or not provider_kind
        or not provider_id
        or not exact_native_dispatch_receipt_matches(
            tool_name,
            raw_input,
            result,
        )
    ):
        return result
    if is_semantic_safe_key(tool_name):
        # Delivery is auditable, but does not prove changed focus/selection/UI.
        # Never mint a postcondition receipt from the key mutation itself.
        return {
            **result,
            "native_dispatch_verified": True,
            "verified_observed_state": "dispatched",
            "data": {
                **data,
                "native_dispatch_verified": True,
                "verified_observed_state": "dispatched",
            },
        }
    return {
        **result,
        "postcondition_verified": True,
        "native_dispatch_verified": True,
        "verified_observed_state": "fulfilled",
        "verification_provider_kind": provider_kind,
        "verification_provider_id": provider_id,
        "data": {
            **data,
            "postcondition_verified": True,
            "native_dispatch_verified": True,
            "verified_observed_state": "fulfilled",
        },
    }


def _replace_latest_terminal_tool_result(
    timeline: list[dict[str, Any]],
    *,
    tool_name: str,
    tool_call_id: str,
    tool_result: Mapping[str, Any],
) -> None:
    clean_call_id = str(tool_call_id or "").strip()
    if not clean_call_id:
        return
    for event in reversed(timeline):
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if str(event.get("detail") or event.get("tool") or "").strip() != tool_name:
            continue
        if str(event.get("tool_call_id") or "").strip() != clean_call_id:
            continue
        event["result"] = dict(tool_result)
        return


def _trusted_observation_optional_scope_matches(
    verifier_request: Mapping[str, Any],
    action_event: Mapping[str, Any],
    *,
    keys: Iterable[str],
) -> bool:
    for key in keys:
        verifier_value = str(verifier_request.get(key) or "").strip()
        action_value = str(action_event.get(key) or "").strip()
        if (verifier_value or action_value) and verifier_value != action_value:
            return False
    return True


def _trusted_runtime_execution_provider_identity(
    context: Mapping[str, Any],
    result: Mapping[str, Any],
) -> tuple[str, str]:
    routed_identity = _trusted_terminal_provider_identity(result)
    if all(routed_identity):
        return routed_identity
    provenance = result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
    if not isinstance(provenance, Mapping) or (
        provenance.get("source") != RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        or provenance.get("version") != RUNTIME_EXECUTION_PROVENANCE_VERSION
    ):
        return "", ""
    provider = _first_mapping(
        result.get("local_desktop_provider"),
        result.get("sandbox_provider"),
        context.get("sandbox_provider"),
        context.get("sandbox_desktop_provider"),
        context.get("desktop_sandbox_provider"),
    )
    route = _first_mapping(
        result.get("desktop_execution_route"),
        context.get("desktop_execution_route"),
    )
    provider_kind = str(provider.get("provider_kind") or "").strip()
    provider_id = str(provider.get("provider_id") or "").strip()
    route_kind = str(route.get("selected_provider_kind") or "").strip()
    route_id = str(route.get("selected_provider_id") or "").strip()
    if not provider_kind and route_kind not in {"", "none"}:
        provider_kind = route_kind
    if not provider_id and route_id:
        provider_id = route_id
    return (
        provider_kind or LOCAL_DESKTOP_PROVIDER_KIND,
        provider_id or LOCAL_DESKTOP_PROVIDER_ID,
    )


def _trusted_local_focus_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    if action_tool not in {"app.focus", "desktop.focus_app"}:
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    if verifier_tool not in {"desktop.active_window", "desktop.verify"}:
        return {}
    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    action_result = (
        action_event.get("result")
        if isinstance(action_event.get("result"), Mapping)
        else {}
    )
    action_data = (
        action_result.get("data")
        if isinstance(action_result.get("data"), Mapping)
        else {}
    )
    expected_app = str(
        action_input.get("app_name") or action_data.get("app_name") or ""
    ).strip()
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    requested_app = str(verifier_input.get("app_name") or "").strip()
    if (
        not expected_app
        or (requested_app and not _app_lookups_same_identity(expected_app, requested_app))
    ):
        return {}
    data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observed_app = str(
        data.get("app_name")
        or data.get("frontmost_app")
        or verifier_result.get("app_name")
        or ""
    ).strip()
    if not _app_lookups_same_identity(expected_app, observed_app):
        return {}
    if verifier_tool == "desktop.verify":
        focus_status = str(data.get("focus_status") or "").strip().lower()
        if not (
            data.get("focus_verified") is True
            or data.get("frontmost") is True
            or focus_status in {"focused", "frontmost"}
        ):
            return {}
    return {
        "verified_observed_state": "focused",
        "observed_app_name": observed_app,
    }


def _trusted_system_volume_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    if action_tool != "system.volume":
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    if verifier_tool != "system.volume" or str(
        verifier_input.get("action") or ""
    ).strip().lower() != "status":
        return {}
    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    requested_action = str(action_input.get("action") or "").strip().lower()
    if requested_action not in _SYSTEM_VOLUME_MUTATION_ACTIONS:
        return {}
    action_result = (
        action_event.get("result")
        if isinstance(action_event.get("result"), Mapping)
        else {}
    )
    action_data = (
        action_result.get("data")
        if isinstance(action_result.get("data"), Mapping)
        else {}
    )
    observed_data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observed_action = str(
        observed_data.get("requested_action")
        or observed_data.get("action")
        or ""
    ).strip().lower()
    if observed_action and observed_action != "status":
        return {}
    observed_level = _system_volume_level(observed_data)
    observed_muted = observed_data.get("muted")
    expected_level: int | None = None
    expected_muted: bool | None = None
    if requested_action == "set":
        expected_level = _system_volume_level(action_input, keys=("level", "volume"))
        expected_muted = False
    elif requested_action in {"up", "down"}:
        expected_level = _system_volume_level(action_data)
        if isinstance(action_data.get("muted"), bool):
            expected_muted = bool(action_data["muted"])
    elif requested_action == "mute":
        expected_muted = True
    elif requested_action == "unmute":
        expected_muted = False
    if expected_level is None and expected_muted is None:
        return {}
    if expected_level is not None and observed_level != expected_level:
        return {}
    if expected_muted is not None and observed_muted is not expected_muted:
        return {}
    if observed_level is None or not isinstance(observed_muted, bool):
        return {}
    return {
        "verified_observed_state": "fulfilled",
        "observed_state_kind": "volume_state",
        "requested_action": requested_action,
        "observed_volume_level": observed_level,
        "observed_muted": observed_muted,
    }


def _system_volume_level(
    source: Mapping[str, Any],
    *,
    keys: tuple[str, ...] = ("level", "volume"),
) -> int | None:
    for key in keys:
        value = source.get(key)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 <= float(value) <= 100
            and float(value).is_integer()
        ):
            return int(value)
    return None


def _trusted_app_search_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    """Observe the exact submitted query in a real app search result tree.

    The caller has already correlated the source call, run, plan and trusted
    execution provider. A Return acknowledgement or an editable query alone
    cannot prove search completion: require a separate results container with
    a matching noneditable result, in the exact requested app/window.
    """
    if action_tool != "desktop.search_submit":
        return {}
    verifier_tool = str(verifier_request.get("tool") or verifier_request.get("tool_name") or "")
    if verifier_tool not in {"desktop.ui_elements", "desktop.read_ui", "desktop.verify"}:
        return {}
    target = action_event.get("action_target")
    if not isinstance(target, Mapping) or (
        target.get("kind") != "desktop_app" or target.get("action") != "submit_ui"
    ):
        return {}
    expected_app = str(target.get("app_name") or "").strip()
    query = target.get("query")
    field_name = str(target.get("target") or "").strip().casefold()
    search_names = {
        "搜索", "查找", "检索", "搜索框", "search", "find", "search field", "search box",
    }
    if (
        not expected_app or not isinstance(query, str) or not query
        or field_name not in search_names
    ):
        return {}
    action_result = action_event.get("result")
    action_data = action_result.get("data") if isinstance(action_result, Mapping) else {}
    if not isinstance(action_data, Mapping) or (
        str(action_data.get("key") or "").casefold() not in {"return", "enter"}
        or action_data.get("modifiers") not in (None, [], ())
    ):
        return {}
    verifier_input = verifier_request.get("input")
    requested_app = (
        str(verifier_input.get("app_name") or "")
        if isinstance(verifier_input, Mapping) else ""
    )
    if not requested_app or not _app_lookups_same_identity(expected_app, requested_app):
        return {}
    data = verifier_result.get("data")
    if not isinstance(data, Mapping):
        return {}
    observed_app, elements = _trusted_ui_observation_elements(data)
    if not _app_lookups_same_identity(expected_app, observed_app):
        return {}
    observed_window = _trusted_ui_window_identity(verifier_result, expected_app_name=expected_app)
    if not observed_window:
        return {}
    source_window = action_event.get("target_window")
    if (
        isinstance(source_window, Mapping) and source_window
        and not _same_trusted_ui_window_identity(source_window, observed_window)
    ):
        return {}
    query_fields = [
        element for element in elements
        if _trusted_ui_element_is_editable(element)
        and element.get("value") == query
        and any(str(element.get(key) or "").strip().casefold() in search_names
                for key in ("name", "description", "identifier"))
    ]
    if len(query_fields) != 1:
        return {}
    result_names = {"搜索结果", "查询结果", "检索结果", "search results", "find results"}
    matches = []
    for index, element in enumerate(elements):
        role = str(element.get("role") or "").strip().casefold()
        if role not in {"axtable", "axoutline", "axlist", "axgroup"} or not any(
            str(element.get(key) or "").strip().casefold() in result_names
            for key in ("name", "description", "identifier")
        ):
            continue
        depth = element.get("depth")
        if not isinstance(depth, int) or isinstance(depth, bool) or depth < 0:
            continue
        for child in elements[index + 1:]:
            child_depth = child.get("depth")
            if not isinstance(child_depth, int) or isinstance(child_depth, bool):
                break
            if child_depth <= depth:
                break
            if (
                str(child.get("role") or "").strip().casefold()
                in {"axrow", "axcell", "axstatictext"}
                and not _trusted_ui_element_is_editable(child)
                and query in (child.get("value"), child.get("name"))
            ):
                matches.append(child)
    if not matches:
        return {}
    return {
        "verification_predicate_kind": "exact_app_search_result_present",
        "verified_observed_state": "sent",
        "observed_app_name": observed_app,
        "observed_query": query,
        "target_window": observed_window,
    }


def _trusted_exact_typed_content_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    if action_tool not in _EXACT_TYPED_CONTENT_OBSERVATION_TOOLS:
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    if verifier_tool not in {"desktop.ui_elements", "desktop.read_ui", "desktop.verify"}:
        return {}
    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    expected_text = action_input.get("text")
    if not isinstance(expected_text, str) or not expected_text:
        return {}
    expected_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    materialized_sha256 = str(
        action_event.get("materialized_content_sha256")
        or action_input.get("materialized_content_sha256")
        or ""
    ).strip()
    if materialized_sha256 and materialized_sha256 != expected_sha256:
        return {}
    action_result = (
        action_event.get("result")
        if isinstance(action_event.get("result"), Mapping)
        else {}
    )
    action_data = (
        action_result.get("data")
        if isinstance(action_result.get("data"), Mapping)
        else {}
    )
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    expected_app = str(
        action_input.get("app_name")
        or action_event.get("target_app_name")
        or action_data.get("app_name")
        or ""
    ).strip()
    requested_app = str(verifier_input.get("app_name") or "").strip()
    if not expected_app or (
        requested_app and not _app_lookups_same_identity(expected_app, requested_app)
    ):
        return {}
    verifier_data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observed_app, elements = _trusted_ui_observation_elements(verifier_data)
    if not _app_lookups_same_identity(expected_app, observed_app):
        return {}
    expected_target = str(action_input.get("target") or "").strip()
    matched_elements = [
        element
        for element in elements
        if isinstance(element.get("value"), str)
        and element.get("value") == expected_text
        and (
            not expected_target
            or _trusted_ui_element_matches_target(element, expected_target)
        )
    ]
    matched_element = matched_elements[0] if len(matched_elements) == 1 else None
    if matched_element is None:
        direct_text = verifier_data.get("value")
        if not (
            isinstance(direct_text, str)
            and direct_text == expected_text
            and not expected_target
        ):
            return {}
    observed_target = (
        _trusted_ui_element_identity(matched_element)
        if matched_element is not None
        else ""
    )
    target_context: dict[str, Any] = {}
    action_window = _trusted_ui_window_identity(
        action_result,
        expected_app_name=expected_app,
    )
    observed_window = _trusted_ui_window_identity(
        verifier_result,
        expected_app_name=expected_app,
    )
    target_ui_identity = _trusted_editable_ui_target_identity(matched_element)
    grounded_target_identities = [
        _trusted_editable_ui_target_identity(grounded_element)
        for source in _structured_result_sources(action_result)
        for grounded_element in [source.get("grounded_element")]
        if isinstance(grounded_element, Mapping)
        and _runtime_positive_int(grounded_element.get("pid"))
        == _runtime_positive_int(action_window.get("pid"))
        and _runtime_positive_int(grounded_element.get("window_id"))
        == _runtime_positive_int(action_window.get("window_id"))
    ]
    if (
        target_ui_identity
        and len(grounded_target_identities) == 1
        and grounded_target_identities[0] == target_ui_identity
        and _same_trusted_ui_window_identity(action_window, observed_window)
    ):
        target_context = {
            "target_app_name": expected_app,
            "target_window": dict(observed_window),
            "target_ui_identity": target_ui_identity,
        }
    return {
        "verification_predicate_kind": EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "fulfilled",
        "observed_app_name": observed_app,
        **({"observed_target": observed_target} if observed_target else {}),
        "content_sha256": expected_sha256,
        "content_length": len(expected_text),
        **target_context,
    }


def _trusted_exact_pasted_content_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    private_binding: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Prove that the exact private clipboard bytes reached one editable UI.

    The binding contains the clipboard text only in this process.  Public
    events and provider payloads can repeat its hashes or ids, but cannot
    manufacture the authority object required by this projector.
    """

    if (
        not isinstance(private_binding, Mapping)
        or private_binding.get("_authority")
        is not _RUNTIME_PRIVATE_CLIPBOARD_SOURCE_AUTHORITY
        or action_tool != str(private_binding.get("paste_tool") or "").strip()
        or action_tool not in _CLIPBOARD_PASTE_TOOLS
    ):
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    if verifier_tool not in {
        "desktop.inspect_app",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.verify",
    }:
        return {}
    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    if str(action_input.get("action") or "").strip().lower() != "paste":
        return {}
    exact_identity = {
        "run_id": str(private_binding.get("run_id") or "").strip(),
        "plan_id": str(private_binding.get("plan_id") or "").strip(),
        "step_id": str(private_binding.get("paste_step_id") or "").strip(),
        "request_id": str(private_binding.get("paste_request_id") or "").strip(),
        "tool_call_id": str(
            private_binding.get("paste_tool_call_id") or ""
        ).strip(),
    }
    if any(not value for value in exact_identity.values()):
        return {}
    for key, expected in exact_identity.items():
        actual = str(
            action_event.get(key)
            or (
                action_event.get("planner_step_id")
                if key == "step_id"
                else ""
            )
            or ""
        ).strip()
        if actual != expected:
            return {}
    if str(verifier_request.get("source_tool_call_id") or "").strip() != (
        exact_identity["tool_call_id"]
    ):
        return {}
    if str(verifier_request.get("source_step_id") or "").strip() != (
        exact_identity["step_id"]
    ):
        return {}
    if str(verifier_request.get("plan_id") or "").strip() != exact_identity[
        "plan_id"
    ]:
        return {}
    if not _runtime_request_receipt_optional_identity_matches(
        verifier_request,
        private_binding,
    ):
        return {}
    provider_kind, provider_id = _trusted_runtime_execution_provider_identity(
        action_event,
        (
            action_event.get("result")
            if isinstance(action_event.get("result"), Mapping)
            else {}
        ),
    )
    if (
        not provider_kind
        or not provider_id
        or provider_kind
        != str(private_binding.get("provider_kind") or "").strip()
        or provider_id != str(private_binding.get("provider_id") or "").strip()
    ):
        return {}
    content = private_binding.get("content")
    content_sha256 = str(private_binding.get("content_sha256") or "").strip()
    if not isinstance(content, str) or not content:
        return {}
    try:
        encoded = content.encode("utf-8")
    except UnicodeEncodeError:
        return {}
    if (
        hashlib.sha256(encoded).hexdigest() != content_sha256
        or private_binding.get("content_length") != len(content)
        or private_binding.get("content_byte_length") != len(encoded)
    ):
        return {}
    verifier_data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    nested_ui = (
        verifier_data.get("ui_elements")
        if isinstance(verifier_data.get("ui_elements"), Mapping)
        else {}
    )
    nested_data = (
        nested_ui.get("data")
        if isinstance(nested_ui.get("data"), Mapping)
        else {}
    )
    if any(
        source.get("truncated") is True
        for source in (verifier_result, verifier_data, nested_ui, nested_data)
    ):
        return {}
    observed_app, elements = _trusted_ui_observation_elements(verifier_data)
    target_app_name = str(private_binding.get("target_app_name") or "").strip()
    requested_app_name = _approval_dependency_request_app_name(verifier_request)
    observed_window = _trusted_ui_window_identity(
        verifier_result,
        expected_app_name=target_app_name,
    )
    target_window = private_binding.get("target_window")
    if (
        not target_app_name
        or not observed_app
        or not _app_lookups_same_identity(target_app_name, observed_app)
        or not _same_trusted_ui_window_identity(target_window, observed_window)
        or (
            requested_app_name
            and not _app_lookups_same_identity(
                target_app_name,
                requested_app_name,
            )
        )
    ):
        return {}
    expected_target = str(
        private_binding.get("target_ui_element") or ""
    ).strip()
    if not expected_target:
        return {}
    matches = [
        element
        for element in elements
        if element.get("value") == content
        and _trusted_ui_element_is_editable(element)
        and (
            not expected_target
            or _trusted_ui_element_matches_target(element, expected_target)
        )
    ]
    if len(matches) != 1:
        return {}
    target_ui_identity = _trusted_editable_ui_target_identity(matches[0])
    if not target_ui_identity:
        return {}
    observed_target = _trusted_ui_element_identity(matches[0])
    return {
        "verification_predicate_kind": EXACT_PASTED_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "fulfilled",
        "observed_app_name": observed_app,
        **({"observed_target": observed_target} if observed_target else {}),
        "target_app_name": target_app_name,
        "target_window": dict(observed_window),
        "target_ui_identity": target_ui_identity,
        "target_ui_readback_verified": True,
        "target_ui_editable_verified": True,
        "clipboard_source_verified": True,
        "content_sha256": content_sha256,
        "content_length": len(content),
        "content_byte_length": len(encoded),
        "paste_request_id": exact_identity["request_id"],
        "clipboard_source_run_id": str(
            private_binding.get("run_id") or ""
        ).strip(),
        "clipboard_source_plan_id": str(
            private_binding.get("plan_id") or ""
        ).strip(),
        "clipboard_source_step_id": str(
            private_binding.get("clipboard_source_step_id") or ""
        ).strip(),
        "clipboard_source_request_id": str(
            private_binding.get("clipboard_source_request_id") or ""
        ).strip(),
        "clipboard_source_tool_call_id": str(
            private_binding.get("clipboard_source_tool_call_id") or ""
        ).strip(),
        "clipboard_source_provider_kind": provider_kind,
        "clipboard_source_provider_id": provider_id,
    }


def _trusted_ui_element_is_editable(element: Mapping[str, Any]) -> bool:
    if element.get("enabled") is False or element.get("editable") is False:
        return False
    role = "".join(
        character
        for character in str(element.get("role") or "").strip().casefold()
        if character.isalnum()
    )
    if role.startswith("ax"):
        role = role[2:]
    return bool(role in _EDITABLE_UI_ROLES or element.get("editable") is True)


def _trusted_exact_clipboard_content_observation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
) -> dict[str, Any]:
    if action_tool != "clipboard.write":
        return {}
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    if verifier_tool != "clipboard.read":
        return {}
    action_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    expected_text = action_input.get("text")
    if not isinstance(expected_text, str) or not expected_text:
        return {}
    data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observed_text = data.get("text")
    if (
        not isinstance(observed_text, str)
        or observed_text != expected_text
        or data.get("truncated") is True
        or data.get("text_length") != len(expected_text)
    ):
        return {}
    content_sha256 = hashlib.sha256(expected_text.encode("utf-8")).hexdigest()
    return {
        "verification_predicate_kind": EXACT_CLIPBOARD_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "persisted",
        "content_sha256": content_sha256,
        "content_length": len(expected_text),
        "clipboard_source_verified": True,
    }


def _tool_result_with_unverified_semantic_clipboard_copy_readback(
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
    run_id: str,
) -> Mapping[str, Any]:
    """Expose a copied clipboard value without claiming its unknown source.

    A generic copy shortcut does not reveal the selected bytes.  Reading the
    clipboard afterwards is useful evidence, but it cannot prove that the
    value came from this particular shortcut instead of an older pasteboard
    value.  Keep that case as an auditable partial result until a provider can
    supply a source-bound clipboard revision receipt.
    """

    if (
        str(verifier_request.get("tool") or "").strip() != "clipboard.read"
        or str(verifier_request.get("source") or "").strip()
        != "runtime_post_action_auto_verify"
        or str(verifier_request.get("source_tool") or "").strip()
        not in {"desktop.safe_shortcut", "desktop.shortcut"}
        or str(verifier_request.get("runtime_stage") or "").strip() != "verify"
        or verifier_result.get("ok") is not True
    ):
        return verifier_result

    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    source_step_id = str(verifier_request.get("source_step_id") or "").strip()
    plan_id = str(verifier_request.get("plan_id") or "").strip()
    clean_run_id = str(run_id or verifier_request.get("run_id") or "").strip()
    source_bound = False
    for event in reversed(timeline[tool_timeline_start:]):
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if str(event.get("tool_call_id") or "").strip() != source_tool_call_id:
            continue
        event_input = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        event_result = (
            event.get("result") if isinstance(event.get("result"), Mapping) else {}
        )
        source_bound = bool(
            source_tool_call_id
            and source_step_id
            and plan_id
            and clean_run_id
            and str(event.get("run_id") or "").strip() == clean_run_id
            and str(event.get("plan_id") or "").strip() == plan_id
            and str(
                event.get("step_id") or event.get("planner_step_id") or ""
            ).strip()
            == source_step_id
            and str(event.get("detail") or event.get("tool") or "").strip()
            == str(verifier_request.get("source_tool") or "").strip()
            and str(event_input.get("action") or "").strip().lower() == "copy"
            and event_result.get("ok") is True
        )
        break

    data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    observed_text = data.get("text")
    observed_sha256 = (
        hashlib.sha256(observed_text.encode("utf-8")).hexdigest()
        if isinstance(observed_text, str) and data.get("truncated") is not True
        else ""
    )
    reason = (
        "clipboard_copy_source_unverified"
        if source_bound
        else "clipboard_copy_source_unbound"
    )
    return {
        **dict(verifier_result),
        "status": "partial",
        "outcome": "partial",
        "reason": reason,
        "retryable": False,
        "verification_failed": True,
        "verification_passed": False,
        "postcondition_verified": False,
        "clipboard_source_verified": False,
        "source_tool_call_id": source_tool_call_id,
        "source_step_id": source_step_id,
        "summary": (
            "Clipboard content was read, but its source could not be verified "
            "as the current selection."
        ),
        "data": {
            **dict(data),
            "clipboard_source_verified": False,
            **(
                {"observed_content_sha256": observed_sha256}
                if observed_sha256
                else {}
            ),
        },
    }


def _tool_result_with_unverified_semantic_clipboard_paste_readback(
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
    run_id: str,
) -> Mapping[str, Any]:
    verifier_tool = str(verifier_request.get("tool") or "").strip()
    source_tool = str(verifier_request.get("source_tool") or "").strip()
    if (
        verifier_tool
        not in {"desktop.inspect_app", "desktop.ui_elements", "desktop.read_ui", "desktop.verify"}
        or str(verifier_request.get("source") or "").strip()
        != "runtime_post_action_auto_verify"
        or source_tool
        not in {
            "desktop.safe_shortcut",
            "desktop.shortcut",
            "app.open_and_safe_shortcut",
            "app.focus_and_safe_shortcut",
        }
        or str(verifier_request.get("runtime_stage") or "").strip() != "verify"
        or verifier_result.get("ok") is not True
    ):
        return verifier_result
    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    clean_run_id = str(run_id or verifier_request.get("run_id") or "").strip()
    source_is_paste = False
    for event in reversed(timeline[tool_timeline_start:]):
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if str(event.get("tool_call_id") or "").strip() != source_tool_call_id:
            continue
        event_input = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        event_result = (
            event.get("result") if isinstance(event.get("result"), Mapping) else {}
        )
        source_is_paste = bool(
            source_tool_call_id
            and clean_run_id
            and str(event.get("run_id") or "").strip() == clean_run_id
            and str(event.get("detail") or event.get("tool") or "").strip()
            == source_tool
            and str(event_input.get("action") or "").strip().lower() == "paste"
            and event_result.get("ok") is True
        )
        break
    if not source_is_paste:
        return verifier_result
    data = (
        verifier_result.get("data")
        if isinstance(verifier_result.get("data"), Mapping)
        else {}
    )
    return {
        **dict(verifier_result),
        "status": "verification_failed",
        "reason": "clipboard_paste_source_or_readback_unverified",
        "retryable": False,
        "verification_failed": True,
        "verification_passed": False,
        "postcondition_verified": False,
        "clipboard_source_verified": False,
        "target_ui_readback_verified": False,
        "source_tool_call_id": source_tool_call_id,
        "summary": (
            "Paste was dispatched, but the Runtime could not bind an exact "
            "clipboard hash to the target UI readback."
        ),
        "data": {
            **dict(data),
            "clipboard_source_verified": False,
            "target_ui_readback_verified": False,
        },
    }


def _trusted_ui_observation_elements(
    data: Mapping[str, Any],
) -> tuple[str, list[Mapping[str, Any]]]:
    nested_ui = data.get("ui_elements")
    nested_data = (
        nested_ui.get("data")
        if isinstance(nested_ui, Mapping)
        and isinstance(nested_ui.get("data"), Mapping)
        else {}
    )
    observed_app = str(
        data.get("app_name") or nested_data.get("app_name") or ""
    ).strip()
    raw_elements = data.get("elements")
    if not isinstance(raw_elements, list):
        raw_elements = nested_data.get("elements")
    elements = [
        element
        for element in (raw_elements if isinstance(raw_elements, list) else [])
        if isinstance(element, Mapping)
    ]
    return observed_app, elements


def _trusted_ui_window_identity(
    result: Mapping[str, Any],
    *,
    expected_app_name: str = "",
) -> dict[str, Any]:
    """Extract one exact app/process/window identity from a trusted result."""

    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    nested_ui = (
        data.get("ui_elements")
        if isinstance(data.get("ui_elements"), Mapping)
        else {}
    )
    nested_data = (
        nested_ui.get("data")
        if isinstance(nested_ui.get("data"), Mapping)
        else {}
    )
    fallback = (
        result.get("fallback_result")
        if isinstance(result.get("fallback_result"), Mapping)
        else {}
    )
    active_window = (
        fallback.get("active_window")
        if isinstance(fallback.get("active_window"), Mapping)
        else {}
    )
    active_window_data = (
        active_window.get("data")
        if isinstance(active_window.get("data"), Mapping)
        else {}
    )
    sources = (result, data, nested_ui, nested_data, active_window, active_window_data)
    app_names = [
        str(
            source.get("app_name")
            or source.get("active_app_name")
            or source.get("frontmost_app")
            or source.get("target_app_name")
            or ""
        ).strip()
        for source in sources
        if isinstance(source, Mapping)
        and str(
            source.get("app_name")
            or source.get("active_app_name")
            or source.get("frontmost_app")
            or source.get("target_app_name")
            or ""
        ).strip()
    ]
    clean_expected = str(expected_app_name or "").strip()
    if clean_expected and any(
        not _app_lookups_same_identity(clean_expected, name) for name in app_names
    ):
        return {}
    observed_app = clean_expected or (app_names[0] if app_names else "")
    if not observed_app:
        return {}
    identities = {
        (pid, window_id)
        for source in sources
        if isinstance(source, Mapping)
        for pid, window_id in [
            (
                _runtime_positive_int(source.get("pid")),
                _runtime_positive_int(source.get("window_id")),
            )
        ]
        if pid is not None and window_id is not None
    }
    if len(identities) != 1:
        return {}
    pid, window_id = next(iter(identities))
    return {
        "app_name": observed_app,
        "pid": pid,
        "window_id": window_id,
    }


def _same_trusted_ui_window_identity(first: Any, second: Any) -> bool:
    if not isinstance(first, Mapping) or not isinstance(second, Mapping):
        return False
    return bool(
        _app_lookups_same_identity(
            str(first.get("app_name") or "").strip(),
            str(second.get("app_name") or "").strip(),
        )
        and _runtime_positive_int(first.get("pid"))
        == _runtime_positive_int(second.get("pid"))
        and _runtime_positive_int(first.get("window_id"))
        == _runtime_positive_int(second.get("window_id"))
        and _runtime_positive_int(first.get("pid")) is not None
        and _runtime_positive_int(first.get("window_id")) is not None
    )


def _trusted_editable_ui_target_identity(
    element: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(element, Mapping) or not _trusted_ui_element_is_editable(element):
        return {}
    role = "".join(
        character
        for character in str(element.get("role") or "").strip().casefold()
        if character.isalnum()
    )
    if role.startswith("ax"):
        role = role[2:]
    if not role:
        return {}
    identity = {
        key: " ".join(str(element.get(key) or "").strip().casefold().split())
        for key in ("identifier", "name", "label", "title", "description")
        if str(element.get(key) or "").strip()
    }
    if not identity:
        return {}
    return {"role": role, **identity}


def _trusted_ui_element_identity(element: Mapping[str, Any] | None) -> str:
    if not isinstance(element, Mapping):
        return ""
    return str(
        element.get("name")
        or element.get("label")
        or element.get("title")
        or element.get("identifier")
        or ""
    ).strip()


def _trusted_ui_element_matches_target(
    element: Mapping[str, Any],
    expected_target: str,
) -> bool:
    expected = " ".join(str(expected_target or "").strip().casefold().split())
    if not expected:
        return False
    identities = {
        " ".join(str(element.get(key) or "").strip().casefold().split())
        for key in ("name", "label", "title", "identifier", "description")
    }
    return expected in identities


def _postcondition_receipt_execution_scope_matches(
    verifier_request: Mapping[str, Any],
    action_event: Mapping[str, Any],
) -> bool:
    source_identity_match = _postcondition_receipt_source_identity_matches(
        verifier_request,
        action_event,
    )
    if source_identity_match is False:
        return False
    strong_identity = source_identity_match is True
    for key in ("run_id", "decision_id", "plan_id"):
        verifier_value = str(verifier_request.get(key) or "").strip()
        action_value = str(action_event.get(key) or "").strip()
        if verifier_value and action_value:
            if verifier_value != action_value:
                return False
            continue
        if (verifier_value or action_value) and not strong_identity:
            return False
    return True


def _postcondition_receipt_source_identity_matches(
    verifier_request: Mapping[str, Any],
    action_event: Mapping[str, Any],
) -> bool | None:
    event_input = (
        action_event.get("input_preview")
        if isinstance(action_event.get("input_preview"), Mapping)
        else {}
    )
    source_tool_call_id = str(
        verifier_request.get("source_tool_call_id") or ""
    ).strip()
    if source_tool_call_id:
        action_tool_call_id = str(
            action_event.get("tool_call_id") or event_input.get("tool_call_id") or ""
        ).strip()
        return bool(action_tool_call_id and action_tool_call_id == source_tool_call_id)
    source_request_id = str(verifier_request.get("source_request_id") or "").strip()
    if source_request_id:
        action_request_id = str(
            action_event.get("request_id") or event_input.get("request_id") or ""
        ).strip()
        return bool(action_request_id and action_request_id == source_request_id)
    return None


def _postcondition_verifier_target_step_ids(request: Mapping[str, Any]) -> set[str]:
    step_ids = {
        str(request.get("source_step_id") or "").strip(),
        *(
            str(value or "").strip()
            for value in request.get("depends_on") or []
            if not isinstance(value, Mapping)
        ),
    }
    for key in ("verification_targets", "task_verification_targets"):
        values = request.get(key)
        if not isinstance(values, list):
            continue
        step_ids.update(
            str(value.get("step_id") or "").strip()
            for value in values
            if isinstance(value, Mapping)
        )
    return {value for value in step_ids if value}


def _native_action_receipt_verifies_postcondition(
    tool_name: str,
    result: Mapping[str, Any],
    *,
    action_input: Mapping[str, Any],
) -> bool:
    if result.get("ok") is not True:
        return False
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    sources = (result, data)
    clean_tool = str(tool_name or "").strip()
    if not _native_receipt_matches_requested_action(
        clean_tool,
        action_input,
        result,
        data,
    ):
        return False
    if has_intrinsic_native_postcondition_contract(clean_tool):
        # Lifecycle commands have exact intrinsic schemas. Semantic shortcuts
        # deliberately yield no intrinsic state here, so provider-supplied
        # success flags cannot promote their own mutations to completion.
        return bool(
            intrinsic_native_postcondition_state(
                clean_tool,
                action_input,
                result,
            )
        )
    if exact_native_dispatch_receipt_matches(
        clean_tool,
        action_input,
        result,
    ):
        return True
    if clean_tool == "browser.click":
        # Clicking a selector proves only that an event was dispatched. The
        # resulting page state must be observed by browser.current_page.
        return False
    if clean_tool == "browser.type_text":
        # A mutation result echoing the requested value is not an independent
        # browser observation.  browser.current_page must actually execute;
        # its result cannot be replaced by the source call's data.value.
        return False
    if clean_tool in {"app.focus", "desktop.focus_app"}:
        # A focus command's own acknowledgement only proves that the request
        # was dispatched.  It must not replace the independent active-window
        # observation required before a sensitive foreground mutation.
        return False
    if clean_tool == "system.volume":
        # The mutation call's own read-after-write payload defines the target
        # state, but an independently executed ``status`` request must observe
        # that same state before the Runtime emits a completion receipt.
        return False
    if clean_tool in _ARTIFACT_BODY_TEXT_TOOLS:
        # Dispatch success and character counts cannot prove the exact UTF-8
        # content is present in the intended app/field.
        return False
    if clean_tool == "clipboard.write":
        # The task-level readback must remain correlated to this exact source
        # call and plan, even when the platform helper also performs a local
        # readback internally.
        return False
    shortcut_action = str(action_input.get("action") or "").strip().lower()
    if (
        clean_tool in {"desktop.safe_shortcut", "desktop.shortcut"}
        and shortcut_action == "copy"
    ) or (
        clean_tool in _CLIPBOARD_PASTE_TOOLS
        and shortcut_action == "paste"
    ):
        # Dispatching copy/paste proves neither the exact clipboard source nor
        # which editable control received those bytes.  Both require an
        # independent source/readback receipt.
        return False
    if clean_tool.startswith("media.") and any(
        source.get("playback_state_unverified") is True
        for source in sources
    ):
        return False
    if any(
        source.get(key) is True
        for source in sources
        for key in (
            "postcondition_verified",
            "launch_verified",
            "focus_verified",
            "foreground_ready",
            "quit_verified",
            "show_verified",
            "hide_verified",
            "minimize_verified",
            "minimized_verified",
        )
    ):
        return True
    if clean_tool == "app.focus_window" and str(
        data.get("matched_window_title") or result.get("matched_window_title") or ""
    ).strip():
        return True
    app_status_contracts = {
        "app.focus": ("focus_status", {"focused"}),
        "app.focus_window": ("focus_status", {"focused"}),
        "app.show": ("show_status", {"shown", "launched"}),
        "app.hide": ("hide_status", {"hidden"}),
        "app.minimize": ("minimize_status", {"minimized"}),
    }
    if clean_tool in app_status_contracts:
        key, accepted = app_status_contracts[clean_tool]
        return str(data.get(key) or result.get(key) or "").strip().lower() in accepted
    if clean_tool == "desktop.show_all_apps":
        shown_count = data.get("shown_app_count", result.get("shown_app_count"))
        return bool(
            isinstance(shown_count, int)
            and not isinstance(shown_count, bool)
            and shown_count >= 0
        )
    if clean_tool.startswith("media.apple_music"):
        state = str(data.get("player_state") or "").strip().lower()
        control = str(data.get("control") or data.get("action") or "").strip().lower()
        if control == "play":
            return state == "playing"
        if control == "pause":
            return state in {"paused", "stopped"}
        if control in {"next", "previous"}:
            return bool(str(data.get("track") or "").strip())
        return bool(
            str(data.get("track") or "").strip()
            or data.get("playback_ok") is True
        )
    return False


def _native_receipt_matches_requested_action(
    tool_name: str,
    action_input: Mapping[str, Any],
    result: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    """Reject a strong receipt that describes a different concrete action."""

    clean_tool = str(tool_name or "").strip()
    app_scoped_actions = {
        "app.focus_window",
        "app.show",
        "app.hide",
        "app.minimize",
    }
    if clean_tool in app_scoped_actions:
        reported_tool = str(result.get("action") or result.get("tool") or "").strip()
        expected_app = str(action_input.get("app_name") or "").strip()
        observed_app = str(
            data.get("resolved_app_name")
            or data.get("app_name")
            or result.get("resolved_app_name")
            or result.get("app_name")
            or ""
        ).strip()
        if (
            reported_tool != clean_tool
            or not expected_app
            or not observed_app
            or not _app_lookups_same_identity(expected_app, observed_app)
        ):
            return False
        if clean_tool == "app.focus_window":
            expected_title = str(
                action_input.get("title_contains")
                or action_input.get("window_title")
                or ""
            ).strip()
            observed_title = str(
                data.get("matched_window_title")
                or data.get("window_title")
                or result.get("matched_window_title")
                or result.get("window_title")
                or ""
            ).strip()
            if not (
                expected_title
                and observed_title
                and expected_title.casefold() in observed_title.casefold()
            ):
                return False
        return True

    semantic_key_field = {
        "desktop.safe_key": "key_action",
        "desktop.safe_shortcut": "shortcut_action",
        "desktop.shortcut": "shortcut_action",
    }.get(clean_tool)
    if semantic_key_field:
        reported_tool = str(result.get("action") or result.get("tool") or "").strip()
        requested_action = str(action_input.get("action") or "").strip().casefold()
        observed_action = str(
            data.get(semantic_key_field)
            or result.get(semantic_key_field)
            or ""
        ).strip().casefold()
        if (
            reported_tool != clean_tool
            or not requested_action
            or observed_action != requested_action
        ):
            return False
        if clean_tool == "desktop.safe_key":
            requested_repeat = action_input.get("repeat_count", 1)
            observed_repeat = data.get("repeat_count", result.get("repeat_count"))
            if (
                isinstance(requested_repeat, bool)
                or isinstance(observed_repeat, bool)
                or not isinstance(requested_repeat, int)
                or not isinstance(observed_repeat, int)
                or requested_repeat != observed_repeat
            ):
                return False
        return True

    return True


def _trusted_native_postcondition_verifier_for_action(
    action_tool: str,
    verifier_request: Mapping[str, Any],
) -> bool:
    verifier_tool = str(
        verifier_request.get("tool") or verifier_request.get("tool_name") or ""
    ).strip()
    verifier_input = (
        verifier_request.get("input")
        if isinstance(verifier_request.get("input"), Mapping)
        else {}
    )
    trusted_tools = _post_action_verification_tools(
        action_tool,
        allowed_tools=list(_POST_ACTION_READ_ONLY_VERIFIER_TOOLS),
    )
    if verifier_tool in trusted_tools:
        return True
    clean_action_tool = str(action_tool or "").strip()
    if clean_action_tool == "system.volume":
        return bool(
            verifier_tool == "system.volume"
            and str(verifier_input.get("action") or "").strip().lower() == "status"
        )
    if clean_action_tool.startswith("media.apple_music"):
        return verifier_tool == "media.apple_music_status"
    return False


_SYSTEM_VOLUME_MUTATION_ACTIONS = frozenset(
    {"set", "up", "down", "mute", "unmute"}
)


def _system_volume_native_receipt_verified(
    result: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    """Accept the volume tool's own read-after-write state as a strong receipt."""

    requested = ""
    for source in (data, result):
        for key in ("requested_action", "requested", "action"):
            candidate = str(source.get(key) or "").strip().lower()
            if candidate in _SYSTEM_VOLUME_MUTATION_ACTIONS:
                requested = candidate
                break
        if requested:
            break
    if not requested:
        return False

    observed_level: Any = None
    observed_muted: Any = None
    for source in (data, result):
        if observed_level is None:
            observed_level = source.get("level", source.get("volume"))
        if observed_muted is None and "muted" in source:
            observed_muted = source.get("muted")
    has_level = bool(
        isinstance(observed_level, (int, float))
        and not isinstance(observed_level, bool)
        and 0 <= float(observed_level) <= 100
    )
    has_muted = isinstance(observed_muted, bool)
    if requested in {"set", "up", "down"}:
        return has_level
    if requested == "mute":
        return observed_muted is True
    if requested == "unmute":
        return observed_muted is False
    return has_level or has_muted


_POST_ACTION_VERIFIER_CANDIDATES_BY_CAPABILITY = {
    "browser_state": ("browser.current_page",),
    "foreground_ui": ("desktop.ui_elements", "desktop.read_ui", "desktop.verify"),
    "system_volume_state": ("system.volume",),
    "clipboard_state": ("clipboard.read",),
}

_POST_ACTION_VERIFIER_CAPABILITY_BY_TOOL = {
    "browser.click": "browser_state",
    "browser.type_text": "browser_state",
    "app.focus_and_click_ui_element": "foreground_ui",
    "app.open_and_click_ui_element": "foreground_ui",
    "app.focus_and_safe_click": "foreground_ui",
    "app.open_and_safe_click": "foreground_ui",
    "app.focus_and_type_into_ui_element": "foreground_ui",
    "app.open_and_type_into_ui_element": "foreground_ui",
    "app.focus_and_safe_type_text": "foreground_ui",
    "app.open_and_safe_type_text": "foreground_ui",
    "system.volume": "system_volume_state",
    "clipboard.write": "clipboard_state",
}

_POST_ACTION_READ_ONLY_VERIFIER_TOOLS = frozenset(
    {
        "browser.current_page",
        "desktop.active_window",
        "desktop.read_ui",
        "desktop.ui_elements",
        "desktop.verify",
        "system.volume",
        "clipboard.read",
    }
)

_POST_ACTION_APP_SCOPED_VERIFIER_TOOLS = frozenset(
    {
        "desktop.read_ui",
        "desktop.ui_elements",
        "desktop.verify",
    }
)


def _post_action_verification_tool(tool_name: str, *, allowed_tools: list[str]) -> str:
    candidates = _post_action_verification_tools(
        tool_name,
        allowed_tools=allowed_tools,
    )
    return candidates[0] if candidates else ""


def _post_action_verification_tools(
    tool_name: str,
    *,
    allowed_tools: list[str],
) -> tuple[str, ...]:
    allowed = {str(tool or "").strip() for tool in allowed_tools}
    clean_tool = str(tool_name or "").strip()
    capability = _POST_ACTION_VERIFIER_CAPABILITY_BY_TOOL.get(clean_tool)
    if clean_tool in _TRUSTED_EXACT_TYPED_CONTENT_RECEIPT_TOOLS:
        candidates = (
            "desktop.verify",
            "desktop.ui_elements",
            "desktop.read_ui",
        )
    elif capability:
        candidates = _POST_ACTION_VERIFIER_CANDIDATES_BY_CAPABILITY[capability]
    elif not _tool_needs_desktop_post_action_verification(clean_tool):
        return ()
    elif clean_tool in _ARTIFACT_BODY_TEXT_TOOLS:
        candidates = ("desktop.ui_elements", "desktop.read_ui")
    elif _tool_can_change_active_app(clean_tool) and "desktop.active_window" in allowed:
        # Keep active-window observation as the synthetic default, but allow
        # any planner-declared read-only desktop verifier to remain
        # authoritative. The Runtime binds that existing step to the concrete
        # source invocation instead of inserting a duplicate synthetic step.
        candidates = (
            "desktop.active_window",
            "desktop.verify",
            "desktop.ui_elements",
            "desktop.read_ui",
        )
    else:
        candidates = ("desktop.ui_elements", "desktop.read_ui", "desktop.verify")
    return tuple(
        candidate
        for candidate in candidates
        if candidate in allowed and candidate in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
    )


def _tool_needs_desktop_post_action_verification(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        _tool_can_change_active_app(clean_tool)
        or clean_tool in _FOREGROUND_READINESS_GATED_TOOLS
        or clean_tool in _DESKTOP_POST_ACTION_VERIFICATION_TOOLS
    )


def _remaining_requests_include_post_action_verification(
    remaining_requests: list[dict[str, Any]],
    *,
    source_tool_name: str,
    allowed_tools: list[str],
    source_step_id: str,
    source_request_id: str = "",
    source_tool_call_id: str = "",
    source_plan_id: str = "",
    source_tool_plan_id: str = "",
    verification_predicate_kind: str = "",
    bind_source_identity: bool = False,
) -> bool:
    allowed_verifier_tools = set(
        _post_action_verification_tools(
            source_tool_name,
            allowed_tools=allowed_tools,
        )
    )
    if not allowed_verifier_tools:
        return False
    for request in remaining_requests:
        if not isinstance(request, Mapping):
            continue
        request_tool = str(
            request.get("tool") or request.get("tool_name") or ""
        ).strip()
        verifier_tool_allowed = bool(
            request_tool in allowed_verifier_tools
            and request_tool in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
        )
        runtime_stage = str(request.get("runtime_stage") or "").strip()
        runtime_role = str(request.get("runtime_role") or "").strip()
        deferred_observation = (
            verifier_tool_allowed
            and request_tool in {"desktop.ui_elements", "desktop.read_ui"}
            and (
                bool(request.get("continue_to_model"))
                or bool(request.get("requires_observation"))
            )
        )
        is_verify_request = (
            verifier_tool_allowed
            and (
                runtime_stage == "verify"
                or runtime_role == "verify_result"
                or request_tool in {"desktop.active_window", "desktop.verify"}
                or deferred_observation
            )
        )
        if not is_verify_request:
            if not source_step_id:
                # Without a step graph, only an immediately adjacent verifier
                # can protect this action. Any intervening request makes later
                # observation too late, even when metadata claims correlation.
                return False
            if source_step_id and source_step_id in _string_list(request.get("depends_on")):
                # A verifier that appears after a dependent operation is too
                # late to protect that operation. Let the runtime insert an
                # immediate verifier ahead of the dependency instead.
                return False
            continue
        if not source_step_id:
            return _post_action_verifier_matches_source_and_plan(
                request,
                source_tool_name=source_tool_name,
                source_step_id=source_step_id,
                source_request_id=source_request_id,
                source_tool_call_id=source_tool_call_id,
                source_plan_id=source_plan_id,
                source_tool_plan_id=source_tool_plan_id,
                verification_predicate_kind=verification_predicate_kind,
                require_match=True,
                bind_source_identity=bind_source_identity,
            )
        depends_on = _string_list(request.get("depends_on"))
        if source_step_id in depends_on:
            return _post_action_verifier_matches_source_and_plan(
                request,
                source_tool_name=source_tool_name,
                source_step_id=source_step_id,
                source_request_id=source_request_id,
                source_tool_call_id=source_tool_call_id,
                source_plan_id=source_plan_id,
                source_tool_plan_id=source_tool_plan_id,
                verification_predicate_kind=verification_predicate_kind,
                require_match=False,
                bind_source_identity=bind_source_identity,
            )
        for key in ("verification_targets", "task_verification_targets"):
            for target in _mapping_list(request.get(key)):
                if str(target.get("step_id") or "").strip() == source_step_id:
                    return _post_action_verifier_matches_source_and_plan(
                        request,
                        source_tool_name=source_tool_name,
                        source_step_id=source_step_id,
                        source_request_id=source_request_id,
                        source_tool_call_id=source_tool_call_id,
                        source_plan_id=source_plan_id,
                        source_tool_plan_id=source_tool_plan_id,
                        verification_predicate_kind=verification_predicate_kind,
                        require_match=False,
                        bind_source_identity=bind_source_identity,
                    )
        action_target = _first_mapping(request.get("action_target"))
        if source_step_id in _string_list(action_target.get("verified_step_ids")):
            return _post_action_verifier_matches_source_and_plan(
                request,
                source_tool_name=source_tool_name,
                source_step_id=source_step_id,
                source_request_id=source_request_id,
                source_tool_call_id=source_tool_call_id,
                source_plan_id=source_plan_id,
                source_tool_plan_id=source_tool_plan_id,
                verification_predicate_kind=verification_predicate_kind,
                require_match=False,
                bind_source_identity=bind_source_identity,
            )
    return False


def _upgrade_declared_planned_verifier(
    remaining_requests: list[dict[str, Any]],
    *,
    verification_tool: str,
    verification_input: Mapping[str, Any],
    source_tool_name: str,
    source_tool_request: Mapping[str, Any],
    app_name: str,
) -> bool:
    """Reuse one exact declared verifier step with a stronger observer.

    A planner can declare a generic read-only observation (for example a
    running-app inventory) that is insufficient to prove a particular
    postcondition.  Inserting a separate ``<source>:runtime-verify`` step
    loses the GoalContract's declared verifier identity.  Instead, upgrade the
    already-declared dependent observation in place while preserving its plan
    and step ids, then bind it to this exact source invocation.

    The replacement is deliberately fail-closed: the candidate must be a
    non-approval Runtime observation in the same plan, explicitly depend on
    the source step, and carry no conflicting request/call identity.
    """

    clean_verification_tool = str(verification_tool or "").strip()
    source_step_id = str(
        source_tool_request.get("step_id")
        or source_tool_request.get("planner_step_id")
        or ""
    ).strip()
    source_request_id = str(source_tool_request.get("request_id") or "").strip()
    source_tool_call_id = str(
        source_tool_request.get("tool_call_id") or ""
    ).strip()
    source_plan_id = str(source_tool_request.get("plan_id") or "").strip()
    source_tool_plan_id = str(
        source_tool_request.get("tool_plan_id") or ""
    ).strip()
    if (
        not clean_verification_tool
        or clean_verification_tool not in _POST_ACTION_READ_ONLY_VERIFIER_TOOLS
        or not source_step_id
        or not source_tool_call_id
        or not source_plan_id
    ):
        return False

    for candidate in remaining_requests:
        if not isinstance(candidate, dict):
            continue
        candidate_step_id = str(
            candidate.get("step_id") or candidate.get("planner_step_id") or ""
        ).strip()
        candidate_plan_id = str(candidate.get("plan_id") or "").strip()
        candidate_tool = str(
            candidate.get("tool") or candidate.get("tool_name") or ""
        ).strip()
        runtime_stage = str(candidate.get("runtime_stage") or "").strip()
        runtime_role = str(candidate.get("runtime_role") or "").strip()
        if not (
            candidate_step_id
            and candidate_step_id != source_step_id
            and candidate_plan_id == source_plan_id
            and candidate_tool in _RUNTIME_OBSERVATION_RETRY_TOOLS
            and candidate.get("approval_required") is not True
            and (runtime_stage == "verify" or runtime_role == "verify_result")
            and _declared_verifier_targets_source_step(candidate, source_step_id)
        ):
            continue
        if not _post_action_verifier_matches_source_and_plan(
            candidate,
            source_tool_name=source_tool_name,
            source_step_id=source_step_id,
            source_request_id=source_request_id,
            source_tool_call_id=source_tool_call_id,
            source_plan_id=source_plan_id,
            source_tool_plan_id=source_tool_plan_id,
            verification_predicate_kind=(
                _post_action_verification_predicate_kind(
                    source_tool_name,
                    source_tool_request,
                )
            ),
            require_match=False,
            bind_source_identity=True,
        ):
            return False

        candidate["planner_verifier_tool"] = candidate_tool
        candidate["tool"] = clean_verification_tool
        candidate.pop("tool_name", None)
        candidate["input"] = dict(verification_input)
        candidate["source"] = "runtime_post_action_auto_verify"
        candidate["planning_reason"] = (
            "runtime_post_action_upgrade_declared_verifier"
        )
        candidate["runtime_stage"] = "verify"
        candidate["runtime_role"] = "verify_result"
        candidate["approval_required"] = False
        candidate["requires_observation"] = True
        candidate["requires_post_action_verification"] = False
        if app_name:
            candidate["verification_target"] = {
                "app_name": app_name,
                "source_tool": source_tool_name,
            }
        desktop_loop = _post_action_verification_desktop_loop(
            source_tool_request,
            verification_tool=clean_verification_tool,
            app_name=app_name,
            source_step_id=source_step_id,
        )
        if desktop_loop:
            candidate["desktop_loop"] = desktop_loop
        _copy_post_action_verification_context(source_tool_request, candidate)
        return True
    return False


def _declared_verifier_targets_source_step(
    request: Mapping[str, Any],
    source_step_id: str,
) -> bool:
    clean_source_step_id = str(source_step_id or "").strip()
    if not clean_source_step_id:
        return False
    if clean_source_step_id in _string_list(request.get("depends_on")):
        return True
    for key in ("verification_targets", "task_verification_targets"):
        if any(
            str(target.get("step_id") or "").strip() == clean_source_step_id
            for target in _mapping_list(request.get(key))
        ):
            return True
    action_target = _first_mapping(request.get("action_target"))
    return clean_source_step_id in _string_list(
        action_target.get("verified_step_ids")
    )


def _post_action_verifier_matches_source_and_plan(
    verifier: Mapping[str, Any],
    *,
    source_tool_name: str,
    source_step_id: str,
    source_request_id: str,
    source_tool_call_id: str,
    source_plan_id: str,
    source_tool_plan_id: str,
    verification_predicate_kind: str,
    require_match: bool,
    bind_source_identity: bool,
) -> bool:
    if not _post_action_verifier_source_identity_matches(
        verifier,
        source_request_id=source_request_id,
        source_tool_call_id=source_tool_call_id,
        require_match=require_match,
    ):
        return False
    for key, expected in (
        ("plan_id", str(source_plan_id or "").strip()),
        ("tool_plan_id", str(source_tool_plan_id or "").strip()),
    ):
        claimed = str(verifier.get(key) or "").strip()
        if expected and claimed and expected != claimed:
            return False
    if not bind_source_identity:
        return True
    if not isinstance(verifier, dict):
        return False
    bindings = {
        "source_tool": str(source_tool_name or "").strip(),
        "source_step_id": str(source_step_id or "").strip(),
        "source_request_id": str(source_request_id or "").strip(),
        "source_tool_call_id": str(source_tool_call_id or "").strip(),
        "plan_id": str(source_plan_id or "").strip(),
        "tool_plan_id": str(source_tool_plan_id or "").strip(),
        "verification_predicate_kind": str(
            verification_predicate_kind or ""
        ).strip(),
    }
    for key, value in bindings.items():
        if value:
            verifier[key] = value
    verifier_step_id = str(
        verifier.get("step_id") or verifier.get("planner_step_id") or ""
    ).strip()
    if verifier_step_id:
        verifier["verifier_step_id"] = verifier_step_id
    return True


def _post_action_verification_predicate_kind(
    tool_name: str,
    tool_request: Mapping[str, Any] | None = None,
) -> str:
    clean_tool = str(tool_name or "").strip()
    if tool_request is not None and _is_clipboard_paste_request(
        {**dict(tool_request), "tool": clean_tool}
    ):
        return EXACT_PASTED_CONTENT_PRESENT_PREDICATE
    if clean_tool in _TRUSTED_APP_WINDOW_RECEIPT_TOOLS:
        return APP_WINDOW_PRESENT_PREDICATE
    if clean_tool in _EXACT_TYPED_CONTENT_OBSERVATION_TOOLS:
        return EXACT_TYPED_CONTENT_PRESENT_PREDICATE
    if clean_tool == "clipboard.write":
        return EXACT_CLIPBOARD_CONTENT_PRESENT_PREDICATE
    if clean_tool == "desktop.submit_foreground":
        return _EXACT_SUBMIT_DISPATCH_PREDICATE
    if clean_tool == "desktop.search_submit":
        return "exact_app_search_result_present"
    return ""


def _post_action_verifier_source_identity_matches(
    verifier: Mapping[str, Any],
    *,
    source_request_id: str,
    source_tool_call_id: str,
    require_match: bool,
) -> bool:
    """Validate verifier correlation without trusting its own request identity."""

    expected_request_id = str(source_request_id or "").strip()
    expected_tool_call_id = str(source_tool_call_id or "").strip()
    verifier_request_id = str(verifier.get("source_request_id") or "").strip()
    verifier_tool_call_id = str(verifier.get("source_tool_call_id") or "").strip()

    # Any supplied identity must agree with the source invocation.  A request
    # id can span retries/recovery attempts, so it is never a substitute when
    # the source has the stronger per-invocation tool-call id.
    if (
        expected_request_id
        and verifier_request_id
        and verifier_request_id != expected_request_id
    ):
        return False
    if (
        expected_tool_call_id
        and verifier_tool_call_id
        and verifier_tool_call_id != expected_tool_call_id
    ):
        return False
    if not require_match:
        return True
    if expected_tool_call_id:
        return verifier_tool_call_id == expected_tool_call_id
    if expected_request_id:
        return verifier_request_id == expected_request_id
    return False


def _post_action_verification_app_name(
    tool_request: Mapping[str, Any],
    tool_result: Mapping[str, Any],
    *,
    active_window_target: Mapping[str, Any] | None,
) -> str:
    tool_name = str(tool_request.get("tool") or "").strip()
    if not _tool_can_change_active_app(tool_name):
        data = (
            tool_result.get("data")
            if isinstance(tool_result.get("data"), Mapping)
            else {}
        )
        raw_input = (
            tool_request.get("input")
            if isinstance(tool_request.get("input"), Mapping)
            else {}
        )
        action_target = _first_mapping(tool_request.get("action_target"))
        target = active_window_target if isinstance(active_window_target, Mapping) else {}
        return _first_text(
            target.get("app_name"),
            data.get("app_name"),
            data.get("discovered_app_name"),
            raw_input.get("app_name"),
            action_target.get("app_name"),
            action_target.get("resolved_app_name"),
        )

    reported_action = str(
        tool_result.get("action") or tool_result.get("tool") or ""
    ).strip()
    if reported_action and reported_action != tool_name:
        return ""
    result_app_name = _post_action_verification_result_app_name(tool_result)
    if not result_app_name:
        return ""
    target = active_window_target if isinstance(active_window_target, Mapping) else {}
    target_app_name = _canonical_post_action_app_name(target.get("app_name"))
    if target_app_name and compact_app_alias(target_app_name) != compact_app_alias(
        result_app_name
    ):
        return ""
    return result_app_name


def _post_action_verification_result_app_name(
    tool_result: Mapping[str, Any],
) -> str:
    data = (
        tool_result.get("data")
        if isinstance(tool_result.get("data"), Mapping)
        else {}
    )
    candidates: list[str] = []

    def append_names(source: Mapping[str, Any], keys: tuple[str, ...]) -> None:
        for key in keys:
            value = _canonical_post_action_app_name(source.get(key))
            if value and value not in candidates:
                candidates.append(value)

    for source in (tool_result, data):
        append_names(source, ("resolved_app_name", "discovered_app_name"))

    nested_results: list[Mapping[str, Any]] = []
    for source in (tool_result, data):
        for key in ("focus_result", "open_result"):
            nested = source.get(key)
            if not isinstance(nested, Mapping):
                continue
            if nested.get("ok") is not True:
                return ""
            nested_results.append(nested)
    fallback_result = tool_result.get("fallback_result")
    if isinstance(fallback_result, Mapping):
        for key in ("focus", "open"):
            nested = fallback_result.get(key)
            if not isinstance(nested, Mapping):
                continue
            if nested.get("ok") is not True:
                return ""
            nested_results.append(nested)
    for nested in nested_results:
        nested_data = (
            nested.get("data")
            if isinstance(nested.get("data"), Mapping)
            else {}
        )
        for source in (nested, nested_data):
            append_names(
                source,
                ("resolved_app_name", "discovered_app_name", "app_name"),
            )

    for source in (tool_result, data):
        append_names(source, ("app_name",))
    if not candidates:
        return ""
    canonical_ids = {compact_app_alias(value) for value in candidates if value}
    if len(canonical_ids) != 1:
        return ""
    return candidates[0]


def _canonical_post_action_app_name(value: Any) -> str:
    clean = str(value or "").strip()
    if not clean or _selected_desktop_app_placeholder_source(clean):
        return ""
    compact = compact_app_alias(clean)
    return str(APP_ALIASES.get(compact) or clean).strip()


def _post_action_verification_input(
    verification_tool: str,
    app_name: str,
) -> dict[str, Any]:
    if verification_tool not in _POST_ACTION_APP_SCOPED_VERIFIER_TOOLS:
        return {}
    return {"app_name": app_name} if app_name else {}


def _post_action_verification_target(
    tool_request: Mapping[str, Any],
    *,
    source_step_id: str,
) -> dict[str, Any]:
    existing_targets = [
        *_mapping_list(tool_request.get("verification_targets")),
        *_mapping_list(tool_request.get("task_verification_targets")),
    ]
    if existing_targets:
        return dict(existing_targets[0])
    if not source_step_id:
        return {}
    target: dict[str, Any] = {"step_id": source_step_id}
    todo = _first_mapping(tool_request.get("task_todo"))
    if todo:
        target["todo"] = dict(todo)
    checkpoints = _mapping_list(tool_request.get("task_checkpoints"))
    if checkpoints:
        target["checkpoints"] = [dict(item) for item in checkpoints]
    workspace_items = _mapping_list(tool_request.get("task_workspace_items"))
    if workspace_items:
        target["workspace_items"] = [dict(item) for item in workspace_items]
    return target


def _post_action_verification_desktop_loop(
    tool_request: Mapping[str, Any],
    *,
    verification_tool: str,
    app_name: str,
    source_step_id: str,
) -> dict[str, Any]:
    if not verification_tool:
        return {}
    action_target = _first_mapping(tool_request.get("action_target"))
    target_ids = [source_step_id] if source_step_id else []
    default_target_kind = (
        "browser_page" if verification_tool.startswith("browser.") else "desktop_app"
    )
    return {
        "stage": "verify",
        "role": "verify_result",
        "action": "verify_after_action",
        "target_kind": str(action_target.get("kind") or default_target_kind).strip(),
        "selection_source": str(action_target.get("selection_source") or "").strip(),
        "app_name": app_name,
        "query": str(action_target.get("query") or "").strip(),
        "source_tool": verification_tool,
        "retry_tool": verification_tool,
        "retry_reason": "verification_failed",
        "retry_input": _post_action_verification_input(verification_tool, app_name),
        "verification_target_step_ids": target_ids,
        "requires_observation": True,
        "requires_post_action_verification": True,
        "can_auto_retry": verification_tool
        in {
            "browser.current_page",
            "desktop.active_window",
            "desktop.ui_elements",
            "desktop.read_ui",
        },
        "source": "runtime_post_action_auto_verify",
    }


def _copy_post_action_verification_context(
    tool_request: Mapping[str, Any],
    request: dict[str, Any],
) -> None:
    for key in (
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "core_id",
        "workspace_id",
        "task_id",
        "run_group_id",
        "group_run_id",
        "group_id",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "attempt_id",
        "execution_attempt_id",
        "materialization_binding_id",
        "materialized_content_sha256",
        "desktop_execution_policy",
        "desktop_execution_route",
        "desktop_provider_session",
        "sandbox_provider",
        "sandbox_desktop_provider",
    ):
        value = tool_request.get(key)
        if value not in (None, "", [], {}):
            request[key] = dict(value) if isinstance(value, Mapping) else value


def _post_action_verification_enqueued_payload(
    tool_name: str,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {
        "source_tool": tool_name,
        "verification_tool": str(request.get("tool") or "").strip(),
        "planning_reason": str(request.get("planning_reason") or "").strip(),
    }
    for key in (
        "step_id",
        "planner_step_id",
        "depends_on",
        "verification_target",
        "verification_targets",
        "task_verification_targets",
        "desktop_loop",
        "desktop_provider_session",
    ):
        value = request.get(key)
        if value not in (None, "", [], {}):
            if key == "desktop_provider_session" and isinstance(value, Mapping):
                payload[key] = _public_desktop_provider_session(value)
            else:
                payload[key] = dict(value) if isinstance(value, Mapping) else value
    return payload


def _declared_post_action_verifier_already_planned(
    verifier_request: Mapping[str, Any],
    timeline: Iterable[Mapping[str, Any]],
) -> bool:
    verifier_tool = str(verifier_request.get("tool") or "").strip()
    verifier_step_id = str(
        verifier_request.get("step_id")
        or verifier_request.get("planner_step_id")
        or ""
    ).strip()
    verifier_plan_id = str(verifier_request.get("plan_id") or "").strip()
    if not verifier_tool or not verifier_step_id or not verifier_plan_id:
        return True
    for raw_event in timeline:
        if not isinstance(raw_event, Mapping):
            continue
        event_type, payload = _runtime_timeline_event_payload(raw_event)
        if event_type != "agent.desktop.intent_planned":
            continue
        event_tool = str(
            payload.get("tool") or payload.get("detail") or ""
        ).strip()
        event_step_id = str(
            payload.get("step_id") or payload.get("planner_step_id") or ""
        ).strip()
        event_plan_id = str(payload.get("plan_id") or "").strip()
        if (
            event_tool == verifier_tool
            and event_step_id == verifier_step_id
            and event_plan_id == verifier_plan_id
        ):
            return True
    return False


def _declared_post_action_verifier_planned_payload(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": str(request.get("tool") or "").strip(),
        "status": "planned",
        "source": "runtime_planner",
        "planning_reason": str(request.get("planning_reason") or "").strip(),
        "input_preview": dict(
            request.get("input")
            if isinstance(request.get("input"), Mapping)
            else {}
        ),
        "planner_declared_verifier": True,
    }
    for key in (
        "step_id",
        "planner_step_id",
        "capability_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "core_id",
        "workspace_id",
        "task_id",
        "run_group_id",
        "group_run_id",
        "group_id",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
    ):
        value = str(request.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


def _deferred_request_signature(request: Mapping[str, Any]) -> tuple[str, str]:
    tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
    raw_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return (tool_name, repr(sorted(dict(raw_input).items())))


def _deferred_continuation_enqueued_payload(
    source_tool_name: str,
    requests: list[dict[str, Any]],
    *,
    retry_source: str = "desktop_provider_session",
    replan_payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "tool": source_tool_name,
        "source_tool": source_tool_name,
        "status": "enqueued",
        "deferred_continuation_count": len(requests),
        "deferred_tools": [
            str(request.get("tool") or "").strip()
            for request in requests
            if str(request.get("tool") or "").strip()
        ],
        "runtime_retry_source": retry_source,
    }
    if isinstance(replan_payload, Mapping):
        for key in (
            "request_id",
            "trigger",
            "decision_id",
            "plan_id",
            "core_id",
            "workspace_id",
            "task_id",
            "run_group_id",
            "group_run_id",
            "group_id",
            "workflow_id",
            "workflow_run_id",
            "workflow_node_id",
            "workflow_node_label",
        ):
            value = replan_payload.get(key)
            if value not in (None, "", [], {}):
                payload["replan_request_id" if key == "request_id" else key] = value
        action_ids = [
            str(
                request.get("replan_recovery_action_id")
                or request.get("action_id")
                or ""
            ).strip()
            for request in requests
        ]
        action_ids = [action_id for action_id in action_ids if action_id]
        if action_ids:
            payload["replan_recovery_action_ids"] = action_ids
        recovery_identities = [
            recovery_action_identity(request)
            for request in requests
        ]
        recovery_identities = [
            identity for identity in recovery_identities if identity
        ]
        if recovery_identities:
            payload["replan_recovery_identities"] = recovery_identities
    return payload


def append_replan_request_event_for_tool_result(
    *,
    tool_request: Mapping[str, Any],
    tool_event: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    timeline_factory: Callable[..., dict[str, Any]],
    append_run_event: Callable[[str, str, dict[str, Any]], Any] | None = None,
    runtime_tool_timeline_start: int | None = None,
    run_id: str = "",
) -> dict[str, Any]:
    payload = _runtime_replan_request_payload_for_tool_result(
        tool_request,
        tool_event,
        run_id=run_id,
    )
    if not payload or _runtime_replan_request_exists(timeline, payload):
        return {}
    event_type = _runtime_replan_event_type(payload)
    event_payload = _runtime_replan_event_payload(payload, event_type)
    if runtime_tool_timeline_start is not None:
        event_payload["runtime_tool_timeline_start"] = max(
            0,
            int(runtime_tool_timeline_start or 0),
        )
    detail = (
        str(payload.get("reason") or "").strip()
        or str(payload.get("failure_detail") or "").strip()
        or str(payload.get("trigger") or "replan requested")
    )
    # Persist the serializable projection before exposing it to the live loop.
    # Process-private replan authority is minted later from this same-call
    # terminal and is never passed to the repository callback.
    if run_id and append_run_event is not None:
        append_run_event(run_id, event_type, event_payload)
    timeline.append(
        timeline_factory(
            event_type,
            detail,
            status="requested",
            source="runtime_tool_request_runner",
            decision_id=str(payload.get("decision_id") or ""),
            plan_id=str(payload.get("plan_id") or ""),
            **_runtime_replan_context_payload(event_payload),
            payload=event_payload,
        )
    )
    return event_payload


def _runtime_replan_request_payload_for_tool_result(
    tool_request: Mapping[str, Any],
    tool_event: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    result = _runtime_replan_tool_event_result(tool_event)
    if not _tool_event_requests_runtime_replan(tool_event, result):
        return {}
    replan_signal_ids = _string_list(tool_request.get("replan_signal_ids"))
    replan_triggers = _string_list(tool_request.get("replan_triggers"))
    parent_replan_request_id = str(tool_request.get("replan_request_id") or "").strip()
    request_fallback_tools = _string_list(tool_request.get("fallback_tools"))
    app_not_found_discovery_action = _runtime_app_not_found_discovery_action(
        tool_request,
        result,
    )
    fallback_tools = (
        ["desktop.list_apps"]
        if app_not_found_discovery_action
        else _runtime_replan_fallback_tools(tool_request, result)
    )
    user_recovery_requested = _tool_result_requests_user_recovery(result)
    if not (
        replan_signal_ids
        or replan_triggers
        or parent_replan_request_id
        or request_fallback_tools
        or fallback_tools
        or user_recovery_requested
        or bool(tool_request.get("requires_observation"))
        or bool(tool_request.get("requires_post_action_verification"))
    ):
        return {}

    trigger = _runtime_replan_trigger(tool_event, result, replan_triggers)
    source_step_id = str(
        tool_request.get("step_id") or tool_request.get("planner_step_id") or ""
    ).strip()
    source_tool_name = str(
        tool_request.get("tool") or tool_request.get("tool_name") or tool_event.get("detail") or ""
    ).strip()
    failure_event_type = str(tool_event.get("event") or tool_event.get("event_type") or "").strip()
    failure_detail = _runtime_replan_failure_detail(tool_event, result)
    request_id = _runtime_replan_request_id(
        decision_id=str(tool_request.get("decision_id") or ""),
        plan_id=str(tool_request.get("plan_id") or ""),
        source_step_id=source_step_id,
        source_tool_name=source_tool_name,
        trigger=trigger,
    )
    input_preview = _runtime_replan_input_preview(tool_request)
    result_preview = _runtime_replan_result_preview(result)
    payload: dict[str, Any] = {
        "request_id": request_id,
        "trigger": trigger,
        "status": "requested",
        "run_id": run_id or str(tool_request.get("run_id") or ""),
        "task_id": str(tool_request.get("task_id") or ""),
        "decision_id": str(tool_request.get("decision_id") or ""),
        "plan_id": str(tool_request.get("plan_id") or ""),
        "core_id": str(tool_request.get("core_id") or ""),
        "workspace_id": str(tool_request.get("workspace_id") or ""),
        "source_step_id": source_step_id,
        "source_tool_name": source_tool_name,
        "target_capability_id": str(
            tool_request.get("target_capability_id") or tool_request.get("capability_id") or ""
        ),
        "input_preview": input_preview,
        "failure_event_type": failure_event_type,
        "failure_detail": failure_detail,
        "fallback_tools": fallback_tools,
        "replan_signal_ids": replan_signal_ids,
        "replan_triggers": replan_triggers,
        "reason": "Runtime requested a replan after a failed or unverified step.",
        "source": "runtime_tool_request_runner",
        "metadata": {
            "input_preview": input_preview,
            "result_preview": result_preview,
            "runtime_stage": str(tool_request.get("runtime_stage") or ""),
            "runtime_role": str(tool_request.get("runtime_role") or ""),
        },
    }
    for key in ("goal_contract_id", "goal_criterion_id"):
        value = str(tool_request.get(key) or "").strip()
        if value:
            payload[key] = value
    source_tool_call_id = str(tool_request.get("tool_call_id") or "").strip()
    if source_tool_call_id:
        payload["source_tool_call_id"] = source_tool_call_id
    _runtime_replan_enrich_recovery_context(
        payload,
        tool_request,
        result,
        trigger=trigger,
        source_step_id=source_step_id,
        source_tool_name=source_tool_name,
        input_preview=input_preview,
        result_preview=result_preview,
        failure_detail=failure_detail,
    )
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    metadata = dict(metadata)
    provider = _runtime_execution_provider_identity(result, expected_tool=source_tool_name)
    source_identity = {
        "source_tool_call_id": source_tool_call_id,
        "source_request_id": str(tool_request.get("request_id") or "").strip(),
        "source_plan_id": str(tool_request.get("plan_id") or "").strip(),
        "source_step_id": source_step_id,
        "source_provider_kind": str(provider.get("provider_kind") or "").strip(),
        "source_provider_id": str(provider.get("provider_id") or "").strip(),
    }
    metadata.update(
        {key: value for key, value in source_identity.items() if value}
    )
    payload["metadata"] = metadata
    if app_not_found_discovery_action:
        metadata["recovery_actions"] = [
            _runtime_replan_recovery_action_with_auto_start_context(
                _runtime_replan_recovery_action_with_execution_context(
                    app_not_found_discovery_action,
                    _runtime_replan_execution_context(tool_request, result),
                )
            )
        ]
    recovery_actions = metadata.get("recovery_actions")
    if isinstance(recovery_actions, list) and recovery_actions:
        payload["recovery_actions"] = list(recovery_actions)
    for key in (
        "run_group_id",
        "group_run_id",
        "group_id",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
    ):
        value = str(tool_request.get(key) or "").strip()
        if value:
            payload[key] = value
    return payload


def _runtime_app_not_found_discovery_action(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Replace exact local app misses with one read-only discovery action."""

    provenance = result.get(RUNTIME_EXECUTION_PROVENANCE_KEY)
    if not isinstance(provenance, Mapping) or (
        provenance.get("source") != RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE
        or provenance.get("version") != RUNTIME_EXECUTION_PROVENANCE_VERSION
    ):
        return {}
    source_tool = str(
        tool_request.get("tool") or tool_request.get("tool_name") or ""
    ).strip()
    if source_tool not in {
        "app.open",
        "desktop.open_app",
        "app.focus",
        "desktop.focus_app",
    }:
        return {}
    request_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    query = str(
        request_input.get("app_name") or request_input.get("target_app_name") or ""
    ).strip()
    if not query:
        return {}

    result_query = ""
    if source_tool in {"app.open", "desktop.open_app"}:
        result_query = _runtime_explicit_app_not_found_query(
            result,
            expected_action=source_tool,
        )
    else:
        if (
            result.get("ok") is not False
            or result.get("action") != source_tool
            or result.get("permission_error") is not False
            or result.get("fallback_used") is not False
            or not str(result.get("error") or "").strip()
        ):
            return {}
        fallback = result.get("fallback_result")
        if not isinstance(fallback, Mapping):
            return {}
        result_query = _runtime_explicit_app_not_found_query(
            fallback,
            expected_action="app.open",
        )
    if result_query != query:
        return {}

    actions = _runtime_default_replan_recovery_actions(
        source_tool,
        request_input,
        ["desktop.list_apps"],
    )
    if len(actions) != 1:
        return {}
    action = dict(actions[0])
    # This exact, provenance-bound branch only enumerates installed apps.  Mark
    # it as an internal observation so the legacy auto-start guard does not
    # mistake the read-only discovery for an external permission action.
    action["permission_target"] = "runtime_observation"
    return action


def _runtime_explicit_app_not_found_query(
    payload: Mapping[str, Any],
    *,
    expected_action: str,
) -> str:
    data = payload.get("data")
    if (
        payload.get("ok") is not False
        or payload.get("action") != expected_action
        or payload.get("error_code") != "app_not_found"
        or payload.get("permission_error") is not False
        or payload.get("fallback_used") is not False
        or not str(payload.get("error") or "").strip()
        or not isinstance(data, Mapping)
    ):
        return ""
    return str(data.get("app_name") or "").strip()


def _tool_event_requests_runtime_replan(
    tool_event: Mapping[str, Any],
    result: Mapping[str, Any],
) -> bool:
    if result.get("approval_required") or tool_event.get("approval_required"):
        return False
    if result.get("replan_allowed") is False:
        return False
    if _tool_result_failed_verification(result) or _tool_result_failed_verification(tool_event):
        return True
    if result.get("ok") is False:
        return True
    status = str(tool_event.get("status") or result.get("status") or "").strip().lower()
    if status in {"failed", "failure", "error", "unavailable", "rejected", "cancelled"}:
        return True
    event_type = str(tool_event.get("event") or tool_event.get("event_type") or "").strip().lower()
    return event_type.endswith(".failed") or event_type.endswith("_failed")


def _runtime_replan_tool_event_result(tool_event: Mapping[str, Any]) -> dict[str, Any]:
    raw_result = tool_event.get("result") if isinstance(tool_event.get("result"), Mapping) else {}
    result = dict(raw_result)
    if tool_event.get("verification_failed") is True:
        result["verification_failed"] = True
    result = _tool_result_with_verification_failure_status(result)
    return result


def _runtime_replan_trigger(
    tool_event: Mapping[str, Any],
    result: Mapping[str, Any],
    replan_triggers: list[str],
) -> str:
    if _tool_result_failed_verification(result):
        return "verification_failed"
    if (
        result.get("desktop_provider_capability_mismatch") is True
        or result.get("retry_with_alternative_capability") is True
        or result.get("error")
        == "desktop_execution_provider_tool_unavailable"
    ):
        return "tool_unavailable"
    if (
        result.get("blocked_by_desktop_target") is True
        and result.get("target_reacquisition_required") is True
        and result.get("blocked_by_desktop_execution_provider") is not True
    ):
        return "tool_failure"
    if (
        result.get("blocked_by_desktop_execution_provider")
        or result.get("error") == "desktop_execution_provider_unavailable"
    ):
        return "desktop_execution_provider_unavailable"
    event_text = " ".join(
        str(value or "").lower()
        for value in (
            tool_event.get("event"),
            tool_event.get("status"),
            result.get("status"),
            result.get("error"),
            result.get("blocking_condition"),
        )
    )
    if "unavailable" in event_text or "not_found" in event_text:
        return "tool_unavailable"
    return replan_triggers[0] if replan_triggers else "tool_failure"


def _runtime_replan_failure_detail(
    tool_event: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str:
    for value in (
        result.get("error"),
        result.get("hint"),
        result.get("summary"),
        tool_event.get("detail"),
        result.get("status"),
        tool_event.get("status"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return "tool result failed"


def _runtime_replan_fallback_tools(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> list[str]:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    tool_name = str(
        tool_request.get("tool") or tool_request.get("tool_name") or ""
    ).strip()
    tools = [
        *_string_list(tool_request.get("fallback_tools")),
        *_string_list(result.get("suggested_tool")),
        *_string_list(result.get("recommended_tools")),
    ]
    observation_retry_tool = _runtime_observation_retry_tool(
        _first_mapping(
            tool_request.get("observation_retry"),
            result.get("observation_retry"),
            data.get("observation_retry"),
        )
    )
    if observation_retry_tool:
        tools.append(observation_retry_tool)
    recovery_actions = result.get("recovery_actions")
    if isinstance(recovery_actions, list):
        for action in recovery_actions:
            if isinstance(action, Mapping):
                tools.extend(_string_list(action.get("tool")))
    if not _string_list(tools):
        if _runtime_focus_unverified_target_app(tool_request, result):
            tools.extend(["app.open", "desktop.active_window"])
    if not _string_list(tools):
        tools.extend(_runtime_default_replan_fallback_tools(tool_name))
    if not _string_list(tools):
        tools.extend(_runtime_capability_replan_fallback_tools(tool_request))
    return _string_list(tools)


def _runtime_capability_replan_fallback_tools(
    tool_request: Mapping[str, Any],
) -> list[str]:
    capability_id = str(
        tool_request.get("target_capability_id") or tool_request.get("capability_id") or ""
    ).strip()
    return capability_recovery_tools(capability_id)


def _runtime_default_replan_fallback_tools(tool_name: str) -> list[str]:
    clean_tool = str(tool_name or "").strip()
    if clean_tool in {
        "app.open",
        "desktop.open_app",
        "media.music_app_open_and_play",
        "media.apple_music_open_and_play",
    }:
        return ["desktop.list_apps", "app.open", "desktop.active_window"]
    if clean_tool in {
        "app.focus",
        "desktop.focus_app",
        "app.focus_window",
        "app.show",
        "media.music_app_control",
        "media.system_control",
    }:
        return [
            "desktop.running_apps",
            "app.open",
            "desktop.active_window",
            "screen.capture",
        ]
    if clean_tool in {
        "desktop.active_window",
        "desktop.running_apps",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.verify",
        "screen.capture",
    }:
        return ["desktop.permissions", "screen.capture"]
    if clean_tool.startswith(("app.open_and_", "app.focus_and_")):
        return ["desktop.active_window", "desktop.ui_elements", "screen.capture"]
    if clean_tool in _FOREGROUND_READINESS_GATED_TOOLS:
        return ["desktop.active_window", "desktop.ui_elements", "screen.capture"]
    return []


def _runtime_default_replan_recovery_actions(
    tool_name: str,
    raw_input: Mapping[str, Any],
    fallback_tools: list[str],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    app_name = str(
        raw_input.get("app_name") or raw_input.get("target_app_name") or ""
    ).strip()
    query = str(raw_input.get("query") or raw_input.get("app_query") or app_name).strip()
    for fallback_tool in fallback_tools:
        action_input = _runtime_default_recovery_input(
            fallback_tool,
            raw_input,
            app_name=app_name,
            query=query,
        )
        if action_input is None:
            continue
        risk_level = _runtime_default_recovery_risk_level(fallback_tool)
        approval_required = _runtime_default_recovery_approval_required(fallback_tool)
        action: dict[str, Any] = {
            "label": _runtime_default_recovery_label(fallback_tool),
            "tool": fallback_tool,
            "input": action_input,
            "permission_target": _runtime_default_recovery_permission_target(
                fallback_tool
            ),
            "risk_level": risk_level,
            "observation_retry": {
                "tool": fallback_tool,
                "input": action_input,
                "reason": "tool_failure",
                "source_tool": tool_name,
            },
        }
        if approval_required:
            action["approval_required"] = True
        actions.append(
            action
        )
    return actions


def _runtime_default_recovery_input(
    fallback_tool: str,
    raw_input: Mapping[str, Any],
    *,
    app_name: str,
    query: str,
) -> dict[str, Any] | None:
    if fallback_tool == "desktop.list_apps":
        return {"query": query, "limit": 20} if query else {"limit": 20}
    if fallback_tool == "desktop.running_apps":
        return {}
    if fallback_tool == "browser.current_page":
        return {}
    if fallback_tool in {"workspace.read", "fs.read_file", "file.read"}:
        path = str(
            raw_input.get("path")
            or raw_input.get("file_path")
            or raw_input.get("source_path")
            or raw_input.get("data_source_hint")
            or ""
        ).strip()
        return {"path": path} if path else None
    if fallback_tool in {"app.open", "desktop.open_app", "app.focus", "desktop.focus_app"}:
        return {"app_name": app_name} if app_name else None
    if fallback_tool == "desktop.active_window":
        return {}
    if fallback_tool in {"desktop.ui_elements", "desktop.read_ui"}:
        payload: dict[str, Any] = {"limit": raw_input.get("limit", 80)}
        if app_name:
            payload["app_name"] = app_name
        role_filter = str(raw_input.get("role_filter") or "").strip()
        if role_filter:
            payload["role_filter"] = role_filter
        return payload
    if fallback_tool == "screen.capture":
        return {"reason": "recover failed desktop tool"}
    if fallback_tool == "desktop.permissions":
        return {}
    if fallback_tool == "python.run":
        code = _runtime_default_python_recovery_code(raw_input)
        return {"code": code} if code else None
    if fallback_tool == "terminal.run":
        command = _runtime_default_terminal_recovery_command(raw_input)
        return {"command": command} if command else None
    return None


def _runtime_default_python_recovery_code(raw_input: Mapping[str, Any]) -> str:
    explicit_code = _runtime_default_recovery_text_value(
        raw_input,
        "code",
        "python_code",
        "script",
    )
    if explicit_code:
        return explicit_code
    path = _runtime_default_recovery_text_value(
        raw_input,
        "path",
        "file_path",
        "source_path",
        "data_source_hint",
    )
    if not path:
        return ""
    return "\n".join(
        [
            "from pathlib import Path",
            "import pandas as pd",
            "",
            f"path = Path({path!r})",
            "suffix = path.suffix.lower()",
            "if suffix == '.csv':",
            "    df = pd.read_csv(path)",
            "elif suffix in {'.xlsx', '.xls'}:",
            "    df = pd.read_excel(path)",
            "elif suffix == '.json':",
            "    df = pd.read_json(path)",
            "else:",
            "    df = pd.read_csv(path)",
            "print('rows:', len(df), 'columns:', len(df.columns))",
            "print(df.head(20).to_string(index=False))",
            "print(df.describe(include='all').to_string())",
        ]
    )


def _runtime_default_terminal_recovery_command(raw_input: Mapping[str, Any]) -> str:
    return _runtime_default_recovery_text_value(
        raw_input,
        "command",
        "cmd",
        "shell_command",
    )


def _runtime_default_recovery_text_value(
    payload: Mapping[str, Any],
    *keys: str,
) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            text = value.strip()
            if text:
                return text
    return ""


def _runtime_default_recovery_label(tool_name: str) -> str:
    return {
        "desktop.list_apps": "Discover installed apps",
        "desktop.running_apps": "Inspect running apps",
        "app.open": "Open target app",
        "desktop.open_app": "Open target app",
        "desktop.active_window": "Observe active window",
        "desktop.ui_elements": "Inspect foreground UI",
        "desktop.read_ui": "Inspect foreground UI",
        "screen.capture": "Capture screen",
        "desktop.permissions": "Check desktop permissions",
        "python.run": "Run Python analysis fallback",
        "terminal.run": "Run terminal fallback",
    }.get(tool_name, "Run recovery tool")


def _runtime_default_recovery_permission_target(tool_name: str) -> str:
    if tool_name == "desktop.list_apps":
        return "app_discovery"
    if tool_name == "desktop.permissions":
        return "desktop_permissions"
    if tool_name == "app.open":
        return "app_launch"
    if tool_name in {"python.run", "terminal.run"}:
        return "terminal_execution"
    return "runtime_observation"


def _runtime_default_recovery_risk_level(tool_name: str) -> str:
    if tool_name in {"python.run", "terminal.run"}:
        return "high"
    return "low"


def _runtime_default_recovery_approval_required(tool_name: str) -> bool:
    return tool_name in {"python.run", "terminal.run"}


def _runtime_replan_result_preview(result: Mapping[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key in (
        "ok",
        "error",
        "status",
        "summary",
        "hint",
        "blocking_condition",
        "verification_failed",
        "verification_passed",
    ):
        value = result.get(key)
        if value not in (None, "", [], {}):
            preview[key] = value
    return preview


def _runtime_replan_input_preview(tool_request: Mapping[str, Any]) -> dict[str, Any]:
    value = tool_request.get("input")
    return dict(value) if isinstance(value, Mapping) else {}


def _runtime_replan_enrich_recovery_context(
    payload: dict[str, Any],
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    trigger: str,
    source_step_id: str,
    source_tool_name: str,
    input_preview: Mapping[str, Any],
    result_preview: Mapping[str, Any],
    failure_detail: str,
) -> None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    metadata = dict(metadata)
    payload["metadata"] = metadata

    for key in ("action_target", "observation_evidence", "observation_retry"):
        value = _first_mapping(tool_request.get(key), result.get(key), metadata.get(key))
        if value:
            payload[key] = dict(value)
            metadata[key] = dict(value)

    recovery_failure_metadata = _runtime_replan_recovery_failure_metadata(
        tool_request,
        result_preview=result_preview,
    )
    if recovery_failure_metadata:
        metadata.update(recovery_failure_metadata)
    execution_context = _runtime_replan_execution_context(tool_request, result)
    if execution_context:
        metadata.update(execution_context)

    target_reacquisition_action = _runtime_background_target_reacquisition_action(
        tool_request,
        result,
    )
    recovery_actions = (
        [target_reacquisition_action]
        if target_reacquisition_action
        else _runtime_replan_recovery_actions(tool_request, result)
    )
    if trigger == "verification_failed":
        verification_context = _runtime_replan_verification_failure_context(
            tool_request,
            source_step_id=source_step_id,
            source_tool_name=source_tool_name,
            input_preview=input_preview,
            result_preview=result_preview,
            failure_detail=failure_detail,
        )
        verification_targets = verification_context.get("verification_targets")
        if isinstance(verification_targets, list) and verification_targets:
            payload.setdefault("verification_targets", verification_targets)
            metadata.setdefault("verification_targets", verification_targets)
        for key in ("action_target", "observation_evidence", "observation_retry"):
            value = verification_context.get(key)
            if isinstance(value, Mapping) and value:
                payload.setdefault(key, dict(value))
                metadata.setdefault(key, dict(value))
        label = str(verification_context.get("recovery_action_label") or "").strip()
        if label:
            payload.setdefault("recovery_action_label", label)
            metadata.setdefault("recovery_action_label", label)
        for key in ("target_app_name", "target_app_query", "target_search_text"):
            value = str(verification_context.get(key) or "").strip()
            if value:
                payload.setdefault(key, value)
                metadata.setdefault(key, value)
        recovery_actions.extend(_mapping_list(verification_context.get("recovery_actions")))

    if not recovery_actions:
        recovery_actions.extend(
            _runtime_default_replan_recovery_actions(
                source_tool_name,
                input_preview,
                _string_list(payload.get("fallback_tools")),
            )
        )

    recovery_actions = _dedupe_runtime_replan_recovery_actions(recovery_actions)
    if recovery_actions:
        recovery_actions = [
            (
                _runtime_replan_recovery_action_with_execution_context(
                    action,
                    execution_context,
                )
                if _runtime_replan_recovery_action_requires_new_evidence(
                    tool_request,
                    action,
                )
                else _runtime_replan_recovery_action_with_auto_start_context(
                    _runtime_replan_recovery_action_with_execution_context(
                        action,
                        execution_context,
                    )
                )
            )
            for action in recovery_actions
        ]
        metadata["recovery_actions"] = recovery_actions


def _runtime_replan_recovery_action_requires_new_evidence(
    tool_request: Mapping[str, Any],
    action: Mapping[str, Any],
) -> bool:
    if str(action.get("tool") or "").strip() != "desktop.list_apps":
        return False
    request_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    return (
        str(request_input.get("selection_source") or "").strip()
        == "desktop.list_apps"
        and str(request_input.get("app_name") or "")
        .strip()
        .casefold()
        .startswith("<selected app from ")
    )


def _runtime_replan_execution_context(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in (
        "desktop_execution_policy",
        "desktop_execution_route",
        "sandbox_provider",
        "sandbox_desktop_provider",
        "desktop_loop",
    ):
        value = _first_mapping(tool_request.get(key), result.get(key))
        if value:
            context[key] = value
    session = _first_mapping(
        tool_request.get("desktop_provider_session"),
        result.get("desktop_provider_session"),
    )
    if session:
        public_session = _public_desktop_provider_session(session)
        if public_session:
            context["desktop_provider_session"] = public_session
    return context


def _runtime_replan_recovery_failure_metadata(
    tool_request: Mapping[str, Any],
    *,
    result_preview: Mapping[str, Any],
) -> dict[str, Any]:
    parent_request_id = str(tool_request.get("replan_request_id") or "").strip()
    if not parent_request_id:
        return {}
    request_input = (
        tool_request.get("input") if isinstance(tool_request.get("input"), Mapping) else {}
    )
    metadata: dict[str, Any] = {
        "replan_recovery_failed": True,
        "parent_replan_request_id": parent_request_id,
        "failed_recovery_tool": str(
            tool_request.get("tool") or tool_request.get("tool_name") or ""
        ).strip(),
        "failed_recovery_input": dict(request_input),
    }
    for key, value in (
        ("parent_replan_trigger", tool_request.get("replan_trigger")),
        (
            "failed_recovery_action_id",
            tool_request.get("replan_recovery_action_id") or tool_request.get("action_id"),
        ),
        ("failed_recovery_action_label", tool_request.get("recovery_action_label")),
        (
            "failed_recovery_step_id",
            tool_request.get("step_id") or tool_request.get("planner_step_id"),
        ),
        ("failed_recovery_source", tool_request.get("source")),
        (
            "failed_recovery_target_capability_id",
            tool_request.get("target_capability_id") or tool_request.get("capability_id"),
        ),
        ("original_source_step_id", tool_request.get("source_step_id")),
        ("original_source_tool_name", tool_request.get("source_tool_name")),
    ):
        text = str(value or "").strip()
        if text:
            metadata[key] = text
    replan_triggers = _string_list(tool_request.get("replan_triggers"))
    if replan_triggers:
        metadata["replan_triggers"] = replan_triggers
    replan_signal_ids = _string_list(tool_request.get("replan_signal_ids"))
    if replan_signal_ids:
        metadata["replan_signal_ids"] = replan_signal_ids
    verification_targets = _mapping_list(
        tool_request.get("verification_targets")
        or tool_request.get("task_verification_targets")
    )
    if verification_targets:
        metadata["failed_recovery_verification_targets"] = [
            dict(target) for target in verification_targets
        ]
    if result_preview:
        metadata["failed_recovery_result_preview"] = dict(result_preview)
    return {key: value for key, value in metadata.items() if value not in ("", [], {})}


def _runtime_replan_verification_failure_context(
    tool_request: Mapping[str, Any],
    *,
    source_step_id: str,
    source_tool_name: str,
    input_preview: Mapping[str, Any],
    result_preview: Mapping[str, Any],
    failure_detail: str,
) -> dict[str, Any]:
    verification_targets = _runtime_replan_verification_targets(tool_request)
    if not verification_targets:
        return {}
    target = verification_targets[0]
    action_target = _runtime_replan_verification_action_target(
        target,
        source_step_id=source_step_id,
        source_tool_name=source_tool_name,
        input_preview=input_preview,
    )
    observation_evidence = {
        "source_tool": source_tool_name,
        "source_step_id": source_step_id,
        "verification_failed": True,
        "input_preview": dict(input_preview),
        "result_preview": dict(result_preview),
    }
    if failure_detail:
        observation_evidence["failure_detail"] = failure_detail
    observation_retry = {
        "tool": source_tool_name,
        "input": dict(input_preview),
        "source_step_id": source_step_id,
        "reason": "verification_failed",
    }
    target_app_name = _runtime_replan_first_text(
        ("target_app_name", "app_name", "expected_app_name"),
        target,
        action_target,
        input_preview,
    )
    target_app_query = _runtime_replan_first_text(
        ("target_app_query", "app_query", "query"),
        target,
        action_target,
        input_preview,
    )
    target_search_text = _runtime_replan_first_text(
        ("target_search_text", "target", "selector", "text", "value"),
        target,
        action_target,
        input_preview,
    )
    recovery_actions: list[dict[str, Any]] = []
    focus_recovery = _runtime_focus_unverified_recovery_action(
        target_app_name=target_app_name,
        source_step_id=source_step_id,
        source_tool_name=source_tool_name,
        action_target=action_target,
        observation_evidence=observation_evidence,
        failure_detail=failure_detail,
    )
    if focus_recovery:
        recovery_actions.append(focus_recovery)
    if source_tool_name:
        recovery_action: dict[str, Any] = {
            "label": "Re-observe failed verification target",
            "tool": source_tool_name,
            "input": dict(input_preview),
            "permission_target": "runtime_verification",
            "risk_level": "low",
            "action_target": action_target,
            "observation_evidence": observation_evidence,
            "observation_retry": observation_retry,
        }
        recovery_actions.append(recovery_action)
    return {
        "verification_targets": verification_targets,
        "action_target": action_target,
        "observation_evidence": observation_evidence,
        "observation_retry": observation_retry,
        "recovery_action_label": "Re-observe failed verification target",
        "recovery_actions": recovery_actions,
        **({"target_app_name": target_app_name} if target_app_name else {}),
        **({"target_app_query": target_app_query} if target_app_query else {}),
        **({"target_search_text": target_search_text} if target_search_text else {}),
    }


def _runtime_focus_unverified_target_app(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> str:
    if not _runtime_result_has_condition(result, "foreground_focus_unverified"):
        return ""
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    verification_target = (
        tool_request.get("verification_target")
        if isinstance(tool_request.get("verification_target"), Mapping)
        else {}
    )
    action_target = (
        tool_request.get("action_target")
        if isinstance(tool_request.get("action_target"), Mapping)
        else {}
    )
    raw_input = (
        tool_request.get("input") if isinstance(tool_request.get("input"), Mapping) else {}
    )
    return _runtime_replan_first_text(
        ("app_name", "target_app_name", "expected_app_name"),
        verification_target,
        action_target,
        raw_input,
        result,
        data,
    )


def _runtime_result_has_condition(result: Mapping[str, Any], condition: str) -> bool:
    clean_condition = str(condition or "").strip()
    if not clean_condition:
        return False
    values = [
        result.get("error"),
        result.get("blocking_condition"),
        result.get("status"),
    ]
    values.extend(_string_list(result.get("blocking_conditions")))
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    values.extend(
        [
            data.get("error"),
            data.get("blocking_condition"),
            data.get("status"),
        ]
    )
    values.extend(_string_list(data.get("blocking_conditions")))
    return clean_condition in {str(value or "").strip() for value in values}


def _runtime_focus_unverified_recovery_action(
    *,
    target_app_name: str,
    source_step_id: str,
    source_tool_name: str,
    action_target: Mapping[str, Any],
    observation_evidence: Mapping[str, Any],
    failure_detail: str,
) -> dict[str, Any]:
    clean_app_name = str(target_app_name or "").strip()
    if not clean_app_name or "foreground_focus_unverified" not in str(failure_detail or ""):
        return {}
    verify_request = {
        "tool": "desktop.active_window",
        "input": {},
        "source": "runtime_replan_recovery",
        "planning_reason": "planner_replan_verify_foreground_focus",
        "replan_triggers": ["verification_failed"],
        "verification_target": {
            "app_name": clean_app_name,
            **({"source_tool": source_tool_name} if source_tool_name else {}),
        },
    }
    if source_step_id:
        verify_request["step_id"] = source_step_id
        verify_request["planner_step_id"] = source_step_id
    return {
        "label": "Bring expected app to foreground",
        "tool": "app.open",
        "input": {"app_name": clean_app_name},
        "permission_target": "app_launch",
        "risk_level": "low",
        "selected": True,
        "action_target": dict(action_target),
        "observation_evidence": dict(observation_evidence),
        "observation_retry": {
            "tool": "app.open",
            "input": {"app_name": clean_app_name},
            "reason": "foreground_focus_unverified",
            "source_tool": source_tool_name,
        },
        "deferred_continuation": [verify_request],
    }


def _runtime_replan_verification_targets(
    tool_request: Mapping[str, Any],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for target in _mapping_list(tool_request.get("task_verification_targets")):
        step_id = str(target.get("step_id") or "").strip()
        todo = target.get("todo") if isinstance(target.get("todo"), Mapping) else {}
        checkpoints = _mapping_list(target.get("checkpoints"))
        snapshot: dict[str, Any] = {}
        if step_id:
            snapshot["step_id"] = step_id
        todo_id = str(todo.get("todo_id") or "").strip()
        if todo_id:
            snapshot["todo_id"] = todo_id
        todo_title = str(todo.get("title") or "").strip()
        if todo_title:
            snapshot["todo_title"] = todo_title
        tool_name = str(todo.get("tool_name") or todo.get("tool") or "").strip()
        if tool_name:
            snapshot["tool_name"] = tool_name
        checkpoint_ids = [
            str(checkpoint.get("checkpoint_id") or "").strip()
            for checkpoint in checkpoints
            if str(checkpoint.get("checkpoint_id") or "").strip()
        ]
        if checkpoint_ids:
            snapshot["checkpoint_ids"] = checkpoint_ids
        checkpoint_titles = [
            str(checkpoint.get("title") or "").strip()
            for checkpoint in checkpoints
            if str(checkpoint.get("title") or "").strip()
        ]
        if checkpoint_titles:
            snapshot["checkpoint_titles"] = checkpoint_titles
        if snapshot:
            targets.append(snapshot)
    explicit_target = _runtime_replan_explicit_verification_target(tool_request)
    if explicit_target:
        targets.append(explicit_target)
    return targets


def _runtime_replan_explicit_verification_target(
    tool_request: Mapping[str, Any],
) -> dict[str, Any]:
    verification_target = (
        tool_request.get("verification_target")
        if isinstance(tool_request.get("verification_target"), Mapping)
        else {}
    )
    input_preview = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    if not verification_target and not any(
        str(tool_request.get(key) or "").strip()
        for key in ("target_app_name", "target_app_query", "target_search_text")
    ):
        return {}
    app_name = _runtime_replan_first_text(
        ("app_name", "target_app_name", "expected_app_name"),
        verification_target,
        tool_request,
        input_preview,
    )
    app_query = _runtime_replan_first_text(
        ("target_app_query", "app_query", "query"),
        verification_target,
        tool_request,
        input_preview,
    )
    search_text = _runtime_replan_first_text(
        ("target_search_text", "target", "selector", "text", "value"),
        verification_target,
        tool_request,
        input_preview,
    )
    if not any((app_name, app_query, search_text)):
        return {}
    snapshot: dict[str, Any] = {"kind": "desktop_verification_target"}
    step_id = str(
        tool_request.get("step_id") or tool_request.get("planner_step_id") or ""
    ).strip()
    if step_id:
        snapshot["step_id"] = step_id
    tool_name = str(tool_request.get("tool") or tool_request.get("tool_name") or "").strip()
    if tool_name:
        snapshot["tool_name"] = tool_name
    if app_name:
        snapshot["app_name"] = app_name
        snapshot["target_app_name"] = app_name
    if app_query:
        snapshot["app_query"] = app_query
        snapshot["target_app_query"] = app_query
    if search_text:
        snapshot["target_search_text"] = search_text
    source_tool = str(verification_target.get("source_tool") or "").strip()
    if source_tool:
        snapshot["source_tool"] = source_tool
    return snapshot


def _runtime_replan_verification_action_target(
    target: Mapping[str, Any],
    *,
    source_step_id: str,
    source_tool_name: str,
    input_preview: Mapping[str, Any],
) -> dict[str, Any]:
    action_target: dict[str, Any] = {
        "kind": str(target.get("kind") or "task_verification_target").strip(),
        "action": "verify_after_action",
        "verified_by_step_id": source_step_id,
        "verification_tool": source_tool_name,
    }
    for key in ("step_id", "todo_id", "todo_title", "tool_name"):
        value = str(target.get(key) or "").strip()
        if value:
            action_target[key] = value
    for key in ("checkpoint_ids", "checkpoint_titles"):
        values = _string_list(target.get(key))
        if values:
            action_target[key] = values
    for source_key, target_key in (
        ("app_name", "app_name"),
        ("target_app_name", "app_name"),
        ("query", "app_query"),
        ("app_query", "app_query"),
        ("target", "target"),
        ("selector", "target"),
        ("text", "text"),
        ("value", "text"),
    ):
        value = str(target.get(source_key) or input_preview.get(source_key) or "").strip()
        if value and not str(action_target.get(target_key) or "").strip():
            action_target[target_key] = value
    return {key: value for key, value in action_target.items() if value not in ("", [], {})}


def _runtime_replan_first_text(
    keys: tuple[str, ...],
    *sources: Mapping[str, Any],
) -> str:
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for key in keys:
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _runtime_replan_recovery_actions(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> list[dict[str, Any]]:
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    actions: list[dict[str, Any]] = []
    for source in (
        tool_request.get("recovery_actions"),
        result.get("recovery_actions"),
        data.get("recovery_actions"),
    ):
        actions.extend(_mapping_list(source))
    observation_retry_action = _runtime_observation_retry_recovery_action(
        observation_retry=_first_mapping(
            tool_request.get("observation_retry"),
            result.get("observation_retry"),
            data.get("observation_retry"),
        ),
        action_target=_first_mapping(
            tool_request.get("action_target"),
            result.get("action_target"),
            data.get("action_target"),
        ),
        observation_evidence=_first_mapping(
            tool_request.get("observation_evidence"),
            result.get("observation_evidence"),
            data.get("observation_evidence"),
        ),
    )
    if observation_retry_action:
        actions.append(observation_retry_action)
    return [dict(action) for action in actions]


_RUNTIME_BACKGROUND_TARGET_REACQUISITION_ERRORS = {
    "cua_background_target_identity_required",
    "cua_background_target_identity_mismatch",
}

_RUNTIME_TARGET_REACQUISITION_RETRY_TOOLS = {
    "desktop.active_window",
    "desktop.inspect_app",
    "desktop.list_windows",
    "desktop.read_ui",
    "desktop.ui_elements",
    "desktop.verify",
}


def _runtime_background_target_reacquisition_action(
    tool_request: Mapping[str, Any],
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Reopen a stale task-owned target without repairing a healthy provider."""

    transport = _first_mapping(result.get("desktop_execution_provider_transport"))
    if not (
        result.get("ok") is False
        and result.get("error") in _RUNTIME_BACKGROUND_TARGET_REACQUISITION_ERRORS
        and result.get("blocked_by_desktop_execution_provider") is not True
        and result.get("blocked_by_desktop_target") is True
        and result.get("target_reacquisition_required") is True
        and result.get("retryable") is True
        and result.get("requires_user_handoff") is not True
        and transport.get("provider_kind") == "background_desktop"
        and transport.get("delivery_mode") == "background"
        and transport.get("foreground_takeover_required") is False
    ):
        return {}
    request_input = (
        tool_request.get("input")
        if isinstance(tool_request.get("input"), Mapping)
        else {}
    )
    app_name = str(
        request_input.get("app_name")
        or request_input.get("target_app_name")
        or request_input.get("expected_app_name")
        or ""
    ).strip()
    source_tool = str(
        tool_request.get("tool") or tool_request.get("tool_name") or ""
    ).strip()
    if not app_name or not source_tool or source_tool in {"app.open", "desktop.open_app"}:
        return {}

    action: dict[str, Any] = {
        "label": "Reopen the isolated background app",
        "tool": "app.open",
        "input": {"app_name": app_name},
        "permission_target": "app_launch",
        "risk_level": "low",
        "planning_reason": "desktop_target_reacquisition",
        "recovery_action_kind": "desktop_target_reacquisition",
        "allow_repeat_after_success": True,
    }
    if source_tool in _RUNTIME_TARGET_REACQUISITION_RETRY_TOOLS:
        continuation: dict[str, Any] = {
            "tool": source_tool,
            "input": dict(request_input),
            "source": "runtime_target_reacquisition_retry",
            "planning_reason": "retry_after_desktop_target_reacquisition",
            "requires_observation": True,
        }
        for key in ("runtime_stage", "runtime_role"):
            value = str(tool_request.get(key) or "").strip()
            if value:
                continuation[key] = value
        action["deferred_continuation"] = [continuation]
    return action


_RUNTIME_OBSERVATION_RETRY_TOOLS = {
    "desktop.list_apps",
    "desktop.active_window",
    "desktop.running_apps",
    "desktop.windows",
    "desktop.list_windows",
    "desktop.ui_elements",
    "desktop.read_ui",
    "desktop.verify",
    "screen.capture",
}

_RUNTIME_REPLAN_AUTO_SAFE_TOOLS = {
    "app.focus",
    "app.open",
    "browser.current_page",
    "desktop.active_window",
    "desktop.focus_app",
    "desktop.list_apps",
    "desktop.open_app",
    "desktop.running_apps",
    "desktop.windows",
    "file.read",
    "fs.read_file",
    "workspace.read",
}

_RUNTIME_REPLAN_NO_REPEAT_AFTER_SUCCESS_TOOLS = {
    "app.open",
    "desktop.open_app",
}


def _runtime_replan_deferred_auto_safe_tools(
    source: Mapping[str, Any],
) -> set[str]:
    tools = set(_RUNTIME_REPLAN_AUTO_SAFE_TOOLS)
    if (
        source.get("recovery_action_kind") == "desktop_target_reacquisition"
        and source.get("allow_repeat_after_success") is True
    ):
        # Only retry read-only, target-bound observations.  Mutations still
        # return to the model after the fresh background instance is opened.
        tools.update(_RUNTIME_TARGET_REACQUISITION_RETRY_TOOLS)
    return tools


def _runtime_observation_retry_recovery_action(
    *,
    observation_retry: Mapping[str, Any],
    action_target: Mapping[str, Any],
    observation_evidence: Mapping[str, Any],
) -> dict[str, Any]:
    tool_name = _runtime_observation_retry_tool(observation_retry)
    if not tool_name:
        return {}
    retry_input = (
        observation_retry.get("input")
        if isinstance(observation_retry.get("input"), Mapping)
        else {}
    )
    action: dict[str, Any] = {
        "label": _runtime_observation_retry_label(tool_name, observation_retry),
        "tool": tool_name,
        "input": dict(retry_input),
        "permission_target": _runtime_observation_retry_permission_target(tool_name),
        "risk_level": "low",
        "observation_retry": dict(observation_retry),
    }
    if action_target:
        action["action_target"] = dict(action_target)
    if observation_evidence:
        action["observation_evidence"] = dict(observation_evidence)
    return action


def _runtime_observation_retry_tool(observation_retry: Mapping[str, Any]) -> str:
    tool_name = str(
        observation_retry.get("tool")
        or observation_retry.get("from_tool")
        or observation_retry.get("source_tool")
        or ""
    ).strip()
    if tool_name not in _RUNTIME_OBSERVATION_RETRY_TOOLS:
        return ""
    return tool_name


def _runtime_observation_retry_label(
    tool_name: str,
    observation_retry: Mapping[str, Any],
) -> str:
    reason = str(observation_retry.get("reason") or "").strip()
    if tool_name == "desktop.list_apps" or reason == "resolve_desktop_app":
        return "Re-run desktop app discovery"
    if reason == "verification_failed":
        return "Re-run runtime observation"
    return "Run runtime observation retry"


def _runtime_observation_retry_permission_target(tool_name: str) -> str:
    if tool_name == "desktop.list_apps":
        return "app_discovery"
    return "runtime_observation"


def _dedupe_runtime_replan_recovery_actions(
    actions: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for action in actions:
        tool_name = str(action.get("tool") or "").strip()
        if not tool_name:
            continue
        action_input = action.get("input") if isinstance(action.get("input"), Mapping) else {}
        signature = (tool_name, repr(sorted(dict(action_input).items())))
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(dict(action))
    return deduped


def _dedupe_runtime_replan_recovery_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str] | tuple[str, str]] = set()
    for request in requests:
        if not isinstance(request, dict):
            continue
        tool_name = str(request.get("tool") or "").strip()
        if not tool_name:
            continue
        raw_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        request_id = str(request.get("replan_request_id") or "").strip()
        identity = ensure_recovery_action_identity(request)
        signature = (
            ("recovery", identity)
            if identity
            else (tool_name, repr(sorted(dict(raw_input).items())), request_id)
        )
        if signature in seen:
            continue
        seen.add(signature)
        deduped.append(request)
    return deduped


def _runtime_replan_recovery_action_with_execution_context(
    action: Mapping[str, Any],
    execution_context: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(action)
    if not execution_context:
        return payload
    metadata = (
        dict(payload.get("metadata"))
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    for key, value in execution_context.items():
        if not isinstance(value, Mapping) or not value:
            continue
        copied = dict(value)
        payload.setdefault(key, copied)
        metadata.setdefault(key, copied)
    if metadata:
        payload["metadata"] = metadata
    return payload


def _runtime_replan_recovery_action_with_auto_start_context(
    action: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(action)
    metadata = (
        dict(payload.get("metadata"))
        if isinstance(payload.get("metadata"), Mapping)
        else {}
    )
    auto_start = _runtime_replan_recovery_action_auto_start_context(payload)
    metadata["runtime_replan_auto_start_eligible"] = auto_start["eligible"]
    metadata["runtime_replan_auto_start_reason"] = auto_start["reason"]
    metadata["runtime_replan_auto_start_blockers"] = list(auto_start["blockers"])
    payload["metadata"] = metadata
    return payload


def _runtime_replan_recovery_action_auto_start_context(
    action: Mapping[str, Any],
) -> dict[str, Any]:
    blockers: list[str] = []
    tool_name = str(action.get("tool") or action.get("tool_name") or "").strip()
    risk_level = str(action.get("risk_level") or "").strip().lower()
    approval_required = bool(action.get("approval_required")) or str(
        action.get("approval_status") or ""
    ).strip().lower() in {
        "pending",
        "required",
        "approval_required",
        "waiting_approval",
    }
    if not tool_name:
        blockers.append("missing_tool")
    if approval_required:
        blockers.append("approval_required")
    if risk_level in {"high", "critical"}:
        blockers.append("high_risk")
    if tool_name and tool_name not in _RUNTIME_REPLAN_AUTO_SAFE_TOOLS:
        blockers.append("tool_not_auto_safe")
    return {
        "eligible": not blockers,
        "reason": (
            "safe_low_risk_runtime_replan_recovery"
            if not blockers
            else "manual_runtime_replan_recovery_required"
        ),
        "blockers": blockers,
    }


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, Mapping) and value:
            return dict(value)
    return {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _runtime_replan_context_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key
        in {
            "run_group_id",
            "group_run_id",
            "group_id",
            "workflow_id",
            "workflow_run_id",
            "workflow_node_id",
            "workflow_node_label",
            "core_id",
            "workspace_id",
            "task_id",
        }
        and value not in (None, "", [], {})
    }


def _runtime_replan_request_exists(
    timeline: list[dict[str, Any]],
    payload: Mapping[str, Any],
) -> bool:
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id:
        return False
    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        if _runtime_replan_base_event_type(event_type) != "agent.replan.requested":
            continue
        event_payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else event
        if str(event_payload.get("request_id") or "").strip() == request_id:
            return True
    return False


def _runtime_replan_request_id(
    *,
    decision_id: str,
    plan_id: str,
    source_step_id: str,
    source_tool_name: str,
    trigger: str,
) -> str:
    parts = [
        value.replace(":", "_")
        for value in (decision_id, plan_id, source_step_id, source_tool_name, trigger)
        if value
    ]
    return "runtime-replan:" + ":".join(parts or ["request"])


_FOREGROUND_READINESS_GATED_TOOLS = {
    "app.open_and_safe_type_text",
    "app.focus_and_safe_type_text",
    "app.open_and_safe_shortcut",
    "app.focus_and_safe_shortcut",
    "app.open_and_safe_key",
    "app.focus_and_safe_key",
    "app.open_and_safe_scroll",
    "app.focus_and_safe_scroll",
    "app.open_and_safe_click",
    "app.focus_and_safe_click",
    "app.open_and_click_ui_element",
    "app.focus_and_click_ui_element",
    "app.open_and_type_into_ui_element",
    "app.focus_and_type_into_ui_element",
    "app.open_and_hotkey",
    "app.focus_and_hotkey",
    "desktop.safe_shortcut",
    "desktop.safe_key",
    "desktop.safe_scroll",
    "desktop.safe_click",
    "desktop.safe_type_text",
    "desktop.search_submit",
    "desktop.hotkey",
    "desktop.click_ui_element",
    "desktop.type_into_ui_element",
    "desktop.submit_foreground",
    "desktop.click",
    "desktop.type_text",
}

_DESKTOP_POST_ACTION_VERIFICATION_TOOLS = {
    "app.show",
    "app.hide",
    "app.minimize",
    "app.quit",
    "desktop.open_path",
    "desktop.reveal_path",
    "desktop.hide_app",
    "desktop.show_all_apps",
    "desktop.minimize_window",
    "desktop.close_window",
    "desktop.quit_app",
}

_FOREGROUND_READINESS_RESET_TOOLS = {
    "app.open",
    "app.focus",
    "app.focus_window",
    "app.show",
    "desktop.list_apps",
    "desktop.active_window",
    "desktop.inspect_app",
}


def _runtime_readiness_skip_result(
    tool_name: str,
    raw_input: dict[str, Any],
    blocker: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if blocker is None or tool_name not in _FOREGROUND_READINESS_GATED_TOOLS:
        return None
    blocked_app = str(blocker.get("app_name") or "").strip()
    requested_app = str(raw_input.get("app_name") or "").strip()
    if blocked_app and requested_app and requested_app != blocked_app:
        return None
    conditions = _string_list(blocker.get("blocking_conditions")) or ["foreground_not_ready"]
    recovery_actions = blocker.get("recovery_actions")
    recommended_tools = blocker.get("recommended_tools")
    result: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        "blocked_by_runtime_readiness": True,
        "tool": tool_name,
        "action": tool_name,
        "error": conditions[0],
        "blocking_condition": conditions[0],
        "blocking_conditions": conditions,
        "source_tool": "desktop.inspect_app",
        "source_summary": str(blocker.get("summary") or "").strip(),
        "hint": (
            "desktop.inspect_app did not prove the target app is ready for foreground "
            "input. Run one of the recovery actions or inspect again before mutating UI."
        ),
        "data": {
            "app_name": blocked_app,
            "requested_app_name": str(blocker.get("requested_app_name") or "").strip(),
            "skipped_tool": tool_name,
            "skipped_input": raw_input,
            "readiness_checks": blocker.get("checks") if isinstance(blocker.get("checks"), dict) else {},
        },
    }
    if isinstance(recovery_actions, list) and recovery_actions:
        result["recovery_actions"] = recovery_actions
    if isinstance(recommended_tools, list) and recommended_tools:
        result["recommended_tools"] = recommended_tools
    return result


def _remaining_request_can_handle_foreground_readiness(
    blocker: dict[str, Any] | None,
    remaining_requests: list[dict[str, Any]],
) -> bool:
    if blocker is None:
        return False
    for request in remaining_requests:
        if not isinstance(request, dict):
            continue
        tool_name = str(request.get("tool") or "").strip()
        raw_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        if _runtime_readiness_skip_result(tool_name, raw_input, blocker) is not None:
            return True
        if _request_may_clear_foreground_readiness(blocker, tool_name, raw_input):
            return True
    return False


def _request_may_clear_foreground_readiness(
    blocker: dict[str, Any],
    tool_name: str,
    raw_input: dict[str, Any],
) -> bool:
    if tool_name not in _FOREGROUND_READINESS_RESET_TOOLS:
        return False
    if tool_name == "desktop.active_window":
        return True
    if tool_name == "desktop.list_apps":
        query = str(raw_input.get("query") or "").strip()
        return not query or _name_matches_blocked_app(blocker, query)
    app_name = str(raw_input.get("app_name") or "").strip()
    return not app_name or _name_matches_blocked_app(blocker, app_name)


def _unresolved_discovered_app_skip_result(
    tool_name: str,
    raw_input: dict[str, Any],
    app_name_resolution: dict[str, str],
) -> dict[str, Any] | None:
    if app_name_resolution:
        return None
    requested_app = _selected_discovered_app_requested_name(raw_input)
    if not requested_app:
        return None
    selection_source = (
        _desktop_app_selection_source(raw_input.get("selection_source"))
        or _selected_desktop_app_placeholder_source(raw_input.get("app_name"))
        or _DESKTOP_APP_SELECTION_SOURCE
    )
    source_label = (
        "running app"
        if selection_source == _DESKTOP_RUNNING_APP_SELECTION_SOURCE
        else "installed app"
    )
    recovery_input = (
        {}
        if selection_source == _DESKTOP_RUNNING_APP_SELECTION_SOURCE
        else {"query": requested_app, "limit": 20}
    )
    result: dict[str, Any] = {
        "ok": False,
        "skipped": True,
        "blocked_by_app_resolution": True,
        "tool": tool_name,
        "action": tool_name,
        "error": "app_resolution_failed",
        "blocking_condition": "app_resolution_failed",
        "blocking_conditions": ["app_resolution_failed"],
        "source_tool": selection_source,
        "source_summary": f"No {source_label} was selected for {requested_app}.",
        "hint": (
            f"{selection_source} did not return a high-confidence app match. "
            "Discover the app again or ask the user to choose a candidate before executing."
        ),
        "data": {
            "requested_app_name": requested_app,
            "selection_source": selection_source,
            "skipped_tool": tool_name,
            "skipped_input": raw_input,
        },
        "recommended_tools": [selection_source],
        "recovery_actions": [
            {
                "label": "重新发现应用",
                "tool": selection_source,
                "input": recovery_input,
                "permission_target": "app_discovery",
                "risk_level": "low",
            }
        ],
    }
    return result


def _unresolved_workspace_file_skip_result(
    tool_name: str,
    raw_input: dict[str, Any],
    file_resolution: dict[str, Any],
) -> dict[str, Any] | None:
    if file_resolution:
        return None
    if not _tool_request_uses_selected_workspace_file(raw_input):
        return None
    requested_path = str(
        raw_input.get("path")
        or raw_input.get("target_path")
        or raw_input.get("file_path")
        or _SELECTED_WORKSPACE_FILE_PATH
    ).strip()
    source_scope = str(raw_input.get("source_scope") or raw_input.get("source_path") or "").strip()
    recovery_input: dict[str, Any] = {"path": source_scope or "."}
    pattern = str(raw_input.get("pattern") or "").strip()
    if pattern:
        recovery_input["pattern"] = pattern
    source_kind = str(raw_input.get("source_kind") or raw_input.get("file_type") or "").strip()
    if source_kind:
        recovery_input["file_type"] = source_kind
    if str(raw_input.get("selection") or "").strip():
        recovery_input["include_metadata"] = True
    return {
        "ok": False,
        "skipped": True,
        "blocked_by_file_resolution": True,
        "tool": tool_name,
        "action": tool_name,
        "error": "file_resolution_failed",
        "blocking_condition": "file_resolution_failed",
        "blocking_conditions": ["file_resolution_failed"],
        "source_tool": "workspace.list",
        "source_summary": "No workspace file was selected from workspace.list.",
        "hint": (
            "workspace.list did not return a unique usable file candidate for this step. "
            "List the target directory again or ask the user to choose a candidate before executing."
        ),
        "data": {
            "requested_path": requested_path,
            "selection_source": "workspace.list",
            "skipped_tool": tool_name,
            "skipped_input": raw_input,
        },
        "recommended_tools": ["workspace.list"],
        "recovery_actions": [
            {
                "label": "重新列出候选文件",
                "tool": "workspace.list",
                "input": recovery_input,
                "permission_target": "workspace_discovery",
                "risk_level": "low",
            }
        ],
    }


def _selected_discovered_app_requested_name(raw_input: dict[str, Any]) -> str:
    raw_app_name = str(raw_input.get("app_name") or "").strip()
    selection_source = _desktop_app_selection_source(raw_input.get("selection_source"))
    if not _selected_desktop_app_placeholder_source(raw_app_name) and not selection_source:
        return ""
    return str(raw_input.get("query") or raw_app_name or "").strip()


def _broker_requires_approval(broker: Any, tool_name: str) -> bool:
    approvals = getattr(broker, "approvals", None)
    if not isinstance(approvals, dict):
        return False
    return bool(approvals.get(str(tool_name or "").strip()))


def _broker_tool_precondition_failure(
    broker: Any,
    tool_name: str,
) -> dict[str, Any] | None:
    preflight = getattr(broker, "tool_precondition_failure", None)
    if not callable(preflight):
        return None
    result = preflight(str(tool_name or "").strip())
    return dict(result) if isinstance(result, Mapping) else None


def _authoritative_permission_preflight_block(
    tool_requests: list[dict[str, Any]],
    broker: Any,
) -> tuple[int, dict[str, Any]] | None:
    """Fail a foreground batch before its first affected atomic action.

    Only a fresh result obtained from the live broker is authority.  Request,
    model, and persisted-event metadata are deliberately ignored here.
    """

    preflight = getattr(broker, "desktop_permission_preflight", None)
    if not callable(preflight) or not tool_requests:
        return None
    try:
        raw_result = preflight()
    except Exception:
        # An unavailable passive cache read cannot manufacture a denial.  Each
        # concrete tool still retains its own fail-closed provider/policy gate.
        return None
    if not isinstance(raw_result, Mapping):
        return None
    result = dict(raw_result)
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if (
        result.get("ok") is not True
        or str(result.get("action") or "").strip()
        != "desktop.permission_preflight"
        or result.get("permission_error") is not True
    ):
        return None
    permission_targets = _ordered_unique_strings(
        [
            *_string_list(result.get("permission_targets")),
            *_string_list(result.get("missing_permissions")),
            *_string_list(data.get("permission_targets")),
            *_string_list(data.get("missing_permissions")),
        ]
    )
    affected_tools = _ordered_unique_strings(
        [
            *_string_list(result.get("affected_tools")),
            *_string_list(data.get("affected_tools")),
        ]
    )
    if not permission_targets or not affected_tools:
        return None
    recovery_actions = [
        dict(action)
        for action in (
            result.get("recovery_actions")
            if isinstance(result.get("recovery_actions"), list)
            else data.get("recovery_actions")
            if isinstance(data.get("recovery_actions"), list)
            else []
        )
        if isinstance(action, Mapping)
    ]
    impacted_indices = _permission_preflight_impacted_request_indices(
        tool_requests,
        affected_tools,
        recovery_actions=recovery_actions,
    )
    if not impacted_indices:
        return None
    blocked_index = min(impacted_indices)
    blocked_request = tool_requests[blocked_index]
    blocked_tool = str(
        blocked_request.get("tool") or blocked_request.get("tool_name") or ""
    ).strip()
    blocked_input = (
        dict(blocked_request.get("input"))
        if isinstance(blocked_request.get("input"), Mapping)
        else {}
    )
    blocking_conditions = _ordered_unique_strings(
        [
            *_string_list(result.get("blocking_conditions")),
            *_string_list(data.get("blocking_conditions")),
            "desktop_permission_required",
        ]
    )
    recovery_hints = _ordered_unique_strings(
        [
            *_string_list(result.get("recovery_hints")),
            *_string_list(data.get("recovery_hints")),
        ]
    )
    diagnostic_route = str(
        result.get("diagnostic_route")
        or data.get("diagnostic_route")
        or "/yachiyo/readiness"
    ).strip()
    blocker: dict[str, Any] = {
        "ok": False,
        "status": "blocked",
        "skipped": True,
        "blocked_by_permission_preflight": True,
        "permission_error": True,
        "user_handoff_required": True,
        "replan_allowed": False,
        "tool": blocked_tool,
        "action": blocked_tool,
        "error": blocking_conditions[0],
        "blocking_condition": blocking_conditions[0],
        "blocking_conditions": blocking_conditions,
        "permission_targets": permission_targets,
        "affected_tools": affected_tools,
        "source_tool": "desktop.permission_preflight",
        "source_summary": str(result.get("summary") or "").strip(),
        "diagnostic_route": diagnostic_route,
        "data": {
            "ready": False,
            "skipped_tool": blocked_tool,
            "skipped_input": blocked_input,
            "permission_targets": permission_targets,
            "affected_tools": affected_tools,
            "diagnostic_route": diagnostic_route,
        },
    }
    if recovery_hints:
        blocker["recovery_hints"] = recovery_hints
    if recovery_actions:
        blocker["recovery_actions"] = recovery_actions
    return blocked_index, blocker


def _permission_preflight_impacted_request_indices(
    tool_requests: list[dict[str, Any]],
    affected_tools: list[str],
    *,
    recovery_actions: list[dict[str, Any]],
) -> set[int]:
    request_tools = [
        str(request.get("tool") or request.get("tool_name") or "").strip()
        for request in tool_requests
    ]
    impacted: set[int] = set()
    for affected_tool in affected_tools:
        exact = {
            index
            for index, request_tool in enumerate(request_tools)
            if request_tool == affected_tool
            and not _permission_request_is_exact_recovery_action(
                tool_requests[index],
                recovery_actions,
            )
        }
        impacted.update(exact)
        activation_tools, mutation_tools = _split_foreground_permission_tools(
            affected_tool
        )
        if not activation_tools or not mutation_tools:
            continue
        activation_indices = {
            index
            for index, request_tool in enumerate(request_tools)
            if request_tool in activation_tools
        }
        mutation_indices = {
            index
            for index, request_tool in enumerate(request_tools)
            if request_tool in mutation_tools
        }
        # A combined affected capability maps to an atomic plan only when both
        # halves are present.  This avoids blocking an unrelated standalone
        # app open merely because another flow would later type into that app.
        if activation_indices and mutation_indices:
            impacted.update(activation_indices)
            impacted.update(mutation_indices)
    return impacted


def _split_foreground_permission_tools(
    affected_tool: str,
) -> tuple[frozenset[str], frozenset[str]]:
    clean_tool = str(affected_tool or "").strip()
    prefixes = {
        "app.open_and_": frozenset({"app.open", "desktop.open_app"}),
        "app.focus_and_": frozenset({"app.focus", "desktop.focus_app"}),
    }
    for prefix, activation_tools in prefixes.items():
        if not clean_tool.startswith(prefix):
            continue
        suffix = clean_tool[len(prefix) :]
        if not suffix:
            return frozenset(), frozenset()
        return activation_tools, frozenset({f"desktop.{suffix}"})
    return frozenset(), frozenset()


def _permission_request_is_exact_recovery_action(
    request: Mapping[str, Any],
    recovery_actions: list[dict[str, Any]],
) -> bool:
    request_tool = str(
        request.get("tool") or request.get("tool_name") or ""
    ).strip()
    request_input = (
        dict(request.get("input"))
        if isinstance(request.get("input"), Mapping)
        else {}
    )
    return any(
        request_tool == str(action.get("tool") or "").strip()
        and isinstance(action.get("input"), Mapping)
        and request_input == dict(action["input"])
        for action in recovery_actions
    )


def _ordered_unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _tool_result_with_active_window_verification_target(
    tool_name: str,
    tool_result: dict[str, Any],
    verification_target: dict[str, Any],
) -> dict[str, Any]:
    if tool_name != "desktop.active_window":
        return tool_result
    expected_app = str(verification_target.get("app_name") or "").strip()
    if not expected_app or tool_result.get("ok") is not True:
        return tool_result
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    active_app = str(data.get("app_name") or data.get("frontmost_app") or "").strip()
    verified = _app_lookups_same_identity(active_app, expected_app)
    updated_data = {
        **data,
        "expected_app_name": expected_app,
        "active_app_name": active_app,
        "focus_verified": verified,
    }
    if verified:
        return {**tool_result, "data": updated_data}
    return {
        **tool_result,
        "ok": False,
        "error": "foreground_focus_unverified",
        "verification_failed": True,
        "blocking_condition": "foreground_focus_unverified",
        "blocking_conditions": ["foreground_focus_unverified"],
        "expected_app_name": expected_app,
        "active_app_name": active_app,
        "hint": (
            "The active window does not match the app that was just opened or focused. "
            "Focus the expected app again or inspect windows before continuing."
        ),
        "data": updated_data,
    }


def _active_window_target_from_tool_result(
    tool_name: str,
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_result.get("ok") is not True or tool_result.get("approval_required"):
        return None
    if not _tool_can_change_active_app(tool_name):
        return None
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    app_name = str(
        data.get("app_name")
        or data.get("discovered_app_name")
        or raw_input.get("app_name")
        or ""
    ).strip()
    if not app_name:
        return None
    return {"app_name": app_name, "source_tool": tool_name}


def _tool_can_change_active_app(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool in {
            "app.open",
            "app.focus",
            "app.focus_window",
            "desktop.open_app",
            "desktop.focus_app",
            "desktop.open_path_with_app",
            "app.open_path_with_app",
        }
        or clean_tool.startswith("app.open_and_")
        or clean_tool.startswith("app.focus_and_")
    )


def _app_names_match(left: str, right: str) -> bool:
    return _app_lookups_related(left, right)


def _foreground_readiness_blocker(
    tool_name: str,
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name != "desktop.inspect_app":
        return None
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    if tool_result.get("ok") is True and data.get("ready_for_foreground_action") is True:
        return None
    conditions = _inspect_app_blocking_conditions(tool_result, data, checks)
    return {
        "app_name": str(data.get("app_name") or raw_input.get("app_name") or "").strip(),
        "requested_app_name": str(
            data.get("requested_app_name") or raw_input.get("app_name") or ""
        ).strip(),
        "summary": str(tool_result.get("summary") or "").strip(),
        "blocking_conditions": conditions,
        "checks": checks,
        "recommended_tools": (
            tool_result.get("recommended_tools")
            if isinstance(tool_result.get("recommended_tools"), list)
            else data.get("recommended_tools")
        ),
        "recovery_actions": (
            tool_result.get("recovery_actions")
            if isinstance(tool_result.get("recovery_actions"), list)
            else data.get("recovery_actions")
        ),
    }


def _updated_foreground_readiness_blocker(
    blocker: dict[str, Any] | None,
    tool_name: str,
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any] | None:
    inspect_blocker = _foreground_readiness_blocker(tool_name, raw_input, tool_result)
    if inspect_blocker is not None:
        return inspect_blocker
    if tool_name == "desktop.inspect_app":
        return None
    if not _clears_foreground_readiness_blocker(blocker, tool_name, raw_input, tool_result):
        return blocker
    return None


def _foreground_readiness_recovered_payload(
    blocker: dict[str, Any],
    tool_name: str,
    input_preview: dict[str, Any],
    tool_result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tool": tool_name,
        "recovery_tool": tool_name,
        "input_preview": input_preview,
        "status": "recovered",
        "app_name": str(blocker.get("app_name") or "").strip(),
        "requested_app_name": str(blocker.get("requested_app_name") or "").strip(),
        "blocking_conditions": _string_list(blocker.get("blocking_conditions")),
        "source_tool": str(blocker.get("source_tool") or "desktop.inspect_app").strip(),
        "source_summary": str(blocker.get("summary") or "").strip(),
        "result_summary": str(tool_result.get("summary") or "").strip(),
    }


def _clears_foreground_readiness_blocker(
    blocker: dict[str, Any] | None,
    tool_name: str,
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> bool:
    if blocker is None or tool_name not in _FOREGROUND_READINESS_RESET_TOOLS:
        return False
    if tool_result.get("ok") is not True:
        return False
    if tool_name == "desktop.list_apps":
        return _list_apps_result_resolves_blocker(blocker, raw_input, tool_result)
    if tool_name == "desktop.active_window":
        return _active_window_result_resolves_blocker(blocker, tool_result)
    return _tool_app_name_matches_blocker(blocker, raw_input, tool_result)


def _list_apps_result_resolves_blocker(
    blocker: dict[str, Any],
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> bool:
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    apps = data.get("apps") if isinstance(data.get("apps"), list) else []
    if not apps:
        return False
    query = str(data.get("query") or raw_input.get("query") or "").strip()
    return _name_matches_blocked_app(blocker, query)


def _active_window_result_resolves_blocker(
    blocker: dict[str, Any],
    tool_result: dict[str, Any],
) -> bool:
    conditions = set(_string_list(blocker.get("blocking_conditions")))
    if conditions - {"foreground_focus_unverified", "foreground_not_ready"}:
        return False
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    active_name = str(data.get("app_name") or data.get("frontmost_app") or "").strip()
    return _name_matches_blocked_app(blocker, active_name)


def _tool_app_name_matches_blocker(
    blocker: dict[str, Any],
    raw_input: dict[str, Any],
    tool_result: dict[str, Any],
) -> bool:
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    app_name = str(
        data.get("app_name")
        or data.get("discovered_app_name")
        or raw_input.get("app_name")
        or ""
    ).strip()
    return _name_matches_blocked_app(blocker, app_name)


def _name_matches_blocked_app(blocker: dict[str, Any], app_name: str) -> bool:
    clean_name = str(app_name or "").strip().casefold()
    if not clean_name:
        return False
    blocked_names = {
        str(blocker.get("app_name") or "").strip().casefold(),
        str(blocker.get("requested_app_name") or "").strip().casefold(),
    }
    return clean_name in {name for name in blocked_names if name}


def _inspect_app_blocking_conditions(
    tool_result: dict[str, Any],
    data: dict[str, Any],
    checks: dict[str, Any],
) -> list[str]:
    conditions: list[str] = []
    if tool_result.get("ok") is False and str(tool_result.get("error") or "").strip():
        conditions.append(str(tool_result.get("error") or "").strip())
    if data.get("app_found") is False or checks.get("discovered_app") is False:
        conditions.append("app_not_found")
    if data.get("running") is False or checks.get("status_running") is False:
        conditions.append("app_not_running")
    if data.get("focus_verified") is False or checks.get("focus_verified") is False:
        conditions.append("foreground_focus_unverified")
    if data.get("visibility_limited") is True:
        conditions.append("foreground_visibility_limited")
    if checks.get("ui_query_ok") is False:
        conditions.append("ui_inspection_failed")
    if data.get("ui_element_count") == 0 or checks.get("named_ui_elements_nonempty") is False:
        conditions.append("ui_elements_empty")
    if data.get("control_like_count") == 0 or checks.get("control_like_ui_visible") is False:
        conditions.append("no_actionable_controls")
    if checks.get("ready_for_foreground_action") is False:
        conditions.append("foreground_not_ready")
    return _string_list(conditions) or ["foreground_not_ready"]


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        value = [value] if value not in (None, "") else []
    items: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"1", "true", "yes", "on"}:
            return True
        if clean in {"0", "false", "no", "off"}:
            return False
    return None
