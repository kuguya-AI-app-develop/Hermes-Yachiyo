"""Runtime execution envelopes derived from planner decisions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from apps.shell.agent.runtime.dispatch_semantics import is_semantic_safe_shortcut
from apps.shell.agent.runtime.action_targets import (
    action_target_matches,
    bind_planned_action_target,
    canonical_action_name,
    canonical_action_target,
)
from apps.shell.agent.runtime.desktop_execution_providers import (
    LOCAL_DESKTOP_PROVIDER_KIND,
    local_desktop_execution_provider_status,
)

from .contracts import (
    DesktopExecutionLoopSnapshot,
    DesktopExecutionRouteSnapshot,
    PlannerDecisionSnapshot,
    RuntimeCheckpointPolicySnapshot,
    RuntimeExecutionEnvelopeSnapshot,
    RuntimeExecutionRequestSnapshot,
    SandboxDesktopProviderSnapshot,
    ToolPlanStepSnapshot,
)
from .desktop_execution_policy import (
    desktop_foreground_provider_route_requested,
    desktop_readonly_provider_route_requested,
    desktop_execution_route_decision,
    is_readonly_desktop_provider_tool,
    sandbox_desktop_provider_status,
    sandbox_desktop_provider_can_execute_tool,
)
from .app_name_hints import legacy_app_name_hint
from .discovered_app_followups import (
    discovered_app_click_followup_target_from_planned_requests,
)
from .planner_execution import (
    planner_desktop_observation_step_needs_model_followup,
    planner_full_plan_execution_tool_requests,
    planner_tool_requests_for_decision,
)
from .policy import desktop_tool_execution_mode_for_input
from .task_progress_snapshots import task_progress_summary_from_task_core

_NON_EXECUTABLE_REQUEST_STATUSES = {
    "blocked",
    "cancelled",
    "canceled",
    "completed",
    "denied",
    "expired",
    "failed",
    "rejected",
    "recovered",
    "skipped",
    "unavailable",
}

RUNTIME_EXECUTION_READINESS_READY = "ready"
RUNTIME_EXECUTION_READINESS_DEFERRED = "deferred"
RUNTIME_EXECUTION_READINESS_BLOCKED = "blocked"
_DEFERRED_RUNTIME_EXECUTION_READINESS_STATUSES = {
    "installed_not_checked",
    "not_checked",
    "unknown",
}
_DEFERRED_RUNTIME_EXECUTION_ROUTE_BLOCKERS = {
    # A background adapter exists, but planning deliberately did not perform
    # the liveness probe. The execution boundary must revalidate it before use.
    "sandbox_desktop_provider_required",
}
_CONFIRMED_RUNTIME_EXECUTION_BLOCK_STATUSES = {
    "cancelled",
    "canceled",
    "denied",
    "expired",
    "failed",
    "permission_required",
    "rejected",
    "skipped",
}


def runtime_execution_readiness_state(value: Any) -> str:
    """Classify execution readiness without treating an unprobed route as denied.

    Planning is deliberately side-effect free, so an installed background
    provider may reach this boundary before its first health probe.  Only the
    three explicit indeterminate statuses are deferred; contradictory or
    confirmed blocking conditions remain fail-closed.
    """

    if not isinstance(value, Mapping):
        return RUNTIME_EXECUTION_READINESS_READY

    nested_route = value.get("desktop_execution_route")
    if isinstance(nested_route, Mapping):
        outer_status = _runtime_execution_readiness_token(value.get("status"))
        outer_blocked_by = {
            _runtime_execution_readiness_token(item)
            for item in _runtime_execution_readiness_values(value.get("blocked_by"))
        }
        outer_blocking_conditions = {
            _runtime_execution_readiness_token(item)
            for key in ("blocking_condition", "blocking_conditions")
            for item in _runtime_execution_readiness_values(value.get(key))
        }
        if (
            outer_status in _CONFIRMED_RUNTIME_EXECUTION_BLOCK_STATUSES
            or outer_blocked_by.difference(
                {"", *_DEFERRED_RUNTIME_EXECUTION_READINESS_STATUSES}
            )
            or outer_blocking_conditions.difference(
                {"", *_DEFERRED_RUNTIME_EXECUTION_READINESS_STATUSES}
            )
        ):
            return RUNTIME_EXECUTION_READINESS_BLOCKED
        if _request_has_deferred_background_provider(value, nested_route):
            return RUNTIME_EXECUTION_READINESS_DEFERRED
        return runtime_execution_readiness_state(nested_route)

    if value.get("can_execute") is True:
        return RUNTIME_EXECUTION_READINESS_READY

    explicit_readiness_tokens = {
        _runtime_execution_readiness_token(item)
        for key in (
            "readiness_status",
            "provider_readiness_status",
            "provider_status",
            "blocked_by",
        )
        for item in _runtime_execution_readiness_values(value.get(key))
    }
    explicit_readiness_tokens.discard("")
    status_tokens = {
        _runtime_execution_readiness_token(item)
        for item in _runtime_execution_readiness_values(value.get("status"))
    }
    status_tokens.discard("")
    blocking_conditions = {
        _runtime_execution_readiness_token(item)
        for key in ("blocking_condition", "blocking_conditions")
        for item in _runtime_execution_readiness_values(value.get(key))
    }
    blocking_conditions.discard("")
    if (explicit_readiness_tokens | status_tokens).intersection(
        _DEFERRED_RUNTIME_EXECUTION_READINESS_STATUSES
    ):
        if blocking_conditions.difference(
            {
                *_DEFERRED_RUNTIME_EXECUTION_READINESS_STATUSES,
                *_DEFERRED_RUNTIME_EXECUTION_ROUTE_BLOCKERS,
            }
        ):
            return RUNTIME_EXECUTION_READINESS_BLOCKED
        return RUNTIME_EXECUTION_READINESS_DEFERRED

    if (
        value.get("can_execute") is False
        or status_tokens.intersection(_NON_EXECUTABLE_REQUEST_STATUSES)
        or blocking_conditions
        or explicit_readiness_tokens.difference(
            {"ready", "available", "executable", "healthy"}
        )
    ):
        return RUNTIME_EXECUTION_READINESS_BLOCKED
    return RUNTIME_EXECUTION_READINESS_READY


def _request_has_deferred_background_provider(
    request: Mapping[str, Any],
    route: Mapping[str, Any],
) -> bool:
    """Recognize older envelopes that stored readiness beside the route."""

    if str(route.get("selected_provider_kind") or "").strip() != "background_desktop":
        return False
    if route.get("can_execute") is True:
        return False
    blockers = {
        _runtime_execution_readiness_token(item)
        for item in _runtime_execution_readiness_values(
            route.get("blocking_conditions")
        )
    }
    blockers.discard("")
    if blockers.difference(_DEFERRED_RUNTIME_EXECUTION_ROUTE_BLOCKERS):
        return False
    provider = request.get("sandbox_provider") or request.get(
        "sandbox_desktop_provider"
    )
    if not isinstance(provider, Mapping):
        return False
    provider_kind = str(provider.get("provider_kind") or "").strip()
    if provider_kind and provider_kind != "background_desktop":
        return False
    route_provider_id = str(route.get("selected_provider_id") or "").strip()
    provider_id = str(provider.get("provider_id") or "").strip()
    if route_provider_id and provider_id and route_provider_id != provider_id:
        return False
    return (
        runtime_execution_readiness_state(provider)
        == RUNTIME_EXECUTION_READINESS_DEFERRED
    )


def _runtime_execution_readiness_values(value: Any) -> list[Any]:
    if value in (None, ""):
        return []
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return list(value)
    return [value]


def _runtime_execution_readiness_token(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def runtime_execution_envelope_from_decision(
    decision: PlannerDecisionSnapshot | None,
    *,
    allowed_tools: Iterable[str] | None = None,
    direct: bool = False,
    full_plan: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> RuntimeExecutionEnvelopeSnapshot | None:
    if decision is None:
        return None
    clean_allowed = _allowed_tools(decision, allowed_tools)
    request_payloads = (
        planner_full_plan_execution_tool_requests(
            _full_plan_tool_requests_from_decision(
                decision,
                clean_allowed,
                metadata=metadata,
            ),
            clean_allowed,
        )
        if full_plan and _supports_full_plan_projection(decision)
        else planner_tool_requests_for_decision(
            decision,
            clean_allowed,
            direct=direct,
            execution_normalized=True,
            metadata=metadata,
        )
    )
    steps = _steps_by_id(decision)
    requests: list[RuntimeExecutionRequestSnapshot] = []
    for index, request in enumerate(request_payloads, start=1):
        requests.append(
            _execution_request_snapshot(
                request,
                index=index,
                decision=decision,
                steps=steps,
                previous_requests=requests,
            )
        )
    requests = _execution_requests_with_internal_dependencies(
        requests,
        block_missing_dependencies=full_plan,
    )
    tool_plan = decision.plan.tool_plan
    runtime_metadata = _execution_envelope_runtime_metadata(requests, decision)
    sandbox_provider = _sandbox_provider_for_envelope(requests)
    desktop_execution_route = _desktop_execution_route_for_envelope(requests)
    desktop_provider_session = _desktop_provider_session_for_envelope(
        requests,
        metadata=metadata,
    )
    return RuntimeExecutionEnvelopeSnapshot(
        envelope_id=f"execution-envelope-{decision.plan.plan_id}",
        decision_id=decision.decision_id,
        plan_id=decision.plan.plan_id,
        intent_kind=str(decision.selected_intent.kind or ""),
        capability_plan=decision.plan.capability_plan,
        execution_strategy=decision.plan.execution_strategy,
        requests=requests,
        task_core=decision.plan.task_core,
        task_progress=task_progress_summary_from_task_core(
            decision.plan.task_core,
            desktop_provider_session=desktop_provider_session,
        ),
        approvals_required=list(tool_plan.approvals_required),
        artifacts_expected=list(tool_plan.artifacts_expected),
        open_questions=list(tool_plan.open_questions),
        route_to_studio=bool(decision.plan.route_to_studio),
        sandbox_provider=sandbox_provider,
        desktop_execution_route=desktop_execution_route,
        desktop_provider_session=desktop_provider_session,
        runtime_doctrine=runtime_metadata["runtime_doctrine"],
        runtime_stage_counts=runtime_metadata["runtime_stage_counts"],
        replan_signal_count=runtime_metadata["replan_signal_count"],
    )


def _execution_requests_with_internal_dependencies(
    requests: Iterable[RuntimeExecutionRequestSnapshot],
    *,
    block_missing_dependencies: bool = False,
) -> list[RuntimeExecutionRequestSnapshot]:
    """Keep subset projections usable without erasing full-plan blockers."""

    items = list(requests)
    included_step_ids = {
        str(request.step_id or "").strip()
        for request in items
        if str(request.step_id or "").strip()
    }
    normalized: list[RuntimeExecutionRequestSnapshot] = []
    for request in items:
        if block_missing_dependencies:
            missing_dependencies = [
                dependency
                for dependency in request.depends_on
                if str(dependency or "").strip() not in included_step_ids
            ]
            normalized.append(
                request.model_copy(update={"status": "blocked"})
                if missing_dependencies
                else request
            )
            continue
        depends_on = [
            dependency
            for dependency in request.depends_on
            if str(dependency or "").strip() in included_step_ids
        ]
        normalized.append(
            request
            if depends_on == request.depends_on
            else request.model_copy(update={"depends_on": depends_on})
        )
    return normalized


def runtime_execution_envelope_payload(
    decision: PlannerDecisionSnapshot | None,
    *,
    allowed_tools: Iterable[str] | None = None,
    direct: bool = False,
    full_plan: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        direct=direct,
        full_plan=full_plan,
        metadata=metadata,
    )
    if envelope is None:
        return {}
    return envelope.model_dump(mode="json")


def runtime_execution_envelope_payload_with_request_context(
    envelope: Mapping[str, Any],
    context: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(envelope)
    request_context = _execution_request_context(context)
    task_workspace_context = _task_workspace_context(context)
    if not request_context and not task_workspace_context:
        return payload

    requests = payload.get("requests")
    if request_context and isinstance(requests, list):
        payload["requests"] = [
            _execution_request_with_context(request, request_context)
            if isinstance(request, Mapping)
            else request
            for request in requests
        ]
    if task_workspace_context:
        payload = _execution_envelope_task_core_with_context(
            payload,
            task_workspace_context,
        )
    return payload


def _supports_full_plan_projection(decision: PlannerDecisionSnapshot) -> bool:
    return str(decision.selected_intent.kind or "").strip() in {
        "clipboard_operation",
        "code_task",
        "communication",
        "data_analysis",
        "desktop_operation",
        "file_access",
        "file_operation",
        "file_organization",
        "information_capture",
        "media_playback",
        "multi_agent",
        "report_generation",
        "schedule",
        "system_control",
        "web_research",
        "workflow_orchestration",
    }


def _full_plan_tool_requests_from_decision(
    decision: PlannerDecisionSnapshot,
    allowed_tools: set[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    request_metadata = _runtime_request_metadata_from_metadata(metadata)
    for step in list(decision.plan.tool_plan.steps or []):
        tool_name = _text(getattr(step, "tool_name", None))
        if not tool_name or tool_name not in allowed_tools:
            continue
        status = _text(getattr(step, "status", None)) or "planned"
        approval_required = bool(getattr(step, "approval_required", False))
        if status in {"unavailable", "skipped"} and not approval_required:
            continue
        input_preview = getattr(step, "input_preview", None)
        raw_request_input = (
            dict(input_preview) if isinstance(input_preview, Mapping) else {}
        )
        request_input = _executable_request_input(tool_name, raw_request_input)
        step_id = _text(getattr(step, "step_id", None))
        capability_id = _text(getattr(step, "capability_id", None))
        request: dict[str, Any] = {
            "protocol": "json_fallback",
            "tool": tool_name,
            "input": request_input,
            "source": "runtime_planner",
            "planning_reason": f"planner_full_plan_{decision.selected_intent.kind}",
            "approval_required": approval_required,
            "status": status,
        }
        if tool_name in {
            "app.focus_and_safe_shortcut",
            "app.open_and_safe_shortcut",
        }:
            request["presentation"] = "summary"
        intent_inputs = getattr(decision.selected_intent, "inputs", None)
        intent_presentation = _text(
            intent_inputs.get("presentation")
            if isinstance(intent_inputs, Mapping)
            else None
        )
        if intent_presentation and tool_name in {
            "browser.search",
            "browser.current_page",
            "browser.extract",
            "browser.extract_text",
            "browser.open_url_and_extract_text",
        }:
            # The authoritative execution envelope must preserve user-facing
            # result presentation chosen by the planner. Otherwise replacing a
            # fallback request with the envelope silently loses summary mode.
            request["presentation"] = intent_presentation
            if (
                intent_presentation == "summary"
                and isinstance(intent_inputs, Mapping)
                and _text(intent_inputs.get("query"))
            ):
                # Search-result summaries need model synthesis; single-page
                # summaries can still use the deterministic direct presenter.
                request["continue_to_model"] = True
        if step_id:
            request["step_id"] = step_id
            request["planner_step_id"] = step_id
        depends_on = [
            _text(dependency)
            for dependency in list(getattr(step, "depends_on", None) or [])
            if _text(dependency)
        ]
        if depends_on:
            request["depends_on"] = depends_on
        if capability_id:
            request["capability_id"] = capability_id
        request.update(request_metadata)
        runtime_stage = _text(
            _task_core_step_runtime_metadata(decision, step_id).get("runtime_stage")
        )
        desktop_observation_followup = (
            planner_desktop_observation_step_needs_model_followup(
                decision,
                step_id,
                tool_name,
            )
            if decision.selected_intent.kind == "desktop_operation"
            else None
        )
        if runtime_stage == "verify":
            request["source"] = "runtime_verification"
            request["runtime_stage"] = "verify"
            # Exact clipboard readback is evaluated locally against the
            # source write's immutable content and lineage.  It is a terminal
            # verifier, not model context.  Planner-classified desktop
            # verifiers likewise remain local unless their observation needs
            # semantic interpretation.
            if (
                tool_name != "clipboard.read"
                and desktop_observation_followup is not False
            ):
                request["continue_to_model"] = True
        if desktop_observation_followup is True:
            request["continue_to_model"] = True
        if step_id == "verify-foreground-search-result":
            request["continue_to_model"] = False
        if step_id.startswith("verify-clipboard-paste-"):
            # Exact private clipboard readback is evaluated by Runtime, before
            # exposing any subsequent send approval or asking for a model.
            request["continue_to_model"] = False
        if _request_needs_model_materialization(tool_name, raw_request_input):
            request["continue_to_model"] = True
        if step_id.startswith(("inspect-typed-draft-", "verify-typed-draft-")):
            request["continue_to_model"] = False
        requests.append(request)
    return requests


def _executable_request_input(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    clean_tool = _text(tool_name)
    payload = dict(request_input)
    if clean_tool in {"workspace.read", "fs.read_file", "file.read"}:
        path = _text(payload.get("path"))
        if not path:
            return {}
        payload["path"] = path
        return payload
    return payload


def _request_needs_model_materialization(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> bool:
    if tool_name == "notes.create":
        return bool(request_input.get("body_source")) and not str(
            request_input.get("body") or ""
        ).strip()
    if tool_name == "artifact.write":
        if str(request_input.get("content") or "").strip():
            return False
        return bool(
            request_input.get("body_source")
            or request_input.get("path")
            or request_input.get("paths")
        )
    if tool_name == "workspace.write_patch":
        if any(
            str(request_input.get(key) or "").strip()
            for key in ("patch", "diff", "content")
        ):
            return False
        if any(request_input.get(key) for key in ("changes", "edits", "operations")):
            return False
        return bool(
            request_input.get("patch_source")
            or request_input.get("diff_source")
            or request_input.get("body_source")
            or request_input.get("mode")
        )
    if tool_name in {"terminal.run", "python.run"}:
        return _command_request_needs_model_materialization(tool_name, request_input)
    if tool_name in {
        "app.focus_and_safe_type_text",
        "app.focus_and_type_into_ui_element",
        "app.open_and_safe_type_text",
        "app.open_and_type_into_ui_element",
        "desktop.safe_type_text",
        "desktop.type",
        "desktop.type_into_ui_element",
        "desktop.type_text",
    }:
        return bool(request_input.get("body_source")) and not str(
            request_input.get("text") or ""
        ).strip()
    return False


def _command_request_needs_model_materialization(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> bool:
    command = str(request_input.get("command") or "").strip()
    code = str(request_input.get("code") or "").strip()
    if tool_name == "python.run" and code:
        return False
    if command and not _command_looks_like_planner_placeholder(command):
        return False
    if _command_looks_like_planner_placeholder(command):
        return True
    return any(
        request_input.get(key)
        for key in (
            "body_source",
            "file_type",
            "operation",
            "path",
            "paths",
            "pattern",
            "query",
            "selection",
            "source",
            "source_path",
        )
    )


def _command_looks_like_planner_placeholder(command: str) -> bool:
    value = str(command or "").strip().lower()
    if not value:
        return False
    return any(
        marker in value
        for marker in (
            "# analyze captured tabular data",
            "# inspect data, compute summary, generate charts",
            "# inspect data",
            "todo:",
            "<model",
            "<generated",
        )
    )


def _runtime_request_metadata_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    payload: dict[str, Any] = {}
    if _metadata_truthy(
        metadata,
        "desktop_provider_health_probe",
        "probe_desktop_provider_health",
        "sandbox_provider_health_probe",
    ):
        payload["desktop_provider_health_probe"] = True
    if _metadata_truthy(
        metadata,
        "desktop_provider_route_readonly",
        "desktop_provider_readonly_route",
        "route_readonly_desktop_provider",
    ):
        payload["desktop_provider_route_readonly"] = True
    if _metadata_truthy(
        metadata,
        "desktop_provider_route_foreground",
        "desktop_provider_foreground_route",
        "route_foreground_desktop_provider",
    ):
        payload["desktop_provider_route_foreground"] = True
    if _metadata_truthy(
        metadata,
        "desktop_provider_local_native",
        "desktop_provider_local",
        "local_desktop_provider",
    ):
        payload["desktop_provider_local_native"] = True
    if _metadata_truthy(
        metadata,
        "allow_user_foreground_takeover",
        "desktop_allow_user_foreground_takeover",
        "allow_nonisolated_desktop_provider",
    ):
        payload["allow_user_foreground_takeover"] = True
    for policy_key in (
        "desktop_execution_policy",
        "yachiyo_desktop_execution_policy",
        "desktop_interaction_policy",
    ):
        policy = metadata.get(policy_key)
        if isinstance(policy, Mapping):
            payload["desktop_execution_policy"] = dict(policy)
            break
        if isinstance(policy, str) and policy.strip():
            payload["desktop_execution_policy"] = {"mode": policy.strip()}
            break
    provider = metadata.get("sandbox_provider") or metadata.get(
        "sandbox_desktop_provider"
    )
    if isinstance(provider, Mapping):
        payload["sandbox_provider"] = dict(provider)
    desktop_provider_session = _desktop_provider_session_from_metadata(metadata)
    if desktop_provider_session:
        payload["desktop_provider_session"] = desktop_provider_session
    return payload


def _desktop_provider_session_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    session = metadata.get("desktop_provider_session")
    if isinstance(session, Mapping):
        return dict(session)
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _desktop_provider_session_from_metadata(nested_metadata)
    return {}


def _metadata_truthy(
    metadata: Mapping[str, Any] | None,
    *keys: str,
) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    for key in keys:
        value = metadata.get(key)
        if value is True:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"}:
            return True
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _metadata_truthy(nested_metadata, *keys)
    return False


_EXECUTION_REQUEST_CONTEXT_KEYS = {
    "workspace_id",
    "group_run_id",
    "run_group_id",
    "group_id",
    "workflow_run_id",
    "workflow_id",
    "workflow_node_id",
    "workflow_node_label",
    "workflow_node_kind",
}

_TASK_WORKSPACE_CONTEXT_KEYS = {
    "task_id",
    "run_id",
    "agent_id",
    "group_run_id",
    "run_group_id",
    "group_id",
    "workflow_run_id",
    "workflow_id",
    "workflow_node_id",
    "workflow_node_label",
    "workflow_node_kind",
}


def _execution_request_context(context: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(context, Mapping):
        return {}
    return {
        key: text
        for key in _EXECUTION_REQUEST_CONTEXT_KEYS
        if (text := _text(context.get(key)))
    }


def _task_workspace_context(context: Mapping[str, Any] | None) -> dict[str, str]:
    if not isinstance(context, Mapping):
        return {}
    return {
        key: text
        for key in _TASK_WORKSPACE_CONTEXT_KEYS
        if (text := _text(context.get(key)))
    }


def _execution_request_with_context(
    request: Mapping[str, Any],
    context: Mapping[str, str],
) -> dict[str, Any]:
    payload = dict(request)
    for key, value in context.items():
        if not _text(payload.get(key)):
            payload[key] = value
    return payload


def _execution_envelope_task_core_with_context(
    envelope: Mapping[str, Any],
    context: Mapping[str, str],
) -> dict[str, Any]:
    task_core = envelope.get("task_core")
    if not isinstance(task_core, Mapping):
        return dict(envelope)
    workspace = task_core.get("workspace")
    if not isinstance(workspace, Mapping):
        return dict(envelope)
    workspace_context = (
        dict(workspace.get("context"))
        if isinstance(workspace.get("context"), Mapping)
        else {}
    )
    updated_context = {
        **context,
        **{
            str(key): value
            for key, value in workspace_context.items()
            if str(key).strip()
        },
    }
    return {
        **dict(envelope),
        "task_core": {
            **dict(task_core),
            "workspace": {
                **dict(workspace),
                "context": updated_context,
            },
        },
    }


def runtime_execution_requests_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if not isinstance(metadata, Mapping):
        return []
    return runtime_execution_requests_from_envelope_payload(
        metadata.get("yachiyo_execution_envelope"),
        allowed_tools=allowed_tools,
    )


def runtime_execution_requests_from_envelope_payload(
    envelope_payload: Any,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(envelope_payload, RuntimeExecutionEnvelopeSnapshot):
        envelope = envelope_payload.model_dump(mode="json")
    elif isinstance(envelope_payload, Mapping):
        envelope = dict(envelope_payload)
    else:
        return []

    allowed = {
        str(tool or "").strip()
        for tool in (allowed_tools or [])
        if str(tool or "").strip()
    }
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return []
    projected: list[dict[str, Any]] = []
    non_executable_step_ids = _non_executable_runtime_step_ids(
        requests,
        envelope=envelope,
        allowed_tools=allowed,
    )
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        if (
            _request_status_is_non_executable(request)
            or _request_desktop_route_is_non_executable(request)
            or step_id in non_executable_step_ids
            or _request_depends_on_non_executable_step(
                request,
                non_executable_step_ids,
            )
        ):
            continue
        projected_request = _tool_request_from_execution_request(request, envelope=envelope)
        tool_name = str(projected_request.get("tool") or "").strip()
        if not tool_name:
            continue
        if allowed and tool_name not in allowed:
            continue
        projected.append(projected_request)
    return _project_discovered_app_semantic_click_followup(
        projected,
        requests,
    )


def _project_discovered_app_semantic_click_followup(
    projected: list[dict[str, Any]],
    planned_requests: list[Any],
) -> list[dict[str, Any]]:
    if len(projected) != 1:
        return projected
    request = projected[0]
    if str(request.get("tool") or "").strip() != "desktop.list_apps":
        return projected
    request_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    app_query = str(request_input.get("query") or "").strip()
    followup_target = discovered_app_click_followup_target_from_planned_requests(
        app_query,
        planned_requests,
    )
    if not followup_target:
        return projected
    return [
        {
            **request,
            "continue_to_model": True,
            "followup_target": followup_target,
        }
    ]


def runtime_execution_continuation_requests_from_envelope_payload(
    envelope_payload: Any,
    *,
    after_request: Mapping[str, Any] | None,
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Project planned requests after an approved envelope request."""

    if isinstance(envelope_payload, RuntimeExecutionEnvelopeSnapshot):
        envelope = envelope_payload.model_dump(mode="json")
    elif isinstance(envelope_payload, Mapping):
        envelope = dict(envelope_payload)
    else:
        return []
    if not isinstance(after_request, Mapping):
        return []
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return []
    allowed = {
        str(tool or "").strip()
        for tool in (allowed_tools or [])
        if str(tool or "").strip()
    }
    current_request_id = _text(after_request.get("request_id"))
    current_step_id = _text(
        after_request.get("step_id") or after_request.get("planner_step_id")
    )
    current_tool = _text(after_request.get("tool") or after_request.get("tool_name"))
    current_index = -1
    for index, request in enumerate(requests):
        if not isinstance(request, Mapping):
            continue
        request_id = _text(request.get("request_id"))
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        tool_name = _text(request.get("tool_name") or request.get("tool"))
        if current_request_id and request_id == current_request_id:
            current_index = index
            break
        if current_step_id and step_id == current_step_id and tool_name == current_tool:
            current_index = index
            break
    if current_index < 0:
        return []

    projected: list[dict[str, Any]] = []
    non_executable_step_ids = _non_executable_runtime_step_ids(
        requests,
        envelope=envelope,
        allowed_tools=allowed,
    )
    for request in requests[current_index + 1 :]:
        if not isinstance(request, Mapping):
            continue
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        if (
            _request_status_is_non_executable(request)
            or _request_desktop_route_is_non_executable(request)
            or step_id in non_executable_step_ids
            or _request_depends_on_non_executable_step(
                request,
                non_executable_step_ids,
            )
        ):
            continue
        tool_request = _tool_request_from_execution_request(request, envelope=envelope)
        tool_name = _text(tool_request.get("tool"))
        if not tool_name or (allowed and tool_name not in allowed):
            continue
        projected.append(tool_request)
    return projected


def runtime_execution_blocked_requests_from_envelope_payload(
    envelope_payload: Any,
    *,
    allowed_tools: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    if isinstance(envelope_payload, RuntimeExecutionEnvelopeSnapshot):
        envelope = envelope_payload.model_dump(mode="json")
    elif isinstance(envelope_payload, Mapping):
        envelope = dict(envelope_payload)
    else:
        return []

    allowed = {
        str(tool or "").strip()
        for tool in (allowed_tools or [])
        if str(tool or "").strip()
    }
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return []
    non_executable_step_ids = _non_executable_runtime_step_ids(
        requests,
        envelope=envelope,
        allowed_tools=allowed,
    )
    projected: list[dict[str, Any]] = []
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        if not (
            _request_status_is_non_executable(request)
            or _request_desktop_route_is_non_executable(request)
            or step_id in non_executable_step_ids
            or _request_depends_on_non_executable_step(
                request,
                non_executable_step_ids,
            )
        ):
            continue
        projected_request = _tool_request_from_execution_request(request, envelope=envelope)
        tool_name = str(projected_request.get("tool") or "").strip()
        if not tool_name:
            continue
        if allowed and tool_name not in allowed:
            continue
        route = (
            projected_request.get("desktop_execution_route")
            if isinstance(projected_request.get("desktop_execution_route"), Mapping)
            else {}
        )
        projected.append(
            {
                **projected_request,
                "status": "blocked",
                "blocked_by_runtime_readiness": True,
                "blocked_by": str(
                    route.get("status")
                    or request.get("status")
                    or "runtime_dependency_unavailable"
                ),
                "policy_reason": str(
                    route.get("reason")
                    or projected_request.get("policy_reason")
                    or "Desktop execution route is not currently executable."
                ),
            }
        )
    return projected


def _non_executable_runtime_step_ids(
    requests: list[Any],
    *,
    envelope: Mapping[str, Any],
    allowed_tools: set[str],
) -> set[str]:
    mapped_requests = [request for request in requests if isinstance(request, Mapping)]
    requests_by_step: dict[str, list[Mapping[str, Any]]] = {}
    positions_by_step: dict[str, list[int]] = {}
    for index, request in enumerate(mapped_requests):
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        if step_id:
            requests_by_step.setdefault(step_id, []).append(request)
            positions_by_step.setdefault(step_id, []).append(index)

    blocked: set[str] = set()
    for index, request in enumerate(mapped_requests):
        step_id = _text(request.get("step_id") or request.get("planner_step_id"))
        for dependency in _string_list(request.get("depends_on")):
            dependency_positions = positions_by_step.get(dependency, [])
            if dependency_positions and any(
                dependency_index < index
                for dependency_index in dependency_positions
            ):
                continue
            # Missing, self, forward, and cyclic dependencies make the
            # connected execution chain untrustworthy. Block both ends so a
            # corrupted envelope cannot run a later prerequisite out of order.
            blocked.add(dependency)
            if step_id:
                blocked.add(step_id)

    for step_id, step_requests in requests_by_step.items():
        if any(_request_satisfies_runtime_dependency(request) for request in step_requests):
            continue
        if any(
            _request_is_currently_executable(
                request,
                envelope=envelope,
                allowed_tools=allowed_tools,
            )
            for request in step_requests
        ):
            continue
        blocked.add(step_id)

    changed = True
    while changed:
        changed = False
        for request in mapped_requests:
            step_id = _text(request.get("step_id") or request.get("planner_step_id"))
            if not step_id or step_id in blocked:
                continue
            if _request_depends_on_non_executable_step(request, blocked):
                blocked.add(step_id)
                changed = True
    return blocked


def _request_satisfies_runtime_dependency(request: Mapping[str, Any]) -> bool:
    return str(request.get("status") or "").strip() in {"completed", "recovered"}


def _request_is_currently_executable(
    request: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any],
    allowed_tools: set[str],
) -> bool:
    projected = _tool_request_from_execution_request(request, envelope=envelope)
    tool_name = _text(projected.get("tool"))
    return bool(
        not _request_status_is_non_executable(request)
        and not _request_desktop_route_is_non_executable(request)
        and tool_name
        and (not allowed_tools or tool_name in allowed_tools)
    )


def _request_depends_on_non_executable_step(
    request: Mapping[str, Any],
    non_executable_step_ids: set[str],
) -> bool:
    return any(
        dependency in non_executable_step_ids
        for dependency in _string_list(request.get("depends_on"))
    )


def _request_status_is_non_executable(request: Mapping[str, Any]) -> bool:
    status = str(request.get("status") or "").strip()
    if status in {"blocked", "unavailable"} and (
        runtime_execution_readiness_state(request)
        == RUNTIME_EXECUTION_READINESS_DEFERRED
    ):
        return False
    return status in _NON_EXECUTABLE_REQUEST_STATUSES


def _request_desktop_route_is_non_executable(request: Mapping[str, Any]) -> bool:
    route = request.get("desktop_execution_route")
    if not isinstance(route, Mapping):
        return False
    return (
        runtime_execution_readiness_state(request)
        == RUNTIME_EXECUTION_READINESS_BLOCKED
    )


def _sandbox_provider_for_envelope(
    requests: Iterable[RuntimeExecutionRequestSnapshot],
) -> SandboxDesktopProviderSnapshot | None:
    for request in requests:
        if request.sandbox_provider is not None:
            return request.sandbox_provider
    return None


def _desktop_execution_route_for_envelope(
    requests: Iterable[RuntimeExecutionRequestSnapshot],
) -> DesktopExecutionRouteSnapshot | None:
    for request in requests:
        if request.desktop_execution_route is not None:
            return request.desktop_execution_route
    return None


def _desktop_provider_session_for_envelope(
    requests: Iterable[RuntimeExecutionRequestSnapshot],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata_session = _desktop_provider_session_from_metadata(metadata)
    if metadata_session:
        return metadata_session
    for request in requests:
        if request.desktop_provider_session:
            return dict(request.desktop_provider_session)
    return {}


def _sandbox_provider_for_request(
    request: Mapping[str, Any],
    *,
    tool_name: str,
    execution_mode: Any,
    desktop_execution_policy: Mapping[str, Any] | None,
) -> SandboxDesktopProviderSnapshot | None:
    explicit_provider = _mapping(request.get("sandbox_provider")) or _mapping(
        request.get("sandbox_desktop_provider")
    )
    if explicit_provider:
        return SandboxDesktopProviderSnapshot.model_validate(
            sandbox_desktop_provider_status({"sandbox_provider": explicit_provider})
        )
    policy_mode = str((desktop_execution_policy or {}).get("mode") or "").strip()
    execution_mode_name = str(getattr(execution_mode, "mode", "") or "").strip()
    if (
        bool(getattr(execution_mode, "sandbox_recommended", False))
        or policy_mode == "sandbox_preferred"
        or execution_mode_name == "sandbox_preferred"
    ):
        return SandboxDesktopProviderSnapshot.model_validate(
            sandbox_desktop_provider_status(request)
        )
    if (
        desktop_readonly_provider_route_requested(request)
        and is_readonly_desktop_provider_tool(tool_name)
    ):
        provider_payload = sandbox_desktop_provider_status(request)
        if sandbox_desktop_provider_can_execute_tool(provider_payload, tool_name):
            return SandboxDesktopProviderSnapshot.model_validate(provider_payload)
    if desktop_foreground_provider_route_requested(request) and (
        bool(getattr(execution_mode, "foreground_control", False))
        or bool(getattr(execution_mode, "keyboard_mouse_capture", False))
        or execution_mode_name == "supervised_live"
    ):
        provider_payload = sandbox_desktop_provider_status(request)
        if (
            sandbox_desktop_provider_can_execute_tool(provider_payload, tool_name)
            or not bool(provider_payload.get("available"))
            or _sandbox_provider_requires_controlled_input(
                provider_payload,
                tool_name=tool_name,
                execution_mode=execution_mode,
            )
        ):
            return SandboxDesktopProviderSnapshot.model_validate(provider_payload)
    return None


def _provider_snapshot_for_route(
    route: DesktopExecutionRouteSnapshot | None,
    provider: SandboxDesktopProviderSnapshot | None,
) -> SandboxDesktopProviderSnapshot | None:
    if route is None:
        return provider
    provider_kind = str(route.selected_provider_kind or "").strip()
    if provider_kind != LOCAL_DESKTOP_PROVIDER_KIND:
        return provider
    if provider is not None and str(provider.provider_kind or "").strip() == provider_kind:
        return provider
    return SandboxDesktopProviderSnapshot.model_validate(
        sandbox_desktop_provider_status(
            {"sandbox_provider": local_desktop_execution_provider_status()},
            probe_health=False,
        )
    )


def _sandbox_provider_requires_controlled_input(
    provider_payload: Mapping[str, Any],
    *,
    tool_name: str,
    execution_mode: Any,
) -> bool:
    if bool(getattr(execution_mode, "keyboard_mouse_capture", False)) and (
        provider_payload.get("keyboard_mouse_capture_supported") is False
    ):
        return True
    if bool(getattr(execution_mode, "keyboard_mouse_capture", False)) and (
        provider_payload.get("keyboard_mouse_capture_supported") is True
    ) and provider_payload.get("desktop_session_isolated") is not True:
        return True
    required_tools = set(
        _string_values(provider_payload.get("requires_real_sandbox_for"))
    )
    return str(tool_name or "").strip() in required_tools


def _desktop_execution_route_for_request(
    tool_name: str,
    request: Mapping[str, Any],
    *,
    execution_mode: Any,
    desktop_execution_policy: Mapping[str, Any] | None,
) -> DesktopExecutionRouteSnapshot | None:
    explicit_route = _mapping(request.get("desktop_execution_route"))
    if explicit_route:
        return DesktopExecutionRouteSnapshot.model_validate(explicit_route)
    if (
        desktop_execution_policy
        or bool(getattr(execution_mode, "sandbox_recommended", False))
        or (
            desktop_readonly_provider_route_requested(request)
            and is_readonly_desktop_provider_tool(tool_name)
        )
        or desktop_foreground_provider_route_requested(request)
    ):
        return DesktopExecutionRouteSnapshot.model_validate(
            desktop_execution_route_decision(
                tool_name,
                policy=desktop_execution_policy,
                execution_mode=execution_mode,
                metadata=request,
            )
        )
    return None


def _execution_request_capability_plan_item(
    decision: PlannerDecisionSnapshot,
    *,
    capability_id: str,
    step_id: str,
) -> Any | None:
    capability_plan = getattr(getattr(decision, "plan", None), "capability_plan", None)
    items = list(getattr(capability_plan, "items", None) or [])
    clean_capability_id = _text(capability_id)
    if clean_capability_id:
        for item in items:
            if _text(getattr(item, "capability_id", None)) == clean_capability_id:
                return item
    clean_step_id = _text(step_id)
    if clean_step_id:
        for item in items:
            if clean_step_id in _string_values(getattr(item, "planned_step_ids", None)):
                return item
    return None


def _execution_request_snapshot(
    request: Mapping[str, Any],
    *,
    index: int,
    decision: PlannerDecisionSnapshot,
    steps: Mapping[str, ToolPlanStepSnapshot],
    previous_requests: Iterable[RuntimeExecutionRequestSnapshot] = (),
) -> RuntimeExecutionRequestSnapshot:
    tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
    deferred_context = _mapping(request.get("deferred_context"))
    step_id = str(
        request.get("step_id")
        or request.get("planner_step_id")
        or deferred_context.get("step_id")
        or deferred_context.get("planner_step_id")
        or ""
    ).strip()
    step = steps.get(step_id)
    capability_id = str(
        request.get("capability_id")
        or deferred_context.get("capability_id")
        or (step.capability_id if step is not None else "")
        or ""
    ).strip()
    capability_plan_item = _execution_request_capability_plan_item(
        decision,
        capability_id=capability_id,
        step_id=step_id,
    )
    request_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    dispatch_action = _execution_request_dispatch_action(request, step)
    runtime_metadata = _execution_request_runtime_metadata(request, step, decision)
    replan_metadata = _execution_request_replan_metadata(
        step_id,
        step,
        decision,
        request=request,
    )
    depends_on = _string_list(request.get("depends_on"))
    if not depends_on and step is not None:
        depends_on = list(step.depends_on)
    request_input_bindings = _model_mapping_list(request.get("input_bindings"))
    if step is not None:
        canonical_input_bindings = _model_mapping_list(step.input_bindings)
        if (
            request_input_bindings
            and request_input_bindings != canonical_input_bindings
        ):
            raise ValueError("runtime_execution_input_bindings_conflict")
        input_bindings = canonical_input_bindings
    else:
        input_bindings = request_input_bindings
    task_context = _execution_request_task_context(
        decision,
        step_id=step_id,
        depends_on=depends_on,
        runtime_stage=runtime_metadata["runtime_stage"],
    )
    checkpoint_policy = _execution_request_checkpoint_policy(
        task_context,
        replan_metadata,
        step=step,
        runtime_metadata=runtime_metadata,
        dispatch_action=dispatch_action,
    )
    approval_required = bool(
        request.get("approval_required")
        or (step.approval_required if step is not None else False)
    )
    desktop_contract = _desktop_execution_request_contract(
        tool_name=tool_name,
        capability_id=capability_id,
        request_input=request_input,
        request=request,
        runtime_metadata=runtime_metadata,
        step_id=step_id,
        depends_on=depends_on,
        previous_requests=previous_requests,
        dispatch_action=dispatch_action,
    )
    desktop_loop = _desktop_execution_loop_snapshot(
        desktop_contract,
        runtime_metadata=runtime_metadata,
        task_context=task_context,
    )
    action_target = _merged_request_contract_mapping(
        request,
        desktop_contract,
        "action_target",
        canonical_action=dispatch_action,
    )
    step_action_target = _execution_request_task_action_target(
        task_context,
        step_id=step_id,
    )
    goal_action_target = _execution_request_goal_action_target(
        decision,
        step_id=step_id,
        capability_id=capability_id,
        planned_step_target=step_action_target,
    )
    if goal_action_target and not _request_projects_goal_source_action(
        step=step,
        tool_name=tool_name,
        goal_action_target=goal_action_target,
        projected_action_target=action_target,
        capability_id=capability_id,
    ):
        goal_action_target = {}
    planned_action_target = goal_action_target or step_action_target
    if goal_action_target or (
        tool_name == "desktop.search_submit"
        and planned_action_target
        and _request_projects_goal_source_action(
            step=step,
            tool_name=tool_name,
            goal_action_target=planned_action_target,
            projected_action_target=action_target,
            capability_id=capability_id,
        )
    ):
        action_target = bind_planned_action_target(
            planned_action_target,
            action_target,
            capability_id=capability_id,
            source_step_id=step_id,
            tool_name=tool_name,
            runtime_stage=runtime_metadata["runtime_stage"],
        )
    elif action_target:
        action_target = canonical_action_target(
            action_target,
            capability_id=capability_id,
            step_id=step_id,
            tool_name=tool_name,
            runtime_stage=runtime_metadata["runtime_stage"],
        )
    elif planned_action_target:
        action_target = canonical_action_target(
            planned_action_target,
            capability_id=capability_id,
            step_id=step_id,
            tool_name=tool_name,
            runtime_stage=runtime_metadata["runtime_stage"],
        )
    execution_mode = (
        step.execution_mode
        if step is not None and step.execution_mode is not None
        else desktop_tool_execution_mode_for_input(tool_name, request_input)
    )
    desktop_execution_policy = _mapping(request.get("desktop_execution_policy")) or None
    sandbox_provider = _sandbox_provider_for_request(
        request,
        tool_name=tool_name,
        execution_mode=execution_mode,
        desktop_execution_policy=desktop_execution_policy,
    )
    desktop_execution_route = _desktop_execution_route_for_request(
        tool_name,
        request,
        execution_mode=execution_mode,
        desktop_execution_policy=desktop_execution_policy,
    )
    sandbox_provider = _provider_snapshot_for_route(
        desktop_execution_route,
        sandbox_provider,
    )
    tool_plan_id = str(getattr(decision.plan.tool_plan, "plan_id", "") or "").strip()
    return RuntimeExecutionRequestSnapshot(
        request_id=str(
            request.get("request_id")
            or request.get("tool_call_id")
            or f"{decision.plan.plan_id}:request:{index}:{tool_name or 'tool'}"
        ),
        step_id=step_id or None,
        capability_id=capability_id or None,
        capability_title=_text(getattr(capability_plan_item, "title", None)),
        capability_status=_text(getattr(capability_plan_item, "status", None)),
        capability_reason=_text(getattr(capability_plan_item, "reason", None)),
        capability_selected_tools=_string_values(
            getattr(capability_plan_item, "selected_tools", None)
        ),
        capability_planned_step_ids=_string_values(
            getattr(capability_plan_item, "planned_step_ids", None)
        ),
        decision_id=decision.decision_id,
        plan_id=decision.plan.plan_id,
        tool_plan_id=(
            tool_plan_id
            if tool_plan_id and tool_plan_id != decision.plan.plan_id
            else None
        ),
        intent_kind=str(decision.selected_intent.kind or "") or None,
        core_id=task_context["core_id"] or None,
        workspace_id=task_context["workspace_id"] or None,
        group_run_id=_optional_text(request.get("group_run_id")),
        run_group_id=_optional_text(request.get("run_group_id")),
        group_id=_optional_text(request.get("group_id")),
        workflow_run_id=_optional_text(request.get("workflow_run_id")),
        workflow_id=_optional_text(request.get("workflow_id")),
        workflow_node_id=_optional_text(request.get("workflow_node_id")),
        workflow_node_label=_optional_text(request.get("workflow_node_label")),
        workflow_node_kind=_optional_text(request.get("workflow_node_kind")),
        tool_name=tool_name or "tool",
        protocol=str(request.get("protocol") or "json_fallback"),
        input=dict(request_input),
        planning_reason=str(request.get("planning_reason") or ""),
        presentation=str(request.get("presentation") or ""),
        approval_required=approval_required,
        risk_level=str(
            request.get("risk_level")
            or (step.risk_level if step is not None else "")
            or "low"
        ),
        execution_mode=execution_mode,
        desktop_execution_policy=desktop_execution_policy,
        sandbox_provider=sandbox_provider,
        desktop_execution_route=desktop_execution_route,
        desktop_provider_session=_mapping(request.get("desktop_provider_session")),
        policy_reason=str(
            request.get("policy_reason") or request.get("approval_reason") or ""
        ),
        continue_to_model=bool(request.get("continue_to_model")),
        deferred_tool=_optional_text(request.get("deferred_tool")),
        deferred_input=_mapping(request.get("deferred_input")),
        deferred_context=deferred_context,
        deferred_continuation=[
            dict(item) for item in _mapping_list(request.get("deferred_continuation"))
        ],
        depends_on=depends_on,
        input_bindings=input_bindings,
        fallback_tools=list(step.fallback_tools) if step is not None else [],
        status=str(request.get("status") or (step.status if step is not None else "planned")),
        runtime_doctrine=runtime_metadata["runtime_doctrine"],
        runtime_stage=runtime_metadata["runtime_stage"],
        runtime_role=runtime_metadata["runtime_role"],
        requires_observation=runtime_metadata["requires_observation"],
        requires_post_action_verification=(
            False
            if dispatch_action
            else bool(
                runtime_metadata["requires_post_action_verification"]
                or (
                    approval_required
                    and getattr(
                        checkpoint_policy,
                        "requires_post_action_verification",
                        False,
                    )
                )
            )
        ),
        replan_triggers=replan_metadata["replan_triggers"],
        replan_signal_ids=replan_metadata["replan_signal_ids"],
        followup_target=_mapping(request.get("followup_target")),
        action_target=action_target,
        observation_evidence=_merged_request_contract_mapping(
            request,
            desktop_contract,
            "observation_evidence",
        ),
        observation_retry=(
            {}
            if dispatch_action
            else _merged_request_contract_mapping(
                request,
                desktop_contract,
                "observation_retry",
            )
        ),
        task_todo=task_context["task_todo"] or _mapping(deferred_context.get("task_todo")),
        task_checkpoints=(
            task_context["task_checkpoints"]
            or [dict(item) for item in _mapping_list(deferred_context.get("task_checkpoints"))]
        ),
        task_workspace_items=(
            task_context["task_workspace_items"]
            or [
                dict(item)
                for item in _mapping_list(deferred_context.get("task_workspace_items"))
            ]
        ),
        verification_targets=(
            task_context["verification_targets"]
            or [
                dict(item)
                for item in _mapping_list(deferred_context.get("verification_targets"))
            ]
        ),
        task_verification_targets=(
            task_context["task_verification_targets"]
            or [
                dict(item)
                for item in _mapping_list(deferred_context.get("task_verification_targets"))
            ]
        ),
        checkpoint_policy=checkpoint_policy,
        desktop_loop=desktop_loop,
        source=str(request.get("source") or "runtime_planner"),
    )


def _merged_request_contract_mapping(
    request: Mapping[str, Any],
    desktop_contract: Mapping[str, Mapping[str, Any]],
    key: str,
    *,
    canonical_action: str = "",
) -> dict[str, Any]:
    contract_value = _mapping(desktop_contract.get(key))
    request_value = _mapping(request.get(key))
    if not contract_value:
        merged = request_value
    elif not request_value:
        merged = contract_value
    else:
        merged = {**contract_value, **request_value}
    if canonical_action:
        return {**merged, "action": canonical_action}
    return merged


def _execution_request_task_action_target(
    task_context: Mapping[str, Any],
    *,
    step_id: str,
) -> dict[str, Any]:
    """Project the planner-owned non-desktop target into runtime evidence.

    Desktop requests already receive their richer action contract from
    ``_desktop_execution_request_contract``.  Structured local tools instead
    inherit the immutable target stored on their TaskCore checkpoint, keeping
    the tool event bound to the same step and plan lineage as the GoalContract.
    """

    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        return {}
    for checkpoint in _mapping_list(task_context.get("task_checkpoints")):
        if str(checkpoint.get("after_step_id") or "").strip() != clean_step_id:
            continue
        payload = checkpoint.get("payload")
        if not isinstance(payload, Mapping):
            continue
        target = payload.get("action_target")
        if not isinstance(target, Mapping) or not target:
            continue
        claimed_step_id = str(target.get("step_id") or "").strip()
        if claimed_step_id and claimed_step_id != clean_step_id:
            raise ValueError("runtime_execution_action_target_step_conflict")
        return {**dict(target), "step_id": clean_step_id}
    return {}


def _execution_request_goal_action_target(
    decision: PlannerDecisionSnapshot,
    *,
    step_id: str,
    capability_id: str,
    planned_step_target: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the one GoalContract target owned by this exact source step."""

    clean_step_id = str(step_id or "").strip()
    clean_capability = str(capability_id or "").strip()
    task_core = getattr(getattr(decision, "plan", None), "task_core", None)
    contract = getattr(task_core, "goal_contract", None)
    if not clean_step_id or contract is None:
        return {}
    matches: list[dict[str, Any]] = []
    for criterion in list(getattr(contract, "criteria", None) or []):
        if clean_step_id not in list(
            getattr(criterion, "source_step_ids", None) or []
        ):
            continue
        required = {
            str(value or "").strip()
            for value in list(
                getattr(criterion, "required_capabilities", None) or []
            )
            if str(value or "").strip()
        }
        if clean_capability and clean_capability not in required:
            continue
        expected = getattr(criterion, "expected", None)
        target = expected.get("target") if isinstance(expected, Mapping) else None
        if isinstance(target, Mapping) and target:
            if planned_step_target and not action_target_matches(
                target,
                planned_step_target,
                capability_ids=(clean_capability,),
                source_step_id=clean_step_id,
            ):
                # One capability criterion can own several preparatory steps.
                # Its single authoritative target belongs only to the source
                # step whose planned checkpoint describes that same action.
                continue
            matches.append(dict(target))
    canonical_matches = {
        repr(sorted(item.items(), key=lambda pair: str(pair[0])))
        for item in matches
    }
    if len(canonical_matches) > 1:
        raise ValueError("runtime_execution_goal_action_target_conflict")
    return {**matches[0], "step_id": clean_step_id} if matches else {}


def _request_projects_goal_source_action(
    *,
    step: ToolPlanStepSnapshot | None,
    tool_name: str,
    goal_action_target: Mapping[str, Any],
    projected_action_target: Mapping[str, Any],
    capability_id: str,
) -> bool:
    """Distinguish a source action from a prefetch sharing its step id."""

    planned_action = canonical_action_name(
        goal_action_target.get("action"),
        capability_id=capability_id,
    )
    projected_action = canonical_action_name(
        projected_action_target.get("action"),
        tool_name=tool_name,
        capability_id=capability_id,
    )
    if not planned_action or not projected_action or planned_action == projected_action:
        return True
    canonical_step_tool = str(getattr(step, "tool_name", "") or "").strip()
    return not canonical_step_tool or canonical_step_tool == str(tool_name or "").strip()


def _tool_request_from_execution_request(
    request: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tool_name = str(request.get("tool_name") or request.get("tool") or "").strip()
    request_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    request_input = _canonical_runtime_request_input(tool_name, request_input)
    payload: dict[str, Any] = {
        "protocol": str(request.get("protocol") or "json_fallback"),
        "tool": tool_name,
        "input": dict(request_input),
        "source": str(request.get("source") or "runtime_planner"),
        "planning_reason": str(request.get("planning_reason") or ""),
    }
    for key in (
        "request_id",
        "step_id",
        "capability_id",
        "capability_title",
        "capability_status",
        "capability_reason",
        "capability_selected_tools",
        "capability_planned_step_ids",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "core_id",
        "workspace_id",
        "group_run_id",
        "run_group_id",
        "group_id",
        "workflow_run_id",
        "workflow_id",
        "workflow_node_id",
        "workflow_node_label",
        "workflow_node_kind",
        "approval_required",
        "risk_level",
        "desktop_execution_policy",
        "sandbox_provider",
        "sandbox_desktop_provider",
        "desktop_execution_route",
        "desktop_provider_session",
        "policy_reason",
        "presentation",
        "continue_to_model",
        "deferred_tool",
        "deferred_input",
        "deferred_context",
        "deferred_continuation",
        "depends_on",
        "input_bindings",
        "fallback_tools",
        "status",
        "runtime_doctrine",
        "runtime_stage",
        "runtime_role",
        "requires_observation",
        "requires_post_action_verification",
        "replan_triggers",
        "replan_signal_ids",
        "followup_target",
        "task_todo",
        "task_checkpoints",
        "task_workspace_items",
        "verification_targets",
        "task_verification_targets",
        "checkpoint_policy",
        "desktop_loop",
        "action_target",
        "observation_evidence",
        "observation_retry",
    ):
        value = request.get(key)
        if value not in (None, "", [], {}):
            payload[key] = value
    if isinstance(envelope, Mapping):
        _apply_envelope_task_context(payload, envelope)
    return payload


def _canonical_runtime_request_input(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(request_input)
    if not payload:
        return payload
    if str(tool_name or "").strip() not in {
        "app.open",
        "app.focus",
        "app.status",
        "desktop.open_app",
        "desktop.focus_app",
        "desktop.inspect_app",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.verify",
        "media.music_app_open_and_play",
    } and not (
        str(tool_name or "").strip().startswith("app.open_and_")
        or str(tool_name or "").strip().startswith("app.focus_and_")
    ):
        return payload
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name or app_name == "企业微信":
        return payload
    canonical = str(legacy_app_name_hint(app_name) or "").strip()
    if not canonical or canonical == app_name:
        return payload
    return {**payload, "app_name": canonical}


_SELECTED_DESKTOP_APP_NAME = "<selected app from desktop.list_apps>"
_SELECTED_RUNNING_DESKTOP_APP_NAME = "<selected app from desktop.running_apps>"
_DESKTOP_APP_SELECTION_SOURCE = "desktop.list_apps"
_DESKTOP_RUNNING_APP_SELECTION_SOURCE = "desktop.running_apps"
_DESKTOP_DIRECT_APP_NAME_SOURCE = "direct_app_name"
_DESKTOP_APP_SELECTION_SOURCES = {
    _DESKTOP_APP_SELECTION_SOURCE,
    _DESKTOP_RUNNING_APP_SELECTION_SOURCE,
    _DESKTOP_DIRECT_APP_NAME_SOURCE,
}
_DESKTOP_APP_UI_ELEMENT_TYPE_TOOLS = {
    "app.focus_and_type_into_ui_element",
    "app.open_and_type_into_ui_element",
}
_DESKTOP_TARGET_DISCOVERY_TOOLS = {
    "desktop.list_apps",
    "desktop.list_windows",
    "desktop.running_apps",
    "desktop.windows",
}


def _desktop_execution_request_contract(
    *,
    tool_name: str,
    capability_id: str,
    request_input: Mapping[str, Any],
    request: Mapping[str, Any],
    runtime_metadata: Mapping[str, Any],
    step_id: str,
    depends_on: list[str],
    previous_requests: Iterable[RuntimeExecutionRequestSnapshot],
    dispatch_action: str = "",
) -> dict[str, dict[str, Any]]:
    if not str(tool_name or "").strip().startswith(
        ("app.", "desktop.", "screen.")
    ):
        return {
            "action_target": {},
            "observation_evidence": {},
            "observation_retry": {},
        }
    runtime_stage = str(runtime_metadata.get("runtime_stage") or "").strip()
    scope = (
        _desktop_discovery_scope(
            tool_name=tool_name,
            request_input=request_input,
            runtime_stage=runtime_stage,
        )
        if str(tool_name or "").strip() in _DESKTOP_TARGET_DISCOVERY_TOOLS
        else {}
    )
    target_kind = "desktop_discovery" if scope else "desktop_app"
    if not scope:
        scope = _desktop_app_selection_scope(request_input, request)
    if not scope and _desktop_request_can_inherit_selection_scope(
        tool_name,
        runtime_stage,
    ):
        scope = _desktop_app_selection_scope_from_previous_requests(
            depends_on,
            previous_requests,
        )
    if not scope:
        scope = _desktop_direct_app_scope(
            tool_name=tool_name,
            request_input=request_input,
        )
    if not scope:
        scope = _desktop_foreground_scope(
            tool_name=tool_name,
            request_input=request_input,
            runtime_stage=runtime_stage,
            depends_on=depends_on,
            previous_requests=previous_requests,
        )
        target_kind = "desktop_foreground"
    if not scope:
        return {
            "action_target": {},
            "observation_evidence": {},
            "observation_retry": {},
        }

    inspect_scope = _desktop_inspect_app_action_scope(tool_name, request_input)
    if inspect_scope.get("role_filter"):
        target_kind = "desktop_ui"
    action = dispatch_action or _desktop_request_action(
        tool_name,
        runtime_stage,
        request_input,
    )
    action_target = canonical_action_target(
        {
        "kind": target_kind,
        "action": action,
        **scope,
        **inspect_scope,
        **_desktop_path_action_scope(tool_name, request_input),
        **_desktop_app_ui_element_action_scope(tool_name, request_input),
        **_desktop_shortcut_action_scope(tool_name, request_input),
        },
        capability_id=str(capability_id or "").strip(),
        step_id=step_id,
        tool_name=tool_name,
        runtime_stage=runtime_stage,
    )
    if runtime_stage == "verify":
        action_target["verified_step_ids"] = list(depends_on)

    observation_evidence = {
        "source_tool": _desktop_observation_source_tool(
            tool_name=tool_name,
            runtime_stage=runtime_stage,
            scope=scope,
            target_kind=target_kind,
        ),
        **scope,
    }
    observation_retry = (
        {}
        if dispatch_action
        else _desktop_observation_retry(
            tool_name=tool_name,
            request_input=request_input,
            runtime_stage=runtime_stage,
            scope=scope,
            target_kind=target_kind,
        )
    )
    return {
        "action_target": _non_empty_mapping(action_target),
        "observation_evidence": _non_empty_mapping(observation_evidence),
        "observation_retry": observation_retry,
    }


_DESKTOP_LOOP_AUTO_RETRY_TOOLS = {
    "browser.current_page",
    "browser.screenshot",
    "desktop.active_window",
    "desktop.list_apps",
    "desktop.read_ui",
    "desktop.running_apps",
    "desktop.ui_elements",
    "screen.capture",
}


def _desktop_execution_loop_snapshot(
    desktop_contract: Mapping[str, Mapping[str, Any]],
    *,
    runtime_metadata: Mapping[str, Any],
    task_context: Mapping[str, Any],
) -> DesktopExecutionLoopSnapshot | None:
    action_target = _mapping(desktop_contract.get("action_target"))
    observation_evidence = _mapping(desktop_contract.get("observation_evidence"))
    observation_retry = _mapping(desktop_contract.get("observation_retry"))
    if not any((action_target, observation_evidence, observation_retry)):
        return None
    retry_tool = _text(observation_retry.get("tool"))
    retry_reason = _text(observation_retry.get("reason"))
    retry_input = _mapping(observation_retry.get("input"))
    verification_targets = [
        *list(_mapping_list(task_context.get("verification_targets"))),
        *list(_mapping_list(task_context.get("task_verification_targets"))),
    ]
    return DesktopExecutionLoopSnapshot(
        stage=_text(runtime_metadata.get("runtime_stage")),
        role=_text(runtime_metadata.get("runtime_role")),
        action=_text(action_target.get("action")),
        target_kind=_text(action_target.get("kind")),
        selection_source=_text(
            action_target.get("selection_source")
            or observation_evidence.get("selection_source")
        ),
        app_name=_text(
            action_target.get("resolved_app_name")
            or action_target.get("app_name")
            or observation_evidence.get("resolved_app_name")
            or observation_evidence.get("app_name")
        ),
        query=_text(action_target.get("query") or observation_evidence.get("query")),
        source_tool=_text(observation_evidence.get("source_tool")),
        retry_tool=retry_tool,
        retry_reason=retry_reason,
        retry_input=retry_input,
        verification_target_step_ids=_dedupe(
            str(target.get("step_id") or "").strip()
            for target in verification_targets
        ),
        requires_observation=bool(runtime_metadata.get("requires_observation")),
        requires_post_action_verification=bool(
            runtime_metadata.get("requires_post_action_verification")
            or verification_targets
        ),
        can_auto_retry=bool(
            retry_tool
            and retry_tool in _DESKTOP_LOOP_AUTO_RETRY_TOOLS
            and retry_reason in {
                "resolve_desktop_app",
                "observe_foreground_ui",
                "verification_failed",
            }
        ),
    )


def _desktop_app_ui_element_action_scope(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    if str(tool_name or "").strip() not in _DESKTOP_APP_UI_ELEMENT_TYPE_TOOLS:
        return {}
    scope: dict[str, Any] = {}
    for key in ("target", "selector", "role_filter", "label"):
        value = request_input.get(key)
        if value not in (None, "", [], {}):
            scope[key] = value
    return scope


def _desktop_inspect_app_action_scope(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    if str(tool_name or "").strip() != "desktop.inspect_app":
        return {}
    scope: dict[str, Any] = {}
    role_filter = str(request_input.get("role_filter") or "").strip()
    if role_filter:
        scope["role_filter"] = role_filter
    raw_limit = request_input.get("limit", 80)
    try:
        limit = int(raw_limit or 80)
    except (TypeError, ValueError):
        limit = 80
    scope["limit"] = max(1, min(200, limit))
    return scope


def _desktop_path_action_scope(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    if str(tool_name or "").strip() not in {
        "app.open_path_with_app",
        "desktop.open_path",
        "desktop.open_path_with_app",
        "desktop.reveal_path",
    }:
        return {}
    return _non_empty_mapping(
        {
            "path": request_input.get("path"),
            "target_path": request_input.get("target_path"),
        }
    )


def _desktop_app_selection_scope(
    request_input: Mapping[str, Any],
    request: Mapping[str, Any],
) -> dict[str, Any]:
    selection_source = str(
        request_input.get("selection_source")
        or request_input.get("app_selection_source")
        or ""
    ).strip()
    input_resolution = (
        request.get("input_resolution")
        if isinstance(request.get("input_resolution"), Mapping)
        else {}
    )
    resolution_source = str(input_resolution.get("source_tool") or "").strip()
    app_name = str(
        request_input.get("app_name")
        or input_resolution.get("resolved_app_name")
        or ""
    ).strip()
    query = str(
        request_input.get("query")
        or input_resolution.get("requested_app_name")
        or input_resolution.get("query")
        or ("" if _desktop_app_placeholder_selection_source(app_name) else app_name)
    ).strip()
    if selection_source not in _DESKTOP_APP_SELECTION_SOURCES:
        selection_source = ""
    if resolution_source not in _DESKTOP_APP_SELECTION_SOURCES:
        resolution_source = ""
    placeholder_source = _desktop_app_placeholder_selection_source(app_name)
    source = selection_source or resolution_source or placeholder_source
    if not source:
        return {}
    scope: dict[str, Any] = {
        "selection_source": source,
    }
    if app_name:
        scope["app_name"] = app_name
    if query:
        scope["query"] = query
    title_contains = str(request_input.get("title_contains") or "").strip()
    if title_contains:
        scope["title_contains"] = title_contains
    resolved_app = str(input_resolution.get("resolved_app_name") or "").strip()
    if resolved_app:
        scope["resolved_app_name"] = resolved_app
    resolved_path = str(input_resolution.get("resolved_app_path") or "").strip()
    if resolved_path:
        scope["resolved_app_path"] = resolved_path
    return scope


def _desktop_app_selection_scope_from_previous_requests(
    depends_on: list[str],
    previous_requests: Iterable[RuntimeExecutionRequestSnapshot],
) -> dict[str, Any]:
    dependency_ids = {
        str(value or "").strip()
        for value in depends_on
        if str(value or "").strip()
    }
    fallback_scope: dict[str, Any] = {}
    for previous in reversed(list(previous_requests)):
        # Verification must inherit the planner-owned canonical target before
        # considering transport-level request input.  The latter may still
        # carry discovery provenance (for example ``desktop.list_apps``) after
        # the immutable GoalContract has bound the action to a concrete app.
        # Reversing that authority would make operate and verify describe the
        # same target with different identities.
        scope = _desktop_app_scope_from_action_target(previous.action_target)
        if not scope:
            request_input = previous.input if isinstance(previous.input, Mapping) else {}
            scope = _desktop_app_selection_scope(
                request_input,
                {
                    "input_resolution": {},
                },
            )
        if not scope:
            continue
        if not fallback_scope:
            fallback_scope = scope
        previous_step_id = str(previous.step_id or "").strip()
        if dependency_ids and previous_step_id in dependency_ids:
            return scope
    return fallback_scope


def _desktop_discovery_scope(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
    runtime_stage: str,
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    if str(runtime_stage or "").strip() != "discover":
        return {}
    if clean_tool not in {
        "desktop.list_apps",
        "desktop.running_apps",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.active_window",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.verify",
        "screen.capture",
    }:
        return {}
    return _non_empty_mapping(
        {
            "selection_source": clean_tool,
            "query": request_input.get("query"),
            "app_name": request_input.get("app_name"),
            "title_contains": request_input.get("title_contains"),
            "role_filter": request_input.get("role_filter"),
            "target": request_input.get("target"),
            "selector": request_input.get("selector"),
            "limit": request_input.get("limit"),
            "reason": request_input.get("reason"),
        }
    )


def _desktop_foreground_scope(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
    runtime_stage: str,
    depends_on: list[str],
    previous_requests: Iterable[RuntimeExecutionRequestSnapshot],
) -> dict[str, Any]:
    if not _desktop_request_supports_foreground_target(tool_name, runtime_stage):
        return {}
    inherited = _desktop_foreground_scope_from_previous_requests(
        depends_on,
        previous_requests,
    )
    scope = {
        "target_scope": "foreground",
        **inherited,
        **_desktop_foreground_scope_from_input(request_input),
    }
    return _non_empty_mapping(scope)


def _desktop_shortcut_action_scope(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    if "shortcut" not in clean_tool and "hotkey" not in clean_tool:
        return {}
    scope: dict[str, Any] = {}
    shortcut_action = str(request_input.get("action") or "").strip().lower()
    if shortcut_action:
        scope["shortcut_action"] = shortcut_action
    key = str(request_input.get("key") or "").strip()
    if key:
        scope["key"] = key
    modifiers = request_input.get("modifiers")
    if isinstance(modifiers, (list, tuple)) and modifiers:
        scope["modifiers"] = list(modifiers)
    elif isinstance(modifiers, str) and modifiers.strip():
        scope["modifiers"] = modifiers
    return scope


def _desktop_direct_app_scope(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    if not clean_tool.startswith("app.") and clean_tool not in {
        "desktop.open_app",
        "desktop.focus_app",
    }:
        return {}
    app_name = _text(request_input.get("app_name"))
    if not app_name or _desktop_app_placeholder_selection_source(app_name):
        return {}
    query = _text(request_input.get("query")) or app_name
    scope = {
        "selection_source": _DESKTOP_DIRECT_APP_NAME_SOURCE,
        "app_name": app_name,
        "query": query,
    }
    title_contains = _text(request_input.get("title_contains"))
    if title_contains:
        scope["title_contains"] = title_contains
    return scope


def _desktop_foreground_scope_from_input(
    request_input: Mapping[str, Any],
) -> dict[str, Any]:
    scope: dict[str, Any] = {}
    for key in (
        "target",
        "selector",
        "role_filter",
        "title_contains",
        "label",
        "query",
        "action",
        "key",
        "direction",
    ):
        value = request_input.get(key)
        if value not in (None, "", [], {}):
            scope[key] = value
    text = _text(request_input.get("text"))
    if text:
        scope["text_preview"] = text[:120]
        scope["text_length"] = len(text)
    return scope


def _desktop_foreground_scope_from_previous_requests(
    depends_on: list[str],
    previous_requests: Iterable[RuntimeExecutionRequestSnapshot],
) -> dict[str, Any]:
    dependency_ids = {
        str(value or "").strip()
        for value in depends_on
        if str(value or "").strip()
    }
    fallback_scope: dict[str, Any] = {}
    for previous in reversed(list(previous_requests)):
        target = (
            previous.action_target
            if isinstance(previous.action_target, Mapping)
            else {}
        )
        if str(target.get("kind") or "").strip() != "desktop_foreground":
            continue
        scope = {
            key: value
            for key, value in target.items()
            if key not in {"kind", "action", "step_id", "verified_step_ids"}
            and value not in (None, "", [], {})
        }
        if not scope:
            continue
        if not fallback_scope:
            fallback_scope = scope
        previous_step_id = str(previous.step_id or "").strip()
        if dependency_ids and previous_step_id in dependency_ids:
            return scope
    return fallback_scope


def _desktop_app_scope_from_action_target(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    if str(value.get("kind") or "").strip() != "desktop_app":
        return {}
    return _non_empty_mapping(
        {
            key: value.get(key)
            for key in (
                "selection_source",
                "app_name",
                "query",
                "title_contains",
                "resolved_app_name",
                "resolved_app_path",
            )
        }
    )


_DESKTOP_REQUEST_ACTION_ALIASES = {
    "app.focus": "focus_app",
    "app.focus_window": "focus_app_window",
    "app.open": "open_app",
    "desktop.active_window": "read_active_window",
    "desktop.focus_app": "focus_app",
    "desktop.inspect_app": "inspect_app",
    "desktop.list_apps": "discover_apps",
    "desktop.list_windows": "list_windows",
    "desktop.open_app": "open_app",
    "desktop.read_ui": "read_ui",
    "desktop.running_apps": "list_running_apps",
    "desktop.ui_elements": "read_ui",
    "desktop.windows": "list_windows",
    "screen.capture": "capture_screen",
}


def _desktop_request_action(
    tool_name: str,
    runtime_stage: str,
    request_input: Mapping[str, Any] | None = None,
) -> str:
    clean_tool = str(tool_name or "").strip()
    if runtime_stage == "verify":
        return canonical_action_name(
            "verify",
            tool_name=clean_tool,
            capability_id="desktop.app_discovery",
            runtime_stage=runtime_stage,
        )
    if clean_tool in {"app.open_path_with_app", "desktop.open_path_with_app"}:
        requested_action = str((request_input or {}).get("action") or "").strip()
        if requested_action in {
            "open_path_with_app",
            "open_path_with_selected_app",
        }:
            return requested_action
        return "open_path_with_app"
    if clean_tool in _DESKTOP_REQUEST_ACTION_ALIASES:
        return canonical_action_name(
            _DESKTOP_REQUEST_ACTION_ALIASES[clean_tool],
            tool_name=clean_tool,
            capability_id="desktop.app_discovery",
            runtime_stage=runtime_stage,
        )
    if clean_tool in {"desktop.search_submit", "desktop.submit_foreground"}:
        return "submit_ui"
    if "click" in clean_tool:
        return "click_ui"
    if "type" in clean_tool:
        return "type_ui"
    if "shortcut" in clean_tool or "hotkey" in clean_tool:
        return "keyboard_shortcut"
    if "key" in clean_tool:
        return "keyboard_key"
    if "scroll" in clean_tool:
        return "scroll_ui"
    return canonical_action_name(
        clean_tool or "desktop_operation",
        tool_name=clean_tool,
        capability_id="desktop.ui_operation",
        runtime_stage=runtime_stage,
    )


def _desktop_request_can_inherit_selection_scope(
    tool_name: str,
    runtime_stage: str,
) -> bool:
    if str(runtime_stage or "").strip() == "verify":
        return True
    return str(tool_name or "").strip() in {
        "desktop.active_window",
        "desktop.click",
        "desktop.click_ui_element",
        "desktop.hotkey",
        "desktop.key",
        "desktop.safe_click",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.shortcut",
        "desktop.submit_foreground",
        "desktop.type",
        "desktop.type_into_ui_element",
        "desktop.type_text",
        "desktop.verify",
        "desktop.ui_elements",
        "desktop.read_ui",
        "screen.capture",
    }


def _desktop_request_supports_foreground_target(
    tool_name: str,
    runtime_stage: str,
) -> bool:
    clean_tool = str(tool_name or "").strip()
    if str(runtime_stage or "").strip() == "verify":
        return (
            clean_tool.startswith(("app.", "desktop."))
            or clean_tool == "screen.capture"
        )
    return clean_tool in {
        "desktop.active_window",
        "desktop.click",
        "desktop.click_ui_element",
        "desktop.hotkey",
        "desktop.key",
        "desktop.read_ui",
        "desktop.safe_click",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.shortcut",
        "desktop.submit_foreground",
        "desktop.type",
        "desktop.type_into_ui_element",
        "desktop.type_text",
        "desktop.ui_elements",
        "desktop.verify",
        "screen.capture",
    }


def _desktop_observation_source_tool(
    *,
    tool_name: str,
    runtime_stage: str,
    scope: Mapping[str, Any],
    target_kind: str,
) -> str:
    if str(runtime_stage or "").strip() == "verify":
        return tool_name or "runtime_verification"
    if target_kind == "desktop_app":
        return str(scope.get("selection_source") or tool_name or "runtime_execution")
    if target_kind == "desktop_discovery":
        return str(scope.get("selection_source") or tool_name or "runtime_discovery")
    return tool_name or "desktop.foreground"


def _desktop_observation_retry(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
    runtime_stage: str,
    scope: Mapping[str, Any],
    target_kind: str = "desktop_app",
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    if target_kind == "desktop_foreground":
        return _desktop_foreground_observation_retry(
            tool_name=tool_name,
            request_input=request_input,
            runtime_stage=runtime_stage,
            scope=scope,
        )
    if target_kind == "desktop_discovery":
        return _desktop_discovery_observation_retry(
            tool_name=tool_name,
            request_input=request_input,
            runtime_stage=runtime_stage,
            scope=scope,
        )
    if runtime_stage != "verify" and clean_tool in {
        "desktop.ui_elements",
        "desktop.read_ui",
    }:
        retry_input = {
            key: request_input[key]
            for key in ("app_name", "role_filter", "target", "selector", "limit", "reason")
            if key in request_input and request_input[key] not in (None, "")
        }
        return _non_empty_mapping(
            {
                "from_tool": clean_tool,
                "tool": clean_tool,
                "input": retry_input,
                "reason": "observe_foreground_ui",
            }
        )
    if runtime_stage != "verify" and clean_tool in {
        "desktop.active_window",
        "screen.capture",
    }:
        return _desktop_discovery_observation_retry(
            tool_name=clean_tool,
            request_input=request_input,
            runtime_stage=runtime_stage,
            scope=scope,
        )
    if runtime_stage == "verify":
        retry_input = {
            key: request_input[key]
            for key in ("app_name", "role_filter", "limit", "reason")
            if key in request_input and request_input[key] not in (None, "")
        }
        if tool_name != "desktop.active_window" and not retry_input:
            retry_input = {
                key: scope[key]
                for key in ("app_name", "query", "selection_source")
                if key in scope and scope[key] not in (None, "")
            }
        return _non_empty_mapping(
            {
                "from_tool": tool_name,
                "tool": tool_name,
                "input": retry_input,
                "reason": "verification_failed",
            }
        )
    query = str(scope.get("query") or scope.get("app_name") or "").strip()
    selection_source = str(scope.get("selection_source") or _DESKTOP_APP_SELECTION_SOURCE).strip()
    if selection_source == _DESKTOP_DIRECT_APP_NAME_SOURCE:
        selection_source = _DESKTOP_APP_SELECTION_SOURCE
    retry_input: dict[str, Any] = (
        {}
        if selection_source == _DESKTOP_RUNNING_APP_SELECTION_SOURCE
        else {"limit": 20}
    )
    if query and selection_source != _DESKTOP_RUNNING_APP_SELECTION_SOURCE:
        retry_input["query"] = query
    return _non_empty_mapping(
        {
            "from_tool": selection_source,
            "tool": selection_source,
            "input": retry_input,
            "reason": "resolve_desktop_app",
        }
    )


def _desktop_discovery_observation_retry(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
    runtime_stage: str,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    if clean_tool in {"desktop.windows", "desktop.list_windows"}:
        retry_input = {
            key: request_input[key]
            for key in ("app_name", "title_contains", "limit")
            if key in request_input and request_input[key] not in (None, "")
        }
        if not retry_input:
            retry_input = {
                key: scope[key]
                for key in ("app_name", "title_contains", "limit")
                if key in scope and scope[key] not in (None, "")
            }
        return _non_empty_mapping(
            {
                "from_tool": clean_tool,
                "tool": clean_tool,
                "input": retry_input,
                "reason": "observe_windows",
            }
        )
    if clean_tool in {"desktop.ui_elements", "desktop.read_ui", "desktop.verify"}:
        retry_input = {
            key: request_input[key]
            for key in ("app_name", "role_filter", "target", "selector", "limit", "reason")
            if key in request_input and request_input[key] not in (None, "")
        }
        if not retry_input:
            retry_input = {
                key: scope[key]
                for key in ("app_name", "role_filter", "target", "selector", "limit", "reason")
                if key in scope and scope[key] not in (None, "")
            }
        return _non_empty_mapping(
            {
                "from_tool": clean_tool,
                "tool": clean_tool,
                "input": retry_input,
                "reason": (
                    "verification_failed"
                    if clean_tool == "desktop.verify" or runtime_stage == "verify"
                    else "observe_ui"
                ),
            }
        )
    if clean_tool == "desktop.active_window":
        return _non_empty_mapping(
            {
                "from_tool": clean_tool,
                "tool": clean_tool,
                "input": {},
                "reason": "observe_active_window",
            }
        )
    if clean_tool == "screen.capture":
        return _non_empty_mapping(
            {
                "from_tool": clean_tool,
                "tool": clean_tool,
                "input": {},
                "reason": "capture_screen",
            }
        )
    query = str(scope.get("query") or scope.get("app_name") or "").strip()
    retry_input: dict[str, Any] = (
        {}
        if clean_tool == _DESKTOP_RUNNING_APP_SELECTION_SOURCE
        else {"limit": 20}
    )
    if query and clean_tool != _DESKTOP_RUNNING_APP_SELECTION_SOURCE:
        retry_input["query"] = query
    return _non_empty_mapping(
        {
            "from_tool": clean_tool,
            "tool": clean_tool,
            "input": retry_input,
            "reason": "resolve_desktop_app",
        }
    )


def _desktop_foreground_observation_retry(
    *,
    tool_name: str,
    request_input: Mapping[str, Any],
    runtime_stage: str,
    scope: Mapping[str, Any],
) -> dict[str, Any]:
    if runtime_stage == "verify":
        retry_input = {
            key: request_input[key]
            for key in ("role_filter", "limit", "reason")
            if key in request_input and request_input[key] not in (None, "")
        }
        if not retry_input:
            retry_input = {
                key: scope[key]
                for key in ("role_filter", "target", "selector", "query")
                if key in scope and scope[key] not in (None, "")
            }
        return _non_empty_mapping(
            {
                "from_tool": tool_name,
                "tool": tool_name,
                "input": retry_input,
                "reason": "verification_failed",
            }
        )

    retry_input = {
        key: request_input[key]
        for key in ("role_filter", "limit", "target", "selector")
        if key in request_input and request_input[key] not in (None, "")
    }
    if not retry_input:
        retry_input = {
            key: scope[key]
            for key in ("role_filter", "target", "selector", "query")
            if key in scope and scope[key] not in (None, "")
        }
    return _non_empty_mapping(
        {
            "from_tool": "desktop.ui_elements",
            "tool": "desktop.ui_elements",
            "input": retry_input,
            "reason": "observe_foreground_ui",
        }
    )


def _desktop_app_placeholder_selection_source(app_name: str) -> str:
    clean_name = str(app_name or "").strip()
    if clean_name == _SELECTED_RUNNING_DESKTOP_APP_NAME:
        return _DESKTOP_RUNNING_APP_SELECTION_SOURCE
    if clean_name == _SELECTED_DESKTOP_APP_NAME:
        return _DESKTOP_APP_SELECTION_SOURCE
    return ""


def _non_empty_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: item
        for key, item in value.items()
        if item not in (None, "", [], {})
    }


def _apply_envelope_task_context(
    payload: dict[str, Any],
    envelope: Mapping[str, Any],
) -> None:
    for key in ("decision_id", "plan_id", "intent_kind"):
        value = envelope.get(key)
        if key not in payload and value not in (None, "", [], {}):
            payload[key] = value

    task_core = _task_core_payload(envelope)
    if not task_core:
        return
    if "core_id" not in payload:
        core_id = str(task_core.get("core_id") or "").strip()
        if core_id:
            payload["core_id"] = core_id
    workspace_id = _task_workspace_id(task_core)
    if workspace_id and "workspace_id" not in payload:
        payload["workspace_id"] = workspace_id

    step_id = str(payload.get("step_id") or payload.get("planner_step_id") or "").strip()
    if not step_id:
        return

    todo = _task_todo_for_step(task_core, step_id)
    if todo and "task_todo" not in payload:
        payload["task_todo"] = todo
    checkpoints = _task_checkpoints_for_step(task_core, step_id)
    if checkpoints and "task_checkpoints" not in payload:
        payload["task_checkpoints"] = checkpoints
    workspace_items = _task_workspace_items_for_step(task_core, step_id)
    if workspace_items and "task_workspace_items" not in payload:
        payload["task_workspace_items"] = workspace_items
    verification_targets = _task_verification_targets_for_request(task_core, payload)
    if verification_targets and "verification_targets" not in payload:
        payload["verification_targets"] = verification_targets
    if verification_targets and "task_verification_targets" not in payload:
        payload["task_verification_targets"] = verification_targets


def _execution_request_task_context(
    decision: PlannerDecisionSnapshot,
    *,
    step_id: str,
    depends_on: list[str],
    runtime_stage: str,
) -> dict[str, Any]:
    task_core = _task_core_payload_from_decision(decision)
    if not task_core:
        return {
            "core_id": "",
            "workspace_id": "",
            "task_todo": {},
            "task_checkpoints": [],
            "task_workspace_items": [],
            "verification_targets": [],
            "task_verification_targets": [],
        }
    payload = {
        "step_id": step_id,
        "runtime_stage": runtime_stage,
        "depends_on": list(depends_on),
    }
    verification_targets = _task_verification_targets_for_request(
        task_core,
        payload,
    )
    return {
        "core_id": str(task_core.get("core_id") or "").strip(),
        "workspace_id": _task_workspace_id(task_core),
        "task_todo": _task_todo_for_step(task_core, step_id),
        "task_checkpoints": _task_checkpoints_for_step(task_core, step_id),
        "task_workspace_items": _task_workspace_items_for_step(task_core, step_id),
        "verification_targets": verification_targets,
        "task_verification_targets": verification_targets,
    }


def _execution_request_checkpoint_policy(
    task_context: Mapping[str, Any],
    replan_metadata: Mapping[str, list[str]],
    *,
    step: ToolPlanStepSnapshot | None,
    runtime_metadata: Mapping[str, Any],
    dispatch_action: str = "",
) -> RuntimeCheckpointPolicySnapshot | None:
    checkpoints = _mapping_list(task_context.get("task_checkpoints"))
    verification_targets = [
        *list(_mapping_list(task_context.get("verification_targets"))),
        *list(_mapping_list(task_context.get("task_verification_targets"))),
    ]
    fallback_tools = list(step.fallback_tools) if step is not None else []
    replan_triggers = _string_list(replan_metadata.get("replan_triggers"))
    replan_signal_ids = _string_list(replan_metadata.get("replan_signal_ids"))
    if not any(
        (
            checkpoints,
            verification_targets,
            fallback_tools,
            replan_triggers,
            replan_signal_ids,
        )
    ):
        return None
    return RuntimeCheckpointPolicySnapshot(
        checkpoint_ids=_dedupe(
            str(checkpoint.get("checkpoint_id") or "").strip()
            for checkpoint in checkpoints
        ),
        checkpoint_titles=_dedupe(
            str(checkpoint.get("title") or "").strip()
            for checkpoint in checkpoints
        ),
        verifies=_dedupe(
            item
            for checkpoint in checkpoints
            for item in _string_list(checkpoint.get("verifies"))
        ),
        replan_on_failure=bool(
            replan_triggers
            or replan_signal_ids
            or any(
                checkpoint.get("replan_on_failure") is not False
                for checkpoint in checkpoints
            )
        ),
        replan_triggers=replan_triggers,
        replan_signal_ids=replan_signal_ids,
        fallback_tools=_dedupe(fallback_tools),
        verification_target_step_ids=_dedupe(
            str(target.get("step_id") or "").strip()
            for target in verification_targets
        ),
        requires_approval=bool(step.approval_required if step is not None else False),
        requires_observation=bool(runtime_metadata.get("requires_observation")),
        requires_post_action_verification=(
            False
            if dispatch_action
            else bool(
                runtime_metadata.get("requires_post_action_verification")
                or verification_targets
                or (checkpoints and not runtime_metadata.get("requires_observation"))
            )
        ),
    )


def _task_core_payload_from_decision(
    decision: PlannerDecisionSnapshot,
) -> Mapping[str, Any]:
    task_core = getattr(decision.plan, "task_core", None)
    if task_core is None:
        return {}
    model_dump = getattr(task_core, "model_dump", None)
    if callable(model_dump):
        payload = model_dump(mode="json")
        return payload if isinstance(payload, Mapping) else {}
    return task_core if isinstance(task_core, Mapping) else {}


def _task_core_payload(envelope: Mapping[str, Any]) -> Mapping[str, Any]:
    task_core = envelope.get("task_core")
    return task_core if isinstance(task_core, Mapping) else {}


def _task_workspace_id(task_core: Mapping[str, Any]) -> str:
    workspace = task_core.get("workspace")
    if not isinstance(workspace, Mapping):
        return ""
    return str(workspace.get("workspace_id") or "").strip()


def _task_todo_for_step(
    task_core: Mapping[str, Any],
    step_id: str,
) -> dict[str, Any]:
    for todo in _mapping_list(task_core.get("todos")):
        if str(todo.get("step_id") or "").strip() == step_id:
            return dict(todo)
    return {}


def _task_checkpoints_for_step(
    task_core: Mapping[str, Any],
    step_id: str,
) -> list[dict[str, Any]]:
    return [
        dict(checkpoint)
        for checkpoint in _mapping_list(task_core.get("checkpoints"))
        if str(checkpoint.get("after_step_id") or "").strip() == step_id
    ]


def _task_workspace_items_for_step(
    task_core: Mapping[str, Any],
    step_id: str,
) -> list[dict[str, Any]]:
    workspace = task_core.get("workspace")
    if not isinstance(workspace, Mapping):
        return []
    return [
        dict(item)
        for item in _mapping_list(workspace.get("items"))
        if str(item.get("source_step_id") or "").strip() == step_id
    ]


def _task_verification_targets_for_request(
    task_core: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> list[dict[str, Any]]:
    if str(payload.get("runtime_stage") or "").strip() != "verify":
        return []
    targets: list[dict[str, Any]] = []
    for dependency in _verification_dependency_steps(
        task_core,
        _string_list(payload.get("depends_on")),
        verification_step_id=str(
            payload.get("step_id") or payload.get("planner_step_id") or ""
        ).strip(),
    ):
        todo = _task_todo_for_step(task_core, dependency)
        checkpoints = _task_checkpoints_for_step(task_core, dependency)
        workspace_items = _task_workspace_items_for_step(task_core, dependency)
        if not todo and not checkpoints and not workspace_items:
            continue
        target: dict[str, Any] = {"step_id": dependency}
        if todo:
            target["todo"] = todo
        if checkpoints:
            target["checkpoints"] = checkpoints
        if workspace_items:
            target["workspace_items"] = workspace_items
        targets.append(target)
    return targets


def _verification_dependency_steps(
    task_core: Mapping[str, Any],
    direct_dependencies: list[str],
    *,
    verification_step_id: str = "",
) -> list[str]:
    """Return explicit targets plus a tightly scoped container-creation action.

    A final UI observation must not implicitly verify every ancestor in the
    plan graph.  The only inferred relation here is the common atomic UI pair
    ``create container -> type content`` (new document/note/task), where the
    two steps share one resolved application and the verifier observes it.
    """

    selected = {
        step_id for step_id in direct_dependencies if str(step_id or "").strip()
    }
    for step_id in list(selected):
        container_step = _verified_container_preparation_step(
            task_core,
            content_step_id=step_id,
            verification_step_id=verification_step_id,
        )
        if container_step:
            selected.add(container_step)
    plan_order = [
        str(todo.get("step_id") or "").strip()
        for todo in _mapping_list(task_core.get("todos"))
        if str(todo.get("step_id") or "").strip() in selected
    ]
    return [
        *plan_order,
        *(step_id for step_id in direct_dependencies if step_id in selected and step_id not in plan_order),
    ]


_VERIFIED_CONTAINER_ACTIONS = {"new_document", "new_note", "new_task"}


def _verified_container_preparation_step(
    task_core: Mapping[str, Any],
    *,
    content_step_id: str,
    verification_step_id: str,
) -> str:
    content_todo = _task_todo_for_step(task_core, content_step_id)
    content_metadata = (
        content_todo.get("metadata")
        if isinstance(content_todo.get("metadata"), Mapping)
        else {}
    )
    if str(content_metadata.get("runtime_role") or "").strip() not in {
        "draft_message",
        "type_ui",
    }:
        return ""
    dependencies = _string_list(content_todo.get("depends_on"))
    if len(dependencies) != 1:
        return ""
    preparation_step_id = dependencies[0]
    preparation_todo = _task_todo_for_step(task_core, preparation_step_id)
    preparation_metadata = (
        preparation_todo.get("metadata")
        if isinstance(preparation_todo.get("metadata"), Mapping)
        else {}
    )
    if str(preparation_metadata.get("runtime_role") or "").strip() not in {
        "prepare_target_app",
        "shortcut_ui",
    }:
        return ""

    content_input = _task_step_input_preview(task_core, content_step_id)
    preparation_input = _task_step_input_preview(task_core, preparation_step_id)
    preparation_action = str(preparation_input.get("action") or "").strip()
    if preparation_action not in _VERIFIED_CONTAINER_ACTIONS:
        return ""
    declared_container = str(content_input.get("container_action") or "").strip()
    if declared_container and declared_container != preparation_action:
        return ""
    if not declared_container and not any(
        content_input.get(key) not in (None, "", [], {})
        for key in ("text", "body_source", "artifact_path")
    ):
        return ""

    preparation_app = _task_step_effective_app_name(task_core, preparation_step_id)
    verification_app = _task_step_effective_app_name(task_core, verification_step_id)
    if not preparation_app or not verification_app:
        return ""
    if preparation_app.casefold() != verification_app.casefold():
        return ""
    return preparation_step_id


def _task_step_input_preview(
    task_core: Mapping[str, Any],
    step_id: str,
) -> dict[str, Any]:
    for item in _task_workspace_items_for_step(task_core, step_id):
        metadata = item.get("metadata") if isinstance(item.get("metadata"), Mapping) else {}
        preview = metadata.get("input_preview")
        if isinstance(preview, Mapping):
            return dict(preview)
    return {}


def _task_step_effective_app_name(
    task_core: Mapping[str, Any],
    step_id: str,
) -> str:
    current = str(step_id or "").strip()
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        for item in _task_workspace_items_for_step(task_core, current):
            metadata = (
                item.get("metadata")
                if isinstance(item.get("metadata"), Mapping)
                else {}
            )
            for source in (
                metadata.get("input_preview"),
                metadata.get("action_target"),
            ):
                if not isinstance(source, Mapping):
                    continue
                app_name = str(
                    source.get("app_name") or source.get("target_app_name") or ""
                ).strip()
                if app_name:
                    return app_name
        todo = _task_todo_for_step(task_core, current)
        dependencies = _string_list(todo.get("depends_on"))
        if len(dependencies) != 1:
            break
        current = dependencies[0]
    return ""


def _mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _model_mapping_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, Mapping):
            result.append(dict(item))
            continue
        model_dump = getattr(item, "model_dump", None)
        if callable(model_dump):
            payload = model_dump()
            if isinstance(payload, Mapping):
                result.append(dict(payload))
    return result


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item or "").strip() for item in value if str(item or "").strip()]


def _string_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return _string_list(value)
    text = _text(value)
    return [text] if text else []


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _execution_envelope_runtime_metadata(
    requests: list[RuntimeExecutionRequestSnapshot],
    decision: PlannerDecisionSnapshot,
) -> dict[str, Any]:
    stage_counts: dict[str, int] = {}
    doctrine = ""
    for request in requests:
        stage = str(request.runtime_stage or "").strip()
        if not stage:
            continue
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        doctrine = doctrine or str(request.runtime_doctrine or "").strip()
    if not doctrine and stage_counts:
        doctrine = "discover_operate_verify"
    return {
        "runtime_doctrine": doctrine,
        "runtime_stage_counts": stage_counts,
        "replan_signal_count": _task_replan_signal_count(decision),
    }


def _execution_request_runtime_metadata(
    request: Mapping[str, Any],
    step: ToolPlanStepSnapshot | None,
    decision: PlannerDecisionSnapshot,
) -> dict[str, Any]:
    step_id = _text(
        request.get("step_id")
        or request.get("planner_step_id")
        or (step.step_id if step is not None else "")
    )
    metadata = {
        **_task_core_step_runtime_metadata(decision, step_id),
        **_mapping_subset(
            request,
            (
                "runtime_doctrine",
                "runtime_stage",
                "runtime_role",
                "requires_observation",
                "requires_post_action_verification",
            ),
        ),
    }
    runtime_stage = _text(metadata.get("runtime_stage"))
    dispatch_action = _execution_request_dispatch_action(request, step)
    return {
        "runtime_doctrine": _text(metadata.get("runtime_doctrine")),
        "runtime_stage": runtime_stage,
        "runtime_role": _text(metadata.get("runtime_role")),
        "requires_observation": bool(metadata.get("requires_observation")),
        "requires_post_action_verification": (
            False
            if dispatch_action
            else bool(metadata.get("requires_post_action_verification"))
        ),
    }


_EXECUTION_DISPATCH_ACTIONS = frozenset(
    {"dispatch_management", "dispatch_shortcut", "dispatch_submit"}
)


def _execution_request_dispatch_action(
    request: Mapping[str, Any],
    step: ToolPlanStepSnapshot | None,
) -> str:
    tool_name = _text(request.get("tool") or request.get("tool_name"))
    request_input = (
        request.get("input")
        if isinstance(request.get("input"), Mapping)
        else {}
    )
    tool_input_action = _dispatch_action_for_tool_input(tool_name, request_input)
    if step is None:
        # There is no canonical planner step to authenticate request/todo
        # metadata.  Only an exact built-in tool/input pair may be projected
        # as a receipt-only dispatch.
        return tool_input_action
    step_action = _text(getattr(step, "action", ""))
    if step_action not in _EXECUTION_DISPATCH_ACTIONS:
        # Request and task-todo metadata are projections of the step and must
        # never upgrade a canonical effect action into a dispatch receipt.
        return ""
    return step_action if step_action == tool_input_action else ""


def _dispatch_action_for_tool_input(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> str:
    clean_tool = _text(tool_name)
    if clean_tool in {
        "desktop.hide_app",
        "desktop.minimize_window",
        "desktop.close_window",
        "desktop.quit_app",
    }:
        return "dispatch_management" if not request_input else ""
    if clean_tool == "desktop.search_submit":
        return "dispatch_submit" if not request_input else ""
    if clean_tool == "desktop.submit_foreground":
        if not _dispatch_input_has_only(request_input, {"action"}):
            return ""
        action = _text(request_input.get("action") or "submit").lower()
        return "dispatch_submit" if action in {"send", "submit", "confirm"} else ""
    if clean_tool in {"desktop.hotkey", "desktop.shortcut"}:
        if not _dispatch_input_has_only(request_input, {"key", "modifiers"}):
            return ""
        return "dispatch_shortcut" if _text(request_input.get("key")) else ""
    if clean_tool == "desktop.safe_shortcut":
        if is_semantic_safe_shortcut(clean_tool, request_input):
            return ""
        if not _dispatch_input_has_only(request_input, {"action"}):
            return ""
        action = _text(request_input.get("action")).lower()
        return "dispatch_shortcut" if action else ""
    if clean_tool == "desktop.safe_key":
        if not _dispatch_input_has_only(request_input, {"action", "repeat_count"}):
            return ""
        return "dispatch_shortcut" if _text(request_input.get("action")) else ""
    if clean_tool == "desktop.safe_scroll":
        if not _dispatch_input_has_only(request_input, {"direction", "pages"}):
            return ""
        direction = _text(request_input.get("direction")).lower()
        return "dispatch_shortcut" if direction in {"up", "down"} else ""
    if clean_tool == "desktop.safe_click":
        if not _dispatch_input_has_only(request_input, {"x", "y"}):
            return ""
        return (
            "dispatch_shortcut"
            if request_input.get("x") is not None
            and request_input.get("y") is not None
            else ""
        )
    return ""


def _dispatch_input_has_only(
    request_input: Mapping[str, Any],
    allowed_keys: set[str],
) -> bool:
    return set(request_input).issubset(allowed_keys)


def _execution_request_replan_metadata(
    step_id: str,
    step: ToolPlanStepSnapshot | None,
    decision: PlannerDecisionSnapshot,
    *,
    request: Mapping[str, Any] | None = None,
) -> dict[str, list[str]]:
    clean_step_id = _text(step_id or (step.step_id if step is not None else ""))
    signal_ids: list[str] = []
    triggers: list[str] = []
    if request is not None:
        triggers.extend(_string_values(request.get("replan_triggers")))
        triggers.extend(_string_values(request.get("replan_trigger")))
        signal_ids.extend(_string_values(request.get("replan_signal_ids")))
        signal_ids.extend(_string_values(request.get("replan_signal_id")))
        signal_ids.extend(_string_values(request.get("replan_request_id")))
    for signal in _task_replan_signals(decision):
        if _text(signal.source_step_id) != clean_step_id:
            continue
        signal_id = _text(signal.signal_id)
        trigger = _text(signal.trigger)
        if signal_id:
            signal_ids.append(signal_id)
        if trigger:
            triggers.append(trigger)
    if _execution_request_runtime_stage(request, decision, clean_step_id) == "verify":
        dependency_signal_metadata = _dependency_verification_replan_metadata(
            decision,
            _execution_request_dependency_step_ids(request, step),
        )
        signal_ids.extend(dependency_signal_metadata["replan_signal_ids"])
        triggers.extend(dependency_signal_metadata["replan_triggers"])
    return {
        "replan_triggers": _dedupe(triggers),
        "replan_signal_ids": _dedupe(signal_ids),
    }


def _execution_request_runtime_stage(
    request: Mapping[str, Any] | None,
    decision: PlannerDecisionSnapshot,
    step_id: str,
) -> str:
    runtime_stage = _text((request or {}).get("runtime_stage"))
    if runtime_stage:
        return runtime_stage
    return _text(
        _task_core_step_runtime_metadata(decision, step_id).get("runtime_stage")
    )


def _execution_request_dependency_step_ids(
    request: Mapping[str, Any] | None,
    step: ToolPlanStepSnapshot | None,
) -> list[str]:
    dependency_ids: list[str] = []
    if request is not None:
        dependency_ids.extend(_string_values(request.get("depends_on")))
    if step is not None:
        dependency_ids.extend(str(item or "").strip() for item in list(step.depends_on))
    return _dedupe(item for item in dependency_ids if item)


def _dependency_verification_replan_metadata(
    decision: PlannerDecisionSnapshot,
    dependency_step_ids: Iterable[str],
) -> dict[str, list[str]]:
    dependency_ids = {
        str(step_id or "").strip()
        for step_id in dependency_step_ids
        if str(step_id or "").strip()
    }
    signal_ids: list[str] = []
    triggers: list[str] = []
    for signal in _task_replan_signals(decision):
        if _text(signal.source_step_id) not in dependency_ids:
            continue
        trigger = _text(signal.trigger)
        if trigger != "verification_failed":
            continue
        signal_id = _text(signal.signal_id)
        if signal_id:
            signal_ids.append(signal_id)
        triggers.append(trigger)
    return {
        "replan_triggers": _dedupe(triggers),
        "replan_signal_ids": _dedupe(signal_ids),
    }


def _task_core_step_runtime_metadata(
    decision: PlannerDecisionSnapshot,
    step_id: str,
) -> dict[str, Any]:
    if not step_id:
        return {}
    task_core = getattr(decision.plan, "task_core", None)
    if task_core is None:
        return {}
    for todo in list(getattr(task_core, "todos", []) or []):
        if _text(getattr(todo, "step_id", None)) == step_id:
            metadata = _runtime_metadata_subset(getattr(todo, "metadata", {}))
            if metadata:
                return metadata
    for checkpoint in list(getattr(task_core, "checkpoints", []) or []):
        if _text(getattr(checkpoint, "after_step_id", None)) == step_id:
            metadata = _runtime_metadata_subset(getattr(checkpoint, "payload", {}))
            if metadata:
                return metadata
    workspace = getattr(task_core, "workspace", None)
    for item in list(getattr(workspace, "items", []) or []):
        if _text(getattr(item, "source_step_id", None)) == step_id:
            metadata = _runtime_metadata_subset(getattr(item, "metadata", {}))
            if metadata:
                return metadata
    return {}


def _runtime_metadata_subset(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return _mapping_subset(
        value,
        (
            "runtime_doctrine",
            "runtime_stage",
            "runtime_role",
            "requires_observation",
            "requires_post_action_verification",
        ),
    )


def _mapping_subset(value: Mapping[str, Any], keys: Iterable[str]) -> dict[str, Any]:
    return {key: value[key] for key in keys if key in value}


def _task_replan_signals(decision: PlannerDecisionSnapshot) -> list[Any]:
    task_core = getattr(decision.plan, "task_core", None)
    if task_core is None:
        return []
    return list(getattr(task_core, "replan_signals", []) or [])


def _task_replan_signal_count(decision: PlannerDecisionSnapshot) -> int:
    return len(_task_replan_signals(decision))


def _allowed_tools(
    decision: PlannerDecisionSnapshot,
    allowed_tools: Iterable[str] | None,
) -> list[str]:
    explicit = [
        str(tool or "").strip()
        for tool in (allowed_tools or [])
        if str(tool or "").strip()
    ]
    if explicit:
        return explicit
    tools: list[str] = []
    for step in decision.plan.tool_plan.steps:
        tool_name = str(step.tool_name or "").strip()
        if tool_name:
            tools.append(tool_name)
        tools.extend(
            str(tool or "").strip()
            for tool in step.fallback_tools
            if str(tool or "").strip()
        )
    return _dedupe(tools)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    return text or None


def _steps_by_id(
    decision: PlannerDecisionSnapshot,
) -> dict[str, ToolPlanStepSnapshot]:
    return {
        str(step.step_id or "").strip(): step
        for step in decision.plan.tool_plan.steps
        if str(step.step_id or "").strip()
    }


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result
