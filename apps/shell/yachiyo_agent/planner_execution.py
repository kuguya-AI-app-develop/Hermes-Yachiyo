"""Convert runtime planner decisions into existing tool request payloads."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from apps.shell.agent.runtime.action_targets import action_target_matches
from apps.shell.agent.runtime.dispatch_semantics import is_semantic_safe_shortcut

from .app_name_hints import is_legacy_app_name_hint, legacy_app_name_hint
from .capture_plan_hints import capture_tool_preview
from .data_analysis_plan_hints import data_source_kind_hint
from .clipboard_plan_hints import clipboard_tool_preview
from .desktop_plan_hints import (
    app_control_tool_candidates,
    media_app_prepare_plan,
    media_app_query_search_plan,
    media_tool_preview,
)
from .file_access_plan_hints import file_access_tool_preview
from .runtime_planner import (
    RuntimePlanner,
    _explicit_ui_observation_before_action_requested,
)
from .schedule_plan_hints import schedule_tool_preview
from .system_plan_hints import system_tool_preview


_MACOS_SYSTEM_SURFACE_APP_NAMES = frozenset(
    {
        "Launchpad",
        "Control Center",
        "Notification Center",
    }
)


def planner_tool_requests(
    prompt: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _decision, requests = planner_decision_and_tool_requests(
        prompt,
        allowed_tools,
        metadata=metadata,
    )
    return requests


def planner_direct_tool_requests(
    prompt: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    _decision, requests = planner_direct_decision_and_tool_requests(
        prompt,
        allowed_tools,
        metadata=metadata,
    )
    return requests


def planner_execution_tool_requests(
    requests: Iterable[Mapping[str, Any]],
    allowed_tools: Iterable[str],
) -> list[dict[str, Any]]:
    """Normalize direct requests into the execution shape used by Chat entrypoints."""

    allowed = {
        str(tool or "").strip()
        for tool in allowed_tools
        if str(tool or "").strip()
    }
    normalized_requests = [
        _request_with_explicit_dispatch_contract(dict(request))
        for request in requests
        if isinstance(request, Mapping)
    ]
    if not normalized_requests:
        return []
    normalized_requests = _expand_inspect_app_execution_requests(normalized_requests, allowed)
    normalized_requests = _prepend_unknown_app_discovery_requests(normalized_requests, allowed)
    normalized_requests = _annotate_selected_app_placeholders_with_discovery_query(
        normalized_requests
    )
    normalized_requests = _defer_unknown_app_ui_element_operations_to_observation(
        normalized_requests,
        allowed,
    )
    normalized_requests = _append_unknown_app_post_execution_verification(
        normalized_requests,
        allowed,
    )
    if not _has_discovered_app_foreground_verification_chain(normalized_requests):
        normalized_requests = _collapse_app_foreground_direct_requests(normalized_requests, allowed)
    normalized_requests = _drop_redundant_app_foreground_prepare_requests(normalized_requests)
    normalized_requests = _drop_redundant_post_inspect_app_prepare_requests(normalized_requests)
    normalized_requests = _defer_search_result_clicks_to_observation(
        normalized_requests,
        allowed,
    )
    normalized_requests = _defer_semantic_ui_clicks_to_observation(
        normalized_requests,
        allowed,
    )
    normalized_requests = _defer_semantic_ui_types_to_observation(
        normalized_requests,
        allowed,
    )
    normalized_requests = _append_foreground_submit_verification_requests(
        normalized_requests,
        allowed,
    )
    normalized_requests = _scope_desktop_app_verification_requests(normalized_requests)
    normalized_requests = _defer_dependent_same_app_foreground_verification(
        normalized_requests
    )
    normalized_requests = _drop_redundant_execution_verification_requests(
        normalized_requests
    )
    verified_requests = runtime_execution_verified_tool_requests(
        normalized_requests,
        allowed,
    )
    return _execution_prefix_through_model_followup(
        _request_with_explicit_dispatch_contract(request)
        for request in verified_requests
    )


def _execution_prefix_through_model_followup(
    requests: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    normalized_requests = [
        dict(request) for request in requests if isinstance(request, Mapping)
    ]
    executable: list[dict[str, Any]] = []
    for index, normalized in enumerate(normalized_requests):
        executable.append(normalized)
        if normalized.get("continue_to_model"):
            if index + 1 < len(normalized_requests):
                dependent = normalized_requests[index + 1]
                if _deterministic_dispatch_can_follow_exact_verifier(
                    normalized,
                    dependent,
                    executable,
                ):
                    executable.append(dependent)
            break
    return executable


_PLANNER_EXECUTION_DISPATCH_ACTIONS = frozenset(
    {"dispatch_management", "dispatch_shortcut", "dispatch_submit"}
)


def _request_with_explicit_dispatch_contract(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    normalized = dict(request)
    if _clipboard_transfer_shortcut_requires_exact_verification(normalized):
        # Copy/paste receipts prove only key dispatch.  Keep them effectful so
        # the Runtime inserts and evaluates an independent readback before a
        # dependent submit is eligible.
        normalized["requires_post_action_verification"] = True
        return normalized
    task_todo = (
        normalized.get("task_todo")
        if isinstance(normalized.get("task_todo"), Mapping)
        else {}
    )
    todo_metadata = (
        task_todo.get("metadata")
        if isinstance(task_todo.get("metadata"), Mapping)
        else {}
    )
    dispatch_action = _canonical_planner_dispatch_action(normalized)
    if dispatch_action not in _PLANNER_EXECUTION_DISPATCH_ACTIONS:
        return normalized

    normalized["requires_post_action_verification"] = False
    if task_todo:
        normalized["task_todo"] = {
            **dict(task_todo),
            "metadata": {
                **dict(todo_metadata),
                "requires_post_action_verification": False,
            },
        }
    checkpoints = normalized.get("task_checkpoints")
    if isinstance(checkpoints, list):
        normalized["task_checkpoints"] = [
            {
                **dict(checkpoint),
                "verifies": [],
                "payload": {
                    **dict(
                        checkpoint.get("payload")
                        if isinstance(checkpoint.get("payload"), Mapping)
                        else {}
                    ),
                    "requires_post_action_verification": False,
                },
            }
            if isinstance(checkpoint, Mapping)
            else checkpoint
            for checkpoint in checkpoints
        ]
    checkpoint_policy = normalized.get("checkpoint_policy")
    if isinstance(checkpoint_policy, Mapping):
        normalized["checkpoint_policy"] = {
            **dict(checkpoint_policy),
            "requires_post_action_verification": False,
            "verification_target_step_ids": [],
            "verifies": [],
        }
    desktop_loop = normalized.get("desktop_loop")
    if isinstance(desktop_loop, Mapping):
        normalized["desktop_loop"] = {
            **dict(desktop_loop),
            "action": dispatch_action,
            "requires_post_action_verification": False,
            "can_auto_retry": False,
            "retry_tool": "",
            "retry_reason": "",
            "retry_input": {},
            "verification_target_step_ids": [],
        }
    action_target = normalized.get("action_target")
    if isinstance(action_target, Mapping):
        normalized["action_target"] = {
            **dict(action_target),
            "action": dispatch_action,
        }
    normalized["observation_retry"] = {}
    normalized["replan_triggers"] = [
        trigger
        for trigger in _string_list(normalized.get("replan_triggers"))
        if trigger not in {"verification_failed", "verification_unavailable"}
    ]
    return normalized


def _clipboard_transfer_shortcut_requires_exact_verification(
    request: Mapping[str, Any],
) -> bool:
    tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
    if tool_name not in {
        "desktop.safe_shortcut",
        "desktop.shortcut",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
    }:
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return str(payload.get("action") or "").strip().lower() in {"copy", "paste"}


def _canonical_planner_dispatch_action(request: Mapping[str, Any]) -> str:
    """Derive dispatch semantics from tool input and optional canonical todo.

    Top-level ``request.action`` is projection metadata and is intentionally not
    authoritative. A todo may only narrow the tool/input allowlist; it can never
    upgrade an effectful operation into a receipt-only dispatch.
    """

    request_input = (
        request.get("input")
        if isinstance(request.get("input"), Mapping)
        else {}
    )
    tool_input_dispatch_action = _planner_dispatch_action_for_tool_input(
        str(request.get("tool") or request.get("tool_name") or "").strip(),
        request_input,
    )
    task_todo = (
        request.get("task_todo")
        if isinstance(request.get("task_todo"), Mapping)
        else {}
    )
    todo_metadata = (
        task_todo.get("metadata")
        if isinstance(task_todo.get("metadata"), Mapping)
        else {}
    )
    todo_action = str(todo_metadata.get("action") or "").strip()
    if todo_action:
        return (
            todo_action
            if todo_action == tool_input_dispatch_action
            and todo_action in _PLANNER_EXECUTION_DISPATCH_ACTIONS
            else ""
        )
    return (
        tool_input_dispatch_action
        if tool_input_dispatch_action in _PLANNER_EXECUTION_DISPATCH_ACTIONS
        else ""
    )


def _planner_dispatch_action_for_tool_input(
    tool_name: str,
    request_input: Mapping[str, Any],
) -> str:
    clean_tool = str(tool_name or "").strip()
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
        if not _planner_dispatch_input_has_only(request_input, {"action"}):
            return ""
        action = str(request_input.get("action") or "submit").strip().lower()
        return "dispatch_submit" if action in {"send", "submit", "confirm"} else ""
    if clean_tool in {"desktop.hotkey", "desktop.shortcut"}:
        if not _planner_dispatch_input_has_only(
            request_input,
            {"key", "modifiers"},
        ):
            return ""
        return (
            "dispatch_shortcut"
            if str(request_input.get("key") or "").strip()
            else ""
        )
    if clean_tool == "desktop.safe_shortcut":
        if is_semantic_safe_shortcut(clean_tool, request_input):
            return ""
        if not _planner_dispatch_input_has_only(request_input, {"action"}):
            return ""
        action = str(request_input.get("action") or "").strip().lower()
        return "dispatch_shortcut" if action else ""
    if clean_tool == "desktop.safe_key":
        if not _planner_dispatch_input_has_only(
            request_input,
            {"action", "repeat_count"},
        ):
            return ""
        return (
            "dispatch_shortcut"
            if str(request_input.get("action") or "").strip()
            else ""
        )
    if clean_tool == "desktop.safe_scroll":
        if not _planner_dispatch_input_has_only(
            request_input,
            {"direction", "pages"},
        ):
            return ""
        direction = str(request_input.get("direction") or "").strip().lower()
        return "dispatch_shortcut" if direction in {"up", "down"} else ""
    if clean_tool == "desktop.safe_click":
        if not _planner_dispatch_input_has_only(request_input, {"x", "y"}):
            return ""
        return (
            "dispatch_shortcut"
            if request_input.get("x") is not None
            and request_input.get("y") is not None
            else ""
        )
    return ""


def _planner_dispatch_input_has_only(
    request_input: Mapping[str, Any],
    allowed_keys: set[str],
) -> bool:
    return set(request_input).issubset(allowed_keys)


def _deterministic_dispatch_can_follow_exact_verifier(
    verifier: Mapping[str, Any],
    dependent: Mapping[str, Any],
    executable_prefix: Iterable[Mapping[str, Any]],
) -> bool:
    if str(verifier.get("runtime_stage") or "").strip() != "verify":
        return False
    if str(verifier.get("runtime_role") or "").strip() != "verify_result":
        return False
    if any(
        verifier.get(key) not in (None, "", [], {})
        for key in (
            "deferred_tool",
            "deferred_input",
            "deferred_context",
            "deferred_continuation",
        )
    ):
        return False
    if str(verifier.get("status") or "").strip().lower() in {
        "blocked",
        "failed",
        "unavailable",
    }:
        return False
    verification_targets = verifier.get("verification_targets")
    if not isinstance(verification_targets, list) or not any(
        isinstance(target, Mapping)
        and str(target.get("step_id") or "").strip()
        for target in verification_targets
    ):
        return False

    verifier_step_id = str(
        verifier.get("step_id") or verifier.get("planner_step_id") or ""
    ).strip()
    dependencies = _string_list(dependent.get("depends_on"))
    if not verifier_step_id or verifier_step_id not in dependencies:
        return False
    completed_step_ids = {
        str(request.get("step_id") or request.get("planner_step_id") or "").strip()
        for request in executable_prefix
        if str(
            request.get("step_id") or request.get("planner_step_id") or ""
        ).strip()
    }
    if any(step_id not in completed_step_ids for step_id in dependencies):
        return False
    if dependent.get("approval_required") is True:
        return False
    if dependent.get("continue_to_model") is True:
        return False
    if dependent.get("requires_post_action_verification") is not False:
        return False
    if any(
        dependent.get(key) not in (None, "", [], {})
        for key in (
            "deferred_tool",
            "deferred_input",
            "deferred_context",
            "deferred_continuation",
        )
    ):
        return False
    dispatch_action = _canonical_planner_dispatch_action(dependent)
    if dispatch_action not in {"dispatch_management", "dispatch_shortcut"}:
        return False
    return not _execution_request_has_unresolved_input(dependent)


def _execution_request_has_unresolved_input(request: Mapping[str, Any]) -> bool:
    payload = request.get("input")

    def unresolved(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(unresolved(item) for item in value.values())
        if isinstance(value, Iterable) and not isinstance(value, (str, bytes)):
            return any(unresolved(item) for item in value)
        if not isinstance(value, str):
            return False
        text = value.strip()
        return bool(text.startswith("<") and text.endswith(">"))

    return unresolved(payload)


def planner_full_plan_execution_tool_requests(
    requests: Iterable[Mapping[str, Any]],
    allowed_tools: Iterable[str],
) -> list[dict[str, Any]]:
    """Normalize Studio full-plan requests without collapsing planned operations."""

    allowed = {
        str(tool or "").strip()
        for tool in allowed_tools
        if str(tool or "").strip()
    }
    normalized_requests = [
        dict(request) for request in requests if isinstance(request, Mapping)
    ]
    if not normalized_requests:
        return []
    normalized_requests = _prepend_unknown_app_discovery_requests(
        normalized_requests,
        allowed,
    )
    normalized_requests = _annotate_selected_app_placeholders_with_discovery_query(
        normalized_requests
    )
    normalized_requests = _append_unknown_app_post_execution_verification(
        normalized_requests,
        allowed,
    )
    normalized_requests = _append_foreground_submit_verification_requests(
        normalized_requests,
        allowed,
    )
    normalized_requests = _scope_desktop_app_verification_requests(normalized_requests)
    normalized_requests = _defer_dependent_same_app_foreground_verification(
        normalized_requests
    )
    return runtime_execution_verified_tool_requests(normalized_requests, allowed)


def runtime_execution_verified_tool_requests(
    requests: Iterable[Mapping[str, Any]],
    allowed_tools: Iterable[str],
    *,
    include_model_app_foreground: bool = False,
) -> list[dict[str, Any]]:
    """Append low-risk verification reads for execution requests from any source."""

    allowed = {
        str(tool or "").strip()
        for tool in allowed_tools
        if str(tool or "").strip()
    }
    normalized_requests = [
        dict(request) for request in requests if isinstance(request, Mapping)
    ]
    if not normalized_requests:
        return []
    verified_requests = _append_system_volume_status_verification_requests(
        normalized_requests,
        allowed,
    )
    if include_model_app_foreground:
        verified_requests = _append_model_app_foreground_verification_requests(
            verified_requests,
            allowed,
        )
    return verified_requests


def _prepend_unknown_app_discovery_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if "desktop.list_apps" not in allowed:
        return requests
    discovered_queries: dict[str, str] = {}
    normalized: list[dict[str, Any]] = []
    for request in requests:
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        if tool_name == "desktop.list_apps":
            query = str(payload.get("query") or "").strip()
            if query:
                for query_key in _discovery_query_keys(query):
                    discovered_queries.setdefault(query_key, query)
            normalized.append(request)
            continue
        app_name = str(payload.get("app_name") or "").strip()
        query_keys = _discovery_query_keys(app_name)
        existing_query = next(
            (
                discovered_queries[query_key]
                for query_key in query_keys
                if query_key in discovered_queries
            ),
            "",
        )
        if existing_query and not _is_macos_system_surface_app_name(app_name):
            request = _request_with_desktop_app_selection_source(
                request,
                existing_query,
            )
        elif _request_needs_app_discovery_first(tool_name, payload, request):
            if query_keys:
                normalized.append(
                    _desktop_app_discovery_request_for_execution(
                        app_name,
                        request,
                    )
                )
                existing_query = app_name
                for query_key in query_keys:
                    discovered_queries.setdefault(query_key, app_name)
            if query_keys:
                request = _request_with_desktop_app_selection_source(
                    request,
                    existing_query or app_name,
                )
        normalized.append(request)
    return normalized


def _desktop_app_discovery_request_for_execution(
    app_name: str,
    source_request: Mapping[str, Any],
) -> dict[str, Any]:
    request = _request(
        "desktop.list_apps",
        {"query": app_name, "limit": 20},
        planning_reason=str(
            source_request.get("planning_reason")
            or "planner_desktop_app_discovery"
        ).strip()
        or "planner_desktop_app_discovery",
    )
    _inherit_request_context_without_step(request, source_request)
    request["capability_id"] = "desktop.app_discovery"
    request["runtime_doctrine"] = "discover_operate_verify"
    request["runtime_stage"] = "discover"
    request["runtime_role"] = "find_target_app"
    request["requires_observation"] = True
    return request


def _request_needs_app_discovery_first(
    tool_name: str,
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
) -> bool:
    if not _tool_uses_app_name_for_foreground_execution(tool_name):
        return False
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name or _is_selected_desktop_app_placeholder(app_name):
        return False
    if _is_macos_system_surface_app_name(app_name):
        return False
    if is_legacy_app_name_hint(app_name):
        return False
    if _is_desktop_app_selection_source(payload.get("selection_source")):
        return False
    input_resolution = (
        request.get("input_resolution")
        if isinstance(request.get("input_resolution"), Mapping)
        else {}
    )
    if _is_desktop_app_selection_source(input_resolution.get("source_tool")):
        return False
    return True


def _is_macos_system_surface_app_name(app_name: str) -> bool:
    canonical = str(legacy_app_name_hint(app_name) or app_name or "").strip()
    return canonical in _MACOS_SYSTEM_SURFACE_APP_NAMES


def _request_with_desktop_app_selection_source(
    request: dict[str, Any],
    app_name: str,
) -> dict[str, Any]:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    clean_app_name = str(app_name or payload.get("app_name") or "").strip()
    if not clean_app_name:
        return request
    return {
        **request,
        "input": {
            **dict(payload),
            "selection_source": "desktop.list_apps",
            "query": clean_app_name,
        },
    }


def _annotate_selected_app_placeholders_with_discovery_query(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    discovery_query = ""
    normalized: list[dict[str, Any]] = []
    for request in requests:
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        if tool_name == "desktop.list_apps":
            query = str(payload.get("query") or "").strip()
            if query:
                discovery_query = query
            normalized.append(request)
            continue
        if not discovery_query:
            normalized.append(request)
            continue
        if not _payload_uses_selected_desktop_app(payload):
            normalized.append(request)
            continue
        normalized.append(
            {
                **request,
                "input": {
                    **dict(payload),
                    "selection_source": "desktop.list_apps",
                    "query": str(payload.get("query") or discovery_query).strip(),
                },
            }
        )
    return normalized


def _payload_uses_selected_desktop_app(payload: Mapping[str, Any]) -> bool:
    if str(payload.get("app_name") or "").strip() == "<selected app from desktop.list_apps>":
        return True
    return str(payload.get("selection_source") or "").strip() == "desktop.list_apps"


def _is_selected_desktop_app_placeholder(value: Any) -> bool:
    return str(value or "").strip() in {
        "<selected app from desktop.list_apps>",
        "<selected app from desktop.running_apps>",
    }


def _is_desktop_app_selection_source(value: Any) -> bool:
    return str(value or "").strip() in {
        "desktop.list_apps",
        "desktop.running_apps",
    }


def _defer_unknown_app_ui_element_operations_to_observation(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if not requests or not {"desktop.ui_elements", "desktop.read_ui"}.intersection(allowed):
        return requests
    normalized: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        if not _should_defer_unknown_app_ui_element_operation(tool_name, payload, request):
            normalized.append(request)
            continue
        prepare_request = _unknown_app_ui_observation_prepare_request(
            tool_name,
            request,
            allowed,
        )
        if not prepare_request:
            normalized.append(request)
            continue
        if not _last_request_matches_tool_and_input(normalized, prepare_request):
            normalized.append(prepare_request)
        observation = _unknown_app_ui_observation_request(
            request,
            allowed,
            deferred_continuation=_unknown_app_ui_deferred_continuation(
                requests,
                index,
                tool_name,
                allowed,
            ),
        )
        immediate_verifier = requests[index + 1] if index + 1 < len(requests) else {}
        if _is_execution_verification_request(immediate_verifier):
            observation["deferred_verifier_context"] = {
                **_deferred_request_context(immediate_verifier),
                "tool": str(immediate_verifier.get("tool") or "").strip(),
                "source": str(
                    immediate_verifier.get("source") or "runtime_verification"
                ).strip(),
                "planning_reason": str(
                    immediate_verifier.get("planning_reason")
                    or "planner_desktop_operation"
                ).strip(),
            }
        normalized.append(observation)
        return normalized
    return normalized


def _should_defer_unknown_app_ui_element_operation(
    tool_name: str,
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
) -> bool:
    if tool_name not in {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
    }:
        return False
    if str(request.get("source") or "").strip() != "runtime_planner":
        return False
    if not _request_targets_unknown_discovered_app(tool_name, payload, request):
        return False
    target = str(payload.get("target") or "").strip()
    text = str(payload.get("text") or "").strip()
    return bool(target or text)


def _unknown_app_ui_observation_prepare_request(
    tool_name: str,
    request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name:
        return {}
    mode = "focus" if tool_name.startswith("app.focus_and_") else "open"
    prepare_tool = _first_allowed(
        app_control_tool_candidates(mode),
        allowed,
    )
    if not prepare_tool:
        return {}
    prepare_payload = {"app_name": app_name}
    _copy_app_selection_metadata(payload, prepare_payload)
    prepare = _request(
        prepare_tool,
        prepare_payload,
        planning_reason=str(
            request.get("planning_reason") or "planner_desktop_operation"
        ).strip()
        or "planner_desktop_operation",
    )
    if (
        _request_targets_unknown_discovered_app(tool_name, payload, request)
        and not str(prepare_payload.get("query") or "").strip()
    ):
        prepare = _request_with_desktop_app_selection_source(prepare, app_name)
    _inherit_request_context_without_step(prepare, request)
    return prepare


def _unknown_app_ui_observation_request(
    request: Mapping[str, Any],
    allowed: set[str],
    *,
    deferred_continuation: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    observe_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    observe_payload = {
        key: payload[key]
        for key in ("app_name", "role_filter", "limit")
        if key in payload and payload[key] not in (None, "")
    }
    if observe_payload.get("app_name"):
        _copy_app_selection_metadata(payload, observe_payload)
    if "limit" not in observe_payload:
        observe_payload["limit"] = 80
    observe = _request(
        observe_tool,
        observe_payload,
        planning_reason=str(
            request.get("planning_reason") or "planner_desktop_operation"
        ).strip()
        or "planner_desktop_operation",
    )
    observe["continue_to_model"] = True
    observe["deferred_tool"] = str(request.get("tool") or "").strip()
    observe["deferred_input"] = dict(payload)
    observe["deferred_context"] = _deferred_request_context(request)
    continuation = [
        dict(item)
        for item in deferred_continuation
        if isinstance(item, Mapping)
    ]
    if continuation:
        observe["deferred_continuation"] = continuation
    _inherit_request_context_without_step(observe, request)
    return observe


def _unknown_app_ui_deferred_continuation(
    requests: list[dict[str, Any]],
    index: int,
    tool_name: str,
    allowed: set[str],
) -> list[dict[str, Any]]:
    if tool_name not in {
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
    }:
        return []
    continuation, _continuation_indexes = _semantic_ui_type_deferred_continuation(
        requests,
        index,
        allowed,
    )
    return continuation


def _defer_search_result_clicks_to_observation(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if not requests or not {"desktop.ui_elements", "desktop.read_ui"}.intersection(allowed):
        return requests
    normalized: list[dict[str, Any]] = []
    skip_indexes: set[int] = set()
    for index, request in enumerate(requests):
        if index in skip_indexes:
            continue
        normalized.append(request)
        if str(request.get("tool") or "").strip() != "desktop.search_submit":
            continue
        next_request = requests[index + 1] if index + 1 < len(requests) else {}
        if not _is_search_result_click_request(next_request):
            continue
        observation = _search_result_observation_request(next_request, allowed)
        if not observation:
            continue
        normalized.append(observation)
        skip_indexes.add(index + 1)
        after_click = requests[index + 2] if index + 2 < len(requests) else {}
        if _is_execution_verification_request(after_click):
            observation["deferred_verifier_context"] = {
                **_deferred_request_context(after_click),
                "tool": str(after_click.get("tool") or "").strip(),
                "source": str(
                    after_click.get("source") or "runtime_verification"
                ).strip(),
                "planning_reason": str(
                    after_click.get("planning_reason")
                    or "planner_desktop_operation"
                ).strip(),
            }
            skip_indexes.add(index + 2)
    return normalized


def _is_search_result_click_request(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "desktop.click_ui_element",
    }:
        return False
    if str(request.get("source") or "").strip() != "runtime_planner":
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    target = str(payload.get("target") or "").strip()
    if not target:
        return False
    return bool(
        _search_result_target_is_ordinal(target)
        or "result" in target.casefold()
        or "结果" in target
    )


def _is_execution_verification_request(request: Mapping[str, Any]) -> bool:
    if not isinstance(request, Mapping):
        return False
    return str(request.get("tool") or "").strip() in _EXECUTION_VERIFICATION_TOOLS


def _search_result_target_is_ordinal(target: str) -> bool:
    clean = str(target or "").strip().casefold()
    if clean in {
        "first result",
        "first item",
        "top result",
        "第一个结果",
        "第1个结果",
        "第一项",
        "第一个",
        "首个结果",
    }:
        return True
    return bool(
        re.fullmatch(r"(?:result|item)\s*\d+", clean)
        or re.fullmatch(r"第\s*\d+\s*(?:个)?(?:结果|项目|项)?", clean)
    )


def _search_result_observation_request(
    click_request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    payload = click_request.get("input") if isinstance(click_request.get("input"), Mapping) else {}
    observe_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    if not observe_tool:
        return {}
    observe_payload = {
        key: payload[key]
        for key in ("app_name", "role_filter", "limit")
        if key in payload and payload[key] not in (None, "")
    }
    if observe_payload.get("app_name"):
        _copy_app_selection_metadata(payload, observe_payload)
    if "limit" not in observe_payload:
        observe_payload["limit"] = 80
    observe = _request(
        observe_tool,
        observe_payload,
        planning_reason=str(
            click_request.get("planning_reason") or "planner_desktop_operation"
        ).strip()
        or "planner_desktop_operation",
    )
    observe["continue_to_model"] = True
    observe["deferred_tool"] = str(click_request.get("tool") or "").strip()
    observe["deferred_input"] = dict(payload)
    observe["deferred_context"] = _deferred_request_context(click_request)
    _inherit_request_context_without_step(observe, click_request)
    return observe


def _defer_semantic_ui_clicks_to_observation(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if not requests or not {"desktop.ui_elements", "desktop.read_ui"}.intersection(allowed):
        return requests
    normalized: list[dict[str, Any]] = []
    skip_indexes: set[int] = set()
    for index, request in enumerate(requests):
        if index in skip_indexes:
            continue
        if not _should_defer_semantic_ui_click_request(request):
            normalized.append(request)
            continue
        if _has_recent_ui_observation_for_action(normalized, request):
            normalized.append(request)
            continue
        observation = _semantic_ui_click_observation_request(request, allowed)
        if not observation:
            normalized.append(request)
            continue
        prepare = _semantic_ui_click_observation_prepare_request(request, allowed)
        if _semantic_ui_click_requires_prepare(request) and not prepare:
            normalized.append(request)
            continue
        if prepare and not _last_request_matches_tool_and_input(normalized, prepare):
            normalized.append(prepare)
        normalized.append(observation)
        after_click = requests[index + 1] if index + 1 < len(requests) else {}
        if _is_execution_verification_request(after_click):
            observation["deferred_verifier_context"] = {
                **_deferred_request_context(after_click),
                "tool": str(after_click.get("tool") or "").strip(),
                "source": str(after_click.get("source") or "runtime_verification").strip(),
                "planning_reason": str(
                    after_click.get("planning_reason") or "planner_desktop_operation"
                ).strip(),
            }
            skip_indexes.add(index + 1)
    return normalized


def _should_defer_semantic_ui_click_request(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "desktop.click_ui_element",
    }:
        return False
    if str(request.get("source") or "").strip() != "runtime_planner":
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return bool(str(payload.get("target") or "").strip())


def _defer_semantic_ui_types_to_observation(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if not requests or not {"desktop.ui_elements", "desktop.read_ui"}.intersection(allowed):
        return requests
    normalized: list[dict[str, Any]] = []
    skip_indexes: set[int] = set()
    for index, request in enumerate(requests):
        if index in skip_indexes:
            continue
        if not _should_defer_semantic_ui_type_request(request):
            normalized.append(request)
            continue
        continuation, continuation_indexes = _semantic_ui_type_deferred_continuation(
            requests,
            index,
            allowed,
        )
        if _has_later_mutation_before_verification(requests, index) and not continuation:
            normalized.append(request)
            continue
        if _has_recent_ui_observation_for_action(normalized, request):
            normalized.append(request)
            continue
        observation = _semantic_ui_type_observation_request(request, allowed)
        if not observation:
            normalized.append(request)
            continue
        prepare = _semantic_ui_type_observation_prepare_request(request, allowed)
        if _semantic_ui_type_requires_prepare(request) and not prepare:
            normalized.append(request)
            continue
        if prepare and not _last_request_matches_tool_and_input(normalized, prepare):
            normalized.append(prepare)
        if continuation:
            observation["deferred_continuation"] = continuation
        normalized.append(observation)
        skip_indexes.update(continuation_indexes)
        after_type = requests[index + 1] if index + 1 < len(requests) else {}
        if not continuation and _is_execution_verification_request(after_type):
            skip_indexes.add(index + 1)
    return normalized


def _should_defer_semantic_ui_type_request(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in {
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.type_into_ui_element",
    }:
        return False
    if str(request.get("source") or "").strip() != "runtime_planner":
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return bool(
        str(payload.get("target") or "").strip()
        and str(payload.get("text") or "").strip()
    )


def _has_later_mutation_before_verification(
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    for later_request in requests[index + 1 :]:
        later_tool = str(later_request.get("tool") or "").strip()
        if later_tool in _EXECUTION_VERIFICATION_TOOLS:
            return False
        if later_tool in _EXECUTION_MUTATION_TOOLS or _tool_continues_foreground_operation_chain(
            later_tool
        ):
            return True
    return False


def _semantic_ui_type_deferred_continuation(
    requests: list[dict[str, Any]],
    index: int,
    allowed: set[str],
) -> tuple[list[dict[str, Any]], set[int]]:
    continuation: list[dict[str, Any]] = []
    continuation_indexes: set[int] = set()
    saw_mutation = False
    for later_index, later_request in enumerate(requests[index + 1 :], start=index + 1):
        later_tool = str(later_request.get("tool") or "").strip()
        if _is_execution_verification_request(later_request):
            if not saw_mutation:
                return [], set()
            continuation.append(dict(later_request))
            continuation_indexes.add(later_index)
            continuation = _defer_search_result_clicks_to_observation(
                continuation,
                allowed,
            )
            return continuation, continuation_indexes
        if not (
            later_tool in _EXECUTION_MUTATION_TOOLS
            or _tool_continues_foreground_operation_chain(later_tool)
        ):
            return [], set()
        saw_mutation = True
        continuation.append(dict(later_request))
        continuation_indexes.add(later_index)
    if not saw_mutation:
        return [], set()
    continuation = _defer_search_result_clicks_to_observation(
        continuation,
        allowed,
    )
    return continuation, continuation_indexes


def _semantic_ui_type_observation_request(
    type_request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    payload = type_request.get("input") if isinstance(type_request.get("input"), Mapping) else {}
    observe_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    if not observe_tool:
        return {}
    observe_payload = {
        key: payload[key]
        for key in ("app_name", "role_filter", "limit")
        if key in payload and payload[key] not in (None, "")
    }
    if observe_payload.get("app_name"):
        _copy_app_selection_metadata(payload, observe_payload)
    if "limit" not in observe_payload:
        observe_payload["limit"] = 80
    observe = _request(
        observe_tool,
        observe_payload,
        planning_reason=str(
            type_request.get("planning_reason") or "planner_desktop_operation"
        ).strip()
        or "planner_desktop_operation",
    )
    observe["continue_to_model"] = True
    observe["deferred_tool"] = str(type_request.get("tool") or "").strip()
    observe["deferred_input"] = dict(payload)
    observe["deferred_context"] = _deferred_request_context(type_request)
    _inherit_request_context_without_step(observe, type_request)
    return observe


def _semantic_ui_type_observation_prepare_request(
    type_request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    tool_name = str(type_request.get("tool") or "").strip()
    if not tool_name.startswith("app.open_and_") and not tool_name.startswith("app.focus_and_"):
        return {}
    return _unknown_app_ui_observation_prepare_request(tool_name, type_request, allowed)


def _semantic_ui_type_requires_prepare(type_request: Mapping[str, Any]) -> bool:
    tool_name = str(type_request.get("tool") or "").strip()
    return tool_name.startswith("app.open_and_") or tool_name.startswith("app.focus_and_")


def _has_recent_ui_observation_for_action(
    previous_requests: list[dict[str, Any]],
    action_request: Mapping[str, Any],
) -> bool:
    skipped_prepare = False
    for previous in reversed(previous_requests):
        previous_tool = str(previous.get("tool") or "").strip()
        if (
            not skipped_prepare
            and previous_tool in {"app.open", "app.focus", "desktop.open_app", "desktop.focus_app"}
            and _prepare_request_matches_action_target(previous, action_request)
        ):
            skipped_prepare = True
            continue
        return _ui_observation_matches_action_target(previous, action_request)
    return False


def _prepare_request_matches_action_target(
    prepare_request: Mapping[str, Any],
    action_request: Mapping[str, Any],
) -> bool:
    action_payload = (
        action_request.get("input") if isinstance(action_request.get("input"), Mapping) else {}
    )
    app_name = str(action_payload.get("app_name") or "").strip()
    if not app_name:
        return True
    prepare_payload = (
        prepare_request.get("input") if isinstance(prepare_request.get("input"), Mapping) else {}
    )
    return str(prepare_payload.get("app_name") or "").strip() == app_name


def _ui_observation_matches_action_target(
    observation_request: Mapping[str, Any],
    action_request: Mapping[str, Any],
) -> bool:
    observation_tool = str(observation_request.get("tool") or "").strip()
    if observation_tool not in {"desktop.ui_elements", "desktop.read_ui", "desktop.inspect_app"}:
        return False
    action_payload = (
        action_request.get("input") if isinstance(action_request.get("input"), Mapping) else {}
    )
    observation_payload = (
        observation_request.get("input")
        if isinstance(observation_request.get("input"), Mapping)
        else {}
    )
    action_app_name = str(action_payload.get("app_name") or "").strip()
    observation_app_name = str(observation_payload.get("app_name") or "").strip()
    if action_app_name and observation_app_name and action_app_name != observation_app_name:
        return False
    return True


def _semantic_ui_click_observation_request(
    click_request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    payload = click_request.get("input") if isinstance(click_request.get("input"), Mapping) else {}
    observe_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    if not observe_tool:
        return {}
    observe_payload = {
        key: payload[key]
        for key in ("app_name", "role_filter", "limit")
        if key in payload and payload[key] not in (None, "")
    }
    if observe_payload.get("app_name"):
        _copy_app_selection_metadata(payload, observe_payload)
    if "limit" not in observe_payload:
        observe_payload["limit"] = 80
    observe = _request(
        observe_tool,
        observe_payload,
        planning_reason=str(
            click_request.get("planning_reason") or "planner_desktop_operation"
        ).strip()
        or "planner_desktop_operation",
    )
    observe["continue_to_model"] = True
    observe["deferred_tool"] = str(click_request.get("tool") or "").strip()
    observe["deferred_input"] = dict(payload)
    observe["deferred_context"] = _deferred_request_context(click_request)
    _inherit_request_context_without_step(observe, click_request)
    return observe


def _semantic_ui_click_observation_prepare_request(
    click_request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    tool_name = str(click_request.get("tool") or "").strip()
    if not tool_name.startswith("app.open_and_") and not tool_name.startswith("app.focus_and_"):
        return {}
    return _unknown_app_ui_observation_prepare_request(tool_name, click_request, allowed)


def _semantic_ui_click_requires_prepare(click_request: Mapping[str, Any]) -> bool:
    tool_name = str(click_request.get("tool") or "").strip()
    return tool_name.startswith("app.open_and_") or tool_name.startswith("app.focus_and_")


def _last_request_matches_tool_and_input(
    requests: list[dict[str, Any]],
    request: Mapping[str, Any],
) -> bool:
    if not requests:
        return False
    previous = requests[-1]
    if str(previous.get("tool") or "").strip() != str(request.get("tool") or "").strip():
        return False
    previous_input = previous.get("input") if isinstance(previous.get("input"), Mapping) else {}
    request_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if dict(previous_input) == dict(request_input):
        return True
    if str(request.get("tool") or "").strip() in {
        "app.open",
        "app.focus",
        "desktop.open_app",
        "desktop.focus_app",
    }:
        return _app_prepare_inputs_match(previous_input, request_input)
    return False


def _app_prepare_inputs_match(
    previous_input: Mapping[str, Any],
    request_input: Mapping[str, Any],
) -> bool:
    previous_app = str(previous_input.get("app_name") or "").strip()
    request_app = str(request_input.get("app_name") or "").strip()
    if not previous_app or previous_app != request_app:
        return False
    ignored_keys = {"selection_source", "app_selection_source", "query"}
    previous_payload = {
        key: value
        for key, value in dict(previous_input).items()
        if key not in ignored_keys
    }
    request_payload = {
        key: value
        for key, value in dict(request_input).items()
        if key not in ignored_keys
    }
    return previous_payload == request_payload


def _deferred_request_context(source: Mapping[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in (
        "step_id",
        "planner_step_id",
        "capability_id",
        "approval_required",
        "risk_level",
        "depends_on",
        "runtime_stage",
        "runtime_role",
        "action_target",
        "task_todo",
        "task_checkpoints",
        "task_workspace_items",
        "verification_targets",
        "task_verification_targets",
    ):
        value = source.get(key)
        if value not in (None, "", [], {}):
            context[key] = value
    canonical_action_target = _deferred_canonical_action_target(source)
    if canonical_action_target:
        context["action_target"] = canonical_action_target
    return context


def _deferred_canonical_action_target(source: Mapping[str, Any]) -> dict[str, Any]:
    """Keep the planner target that the immutable goal contract was bound to."""

    raw_checkpoints = source.get("task_checkpoints")
    for raw_checkpoint in (
        raw_checkpoints if isinstance(raw_checkpoints, (list, tuple)) else ()
    ):
        if not isinstance(raw_checkpoint, Mapping):
            continue
        payload = (
            raw_checkpoint.get("payload")
            if isinstance(raw_checkpoint.get("payload"), Mapping)
            else {}
        )
        action_target = payload.get("action_target")
        if isinstance(action_target, Mapping) and action_target:
            return dict(action_target)
    raw_workspace_items = source.get("task_workspace_items")
    for raw_item in (
        raw_workspace_items if isinstance(raw_workspace_items, (list, tuple)) else ()
    ):
        if not isinstance(raw_item, Mapping):
            continue
        metadata = (
            raw_item.get("metadata")
            if isinstance(raw_item.get("metadata"), Mapping)
            else {}
        )
        action_target = metadata.get("action_target")
        if isinstance(action_target, Mapping) and action_target:
            return dict(action_target)
    action_target = source.get("action_target")
    return dict(action_target) if isinstance(action_target, Mapping) else {}


_REQUEST_CONTEXT_WITHOUT_STEP_KEYS = (
    "request_id",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "core_id",
    "workspace_id",
    "task_id",
    "run_id",
    "runtime_doctrine",
    "desktop_provider_health_probe",
    "desktop_provider_route_readonly",
    "desktop_provider_route_foreground",
    "requires_observation",
    "requires_post_action_verification",
    "replan_signal_ids",
    "replan_triggers",
)


def _inherit_request_context_without_step(
    target: dict[str, Any],
    source: Mapping[str, Any],
) -> None:
    for key in _REQUEST_CONTEXT_WITHOUT_STEP_KEYS:
        value = source.get(key)
        if value in (None, "", [], {}):
            continue
        target[key] = value


_OPEN_PATH_WITH_APP_TOOLS = frozenset({"desktop.open_path_with_app", "app.open_path_with_app"})


def _is_open_path_with_app_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in _OPEN_PATH_WITH_APP_TOOLS


def _tool_uses_app_name_for_foreground_execution(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool in {
            "app.open",
            "app.focus",
            "desktop.open_app",
            "desktop.focus_app",
            "desktop.inspect_app",
        }
        or _is_open_path_with_app_tool(clean_tool)
        or clean_tool.startswith("app.open_and_")
        or clean_tool.startswith("app.focus_and_")
    )


def _discovery_query_key(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _discovery_query_keys(value: str) -> set[str]:
    clean = str(value or "").strip()
    if not clean:
        return set()
    keys = {_discovery_query_key(clean)}
    canonical = str(legacy_app_name_hint(clean) or "").strip()
    if canonical:
        keys.add(_discovery_query_key(canonical))
    return {key for key in keys if key}


def _append_unknown_app_post_execution_verification(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if not _has_unknown_discovered_app_query(requests):
        return requests
    normalized: list[dict[str, Any]] = []
    pending_unknown_app_chain = False
    for index, request in enumerate(requests):
        normalized.append(request)
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        starts_unknown_app_chain = _request_targets_unknown_discovered_app(
            tool_name,
            payload,
            request,
        )
        if starts_unknown_app_chain:
            pending_unknown_app_chain = True
        elif tool_name in _EXECUTION_VERIFICATION_TOOLS:
            pending_unknown_app_chain = False
            continue
        elif not pending_unknown_app_chain or not _tool_continues_foreground_operation_chain(tool_name):
            continue
        if _has_later_execution_verification_before_mutation(requests, index):
            pending_unknown_app_chain = False
            continue
        if _has_later_foreground_operation_before_verification(requests, index):
            continue
        verification = _unknown_app_execution_verification_request(
            tool_name,
            request,
            allowed,
        )
        if verification:
            normalized.append(verification)
            pending_unknown_app_chain = False
    return normalized


def _request_targets_unknown_discovered_app(
    tool_name: str,
    payload: Mapping[str, Any],
    request: Mapping[str, Any],
) -> bool:
    if not _tool_changes_unknown_app_foreground_state(tool_name):
        return False
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name or app_name == "<selected app from desktop.list_apps>":
        return False
    if str(payload.get("selection_source") or "").strip() == "desktop.list_apps":
        return True
    input_resolution = (
        request.get("input_resolution")
        if isinstance(request.get("input_resolution"), Mapping)
        else {}
    )
    if str(input_resolution.get("source_tool") or "").strip() == "desktop.list_apps":
        return True
    return not is_legacy_app_name_hint(app_name)


def _tool_changes_unknown_app_foreground_state(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool in {
            "app.open",
            "app.focus",
            "desktop.open_app",
            "desktop.focus_app",
        }
        or _is_open_path_with_app_tool(clean_tool)
        or clean_tool.startswith("app.open_and_")
        or clean_tool.startswith("app.focus_and_")
    )


def _has_later_execution_verification_before_mutation(
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    for later_request in requests[index + 1 :]:
        later_tool = str(later_request.get("tool") or "").strip()
        if later_tool in _EXECUTION_VERIFICATION_TOOLS:
            return True
        if later_tool in _EXECUTION_MUTATION_TOOLS:
            return False
    return False


def _has_later_foreground_operation_before_verification(
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    for later_request in requests[index + 1 :]:
        later_tool = str(later_request.get("tool") or "").strip()
        if later_tool in _EXECUTION_VERIFICATION_TOOLS:
            return False
        if later_tool in _EXECUTION_MUTATION_TOOLS or _tool_continues_foreground_operation_chain(later_tool):
            return True
    return False


def _append_foreground_submit_verification_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    verification_tool = _first_allowed(
        (
            "desktop.ui_elements",
            "desktop.read_ui",
            "desktop.verify",
            "desktop.active_window",
            "screen.capture",
        ),
        allowed,
    )
    if not verification_tool:
        return requests
    normalized: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        normalized.append(request)
        tool_name = str(request.get("tool") or "").strip()
        if tool_name != "desktop.submit_foreground":
            continue
        if _has_later_execution_verification_before_mutation(requests, index):
            continue
        if _has_later_foreground_operation_before_verification(requests, index):
            continue
        verification = _foreground_submit_verification_request(
            request,
            verification_tool,
            previous_requests=normalized[:-1],
        )
        if not verification or _last_request_matches_tool_and_input(normalized, verification):
            continue
        normalized.append(verification)
    return normalized


def _scope_desktop_app_verification_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for request in requests:
        if not _is_desktop_app_verification_request(request):
            normalized.append(request)
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        if str(payload.get("app_name") or "").strip() and str(
            payload.get("selection_source") or payload.get("app_selection_source") or ""
        ).strip():
            normalized.append(request)
            continue
        app_scope = _desktop_verification_app_scope(
            request,
            normalized,
        )
        if not app_scope:
            normalized.append(request)
            continue
        if str(payload.get("app_name") or "").strip() and str(
            app_scope.get("app_name") or ""
        ).strip() not in {"", str(payload.get("app_name") or "").strip()}:
            normalized.append(request)
            continue
        normalized.append(
            {
                **request,
                "input": {
                    **app_scope,
                    **dict(payload),
                },
            }
        )
    return normalized


def _defer_dependent_same_app_foreground_verification(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requests_by_step_id = {
        step_id: request
        for request in requests
        for step_id in [
            str(request.get("step_id") or request.get("planner_step_id") or "").strip()
        ]
        if step_id
    }
    if not requests_by_step_id:
        return requests

    deferred_step_ids: set[str] = set()
    for request in requests:
        if not _is_desktop_app_verification_request(request):
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(payload.get("app_name") or "").strip()
        if not app_name:
            continue
        target_step_ids = _dependent_same_app_foreground_step_ids(
            request,
            requests_by_step_id,
            app_name,
        )
        direct_dependencies = set(_string_list(request.get("depends_on")))
        deferred_step_ids.update(
            step_id
            for step_id in target_step_ids
            if step_id not in direct_dependencies
        )
    if not deferred_step_ids:
        return requests
    return [
        {
            **request,
            "requires_post_action_verification": False,
        }
        if str(
            request.get("step_id") or request.get("planner_step_id") or ""
        ).strip()
        in deferred_step_ids
        else request
        for request in requests
    ]


def _dependent_same_app_foreground_step_ids(
    verification: Mapping[str, Any],
    requests_by_step_id: Mapping[str, Mapping[str, Any]],
    app_name: str,
) -> list[str]:
    pending = list(reversed(_string_list(verification.get("depends_on"))))
    visited: set[str] = set()
    foreground_step_ids: list[str] = []
    while pending:
        step_id = pending.pop()
        if step_id in visited:
            continue
        visited.add(step_id)
        request = requests_by_step_id.get(step_id)
        if not isinstance(request, Mapping):
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        request_app_name = str(payload.get("app_name") or "").strip()
        if request_app_name and not _desktop_scope_matches(
            {"app_name": app_name},
            {"app_name": request_app_name},
        ):
            continue
        tool_name = str(request.get("tool") or "").strip()
        if (
            not request_app_name
            and _tool_continues_foreground_operation_chain(tool_name)
            and (
                tool_name == "desktop.hotkey"
                or (
                    tool_name == "desktop.safe_shortcut"
                    and str(payload.get("action") or "").strip()
                    in {
                        "application_windows",
                        "force_quit_dialog",
                        "lock_screen",
                        "mission_control",
                        "spotlight_search",
                        "switch_next_app",
                        "switch_previous_app",
                    }
                )
            )
        ):
            continue
        if (
            request.get("requires_post_action_verification") is True
            and (
                _tool_changes_unknown_app_foreground_state(tool_name)
                or _tool_continues_foreground_operation_chain(tool_name)
            )
        ):
            foreground_step_ids.append(step_id)
        pending.extend(reversed(_string_list(request.get("depends_on"))))
    return foreground_step_ids


def _is_desktop_app_verification_request(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in {"desktop.ui_elements", "desktop.read_ui", "desktop.verify"}:
        return False
    step_id = str(
        request.get("step_id") or request.get("planner_step_id") or ""
    ).strip()
    return bool(
        str(request.get("source") or "").strip() == "runtime_verification"
        or str(request.get("runtime_stage") or "").strip() == "verify"
        or str(request.get("runtime_role") or "").strip() == "verify_result"
        or step_id.startswith("verify-")
    )


def _foreground_submit_verification_request(
    source_request: Mapping[str, Any],
    verification_tool: str,
    *,
    previous_requests: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    planning_reason = str(
        source_request.get("planning_reason") or "planner_desktop_operation"
    ).strip() or "planner_desktop_operation"
    if verification_tool in {"desktop.ui_elements", "desktop.read_ui"}:
        payload: dict[str, Any] = _desktop_app_verification_payload(
            source_request,
            previous_requests=previous_requests,
        )
    elif verification_tool == "desktop.verify":
        payload = _desktop_app_verification_payload(
            source_request,
            previous_requests=previous_requests,
        )
    elif verification_tool == "screen.capture":
        payload = {"reason": "verify foreground submit"}
    else:
        payload = {}
    request = _request(
        verification_tool,
        payload,
        planning_reason=planning_reason,
    )
    request["source"] = "runtime_verification"
    request["runtime_doctrine"] = "discover_operate_verify"
    request["continue_to_model"] = True
    request["requires_observation"] = True
    request["runtime_stage"] = "verify"
    request["runtime_role"] = "verify_result"
    request["replan_triggers"] = ["verification_failed"]
    _inherit_request_context_without_step(request, source_request)
    return request


def _unknown_app_execution_verification_request(
    tool_name: str,
    request: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    is_operation = _tool_performs_foreground_ui_operation(tool_name)
    if is_operation:
        verification_tool = _first_allowed(
            (
                "desktop.ui_elements",
                "desktop.read_ui",
                "desktop.verify",
                "desktop.active_window",
                "screen.capture",
            ),
            allowed,
        )
    else:
        verification_tool = _first_allowed(
            (
                "desktop.active_window",
                "desktop.verify",
                "desktop.ui_elements",
                "desktop.read_ui",
                "screen.capture",
            ),
            allowed,
        )
    if not verification_tool:
        return {}
    planning_reason = (
        "runtime_desktop_app_operation_verification"
        if is_operation
        else "runtime_desktop_app_foreground_verification"
    )
    if verification_tool in {"desktop.ui_elements", "desktop.read_ui"}:
        verification = _request(
            verification_tool,
            _desktop_app_verification_payload(request),
            planning_reason=planning_reason,
        )
    elif verification_tool == "desktop.verify":
        verification = _request(
            verification_tool,
            _desktop_app_verification_payload(request),
            planning_reason=planning_reason,
        )
    elif verification_tool == "screen.capture":
        verification = _request(
            "screen.capture",
            {"reason": "verify desktop app operation"},
            planning_reason=planning_reason,
        )
    else:
        verification = _request(verification_tool, {}, planning_reason=planning_reason)
    _inherit_request_context_without_step(verification, request)
    verification["source"] = "runtime_verification"
    verification["runtime_doctrine"] = "discover_operate_verify"
    verification["continue_to_model"] = True
    verification["requires_observation"] = True
    verification["runtime_stage"] = "verify"
    verification["runtime_role"] = "verify_result"
    verification["replan_triggers"] = ["verification_failed"]
    app_scope = _desktop_verification_app_scope(request, ())
    app_name = str(app_scope.get("app_name") or "").strip()
    if app_name:
        verification["target_app_name"] = app_name
        if verification_tool == "desktop.active_window":
            verification["verification_target"] = {"app_name": app_name}
    return verification


def _desktop_app_verification_payload(
    source_request: Mapping[str, Any],
    *,
    previous_requests: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {"limit": 80}
    app_scope = _desktop_verification_app_scope(source_request, previous_requests)
    if app_scope:
        payload = {**app_scope, **payload}
    return payload


def _desktop_verification_app_scope(
    source_request: Mapping[str, Any],
    previous_requests: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    source_payload = (
        source_request.get("input")
        if isinstance(source_request.get("input"), Mapping)
        else {}
    )
    source_scope = _desktop_verification_app_scope_from_payload(source_payload)
    if source_scope and (
        str(source_scope.get("selection_source") or source_scope.get("app_selection_source") or "").strip()
    ):
        return source_scope
    scoped_previous_requests = [
        item for item in previous_requests if isinstance(item, Mapping)
    ]
    for previous in reversed(scoped_previous_requests):
        tool_name = str(previous.get("tool") or "").strip()
        if not (
            _tool_changes_unknown_app_foreground_state(tool_name)
            or _tool_continues_foreground_operation_chain(tool_name)
        ):
            continue
        previous_payload = (
            previous.get("input") if isinstance(previous.get("input"), Mapping) else {}
        )
        previous_scope = _desktop_verification_app_scope_from_payload(previous_payload)
        if previous_scope and _desktop_scope_matches(source_scope, previous_scope):
            return {**previous_scope, **source_scope}
        if previous_scope:
            return previous_scope
    return source_scope


def _desktop_scope_matches(
    source_scope: Mapping[str, Any],
    previous_scope: Mapping[str, Any],
) -> bool:
    source_app = str(source_scope.get("app_name") or "").strip()
    previous_app = str(previous_scope.get("app_name") or "").strip()
    return not source_app or not previous_app or source_app == previous_app


def _desktop_verification_app_scope_from_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name:
        return {}
    scope = {"app_name": app_name}
    _copy_app_selection_metadata(payload, scope)
    return scope


def _tool_performs_foreground_ui_operation(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool.startswith("app.open_and_")
        or clean_tool.startswith("app.focus_and_")
        or clean_tool in _APP_FOREGROUND_DIRECT_OPERATION_SUFFIX
        or clean_tool in {"desktop.submit_foreground", "desktop.search_submit"}
    )


def _tool_continues_foreground_operation_chain(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool in _APP_FOREGROUND_DIRECT_OPERATION_SUFFIX
        or clean_tool in {"desktop.submit_foreground", "desktop.search_submit"}
    )


def _expand_inspect_app_execution_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(payload.get("app_name") or "").strip()
        if tool_name != "desktop.inspect_app" or not app_name or "desktop.ui_elements" not in allowed:
            expanded.append(request)
            continue
        canonical_app_name = legacy_app_name_hint(app_name)
        if canonical_app_name and canonical_app_name != app_name:
            request = {**request, "input": {**payload, "app_name": canonical_app_name}}
            payload = request["input"]
            app_name = canonical_app_name
        if "desktop.inspect_app" in allowed:
            expanded.append(request)
            continue
        if _has_later_app_ui_approval_request_for_app(requests[index + 1 :], app_name):
            expanded.append(request)
            continue
        if is_legacy_app_name_hint(app_name) and _has_later_app_scoped_operation_for_app(
            requests[index + 1 :],
            app_name,
        ):
            continue
        focus_tool = _first_allowed(
            ("app.focus", "desktop.focus_app", "app.open", "desktop.open_app"),
            allowed,
        )
        if not focus_tool:
            expanded.append(request)
            continue
        planning_reason = str(request.get("planning_reason") or "planner_desktop_operation").strip()
        expanded.append(
            _request(
                focus_tool,
                {"app_name": app_name},
                planning_reason=planning_reason,
            )
        )
        if _has_later_app_scoped_operation_for_app(requests[index + 1 :], app_name):
            continue
        ui_payload = {
            key: payload[key]
            for key in ("app_name", "role_filter", "limit")
            if key in payload and payload[key] not in (None, "")
        }
        ui_request = _request(
            "desktop.ui_elements",
            ui_payload,
            planning_reason=planning_reason,
        )
        if request.get("continue_to_model"):
            ui_request["continue_to_model"] = True
        expanded.append(ui_request)
    return expanded


def _has_later_app_scoped_operation_for_app(
    requests: Iterable[Mapping[str, Any]],
    app_name: str,
) -> bool:
    expected = legacy_app_name_hint(app_name)
    if not expected:
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        tool_name = str(request.get("tool") or "").strip()
        if not (
            tool_name.startswith("app.open_and_")
            or tool_name.startswith("app.focus_and_")
        ):
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        later_app = str(payload.get("app_name") or "").strip()
        if later_app and legacy_app_name_hint(later_app) == expected:
            return True
    return False


def _has_later_app_ui_approval_request_for_app(
    requests: Iterable[Mapping[str, Any]],
    app_name: str,
) -> bool:
    expected = legacy_app_name_hint(app_name)
    if not expected:
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        tool_name = str(request.get("tool") or "").strip()
        if tool_name not in {
            "app.open_and_click_ui_element",
            "app.focus_and_click_ui_element",
            "app.open_and_type_into_ui_element",
            "app.focus_and_type_into_ui_element",
            "desktop.click_ui_element",
            "desktop.type_into_ui_element",
        }:
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        later_app = str(
            payload.get("app_name") or payload.get("expected_app_name") or ""
        ).strip()
        if later_app and legacy_app_name_hint(later_app) == expected:
            return True
    return False


def planner_orchestration_requests(
    prompt: str,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=["workflow.run", "group.run", "agent.group_run"],
        metadata=metadata,
    )
    intent_kind = str(decision.selected_intent.kind or "").strip()
    intent_inputs = decision.selected_intent.inputs if isinstance(decision.selected_intent.inputs, Mapping) else {}
    target_name = str(intent_inputs.get("target_name_hint") or "").strip()
    if intent_kind == "workflow_orchestration":
        if not target_name and not _looks_like_orchestration_action(prompt, "workflow"):
            return []
        return [_orchestration_request(decision, "workflow")]
    if intent_kind == "multi_agent":
        if not target_name and not _looks_like_orchestration_action(prompt, "group_run"):
            return []
        return [_orchestration_request(decision, "group_run")]
    return []


def planner_decision_and_tool_requests(
    prompt: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Any | None, list[dict[str, Any]]]:
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    if not allowed:
        return None, []
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=allowed,
        metadata=metadata,
    )
    requests = _tool_requests_for_decision(
        decision,
        allowed,
        allow_unavailable_context=_request_execution_context_enabled(metadata),
    )
    requests = _annotated_tool_requests_for_decision(
        requests,
        decision,
        include_trace=_request_trace_enabled(metadata),
        include_execution_context=_request_execution_context_enabled(metadata),
        metadata=metadata,
    )
    return decision, requests


def planner_direct_decision_and_tool_requests(
    prompt: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> tuple[Any | None, list[dict[str, Any]]]:
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    if not allowed:
        return None, []
    decision = RuntimePlanner().decision(
        prompt,
        allowed_tools=allowed,
        metadata=metadata,
    )
    requests = _direct_tool_requests_for_decision(
        decision,
        allowed,
        metadata=metadata,
    )
    requests = _annotated_tool_requests_for_decision(
        requests,
        decision,
        include_trace=_request_trace_enabled(metadata),
        include_execution_context=_request_execution_context_enabled(metadata),
        metadata=metadata,
    )
    return decision, requests


def planner_tool_requests_for_decision(
    decision: Any,
    allowed_tools: Iterable[str],
    *,
    direct: bool = False,
    execution_normalized: bool = True,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    allowed = {str(tool or "").strip() for tool in allowed_tools if str(tool or "").strip()}
    if not allowed:
        return []
    if direct:
        requests = _direct_tool_requests_for_decision(decision, allowed)
    else:
        requests = _tool_requests_for_decision(decision, allowed)
    requests = _annotated_tool_requests_for_decision(
        requests,
        decision,
        include_trace=True,
        include_execution_context=True,
        metadata=metadata,
    )
    if not execution_normalized:
        return requests
    return planner_execution_tool_requests(requests, allowed) or requests


def _tool_requests_for_decision(
    decision: Any,
    allowed: set[str],
    *,
    allow_unavailable_context: bool = False,
) -> list[dict[str, Any]]:
    if decision.selected_intent.kind == "media_playback":
        return _media_tool_requests(decision.selected_intent.inputs, allowed)
    if decision.selected_intent.kind == "data_analysis":
        return _data_analysis_tool_requests(
            decision,
            allowed,
            allow_readiness_blocked=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "system_control":
        return _system_tool_requests(decision.selected_intent.inputs, allowed)
    if decision.selected_intent.kind == "web_research":
        return _web_tool_requests(
            decision,
            allowed,
            allow_unavailable_context=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "report_generation":
        if str(decision.selected_intent.inputs.get("context_source") or "").strip():
            return _context_source_tool_requests(
                decision,
                allowed,
                step_ids=("copy-selected-report-context", "read-report-context"),
                planning_reason="planner_prefetch_report_context",
                allow_unavailable=allow_unavailable_context,
            )
        research_requests = _report_research_tool_requests(
            decision,
            allowed,
            allow_unavailable=allow_unavailable_context,
        )
        if research_requests:
            return research_requests
        return _context_prefetch_tool_requests(
            decision,
            allowed,
            step_ids=(
                "read-report-file-context",
                "inspect-report-file-scope",
                "gather-context",
            ),
            planning_reason="planner_prefetch_report_context",
            allow_unavailable=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "code_task":
        return _code_task_tool_requests(decision, allowed)
    if decision.selected_intent.kind == "file_organization":
        return _context_prefetch_tool_requests(
            decision,
            allowed,
            step_ids=("inspect-file-scope",),
            planning_reason="planner_prefetch_file_scope",
            allow_unavailable=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "file_access":
        return _file_access_tool_requests(decision.selected_intent.inputs, allowed)
    if decision.selected_intent.kind == "communication":
        direct_requests = _direct_communication_tool_requests(decision, allowed)
        if direct_requests:
            return direct_requests
        direct_context_requests = _direct_communication_context_tool_requests(
            decision,
            allowed,
        )
        if direct_context_requests:
            return direct_context_requests
        context_requests = _context_source_tool_requests(
            decision,
            allowed,
            step_ids=("copy-selected-communication-context", "read-communication-context"),
            planning_reason="planner_prefetch_communication_context",
            allow_unavailable=allow_unavailable_context,
        )
        if context_requests:
            return context_requests
        return _context_prefetch_tool_requests(
            decision,
            allowed,
            step_ids=("discover_apps-desktop-state", "discover-communication-surface"),
            planning_reason="planner_prefetch_communication_surface",
            allow_unavailable=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "information_capture":
        return _information_capture_tool_requests(
            decision,
            allowed,
            allow_unavailable_context=allow_unavailable_context,
        )
    if decision.selected_intent.kind == "schedule":
        return _schedule_tool_requests(decision, allowed)
    if decision.selected_intent.kind == "clipboard_operation":
        return _clipboard_tool_requests(decision.selected_intent.inputs, allowed)
    if decision.selected_intent.kind == "workflow_orchestration":
        return _workflow_tool_requests(decision, allowed)
    if decision.selected_intent.kind == "multi_agent":
        return _multi_agent_tool_requests(decision, allowed)
    if decision.selected_intent.kind != "desktop_operation":
        return []
    return _desktop_tool_requests(decision, allowed)


def _direct_tool_requests_for_decision(
    decision: Any,
    allowed: set[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if decision.selected_intent.kind == "schedule":
        direct_requests = _direct_schedule_context_app_item_tool_requests(decision, allowed)
        if direct_requests:
            return direct_requests
        return _tool_requests_for_decision(
            decision,
            allowed,
            allow_unavailable_context=_request_execution_context_enabled(metadata),
        )
    if decision.selected_intent.kind != "desktop_operation":
        return _tool_requests_for_decision(
            decision,
            allowed,
            allow_unavailable_context=_request_execution_context_enabled(metadata),
        )
    return _direct_desktop_tool_requests(
        decision,
        allowed,
        allow_readiness_blocked=_request_execution_context_enabled(metadata),
    )


def planner_desktop_tool_requests(
    prompt: str,
    allowed_tools: Iterable[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    return planner_tool_requests(
        prompt,
        allowed_tools,
        metadata=metadata,
    )


def _request(
    tool: str,
    payload: dict[str, Any],
    *,
    planning_reason: str = "planner_desktop_operation",
) -> dict[str, Any]:
    clean_payload = dict(payload)
    input_resolution = clean_payload.pop("_input_resolution", None)
    request = {
        "protocol": "json_fallback",
        "tool": tool,
        "input": clean_payload,
        "source": "runtime_planner",
        "planning_reason": planning_reason,
    }
    if isinstance(input_resolution, Mapping):
        request["input_resolution"] = dict(input_resolution)
    return request


def _request_trace_enabled(metadata: Mapping[str, Any] | None) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        metadata.get("runtime_planner_request_trace")
        or metadata.get("yachiyo_runtime_request_trace")
    )


def _request_execution_context_enabled(metadata: Mapping[str, Any] | None) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    return bool(
        _request_trace_enabled(metadata)
        or metadata.get("runtime_planner_execution_context")
        or metadata.get("yachiyo_runtime_execution_context")
    )


def _annotated_tool_requests_for_decision(
    requests: list[dict[str, Any]],
    decision: Any,
    *,
    include_trace: bool = False,
    include_execution_context: bool = False,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    request_metadata = _runtime_request_metadata_from_planner_metadata(metadata)
    steps = [
        step
        for step in getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", [])
        if str(getattr(step, "tool_name", "") or "").strip()
    ]
    if not steps:
        return [
            _request_with_runtime_metadata(request, request_metadata)
            for request in requests
        ]
    used_step_indexes: set[int] = set()
    annotated: list[dict[str, Any]] = []
    for request in requests:
        next_request = _request_with_runtime_metadata(request, request_metadata)
        step_index, step = _matching_trace_step(next_request, steps, used_step_indexes)
        if step_index >= 0 and step is not None:
            used_step_indexes.add(step_index)
            _annotate_request_trace(
                next_request,
                decision,
                step,
                include_trace=include_trace,
                include_execution_context=include_execution_context,
            )
        annotated.append(next_request)
    return annotated


def _runtime_request_metadata_from_planner_metadata(
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
    return payload


def _request_with_runtime_metadata(
    request: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    next_request = dict(request)
    for key, value in metadata.items():
        next_request.setdefault(key, value)
    return next_request


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


def _string_list(values: Any) -> list[str]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        return []
    result: list[str] = []
    for value in values:
        clean = str(value or "").strip()
        if clean and clean not in result:
            result.append(clean)
    return result


def _snapshot_mapping_list(values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, Iterable) or isinstance(values, (str, bytes, Mapping)):
        return []
    result: list[dict[str, Any]] = []
    for value in values:
        if isinstance(value, Mapping):
            result.append(dict(value))
            continue
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            payload = model_dump()
            if isinstance(payload, Mapping):
                result.append(dict(payload))
    return result


def _matching_trace_step(
    request: Mapping[str, Any],
    steps: list[Any],
    used_step_indexes: set[int],
) -> tuple[int, Any | None]:
    tool_name = str(request.get("tool") or "").strip()
    if not tool_name:
        return -1, None
    for index, step in enumerate(steps):
        if index in used_step_indexes:
            continue
        if str(getattr(step, "tool_name", "") or "").strip() == tool_name:
            return index, step
    return -1, None


def _annotate_request_trace(
    request: dict[str, Any],
    decision: Any,
    step: Any,
    *,
    include_trace: bool = True,
    include_execution_context: bool = False,
) -> None:
    if not include_trace and not include_execution_context:
        return
    step_id = str(getattr(step, "step_id", "") or "").strip()
    capability_id = str(getattr(step, "capability_id", "") or "").strip()
    if step_id:
        request["step_id"] = step_id
        request.setdefault("planner_step_id", step_id)
    if capability_id:
        request["capability_id"] = capability_id
    decision_id = str(getattr(decision, "decision_id", "") or "").strip()
    plan = getattr(decision, "plan", None)
    plan_id = str(getattr(plan, "plan_id", "") or "").strip()
    tool_plan = getattr(plan, "tool_plan", None)
    tool_plan_id = str(getattr(tool_plan, "plan_id", "") or "").strip()
    intent = getattr(decision, "selected_intent", None)
    intent_kind = str(getattr(intent, "kind", "") or "").strip()
    if decision_id:
        request["decision_id"] = decision_id
    if plan_id:
        request["plan_id"] = plan_id
    if tool_plan_id and tool_plan_id != plan_id:
        request["tool_plan_id"] = tool_plan_id
    if intent_kind:
        request["intent_kind"] = intent_kind
    if bool(getattr(step, "approval_required", False)):
        request["approval_required"] = True
    depends_on = _string_list(getattr(step, "depends_on", []))
    if depends_on:
        request["depends_on"] = depends_on
    else:
        request.pop("depends_on", None)
    input_bindings = _snapshot_mapping_list(getattr(step, "input_bindings", []))
    if input_bindings:
        request["input_bindings"] = input_bindings
    else:
        request.pop("input_bindings", None)
    fallback_tools = _string_list(getattr(step, "fallback_tools", []))
    if fallback_tools:
        request.setdefault("fallback_tools", fallback_tools)
    risk_level = str(getattr(step, "risk_level", "") or "").strip()
    if risk_level:
        request.setdefault("risk_level", risk_level)
    request.update(_runtime_trace_metadata_for_step(decision, step_id))
    request.update(_task_execution_context_for_step(decision, step_id))


def _attach_basic_step_metadata(request: dict[str, Any], step: Any | None) -> dict[str, Any]:
    step_id = str(getattr(step, "step_id", "") or "").strip()
    capability_id = str(getattr(step, "capability_id", "") or "").strip()
    if step_id:
        request["step_id"] = step_id
    if capability_id:
        request["capability_id"] = capability_id
    return request


_RUNTIME_TRACE_TEXT_KEYS = (
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
)

_RUNTIME_TRACE_BOOL_KEYS = (
    "requires_observation",
    "requires_post_action_verification",
)


def _runtime_trace_metadata_for_step(decision: Any, step_id: str) -> dict[str, Any]:
    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        return {}
    task_core = getattr(getattr(decision, "plan", None), "task_core", None)
    if task_core is None:
        return {}
    for todo in list(getattr(task_core, "todos", []) or []):
        if str(getattr(todo, "step_id", "") or "").strip() == clean_step_id:
            metadata = _runtime_trace_metadata_from_mapping(getattr(todo, "metadata", {}))
            if metadata:
                return metadata
    for checkpoint in list(getattr(task_core, "checkpoints", []) or []):
        if str(getattr(checkpoint, "after_step_id", "") or "").strip() == clean_step_id:
            metadata = _runtime_trace_metadata_from_mapping(getattr(checkpoint, "payload", {}))
            if metadata:
                return metadata
    workspace = getattr(task_core, "workspace", None)
    for item in list(getattr(workspace, "items", []) or []):
        if str(getattr(item, "source_step_id", "") or "").strip() == clean_step_id:
            metadata = _runtime_trace_metadata_from_mapping(getattr(item, "metadata", {}))
            if metadata:
                return metadata
    return {}


def _runtime_trace_metadata_from_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    payload: dict[str, Any] = {}
    for key in _RUNTIME_TRACE_TEXT_KEYS:
        item = str(value.get(key) or "").strip()
        if item:
            payload[key] = item
    for key in _RUNTIME_TRACE_BOOL_KEYS:
        item = value.get(key)
        if isinstance(item, bool):
            payload[key] = item
    return payload


def _task_execution_context_for_step(decision: Any, step_id: str) -> dict[str, Any]:
    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        return {}
    task_core = getattr(getattr(decision, "plan", None), "task_core", None)
    if task_core is None:
        return {}

    payload: dict[str, Any] = {}
    core_id = str(getattr(task_core, "core_id", "") or "").strip()
    if core_id:
        payload["core_id"] = core_id
    workspace = getattr(task_core, "workspace", None)
    workspace_id = str(getattr(workspace, "workspace_id", "") or "").strip()
    if workspace_id:
        payload["workspace_id"] = workspace_id

    todo = _task_todo_for_step(task_core, clean_step_id)
    if todo:
        payload["task_todo"] = todo
    checkpoints = _task_checkpoints_for_step(task_core, clean_step_id)
    if checkpoints:
        payload["task_checkpoints"] = checkpoints
    workspace_items = _task_workspace_items_for_step(workspace, clean_step_id)
    if workspace_items:
        payload["task_workspace_items"] = workspace_items
    verification_targets = _task_verification_targets_for_step(
        decision,
        task_core,
        clean_step_id,
    )
    if verification_targets:
        payload["verification_targets"] = verification_targets
        payload["task_verification_targets"] = verification_targets

    replan_metadata = _task_replan_metadata_for_step(task_core, clean_step_id)
    if replan_metadata:
        payload.update(replan_metadata)
    return payload


def _task_todo_for_step(task_core: Any, step_id: str) -> dict[str, Any]:
    for todo in list(getattr(task_core, "todos", []) or []):
        if str(getattr(todo, "step_id", "") or "").strip() == step_id:
            return _snapshot_payload(todo)
    return {}


def _task_checkpoints_for_step(task_core: Any, step_id: str) -> list[dict[str, Any]]:
    return [
        payload
        for checkpoint in list(getattr(task_core, "checkpoints", []) or [])
        if str(getattr(checkpoint, "after_step_id", "") or "").strip() == step_id
        for payload in [_snapshot_payload(checkpoint)]
        if payload
    ]


def _task_workspace_items_for_step(workspace: Any, step_id: str) -> list[dict[str, Any]]:
    return [
        payload
        for item in list(getattr(workspace, "items", []) or [])
        if str(getattr(item, "source_step_id", "") or "").strip() == step_id
        for payload in [_snapshot_payload(item)]
        if payload
    ]


def _task_verification_targets_for_step(
    decision: Any,
    task_core: Any,
    step_id: str,
) -> list[dict[str, Any]]:
    if _runtime_trace_metadata_for_step(decision, step_id).get("runtime_stage") != "verify":
        return []
    step = _tool_plan_step_for_id(decision, step_id)
    if step is None:
        return []
    targets: list[dict[str, Any]] = []
    for dependency in list(getattr(step, "depends_on", []) or []):
        dependency_id = str(dependency or "").strip()
        if not dependency_id:
            continue
        todo = _task_todo_for_step(task_core, dependency_id)
        checkpoints = _task_checkpoints_for_step(task_core, dependency_id)
        workspace = getattr(task_core, "workspace", None)
        workspace_items = _task_workspace_items_for_step(workspace, dependency_id)
        if not todo and not checkpoints and not workspace_items:
            continue
        target: dict[str, Any] = {"step_id": dependency_id}
        if todo:
            target["todo"] = todo
        if checkpoints:
            target["checkpoints"] = checkpoints
        if workspace_items:
            target["workspace_items"] = workspace_items
        targets.append(target)
    return targets


def _tool_plan_step_for_id(decision: Any, step_id: str) -> Any | None:
    tool_plan = getattr(getattr(decision, "plan", None), "tool_plan", None)
    for step in list(getattr(tool_plan, "steps", []) or []):
        if str(getattr(step, "step_id", "") or "").strip() == step_id:
            return step
    return None


def _task_replan_metadata_for_step(task_core: Any, step_id: str) -> dict[str, list[str]]:
    signal_ids: list[str] = []
    triggers: list[str] = []
    for signal in list(getattr(task_core, "replan_signals", []) or []):
        if str(getattr(signal, "source_step_id", "") or "").strip() != step_id:
            continue
        signal_id = str(getattr(signal, "signal_id", "") or "").strip()
        trigger = str(getattr(signal, "trigger", "") or "").strip()
        if signal_id and signal_id not in signal_ids:
            signal_ids.append(signal_id)
        if trigger and trigger not in triggers:
            triggers.append(trigger)
    return {
        key: value
        for key, value in {
            "replan_signal_ids": signal_ids,
            "replan_triggers": triggers,
        }.items()
        if value
    }


def _snapshot_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    return {}


def _orchestration_request(decision: Any, orchestration_kind: str) -> dict[str, Any]:
    intent = decision.selected_intent
    inputs = intent.inputs if isinstance(intent.inputs, Mapping) else {}
    target_name = str(inputs.get("target_name_hint") or "").strip()
    step_id = (
        "group-multi_agent"
        if orchestration_kind == "group_run"
        else "workflow-orchestration"
    )
    step = _planned_step_by_id(decision, step_id)
    input_preview = getattr(step, "input_preview", None) if step is not None else None
    preview_payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    request_input = {
        "objective": str(intent.user_goal or "").strip(),
        "title": str(intent.title or "").strip(),
        "target_name": target_name,
    }
    for key in ("group_task_plan",):
        value = preview_payload.get(key)
        if value:
            request_input[key] = value
    return {
        "kind": "orchestration",
        "orchestration_kind": orchestration_kind,
        "source": "runtime_planner",
        "planning_reason": f"planner_orchestration_{orchestration_kind}",
        "route_to_studio": bool(decision.plan.route_to_studio),
        "decision_id": str(decision.decision_id or ""),
        "plan_id": str(decision.plan.plan_id or ""),
        "intent_kind": str(intent.kind or ""),
        "input": request_input,
    }


def _multi_agent_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    step = _planned_step_by_id(decision, "group-multi_agent")
    if step is None or not _step_available(step):
        return []
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if tool_name not in {"group.run", "agent.group_run"} or tool_name not in allowed:
        return []
    intent = decision.selected_intent
    inputs = intent.inputs if isinstance(intent.inputs, Mapping) else {}
    target_name = str(inputs.get("target_name_hint") or "").strip()
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    target_name = str(payload.get("target_name") or target_name).strip()
    request_input = {
        "objective": str(intent.user_goal or "").strip(),
        "title": str(intent.title or "").strip(),
        "target_name": target_name,
    }
    group_task_plan = payload.get("group_task_plan")
    if group_task_plan:
        request_input["group_task_plan"] = group_task_plan
    return [
        _request(
            tool_name,
            request_input,
            planning_reason="planner_fallback_group_run",
        )
    ]


def _workflow_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    step = _planned_step_by_id(decision, "workflow-orchestration")
    if step is None or not _step_available(step):
        return []
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if tool_name not in allowed:
        return []
    intent = decision.selected_intent
    inputs = intent.inputs if isinstance(intent.inputs, Mapping) else {}
    target_name = str(inputs.get("target_name_hint") or "").strip()
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    target_name = str(payload.get("target_name") or target_name).strip()
    return [
        _request(
            tool_name,
            {
                "objective": str(intent.user_goal or "").strip(),
                "title": str(intent.title or "").strip(),
                "target_name": target_name,
            },
            planning_reason="planner_fallback_workflow_orchestration",
        )
    ]


def _planned_step_by_id(decision: Any, step_id: str) -> Any | None:
    for step in getattr(getattr(decision.plan, "tool_plan", None), "steps", []) or []:
        if str(getattr(step, "step_id", "") or "").strip() == step_id:
            return step
    return None


def _looks_like_orchestration_action(prompt: str, orchestration_kind: str) -> bool:
    text = str(prompt or "").strip()
    if not text:
        return False
    if re.search(
        r"(?:什么是|是什么|介绍|解释|说明|为什么|怎么设计|如何设计|不要|不用|无需|不需要|不使用|"
        r"what is|explain|describe|why|how should|do not|don't)",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    if orchestration_kind == "workflow":
        return bool(
            re.search(r"(?:workflow|flow|工作流|流程)", text, flags=re.IGNORECASE)
            and re.search(
                r"(?:运行|启动|执行|创建|新建|调试|跑|打开|run|start|execute|create|debug)",
                text,
                flags=re.IGNORECASE,
            )
        )
    explicit_group = re.search(
        r"(?:multi-agent|group|agents?|群组|小组|团队|多\s*agent|多Agent|协作|智能体|代理)",
        text,
        flags=re.IGNORECASE,
    ) and re.search(
        r"(?:让|安排|派发|派活|委派|分配|指派|分别|各自|并行|协作|汇总|"
        r"运行|启动|开启|开|创建|新建|组建|组成|执行|做|产出|用|使用|通过|"
        r"assign|dispatch|delegate|parallel|coordinate|run|start|open|create|execute|with|using)",
        text,
        flags=re.IGNORECASE,
    )
    role_terms = re.search(
        r"(?:研究员|调研员|写作者|作者|分析师|设计师|开发者|工程师|审阅者|审核员|"
        r"researcher|writer|analyst|designer|developer|engineer|reviewer)",
        text,
        flags=re.IGNORECASE,
    )
    role_task_terms = re.search(
        r"(?:协作|合作|分别|各自|并行|分工|汇总|产出|生成|输出|报告|分析(?!师)|研究(?!员)|调研|"
        r"总结|比较|评审|撰写|写作(?!者)|"
        r"collaborate|coordinate|parallel|separately|divide|report|analy[sz]e|"
        r"research|summari[sz]e|compare|review|write|produce|deliver)",
        text,
        flags=re.IGNORECASE,
    )
    role_together_task = re.search(
        r"(?:一起|together)",
        text,
        flags=re.IGNORECASE,
    ) and re.search(
        r"(?:产出|生成|输出|报告|分析(?!师)|研究(?!员)|调研|总结|比较|评审|撰写|写作(?!者)|"
        r"produce|deliver|report|analy[sz]e|research|summari[sz]e|compare|review|write)",
        text,
        flags=re.IGNORECASE,
    )
    role_collaboration = role_terms and (role_task_terms or role_together_task)
    return bool(explicit_group or role_collaboration)


def _first_allowed(candidates: Iterable[str], allowed: set[str]) -> str:
    ordered = tuple(
        dict.fromkeys(
            tool_name
            for candidate in candidates
            if (tool_name := str(candidate or "").strip())
        )
    )
    if not ordered:
        return ""

    # This projection layer must not turn an allowlisted-looking string into
    # execution authority.  Validate candidates through the Runtime-owned
    # schema/dispatch boundary while retaining the historical stable order.
    from apps.shell.agent.runtime.tool_candidate_selection import (
        select_tool_candidate,
    )

    selection = select_tool_candidate(ordered, allowed)
    eligible = {candidate.tool_name for candidate in selection.ranked_candidates}
    for tool_name in ordered:
        if tool_name in eligible:
            return tool_name
    return ""


def _desktop_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    coordinate_resolution_requests = _coordinate_click_resolution_requests(
        decision,
        allowed,
    )
    if coordinate_resolution_requests:
        return coordinate_resolution_requests
    if _has_unavailable_required_desktop_step(decision):
        return []
    requests: list[dict[str, Any]] = []
    steps = list(decision.plan.tool_plan.steps)
    steps_by_id = _steps_by_id(steps)
    selected_communication_query = _selected_communication_app_query(steps_by_id)
    model_selected_step_ids = _model_selected_desktop_step_ids(steps)
    for step in steps:
        if not _step_available(step):
            unavailable_verification = _unavailable_desktop_verification_request(
                step,
                allowed,
            )
            if unavailable_verification:
                requests.append(unavailable_verification)
            continue
        step_id = str(getattr(step, "step_id", "") or "").strip()
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed:
            continue
        if step_id == "write-desktop-content-artifact" or step_id in model_selected_step_ids:
            continue
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        if step_id in _SELECTED_COMMUNICATION_COMPOSE_STEP_IDS:
            payload = _selected_communication_payload(payload, selected_communication_query)
        request = _request(
            tool_name,
            _desktop_request_payload(tool_name, payload),
            planning_reason=_desktop_step_planning_reason(step, tool_name),
        )
        if _desktop_request_needs_basic_step_metadata(decision, step_id):
            _attach_basic_step_metadata(request, step)
        if step_id == "read-desktop-content" or _desktop_observation_step_needs_model_followup(
            decision,
            step_id,
            tool_name,
        ) or _desktop_discovery_step_needs_model_followup(
            decision,
            step_id,
            tool_name,
        ):
            request["continue_to_model"] = True
        requests.append(request)
    if _weak_desktop_discovery_plan(decision, requests):
        return []
    return requests


def _unavailable_desktop_verification_request(
    step: Any,
    allowed: set[str],
) -> dict[str, Any]:
    step_id = str(getattr(step, "step_id", "") or "").strip()
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not step_id.startswith("verify-") or tool_name not in _EXECUTION_VERIFICATION_TOOLS:
        return {}
    if tool_name not in allowed:
        return {}
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    request = _request(
        tool_name,
        _desktop_request_payload(tool_name, payload),
        planning_reason=_desktop_step_planning_reason(step, tool_name),
    )
    request["source"] = "runtime_verification"
    request["status"] = "unavailable"
    request["continue_to_model"] = True
    request["runtime_doctrine"] = "discover_operate_verify"
    request["runtime_stage"] = "verify"
    request["runtime_role"] = "verify_result"
    request["replan_triggers"] = ["verification_unavailable"]
    raw_blockers = payload.get("blocking_conditions", [])
    raw_missing_permissions = payload.get("missing_permissions", [])
    blockers = [
        str(item or "").strip()
        for item in (
            raw_blockers
            if isinstance(raw_blockers, (list, tuple, set))
            else [raw_blockers]
        )
        if str(item or "").strip()
    ]
    missing_permissions = [
        str(item or "").strip()
        for item in (
            raw_missing_permissions
            if isinstance(raw_missing_permissions, (list, tuple, set))
            else [raw_missing_permissions]
        )
        if str(item or "").strip()
    ]
    if blockers or missing_permissions:
        request["policy_reason"] = ", ".join([*blockers, *missing_permissions])
        retry_reason = blockers[0] if blockers else missing_permissions[0]
        request["observation_evidence"] = {
            key: value
            for key, value in {
                "blocking_condition": blockers[0] if blockers else "",
                "blocking_conditions": blockers,
                "missing_permissions": missing_permissions,
            }.items()
            if value not in (None, "", [], {})
        }
        request["observation_retry"] = {"reason": retry_reason}
    _attach_basic_step_metadata(request, step)
    return request


def _desktop_request_needs_basic_step_metadata(decision: Any, step_id: str) -> bool:
    if step_id not in {"discover-file-open-target", "discover_apps-desktop-state"}:
        return False
    return any(
        str(getattr(step, "step_id", "") or "").strip() == "verify-opened-file"
        for step in getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", [])
    )


def _model_selected_desktop_step_ids(steps: list[Any]) -> set[str]:
    selected_step_ids: set[str] = set()
    steps_by_id = _steps_by_id(steps)
    selected_communication_query = _selected_communication_app_query(steps_by_id)
    changed = True
    while changed:
        changed = False
        for step in steps:
            step_id = str(getattr(step, "step_id", "") or "").strip()
            if not step_id or step_id in selected_step_ids:
                continue
            input_preview = getattr(step, "input_preview", None)
            payload = input_preview if isinstance(input_preview, Mapping) else {}
            tool_name = str(getattr(step, "tool_name", "") or "").strip()
            depends_on = [
                str(item or "").strip()
                for item in (getattr(step, "depends_on", None) or [])
                if str(item or "").strip()
            ]
            payload_requires_model = _selected_discovered_app_payload_requires_model(
                payload,
                tool_name,
            )
            if payload_requires_model and _selected_communication_step_can_resolve_app(
                step_id,
                payload,
                selected_communication_query,
            ):
                payload_requires_model = False
            if (
                _selected_communication_step_requires_model(step_id, payload)
                or
                _selected_discovered_app_step_requires_model(step_id, payload, tool_name)
                or (
                    step_id == "open-discovered-file-with-app"
                    and not _runtime_resolvable_dynamic_file_open_step(step)
                )
                or payload_requires_model
                or (
                    str(payload.get("target_path") or "").strip()
                    == "<selected file from workspace.list>"
                    and not _runtime_resolvable_workspace_file_payload(payload, tool_name)
                )
                or any(item in selected_step_ids for item in depends_on)
            ):
                selected_step_ids.add(step_id)
                changed = True
    return selected_step_ids


_SELECTED_COMMUNICATION_COMPOSE_STEP_IDS = {
    "inspect-selected-communication-compose-ui",
    "fill-selected-communication-recipient",
    "draft-selected-communication-message",
    "send-selected-communication-message",
}


def _steps_by_id(steps: Iterable[Any]) -> dict[str, Any]:
    return {
        step_id: step
        for step in steps
        for step_id in [str(getattr(step, "step_id", "") or "").strip()]
        if step_id
    }


def _selected_communication_step_can_resolve_app(
    step_id: str,
    payload: Mapping[str, Any],
    selected_query: str,
) -> bool:
    if step_id not in _SELECTED_COMMUNICATION_COMPOSE_STEP_IDS:
        return False
    if _selected_communication_step_requires_model(step_id, payload):
        return False
    if str(payload.get("app_name") or "").strip() != "<selected app from desktop.list_apps>":
        return False
    return bool(str(selected_query or "").strip())


def _selected_communication_step_requires_model(
    step_id: str,
    payload: Mapping[str, Any],
) -> bool:
    if step_id == "draft-selected-communication-message" and (
        str(payload.get("body_source") or "").strip()
        or str(payload.get("transform") or "").strip()
        or str(payload.get("content_transform_hint") or "").strip()
    ):
        return True
    if step_id == "fill-selected-communication-recipient":
        return not bool(str(payload.get("text") or "").strip())
    if step_id == "draft-selected-communication-message":
        return not bool(str(payload.get("text") or "").strip())
    return False


def _selected_discovered_app_step_requires_model(
    step_id: str,
    payload: Mapping[str, Any],
    tool_name: str = "",
) -> bool:
    if step_id != "open-selected-discovered-app":
        return False
    return _selected_discovered_app_payload_requires_model(payload, tool_name)


def _selected_discovered_app_payload_requires_model(
    payload: Mapping[str, Any],
    tool_name: str = "",
) -> bool:
    if str(payload.get("app_name") or "").strip() != "<selected app from desktop.list_apps>":
        return False
    if (
        _selected_discovered_app_payload_needs_open_path_tool(payload)
        and not _is_open_path_with_app_tool(tool_name)
    ):
        return True
    return not _runtime_resolvable_selected_app_payload(payload, tool_name)


def _runtime_resolvable_selected_app_payload(
    payload: Mapping[str, Any],
    tool_name: str = "",
) -> bool:
    app_selection_source = str(
        payload.get("app_selection_source") or payload.get("selection_source") or ""
    ).strip()
    if not (
        app_selection_source == "desktop.list_apps"
        and bool(str(payload.get("query") or "").strip())
    ):
        return False
    if _selected_discovered_app_payload_needs_open_path_tool(payload):
        return _is_open_path_with_app_tool(tool_name)
    return True


def _runtime_resolvable_workspace_file_payload(
    payload: Mapping[str, Any],
    tool_name: str = "",
) -> bool:
    selected_path = str(payload.get("target_path") or payload.get("path") or "").strip()
    return bool(
        _is_open_path_with_app_tool(tool_name)
        and selected_path == "<selected file from workspace.list>"
        and str(payload.get("selection_source") or "").strip() == "workspace.list"
    )


def _runtime_resolvable_dynamic_file_open_step(step: Any) -> bool:
    if str(getattr(step, "step_id", "") or "").strip() != "open-discovered-file-with-app":
        return False
    input_preview = getattr(step, "input_preview", None)
    payload = input_preview if isinstance(input_preview, Mapping) else {}
    return _runtime_resolvable_workspace_file_payload(
        payload,
        str(getattr(step, "tool_name", "") or "").strip(),
    )


def _runtime_resolvable_dynamic_file_open_plan(decision: Any) -> bool:
    return any(
        _runtime_resolvable_dynamic_file_open_step(step)
        for step in list(
            getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", [])
            or []
        )
    )


def _selected_discovered_app_payload_needs_open_path_tool(payload: Mapping[str, Any]) -> bool:
    return bool(str(payload.get("target_path") or "").strip()) or (
        str(payload.get("action") or "").strip() == "open_path_with_selected_app"
    )


def _runtime_resolvable_selected_app_consumer(
    payload: Mapping[str, Any],
    tool_name: str = "",
) -> bool:
    return bool(
        str(payload.get("app_name") or "").strip()
        == "<selected app from desktop.list_apps>"
        and _runtime_resolvable_selected_app_payload(payload, tool_name)
    )


def _runtime_resolvable_discovered_app_plan(decision: Any) -> bool:
    steps = list(getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", []) or [])
    if not steps or _discovered_app_plan_needs_model_reasoning(decision, steps):
        return False
    steps_by_id = _steps_by_id(steps)
    selected_communication_query = _selected_communication_app_query(steps_by_id)
    has_resolvable_selected_app_consumer = False
    for step in steps:
        if not _step_available(step):
            continue
        input_preview = getattr(step, "input_preview", None)
        payload = input_preview if isinstance(input_preview, Mapping) else {}
        step_id = str(getattr(step, "step_id", "") or "").strip()
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if _runtime_resolvable_selected_app_consumer(payload, tool_name):
            has_resolvable_selected_app_consumer = True
        if step_id == "open-discovered-file-with-app":
            has_resolvable_selected_app_consumer = (
                has_resolvable_selected_app_consumer
                or _runtime_resolvable_dynamic_file_open_step(step)
            )
        payload_requires_model = _selected_discovered_app_payload_requires_model(
            payload,
            tool_name,
        )
        if payload_requires_model and _selected_communication_step_can_resolve_app(
            step_id,
            payload,
            selected_communication_query,
        ):
            payload_requires_model = False
            has_resolvable_selected_app_consumer = True
        if payload_requires_model:
            return False
    return has_resolvable_selected_app_consumer


def _discovered_app_plan_needs_model_reasoning(
    decision: Any,
    steps: list[Any],
) -> bool:
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if isinstance(inputs, Mapping) and (
        isinstance(inputs.get("creative_canvas_hint"), Mapping)
        or isinstance(inputs.get("desktop_content_artifact_hint"), Mapping)
        or isinstance(inputs.get("model_generated_content_hint"), Mapping)
    ):
        return True
    for step in steps:
        step_id = str(getattr(step, "step_id", "") or "").strip()
        if step_id in {
            "read-desktop-content",
            "write-desktop-content-artifact",
        }:
            return True
        if step_id == "open-discovered-file-with-app":
            if _runtime_resolvable_dynamic_file_open_step(step):
                continue
            return True
    return False


def _has_unavailable_required_desktop_step(
    decision: Any,
    *,
    allow_readiness_blocked: bool = False,
) -> bool:
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = getattr(tool_plan, "steps", None)
    if not isinstance(steps, list):
        return False
    has_actionable_discovery = _has_actionable_desktop_app_discovery_step(steps)
    for index, step in enumerate(steps):
        status = str(getattr(step, "status", "") or "").strip()
        if status != "unavailable":
            continue
        if allow_readiness_blocked and _unavailable_step_has_runtime_tool(step):
            continue
        step_id = str(getattr(step, "step_id", "") or "").strip()
        capability_id = str(getattr(step, "capability_id", "") or "").strip()
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        input_preview = getattr(step, "input_preview", None)
        payload = input_preview if isinstance(input_preview, Mapping) else {}
        if _is_desktop_result_verification_step_id(step_id):
            continue
        if not tool_name and step_id == "submit-foreground-ui":
            continue
        if capability_id == "desktop.ui_operation" and not tool_name:
            if payload.get("blocking_conditions") or payload.get("missing_permissions"):
                return True
            if _unavailable_desktop_ui_step_can_continue_with_model(
                steps,
                index,
                has_actionable_discovery=has_actionable_discovery,
            ):
                continue
            if (
                has_actionable_discovery
                and _unavailable_desktop_ui_step_is_model_resolvable(step, payload)
            ):
                continue
            return True
        if (
            capability_id in {"desktop.app_control", "desktop.ui_operation"}
            and (
                payload.get("blocking_conditions")
                or payload.get("missing_permissions")
            )
        ):
            return True
        if has_actionable_discovery and capability_id in {
            "desktop.app_control",
            "desktop.ui_operation",
        }:
            continue
        if capability_id in {"desktop.app_control", "desktop.ui_operation"}:
            return True
    return False


def _has_actionable_desktop_app_discovery_step(steps: list[Any]) -> bool:
    for step in steps:
        if str(getattr(step, "status", "") or "").strip() == "unavailable":
            continue
        if str(getattr(step, "tool_name", "") or "").strip() != "desktop.list_apps":
            continue
        step_id = str(getattr(step, "step_id", "") or "").strip()
        if step_id in {"discover-desktop-state", "discover_apps-desktop-state"}:
            return True
    return False


def _unavailable_desktop_ui_step_can_continue_with_model(
    steps: list[Any],
    index: int,
    *,
    has_actionable_discovery: bool,
) -> bool:
    if not has_actionable_discovery or index < 0 or index >= len(steps):
        return False
    step = steps[index]
    if str(getattr(step, "status", "") or "").strip() != "unavailable":
        return False
    if str(getattr(step, "capability_id", "") or "").strip() != "desktop.ui_operation":
        return False
    if str(getattr(step, "tool_name", "") or "").strip():
        return False
    input_preview = getattr(step, "input_preview", None)
    payload = input_preview if isinstance(input_preview, Mapping) else {}
    if not _unavailable_desktop_ui_step_is_model_resolvable(step, payload):
        return False
    return _has_later_available_desktop_observation_step(steps, index)


def _unavailable_desktop_ui_step_is_model_resolvable(
    step: Any,
    payload: Mapping[str, Any],
) -> bool:
    if _unavailable_desktop_ui_payload_is_model_resolvable(payload):
        return True
    step_id = str(getattr(step, "step_id", "") or "").strip()
    if step_id in {
        "hotkey-selected-discovered-app",
        "key-selected-discovered-app",
        "save-discovered-app-creative-result",
    }:
        return bool(str(payload.get("key") or "").strip())
    return False


def _unavailable_desktop_ui_payload_is_model_resolvable(payload: Mapping[str, Any]) -> bool:
    for key in ("target", "role_filter", "action", "text", "direction"):
        if str(payload.get(key) or "").strip():
            return True
    if payload.get("click_count") is not None:
        return True
    return False


def _has_later_available_desktop_observation_step(steps: list[Any], index: int) -> bool:
    for step in steps[index + 1 :]:
        if not _step_available(step):
            continue
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if tool_name in _EXECUTION_VERIFICATION_TOOLS:
            return True
    return False


def _keep_direct_discovery_step(step: Any, tool_name: str) -> bool:
    if str(getattr(step, "step_id", "") or "").strip() != "discover-desktop-state":
        return False
    if tool_name != "desktop.list_apps":
        return False
    input_preview = getattr(step, "input_preview", None)
    payload = input_preview if isinstance(input_preview, Mapping) else {}
    query = str(payload.get("query") or "").strip()
    return bool(query)


_APP_FOREGROUND_DIRECT_OPERATION_SUFFIX = {
    "desktop.safe_shortcut": "safe_shortcut",
    "desktop.safe_key": "safe_key",
    "desktop.safe_scroll": "safe_scroll",
    "desktop.safe_click": "safe_click",
    "desktop.safe_type_text": "safe_type_text",
    "desktop.type_text": "safe_type_text",
    "desktop.hotkey": "hotkey",
    "desktop.click_ui_element": "click_ui_element",
    "desktop.type_into_ui_element": "type_into_ui_element",
}


def _collapse_app_foreground_direct_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    collapsed: list[dict[str, Any]] = []
    index = 0
    while index < len(requests):
        request = requests[index]
        tool_name = str(request.get("tool") or "").strip()
        if tool_name not in {"app.open", "app.focus"}:
            collapsed.append(request)
            index += 1
            continue
        input_preview = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(input_preview.get("app_name") or "").strip()
        if not app_name:
            collapsed.append(request)
            index += 1
            continue
        operation_index = index + 1
        mode = "open" if tool_name == "app.open" else "focus"
        if (
            tool_name == "app.open"
            and operation_index < len(requests)
            and _same_app_control_request(requests[operation_index], "app.focus", app_name)
        ):
            operation_index += 1
        if operation_index >= len(requests):
            collapsed.append(request)
            index += 1
            continue
        operation = requests[operation_index]
        operation_tool = str(operation.get("tool") or "").strip()
        operation_input = operation.get("input") if isinstance(operation.get("input"), Mapping) else {}
        if (
            operation_tool == "desktop.safe_shortcut"
            and str(operation_input.get("action") or "").strip() == "find"
            and _has_later_search_submit(requests, operation_index)
        ):
            collapsed.append(request)
            index += 1
            continue
        suffix = _APP_FOREGROUND_DIRECT_OPERATION_SUFFIX.get(operation_tool, "")
        combined_tool = f"app.{mode}_and_{suffix}" if suffix else ""
        if not combined_tool or combined_tool not in allowed:
            collapsed.append(request)
            index += 1
            continue
        combined_payload = {"app_name": app_name, **dict(operation_input)}
        combined_request = _request(
            combined_tool,
            _desktop_request_payload(combined_tool, combined_payload),
            planning_reason=str(
                operation.get("planning_reason")
                or request.get("planning_reason")
                or "planner_desktop_operation"
            ),
        )
        _inherit_combined_request_trace_metadata(combined_request, operation, request)
        collapsed.append(combined_request)
        index = operation_index + 1
    return collapsed


_COMBINED_REQUEST_TRACE_KEYS = (
    "request_id",
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
    "run_id",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
    "requires_observation",
    "requires_post_action_verification",
    "task_todo",
    "task_checkpoints",
    "task_workspace_items",
    "verification_targets",
    "task_verification_targets",
    "replan_signal_ids",
    "replan_triggers",
)


def _inherit_combined_request_trace_metadata(
    target: dict[str, Any],
    operation: Mapping[str, Any],
    prepare: Mapping[str, Any],
) -> None:
    for key in _COMBINED_REQUEST_TRACE_KEYS:
        value = operation.get(key)
        if value in (None, "", [], {}):
            value = prepare.get(key)
        if value in (None, "", [], {}):
            continue
        target[key] = value
    if target.get("step_id") and not target.get("planner_step_id"):
        target["planner_step_id"] = str(target.get("step_id") or "").strip()


def _has_later_search_submit(
    requests: list[dict[str, Any]],
    operation_index: int,
) -> bool:
    for later_request in requests[operation_index + 1 :]:
        if str(later_request.get("tool") or "").strip() == "desktop.search_submit":
            return True
    return False


def _has_discovered_app_foreground_verification_chain(
    requests: list[dict[str, Any]],
) -> bool:
    if not any(str(request.get("source") or "").strip() == "runtime_planner" for request in requests):
        return False
    if not _has_unknown_discovered_app_query(requests):
        return False
    if not any(str(request.get("tool") or "").strip() == "desktop.list_apps" for request in requests):
        return False
    has_foreground_operation = False
    for index, request in enumerate(requests[:-1]):
        if str(request.get("tool") or "").strip() not in {
            "app.open",
            "app.focus",
            "desktop.open_app",
            "desktop.focus_app",
        }:
            continue
        for later_request in requests[index + 1 :]:
            if str(later_request.get("tool") or "").strip() in _APP_FOREGROUND_DIRECT_OPERATION_SUFFIX:
                has_foreground_operation = True
                break
        if has_foreground_operation:
            break
    if not has_foreground_operation:
        return False
    return any(
        str(request.get("tool") or "").strip() in _EXECUTION_VERIFICATION_TOOLS
        for request in requests
    )


def _has_unknown_discovered_app_query(requests: list[dict[str, Any]]) -> bool:
    for request in requests:
        if str(request.get("tool") or "").strip() != "desktop.list_apps":
            continue
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        query = str(payload.get("query") or "").strip()
        if query and not is_legacy_app_name_hint(query):
            return True
    return False


_EXECUTION_VERIFICATION_TOOLS = {
    "desktop.active_window",
    "desktop.running_apps",
    "desktop.windows",
    "desktop.list_windows",
    "desktop.ui_elements",
    "desktop.read_ui",
    "desktop.inspect_app",
    "desktop.verify",
    "screen.capture",
}

_EXECUTION_MUTATION_TOOLS = {
    "app.open",
    "app.focus",
    "desktop.open_app",
    "desktop.focus_app",
    "app.focus_window",
    "app.status",
    "app.show",
    "app.hide",
    "app.minimize",
    "app.quit",
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
    "desktop.shortcut",
    "desktop.hotkey",
    "desktop.type",
    "desktop.open_path_with_app",
    "app.open_path_with_app",
    "desktop.click_ui_element",
    "desktop.type_into_ui_element",
    "desktop.submit_foreground",
    "desktop.hide_app",
    "desktop.show_all_apps",
    "desktop.minimize_window",
    "desktop.close_window",
    "desktop.quit_app",
}


def _drop_redundant_execution_verification_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if len(requests) <= 1:
        return requests
    filtered: list[dict[str, Any]] = []
    saw_mutation = False
    last_mutation_tool = ""
    for index, request in enumerate(requests):
        tool_name = str(request.get("tool") or "").strip()
        if (
            not saw_mutation
            and tool_name in _EXECUTION_VERIFICATION_TOOLS
            and not _keep_pre_mutation_verification_request(request)
            and any(
                str(item.get("tool") or "").strip() in _EXECUTION_MUTATION_TOOLS
                or _later_verification_supersedes(tool_name, str(item.get("tool") or "").strip())
                for item in requests[index + 1 :]
            )
        ):
            continue
        if (
            saw_mutation
            and tool_name in _EXECUTION_VERIFICATION_TOOLS
            and not _keep_post_mutation_verification_request(request, last_mutation_tool)
        ):
            continue
        filtered.append(request)
        if tool_name in _EXECUTION_MUTATION_TOOLS:
            saw_mutation = True
            last_mutation_tool = tool_name
    return filtered


_APP_OPEN_AND_FOREGROUND_TOOLS = {
    "app.open_and_safe_type_text",
    "app.open_and_safe_shortcut",
    "app.open_and_safe_key",
    "app.open_and_safe_scroll",
    "app.open_and_safe_click",
    "app.open_and_click_ui_element",
    "app.open_and_type_into_ui_element",
    "app.open_and_hotkey",
}

_APP_FOCUS_AND_FOREGROUND_TOOLS = {
    "app.focus_and_safe_type_text",
    "app.focus_and_safe_shortcut",
    "app.focus_and_safe_key",
    "app.focus_and_safe_scroll",
    "app.focus_and_safe_click",
    "app.focus_and_click_ui_element",
    "app.focus_and_type_into_ui_element",
    "app.focus_and_hotkey",
}


def _drop_redundant_app_foreground_prepare_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(payload.get("app_name") or "").strip()
        if (
            app_name
            and tool_name in _APP_OPEN_AND_FOREGROUND_TOOLS
            and not _starts_multistep_app_search_chain(request, requests, index)
        ):
            while _last_prepare_request_matches(
                filtered,
                app_name,
                {"app.open", "app.focus", "desktop.open_app", "desktop.focus_app"},
            ):
                filtered.pop()
        elif (
            app_name
            and tool_name in _APP_FOCUS_AND_FOREGROUND_TOOLS
            and not _starts_multistep_app_search_chain(request, requests, index)
        ):
            while _last_prepare_request_matches(
                filtered,
                app_name,
                {"app.focus", "desktop.focus_app"},
            ):
                filtered.pop()
        filtered.append(request)
    return filtered


def _starts_multistep_app_search_chain(
    request: Mapping[str, Any],
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in (_APP_OPEN_AND_FOREGROUND_TOOLS | _APP_FOCUS_AND_FOREGROUND_TOOLS):
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if str(payload.get("action") or "").strip() != "find":
        return False
    return any(
        str(later.get("tool") or "").strip()
        in {"desktop.safe_type_text", "desktop.type_text", "desktop.search_submit"}
        for later in requests[index + 1 :]
    )


def _last_prepare_request_matches(
    requests: list[dict[str, Any]],
    app_name: str,
    tools: set[str],
) -> bool:
    if not requests:
        return False
    request = requests[-1]
    tool_name = str(request.get("tool") or "").strip()
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return tool_name in tools and str(payload.get("app_name") or "").strip() == app_name


def _drop_redundant_post_inspect_app_prepare_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    inspect_app_name = ""
    for request in requests:
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(payload.get("app_name") or "").strip()
        if (
            tool_name in {"app.open", "app.focus", "desktop.open_app", "desktop.focus_app"}
            and app_name
            and inspect_app_name
            and app_name == inspect_app_name
        ):
            continue
        filtered.append(request)
        if tool_name == "desktop.inspect_app":
            focus_requested = payload.get("focus", False) is True
            open_requested = payload.get("open_if_needed", False) is True
            inspect_app_name = app_name if focus_requested or open_requested else ""
        elif tool_name not in {"app.open", "app.focus", "desktop.open_app", "desktop.focus_app"}:
            inspect_app_name = ""
    return filtered


def _keep_pre_mutation_verification_request(request: dict[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if (
        tool_name in {"desktop.ui_elements", "desktop.read_ui"}
        and str(request.get("runtime_stage") or "").strip() == "discover"
        and str(request.get("runtime_role") or "").strip() == "inspect_ui"
        and request.get("requires_observation") is True
    ):
        # A semantic foreground action is grounded by this pre-action read.
        # The later verifier proves the mutation; it does not supersede the
        # observation that identified the target before input was sent.
        return True
    if tool_name == "desktop.inspect_app":
        return bool(str(payload.get("app_name") or "").strip())
    if tool_name in {"desktop.windows", "desktop.list_windows", "desktop.verify"}:
        return bool(str(payload.get("app_name") or "").strip())
    if tool_name == "screen.capture":
        return bool(str(payload.get("reason") or "").strip())
    return False


def _keep_post_mutation_verification_request(
    request: dict[str, Any],
    previous_mutation_tool: str,
) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if request.get("continue_to_model"):
        return True
    if _is_open_path_with_app_tool(previous_mutation_tool):
        return False
    if tool_name in {"desktop.ui_elements", "desktop.read_ui", "desktop.windows", "desktop.list_windows"}:
        return True
    if tool_name == "desktop.verify":
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        return bool(str(payload.get("app_name") or "").strip())
    if previous_mutation_tool in {
        "app.quit",
        "app.hide",
        "app.show",
        "app.minimize",
        "desktop.close_window",
        "desktop.minimize_window",
        "desktop.quit_app",
    } and tool_name in {"desktop.active_window", "desktop.running_apps"}:
        return True
    if tool_name == "desktop.active_window" and (
        previous_mutation_tool.startswith("app.open_and_")
        or previous_mutation_tool.startswith("app.focus_and_")
    ):
        return True
    if tool_name == "desktop.active_window" and previous_mutation_tool in {
        "app.open",
        "app.focus",
        "desktop.open_app",
        "desktop.focus_app",
        "app.focus_window",
    }:
        return True
    if tool_name in {"desktop.active_window", "desktop.running_apps"}:
        return False
    if tool_name == "screen.capture":
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        return bool(str(payload.get("reason") or "").strip())
    return False


def _later_verification_supersedes(current_tool: str, later_tool: str) -> bool:
    if current_tool == later_tool:
        return False
    if later_tool in {"desktop.inspect_app", "desktop.verify"}:
        return True
    if current_tool == "screen.capture" and later_tool in {
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.windows",
        "desktop.list_windows",
    }:
        return True
    if current_tool in {"desktop.running_apps", "desktop.active_window"} and later_tool in {
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.windows",
        "desktop.list_windows",
    }:
        return True
    return False


def _same_app_control_request(request: dict[str, Any], tool_name: str, app_name: str) -> bool:
    if str(request.get("tool") or "").strip() != tool_name:
        return False
    input_preview = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return str(input_preview.get("app_name") or "").strip() == app_name


def _direct_desktop_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_readiness_blocked: bool = False,
) -> list[dict[str, Any]]:
    coordinate_resolution_requests = _coordinate_click_resolution_requests(
        decision,
        allowed,
    )
    if coordinate_resolution_requests:
        return coordinate_resolution_requests
    if _has_unavailable_required_desktop_step(
        decision,
        allow_readiness_blocked=allow_readiness_blocked,
    ):
        return []
    observe_before_action_requests = _observe_before_action_direct_requests(
        decision,
        allowed,
        allow_readiness_blocked=allow_readiness_blocked,
    )
    if observe_before_action_requests:
        return observe_before_action_requests
    requests: list[dict[str, Any]] = []
    steps = list(decision.plan.tool_plan.steps)
    steps_by_id = _steps_by_id(steps)
    selected_communication_query = _selected_communication_app_query(steps_by_id)
    model_selected_step_ids = _model_selected_desktop_step_ids(steps)
    for step in steps:
        if not _direct_step_available(
            step,
            allow_readiness_blocked=allow_readiness_blocked,
        ):
            continue
        step_id = str(getattr(step, "step_id", "") or "").strip()
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed:
            continue
        if step_id == "write-desktop-content-artifact" or step_id in model_selected_step_ids:
            continue
        if step_id == "discover-desktop-state" and not _keep_direct_discovery_step(step, tool_name):
            continue
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        if step_id in _SELECTED_COMMUNICATION_COMPOSE_STEP_IDS:
            payload = _selected_communication_payload(payload, selected_communication_query)
        request = _request(
            tool_name,
            _desktop_request_payload(tool_name, payload),
            planning_reason=_desktop_step_planning_reason(step, tool_name),
        )
        if step_id == "read-desktop-content" or _desktop_observation_step_needs_model_followup(
            decision,
            step_id,
            tool_name,
        ) or _desktop_discovery_step_needs_model_followup(
            decision,
            step_id,
            tool_name,
        ):
            request["continue_to_model"] = True
        requests.append(request)
    if _weak_desktop_discovery_plan(decision, requests):
        return []
    return requests


def _observe_before_action_direct_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_readiness_blocked: bool = False,
) -> list[dict[str, Any]]:
    prompt = str(getattr(getattr(decision, "selected_intent", None), "user_goal", "") or "")
    if not _explicit_ui_observation_before_action_requested(prompt):
        return []
    steps = list(getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", []) or [])
    operation_index = next(
        (
            index
            for index, step in enumerate(steps)
            if str(getattr(step, "step_id", "") or "").strip()
            in {"operate-foreground-ui", "operate-foreground-ui-followup-click"}
        ),
        -1,
    )
    if operation_index <= 0:
        return []
    for step in steps[:operation_index]:
        step_id = str(getattr(step, "step_id", "") or "").strip()
        if step_id != "read-foreground-ui" or not _direct_step_available(
            step,
            allow_readiness_blocked=allow_readiness_blocked,
        ):
            continue
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if tool_name not in {"desktop.ui_elements", "desktop.read_ui"} or tool_name not in allowed:
            continue
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        request = _request(
            tool_name,
            _desktop_request_payload(tool_name, payload),
            planning_reason="planner_prefetch_desktop_observation",
        )
        request["continue_to_model"] = True
        return [request]
    return []


def _step_available(step: Any) -> bool:
    return str(getattr(step, "status", "") or "").strip() != "unavailable"


def _direct_step_available(
    step: Any,
    *,
    allow_readiness_blocked: bool = False,
) -> bool:
    if _step_available(step):
        return True
    if not allow_readiness_blocked:
        return False
    return _unavailable_step_has_runtime_tool(step)


def _unavailable_step_has_runtime_tool(step: Any) -> bool:
    if str(getattr(step, "status", "") or "").strip() != "unavailable":
        return False
    if not str(getattr(step, "tool_name", "") or "").strip():
        return False
    input_preview = getattr(step, "input_preview", None)
    payload = input_preview if isinstance(input_preview, Mapping) else {}
    return bool(payload.get("blocking_conditions") or payload.get("missing_permissions"))


def _weak_desktop_discovery_plan(decision: Any, requests: list[dict[str, Any]]) -> bool:
    if len(requests) != 1:
        return False
    if str(requests[0].get("tool") or "") not in {
        "desktop.running_apps",
        "desktop.active_window",
        "screen.capture",
    }:
        return False
    intent = getattr(decision, "selected_intent", None)
    inputs = getattr(intent, "inputs", None)
    if not isinstance(inputs, Mapping):
        return False
    if str(inputs.get("app_name_hint") or "").strip():
        return False
    if str(inputs.get("operation_hint") or "").strip():
        return False
    hint_keys = {
        "window_list_hint",
        "focus_window_hint",
        "ui_inspection_hint",
        "screen_capture_hint",
        "app_management_hint",
        "foreground_management_hint",
        "safe_shortcut_hint",
        "safe_key_hint",
        "safe_scroll_hint",
        "safe_click_hint",
        "desktop_discovery_hint",
        "browser_internal_page_hint",
        "app_preferences_hint",
    }
    return not any(inputs.get(key) for key in hint_keys)


def _desktop_step_planning_reason(step: Any, tool_name: str) -> str:
    if str(getattr(step, "step_id", "") or "").strip() == "read-desktop-content":
        return "planner_prefetch_desktop_content"
    if str(getattr(step, "step_id", "") or "").strip() == "discover-file-open-target":
        return "planner_prefetch_file_open_target"
    input_preview = getattr(step, "input_preview", None)
    if "hotkey" in tool_name or (
        isinstance(input_preview, Mapping)
        and input_preview.get("key")
        and input_preview.get("modifiers") is not None
    ):
        return "planner_desktop_hotkey"
    return "planner_desktop_operation"


_DESKTOP_OBSERVATION_MODEL_FOLLOWUP_STEP_IDS = frozenset(
    {
        "capture-screen",
        "inspect-app",
        "observe-selected-discovered-app",
        "read-foreground-ui",
        "verify-desktop-result",
        "verify-opened-file",
    }
)
_DESKTOP_OBSERVATION_MODEL_FOLLOWUP_TOOLS = frozenset(
    {
        "screen.capture",
        "desktop.active_window",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.inspect_app",
        "desktop.list_windows",
        "desktop.running_apps",
        "desktop.verify",
        "desktop.windows",
    }
)


def _is_desktop_result_verification_step_id(step_id: str) -> bool:
    return step_id == "verify-desktop-result" or step_id.startswith(
        "verify-desktop-result-"
    )


def _is_desktop_observation_model_followup_step_id(step_id: str) -> bool:
    return any(
        step_id == candidate or step_id.startswith(f"{candidate}-")
        for candidate in _DESKTOP_OBSERVATION_MODEL_FOLLOWUP_STEP_IDS
    )


def _is_planned_desktop_observation_step(
    decision: Any,
    step_id: str,
    tool_name: str,
) -> bool:
    planned_step = _planned_step_by_id(decision, step_id)
    return bool(
        _is_desktop_observation_model_followup_step_id(step_id)
        or tool_name in _DESKTOP_OBSERVATION_MODEL_FOLLOWUP_TOOLS
        or str(getattr(planned_step, "action", "") or "").strip()
        == "observe_ui_target"
    )


def planner_desktop_observation_step_needs_model_followup(
    decision: Any,
    step_id: str,
    tool_name: str,
) -> bool | None:
    """Classify desktop observations without deciding non-desktop verifiers.

    ``None`` preserves the caller's policy for non-desktop tools or incomplete
    verifier contracts. A local terminal verdict requires both a GoalContract
    and an exact task verification target.
    """

    if step_id == "read-desktop-content":
        return True
    if not _is_planned_desktop_observation_step(decision, step_id, tool_name):
        return None
    if _desktop_observation_step_needs_model_followup(
        decision,
        step_id,
        tool_name,
    ):
        return True
    runtime_stage = str(
        _runtime_trace_metadata_for_step(decision, step_id).get("runtime_stage") or ""
    ).strip()
    if runtime_stage != "verify":
        return False
    task_core = getattr(getattr(decision, "plan", None), "task_core", None)
    if task_core is None or getattr(task_core, "goal_contract", None) is None:
        return None
    verification_targets = _task_verification_targets_for_step(
        decision,
        task_core,
        step_id,
    )
    if not verification_targets:
        return None
    if not _goal_contract_binds_task_verification_targets(
        task_core,
        step_id,
        verification_targets,
    ):
        return None
    return False


def _goal_contract_binds_task_verification_targets(
    task_core: Any,
    verifier_step_id: str,
    verification_targets: Iterable[Mapping[str, Any]],
) -> bool:
    goal_contract = getattr(task_core, "goal_contract", None)
    targets_by_step_id = {
        str(target.get("step_id") or "").strip(): target
        for target in verification_targets
        if str(target.get("step_id") or "").strip()
    }
    if goal_contract is None or not targets_by_step_id:
        return False
    bound_target_step_ids: set[str] = set()
    for criterion in list(getattr(goal_contract, "criteria", None) or []):
        source_step_ids = {
            str(value or "").strip()
            for value in (getattr(criterion, "source_step_ids", None) or [])
            if str(value or "").strip()
        }
        verifier_step_ids = {
            str(value or "").strip()
            for value in (getattr(criterion, "verifier_step_ids", None) or [])
            if str(value or "").strip()
        }
        expected = getattr(criterion, "expected", None)
        expected_target = (
            expected.get("target") if isinstance(expected, Mapping) else None
        )
        if (
            verifier_step_id not in verifier_step_ids
            or not isinstance(expected_target, Mapping)
            or not expected_target
        ):
            continue
        capability_ids = list(
            getattr(criterion, "required_capabilities", None) or []
        )
        for source_step_id in source_step_ids.intersection(targets_by_step_id):
            if _goal_expected_target_matches_task_verification_target(
                expected_target,
                targets_by_step_id[source_step_id],
                capability_ids=capability_ids,
                source_step_id=source_step_id,
            ):
                bound_target_step_ids.add(source_step_id)
    return bound_target_step_ids == set(targets_by_step_id)


def _goal_expected_target_matches_task_verification_target(
    expected_target: Mapping[str, Any],
    verification_target: Mapping[str, Any],
    *,
    capability_ids: Iterable[str],
    source_step_id: str,
) -> bool:
    for checkpoint in list(verification_target.get("checkpoints") or []):
        if not isinstance(checkpoint, Mapping):
            continue
        if str(checkpoint.get("after_step_id") or "").strip() != source_step_id:
            continue
        payload = checkpoint.get("payload")
        action_target = (
            payload.get("action_target") if isinstance(payload, Mapping) else None
        )
        if isinstance(action_target, Mapping) and action_target_matches(
            expected_target,
            action_target,
            capability_ids=capability_ids,
            source_step_id=source_step_id,
        ):
            return True
    return False


def _desktop_observation_step_needs_model_followup(
    decision: Any,
    step_id: str,
    tool_name: str,
) -> bool:
    if not _is_planned_desktop_observation_step(decision, step_id, tool_name):
        return False
    if step_id == "verify-opened-file":
        return _dynamic_file_open_step_needs_model_followup(decision)
    planned_step = _planned_step_by_id(decision, step_id)
    if str(getattr(planned_step, "action", "") or "").strip() == "observe_ui_target":
        return True
    if _desktop_observation_step_depends_on_model_resolved_ui_step(decision, step_id):
        return True
    if _selected_discovered_app_observation_needs_model_followup(decision, step_id):
        return True
    prompt = str(getattr(getattr(decision, "selected_intent", None), "user_goal", "") or "")
    if not prompt:
        return False
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if isinstance(inputs, Mapping) and isinstance(
        inputs.get("control_presence_inspection_hint"),
        Mapping,
    ):
        return _control_presence_prompt_needs_model_followup(prompt)
    if (
        _is_desktop_result_verification_step_id(step_id)
        and isinstance(inputs, Mapping)
        and (
            isinstance(inputs.get("creative_canvas_hint"), Mapping)
            or isinstance(inputs.get("model_generated_content_hint"), Mapping)
        )
    ):
        return True
    if _desktop_observation_step_is_direct_readback(prompt, inputs):
        return False
    if _desktop_observation_prompt_needs_model_followup(prompt, inputs):
        return True
    if _desktop_verify_step_is_direct_control(step_id, tool_name, inputs):
        return False
    return False


def _desktop_observation_step_depends_on_model_resolved_ui_step(
    decision: Any,
    step_id: str,
) -> bool:
    if not _is_desktop_result_verification_step_id(step_id):
        return False
    steps = list(getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", []) or [])
    step_indexes = {
        str(getattr(step, "step_id", "") or "").strip(): index
        for index, step in enumerate(steps)
        if str(getattr(step, "step_id", "") or "").strip()
    }
    target_index = step_indexes.get(step_id, -1)
    if target_index <= 0:
        return False
    dependencies = _transitive_step_dependencies(steps, step_id)
    if not dependencies:
        return False
    has_actionable_discovery = _has_actionable_desktop_app_discovery_step(steps)
    for dependency_id in dependencies:
        dependency_index = step_indexes.get(dependency_id, -1)
        if dependency_index < 0 or dependency_index >= target_index:
            continue
        if _unavailable_desktop_ui_step_can_continue_with_model(
            steps,
            dependency_index,
            has_actionable_discovery=has_actionable_discovery,
        ):
            return True
    return False


def _transitive_step_dependencies(steps: list[Any], step_id: str) -> set[str]:
    by_id = {
        str(getattr(step, "step_id", "") or "").strip(): step
        for step in steps
        if str(getattr(step, "step_id", "") or "").strip()
    }
    pending = [
        str(item or "").strip()
        for item in (getattr(by_id.get(step_id), "depends_on", None) or [])
        if str(item or "").strip()
    ]
    dependencies: set[str] = set()
    while pending:
        current = pending.pop()
        if current in dependencies:
            continue
        dependencies.add(current)
        current_step = by_id.get(current)
        if current_step is None:
            continue
        pending.extend(
            str(item or "").strip()
            for item in (getattr(current_step, "depends_on", None) or [])
            if str(item or "").strip()
        )
    return dependencies


def _selected_discovered_app_observation_needs_model_followup(
    decision: Any,
    step_id: str,
) -> bool:
    if step_id != "observe-selected-discovered-app":
        return False
    steps = list(getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", []) or [])
    saw_observation = False
    for step in steps:
        current_step_id = str(getattr(step, "step_id", "") or "").strip()
        if current_step_id == step_id:
            saw_observation = True
            continue
        if not saw_observation or not _step_available(step):
            continue
        depends_on = {
            str(item or "").strip()
            for item in (getattr(step, "depends_on", None) or [])
            if str(item or "").strip()
        }
        if step_id not in depends_on:
            continue
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if current_step_id.startswith("verify-") or tool_name in _EXECUTION_VERIFICATION_TOOLS:
            continue
        return False
    return saw_observation


def _dynamic_file_open_step_needs_model_followup(decision: Any) -> bool:
    if not _runtime_resolvable_dynamic_file_open_plan(decision):
        return False
    intent = getattr(decision, "selected_intent", None)
    prompt = str(getattr(intent, "user_goal", "") or "").strip()
    if not prompt:
        return False
    inputs = getattr(intent, "inputs", None)
    file_hint = (
        inputs.get("file_open_discovery_hint")
        if isinstance(inputs, Mapping)
        and isinstance(inputs.get("file_open_discovery_hint"), Mapping)
        else {}
    )
    candidates = _dynamic_file_open_target_candidates(file_hint)
    for candidate in candidates:
        index = prompt.lower().find(candidate.lower())
        if index < 0:
            continue
        tail = prompt[index + len(candidate) :]
        if _dynamic_file_open_tail_has_pending_action(tail):
            return True
    return bool(
        re.search(
            r"(?:打开|导入|载入|使用|用|open|import|load|use).{0,160}"
            r"(?:，|,|并且|并|然后|再|接着|之后|后|\band\b|\bthen\b).{0,80}"
            r"(?:调整|缩放|裁剪|筛选|过滤|排序|编辑|处理|压缩|转换|保存|导出|标注|"
            r"设计|绘制|画|搜索|查找|替换|resize|scale|crop|filter|sort|edit|"
            r"process|compress|convert|save|export|annotate|design|draw|search|find|replace)",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def _dynamic_file_open_target_candidates(file_hint: Mapping[str, Any]) -> list[str]:
    candidates: list[str] = []
    for key in ("pattern", "target_path", "path"):
        value = str(file_hint.get(key) or "").strip()
        if not value or "*" in value or "{" in value:
            continue
        candidates.append(value.rsplit("/", 1)[-1])
        candidates.append(value)
    return [
        candidate
        for index, candidate in enumerate(candidates)
        if candidate and candidate not in candidates[:index]
    ]


def _dynamic_file_open_tail_has_pending_action(tail: str) -> bool:
    value = str(tail or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            r"^(?:[，,。；;:：\s]*(?:并且|并|然后|再|接着|之后|后|and\s+then|then|and)?"
            r"[，,。；;:：\s]*){0,3}"
            r".{0,80}(?:调整|缩放|裁剪|筛选|过滤|排序|编辑|处理|压缩|转换|保存|导出|"
            r"标注|设计|绘制|画|搜索|查找|替换|resize|scale|crop|filter|sort|"
            r"edit|process|compress|convert|save|export|annotate|design|draw|search|find|replace)",
            value,
            flags=re.IGNORECASE,
        )
    )


def _control_presence_prompt_needs_model_followup(prompt: str) -> bool:
    value = str(prompt or "").strip()
    if not value:
        return False
    if re.search(
        r"(?:有哪些|有什么|列出|列一下|显示|查看|看看|看一下|读取|识别|在哪|在哪里|哪里|位置|坐标)",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    if re.search(
        r"\b(?:list|show|read|inspect|where|what|which)\b",
        value,
        flags=re.IGNORECASE,
    ):
        return False
    return True


def _coordinate_click_resolution_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    if "desktop.click" not in allowed:
        return []
    tool_plan = getattr(getattr(decision, "plan", None), "tool_plan", None)
    steps = list(getattr(tool_plan, "steps", None) or [])
    click_step = next(
        (
            item
            for item in steps
            if str(getattr(item, "step_id", "") or "").strip() == "operate-foreground-ui"
            and str(getattr(item, "status", "") or "").strip() == "unavailable"
            and str(getattr(item, "capability_id", "") or "").strip() == "desktop.ui_operation"
        ),
        None,
    )
    if click_step is None:
        return []
    click_payload = getattr(click_step, "input_preview", None)
    if not isinstance(click_payload, Mapping):
        return []
    if not str(click_payload.get("target") or "").strip():
        return []
    if click_payload.get("x") is not None or click_payload.get("y") is not None:
        return []

    observation_step = _coordinate_click_resolution_observation_step(steps, allowed)
    if observation_step is None:
        return []

    requests: list[dict[str, Any]] = []
    discover_step = next(
        (
            item
            for item in steps
            if str(getattr(item, "step_id", "") or "").strip() == "discover-desktop-state"
            and item is not observation_step
            and _step_available(item)
        ),
        None,
    )
    discover_tool = str(getattr(discover_step, "tool_name", "") or "").strip()
    if discover_step is not None and discover_tool in allowed:
        discover_payload = getattr(discover_step, "input_preview", None)
        requests.append(
            _request(
                discover_tool,
                _desktop_request_payload(
                    discover_tool,
                    dict(discover_payload) if isinstance(discover_payload, Mapping) else {},
                ),
                planning_reason=_desktop_step_planning_reason(discover_step, discover_tool),
            )
        )

    observation_tool = str(getattr(observation_step, "tool_name", "") or "").strip()
    observation_payload = getattr(observation_step, "input_preview", None)
    request = _request(
        observation_tool,
        _desktop_request_payload(
            observation_tool,
            dict(observation_payload) if isinstance(observation_payload, Mapping) else {},
        ),
        planning_reason=_desktop_step_planning_reason(observation_step, observation_tool),
    )
    request["continue_to_model"] = True
    requests.append(request)
    return requests


def _coordinate_click_resolution_observation_step(
    steps: list[Any],
    allowed: set[str],
) -> Any | None:
    preferred_tools = {
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.inspect_app",
        "screen.capture",
    }
    for step in steps:
        if not _step_available(step):
            continue
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if tool_name in preferred_tools and tool_name in allowed:
            return step
    return None


def _desktop_observation_prompt_needs_model_followup(prompt: str, inputs: Any) -> bool:
    prompt_for_intent = prompt
    if isinstance(inputs, Mapping):
        compose_text = str(inputs.get("foreground_compose_text_hint") or "").strip()
        if compose_text:
            prompt_for_intent = prompt_for_intent.replace(compose_text, "")
    return bool(
        re.search(
            r"(?:判断|决定|分析|识别|告诉|说明|总结|摘要|下一步|该点哪里|该点哪个|"
            r"可以点|是否可以点|是否能点|如果能点|如果可以点|"
            r"最像|最接近|相关|有关|匹配|合适|适合|应该|可能|哪一个|哪项|哪条)",
            prompt_for_intent,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"(?:看看|检查|确认|验证|核对|比较|对比).{0,16}"
            r"(?:结果|内容|状态|是否|有没有|包含|存在|相同|一致)|"
            r"(?:结果|内容|状态).{0,16}"
            r"(?:是否|有没有|包含|存在|相同|一致)",
            prompt_for_intent,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:judge|decide|analy[sz]e|identify|tell|explain|summari[sz]e|"
            r"determine|whether|what|which|where|should|next\s+step|closest|similar|"
            r"related|matching|appropriate|suitable|possible)\b",
            prompt_for_intent,
            flags=re.IGNORECASE,
        )
    )


def _desktop_verify_step_is_direct_control(
    step_id: str,
    tool_name: str,
    inputs: Any,
) -> bool:
    if not _is_desktop_result_verification_step_id(step_id) or tool_name not in {
        "desktop.active_window",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.verify",
    }:
        return False
    if not isinstance(inputs, Mapping):
        return False
    if any(
        isinstance(inputs.get(key), Mapping)
        for key in (
            "creative_canvas_hint",
            "ui_inspection_hint",
            "screen_capture_hint",
            "app_capability_hint",
        )
    ):
        return False
    operation = str(inputs.get("operation_hint") or "").strip()
    return operation in {
        "",
        "open",
        "focus",
        "open_app",
        "focus_app",
        "hide_app",
        "minimize_window",
        "show_all_apps",
        "safe_shortcut",
        "safe_key",
        "safe_scroll",
    }


def _desktop_observation_step_is_direct_readback(
    prompt: str,
    inputs: Any,
) -> bool:
    if not isinstance(inputs, Mapping):
        return False
    operation = str(inputs.get("operation_hint") or "").strip()
    if operation in {"read_active_window", "list_windows", "list_running_apps"}:
        # Enumerating window identities is a native readback. Words such as
        # "what" and "which" alone do not require a model to interpret it.
        return not bool(re.search(
            r"(?:分析|总结|摘要|比较|对比|下一步|该点|应该|"
            r"\b(?:analy[sz]e|summari[sz]e|compare|decide|should|next\s+step)\b)",
            prompt,
            flags=re.IGNORECASE,
        ))
    if str(inputs.get("operation_hint") or "").strip() != "read_ui":
        return False
    if not isinstance(inputs.get("ui_inspection_hint"), Mapping):
        return False
    return bool(
        re.search(
            r"(?:有哪些|有什么|有什么可见|可见.*(?:按钮|控件|元素)|"
            r"(?:按钮|控件|元素).{0,8}(?:有哪些|有什么|可见))",
            prompt,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bwhat\s+(?:buttons?|controls?|elements?)\s+(?:are\s+)?(?:visible|shown|available)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:visible|shown|available)\s+(?:buttons?|controls?|elements?)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\bwhere\s+(?:is|are)\s+(?:the\s+)?(?:.+?\s+)?(?:buttons?|controls?|elements?)\b",
            prompt,
            flags=re.IGNORECASE,
        )
    )


def _desktop_discovery_step_needs_model_followup(
    decision: Any,
    step_id: str,
    tool_name: str,
) -> bool:
    if tool_name == "workspace.list" and step_id == "discover-file-open-target":
        return not _runtime_resolvable_dynamic_file_open_plan(decision)
    if tool_name != "desktop.list_apps":
        return False
    if step_id not in {"discover-desktop-state", "discover_apps-desktop-state"}:
        return False
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if not isinstance(inputs, Mapping):
        return False
    if _runtime_resolvable_discovered_app_plan(decision):
        return False
    if isinstance(inputs.get("app_capability_hint"), Mapping):
        return True
    if isinstance(inputs.get("generic_browser_discovery_hint"), Mapping):
        return True
    if isinstance(inputs.get("generic_music_app_discovery_hint"), Mapping):
        return True
    if isinstance(inputs.get("generic_file_manager_discovery_hint"), Mapping):
        return True
    if isinstance(inputs.get("generic_terminal_app_discovery_hint"), Mapping):
        return True
    return any(
        str(getattr(step, "step_id", "") or "").strip() == "open-selected-discovered-app"
        for step in getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", [])
    )


def _desktop_request_payload(tool_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = _drop_readiness_payload_fields(payload)
    if tool_name.startswith("app.") or tool_name in {
        "desktop.open_app",
        "desktop.focus_app",
        "desktop.open_path_with_app",
    }:
        return _canonicalize_app_payload(payload)
    if tool_name == "desktop.list_apps":
        query = str(payload.get("query") or "").strip()
        if not query:
            return payload
        canonical = _canonical_app_name(query) if not query.isascii() else query
        return {**payload, "query": canonical}
    if tool_name == "desktop.inspect_app":
        app_name = str(payload.get("app_name") or "").strip()
        request_payload = {
            key: payload[key]
            for key in ("open_if_needed", "focus", "role_filter", "limit")
            if key in payload and payload[key] not in (None, "")
        }
        if app_name:
            request_payload["app_name"] = _canonical_app_name(app_name)
            _copy_app_selection_metadata(payload, request_payload)
        return request_payload
    if tool_name in {"desktop.running_apps", "desktop.active_window"}:
        return {}
    if tool_name == "screen.capture":
        reason = str(payload.get("reason") or "").strip()
        return {"reason": reason} if reason else {}
    if tool_name in {"desktop.ui_elements", "desktop.read_ui"}:
        request_payload = {
            key: payload[key]
            for key in ("app_name", "role_filter", "limit")
            if key in payload and payload[key] not in (None, "")
        }
        if request_payload.get("app_name"):
            request_payload["app_name"] = _canonical_app_name(
                str(request_payload["app_name"] or "")
            )
            _copy_app_selection_metadata(payload, request_payload)
        return request_payload
    if tool_name in {"desktop.windows", "desktop.list_windows", "desktop.verify"}:
        app_name = str(payload.get("app_name") or "").strip()
        request_payload = {
            key: payload[key]
            for key in ("role_filter", "limit")
            if key in payload and payload[key] not in (None, "")
        }
        if (
            tool_name == "desktop.verify"
            and payload.get("verification_goal") not in (None, "")
        ):
            request_payload["verification_goal"] = payload["verification_goal"]
        if app_name:
            request_payload["app_name"] = _canonical_app_name(app_name)
            _copy_app_selection_metadata(payload, request_payload)
        return request_payload
    if tool_name == "media.music_app_open_and_play":
        app_name = str(payload.get("app_name") or "").strip()
        return {"app_name": _canonical_app_name(app_name)} if app_name else {}
    return payload


def _drop_readiness_payload_fields(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in dict(payload).items()
        if key not in {"blocking_conditions", "missing_permissions"}
    }


def _copy_app_selection_metadata(
    source: Mapping[str, Any],
    target: dict[str, Any],
) -> None:
    if str(source.get("selection_source") or "").strip():
        target["selection_source"] = str(source.get("selection_source") or "").strip()
    if str(source.get("app_selection_source") or "").strip():
        target["app_selection_source"] = str(
            source.get("app_selection_source") or ""
        ).strip()
    if str(source.get("query") or "").strip():
        target["query"] = str(source.get("query") or "").strip()


def _canonicalize_app_payload(payload: dict[str, Any]) -> dict[str, Any]:
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name:
        return payload
    canonical = _canonical_app_name(app_name)
    canonical_payload = payload if canonical == app_name else {**payload, "app_name": canonical}
    # Application aliases must not rewrite the planner-owned UI label.
    return canonical_payload


def _canonical_app_name(app_name: str) -> str:
    if str(app_name or "").strip() == "企业微信":
        return "企业微信"
    return legacy_app_name_hint(app_name)


def _data_analysis_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_readiness_blocked: bool = False,
) -> list[dict[str, Any]]:
    app_requests = _data_analysis_spreadsheet_app_requests(decision, allowed)
    file_open_requests = _data_analysis_file_open_requests(
        decision,
        allowed,
        allow_readiness_blocked=allow_readiness_blocked,
    )
    data_analyze_step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "tool_name", "") == "data.analyze"
        ),
        None,
    )
    if (
        data_analyze_step is not None
        and _step_available(data_analyze_step)
        and "data.analyze" in allowed
    ):
        input_preview = getattr(data_analyze_step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        path_value = str(payload.get("path") or "").strip()
        uses_workspace_file_selection = (
            path_value
            in {
                "<selected file from workspace.list>",
                "<selected files from workspace.list>",
            }
            and str(payload.get("selection_source") or "").strip() == "workspace.list"
        )
        if (
            path_value
            and (
                uses_workspace_file_selection
                or not (path_value.startswith("<") and path_value.endswith(">"))
            )
        ):
            request_input = _data_analysis_request_input_from_payload(
                payload,
                include_workspace_file_selection=uses_workspace_file_selection,
            )
            analyze_request = _request(
                "data.analyze",
                request_input,
                planning_reason="planner_builtin_data_analysis",
            )
            workspace_file_selection_requests: list[dict[str, Any]] = []
            if uses_workspace_file_selection:
                workspace_file_selection_requests = _context_prefetch_tool_requests(
                    decision,
                    allowed,
                    step_ids=("inspect-data-source",),
                    planning_reason="planner_prefetch_data_source",
                )
                for request in workspace_file_selection_requests:
                    request.pop("continue_to_model", None)
            runtime_followup_requests = _data_analysis_app_write_followup_requests(
                decision,
                allowed,
            )
            if runtime_followup_requests:
                return [
                    *app_requests,
                    *file_open_requests,
                    *workspace_file_selection_requests,
                    analyze_request,
                    *runtime_followup_requests,
                ]
            if _data_analysis_requires_model_followup(decision):
                analyze_request["continue_to_model"] = True
                return [
                    *app_requests,
                    *file_open_requests,
                    *workspace_file_selection_requests,
                    analyze_request,
                ]
            artifact_reveal_requests = _artifact_reveal_tool_requests(
                decision,
                allowed,
                planning_reason="planner_builtin_data_analysis",
            )
            if _artifact_reveal_requests_include_analysis_app_open(decision):
                _attach_basic_step_metadata(analyze_request, data_analyze_step)
            return [
                *app_requests,
                *file_open_requests,
                *workspace_file_selection_requests,
                analyze_request,
                *artifact_reveal_requests,
            ]

    inputs = decision.selected_intent.inputs
    context_source = str(inputs.get("context_source") or "").strip()
    if context_source in {"selection", "clipboard"}:
        context_requests = _context_source_tool_requests(
            decision,
            allowed,
            step_ids=("copy-selected-data-context", "read-data-context"),
            planning_reason="planner_prefetch_data_source",
        )
        return _append_model_followup_requests(context_requests, app_requests)
    if context_source in {"current_page_content", "visible_text"}:
        context_requests = _context_source_tool_requests(
            decision,
            allowed,
            step_ids=(
                "select-current-data-context",
                "copy-current-data-context",
                "read-data-context",
            ),
            planning_reason="planner_prefetch_data_source",
        )
        if _data_analysis_opens_spreadsheet_before_context(decision):
            if context_requests:
                return [*app_requests, *file_open_requests, *context_requests]
            return _mark_last_request_for_model_followup([*app_requests, *file_open_requests])
        return _append_model_followup_requests(context_requests, [*app_requests, *file_open_requests])
    source_hint = str(inputs.get("data_source_hint") or "").strip()
    readable_tool = _first_allowed(("workspace.read", "fs.read_file", "file.read"), allowed)
    if _workspace_readable_data_source(source_hint, inputs) and readable_tool:
        request = _request(
            readable_tool,
            {"path": source_hint},
            planning_reason="planner_prefetch_data_source",
        )
        request["continue_to_model"] = True
        return _append_model_followup_requests([request], [*app_requests, *file_open_requests])
    if source_hint:
        return _mark_last_request_for_model_followup([*app_requests, *file_open_requests])
    source_scope = str(inputs.get("data_source_scope_hint") or "").strip()
    if source_scope and not _workspace_listable_data_scope(source_scope):
        return _mark_last_request_for_model_followup([*app_requests, *file_open_requests])
    context_requests = _context_prefetch_tool_requests(
        decision,
        allowed,
        step_ids=("inspect-data-source",),
        planning_reason="planner_prefetch_data_source",
    )
    return _append_model_followup_requests(context_requests, [*app_requests, *file_open_requests])


_DATA_ANALYSIS_APP_WRITE_FOLLOWUP_STEP_IDS = {
    "discover-analysis-target-app",
    "prepare-analysis-target-app",
    "prepare-analysis-discovered-target-app",
    "insert-analysis-into-target-app",
    "verify-analysis-target-app",
}


def _data_analysis_app_write_followup_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if not isinstance(inputs, Mapping):
        return []
    if str(inputs.get("target_action_hint") or "").strip() != "app_paste":
        return []
    if not (
        str(inputs.get("target_app_hint") or "").strip()
        or isinstance(inputs.get("target_app_capability_hint"), Mapping)
    ):
        return []

    requests: list[dict[str, Any]] = []
    saw_analyze_step = False
    saw_insert_step = False
    for step in list(getattr(decision.plan.tool_plan, "steps", []) or []):
        step_id = str(getattr(step, "step_id", "") or "").strip()
        if step_id == "analyze-data-file":
            saw_analyze_step = True
            continue
        if not saw_analyze_step or step_id not in _DATA_ANALYSIS_APP_WRITE_FOLLOWUP_STEP_IDS:
            continue
        if not _step_available(step):
            if step_id == "verify-analysis-target-app":
                continue
            return []
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed:
            if step_id == "verify-analysis-target-app":
                continue
            return []
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        requests.append(
            _request(
                tool_name,
                _desktop_request_payload(tool_name, payload),
                planning_reason=_data_analysis_followup_planning_reason(step_id),
            )
        )
        if step_id == "insert-analysis-into-target-app":
            saw_insert_step = True
    return requests if saw_insert_step else []


def _data_analysis_followup_planning_reason(step_id: str) -> str:
    if step_id == "discover-analysis-target-app":
        return "planner_data_analysis_target_app_discovery"
    if step_id in {
        "prepare-analysis-target-app",
        "prepare-analysis-discovered-target-app",
    }:
        return "planner_data_analysis_target_app_prepare"
    if step_id == "insert-analysis-into-target-app":
        return "planner_data_analysis_artifact_insert"
    if step_id == "verify-analysis-target-app":
        return "planner_data_analysis_target_app_verify"
    return "planner_builtin_data_analysis"


def _data_analysis_request_input_from_payload(
    payload: Mapping[str, Any],
    *,
    include_workspace_file_selection: bool = False,
) -> dict[str, Any]:
    request_input: dict[str, Any] = {
        "path": str(payload.get("path") or "").strip(),
        "artifact_path": str(payload.get("artifact_path") or "analysis-report.md"),
    }
    source_kind = str(payload.get("source_kind") or "").strip()
    if source_kind:
        request_input["source_kind"] = source_kind
    requested_outputs = payload.get("requested_outputs")
    if isinstance(requested_outputs, list):
        request_input["requested_outputs"] = [
            str(item or "").strip()
            for item in requested_outputs
            if str(item or "").strip()
        ]
    artifact_manifest = payload.get("artifact_manifest")
    if isinstance(artifact_manifest, list):
        request_input["artifact_manifest"] = [
            dict(item)
            for item in artifact_manifest
            if isinstance(item, Mapping)
        ]
    artifact_paths = payload.get("artifact_paths")
    if isinstance(artifact_paths, list):
        request_input["artifact_paths"] = [
            str(path or "").strip()
            for path in artifact_paths
            if str(path or "").strip()
        ]
    if payload.get("max_rows"):
        request_input["max_rows"] = int(payload.get("max_rows") or 1000)
    if include_workspace_file_selection:
        for key in ("selection_source", "source_scope", "pattern", "file_type", "selection"):
            value = str(payload.get(key) or "").strip()
            if value:
                request_input[key] = value
    return request_input


def _data_analysis_requires_model_followup(decision: Any) -> bool:
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if isinstance(inputs, Mapping):
        target_app = str(inputs.get("target_app_hint") or "").strip()
        target_action = str(inputs.get("target_action_hint") or "").strip()
        if target_app and target_action == "app_paste":
            return True
        if isinstance(inputs.get("target_app_capability_hint"), Mapping):
            return True
        if str(inputs.get("output_target_hint") or "").strip() == "clipboard":
            return True
    followup_step_ids = {
        "prepare-analysis-target-app",
        "write-clipboard-output",
        "draft-analysis-communication",
        "draft-analysis-communication-message",
        "send-analysis-communication-message",
    }
    return any(
        str(getattr(step, "step_id", "") or "").strip() in followup_step_ids
        for step in decision.plan.tool_plan.steps
    )


def _data_analysis_opens_spreadsheet_before_context(decision: Any) -> bool:
    step_ids = [
        str(getattr(step, "step_id", "") or "").strip()
        for step in getattr(getattr(decision, "plan", None), "tool_plan", None).steps
    ]
    try:
        return step_ids.index("open-spreadsheet-app") < step_ids.index("read-data-context")
    except (AttributeError, ValueError):
        return False


def _data_analysis_spreadsheet_app_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "step_id", "") == "open-spreadsheet-app"
        ),
        None,
    )
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not _step_available(step):
        return []
    if not tool_name or tool_name not in allowed:
        return []
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    return [
        _request(
            tool_name,
            _desktop_request_payload(tool_name, payload),
            planning_reason="planner_fallback_data_analysis_spreadsheet_app",
        )
    ]


def _data_analysis_file_open_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_readiness_blocked: bool = False,
) -> list[dict[str, Any]]:
    step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "step_id", "") == "open-data-file"
        ),
        None,
    )
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not _direct_step_available(
        step,
        allow_readiness_blocked=allow_readiness_blocked,
    ):
        return []
    if tool_name != "desktop.open_path" or tool_name not in allowed:
        return []
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    path = str(payload.get("path") or "").strip()
    if not path:
        return []
    return [
        _request(
            "desktop.open_path",
            {"path": path},
            planning_reason="planner_fallback_data_analysis_file_open",
        )
    ]


def _artifact_reveal_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    planning_reason: str,
) -> list[dict[str, Any]]:
    step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "step_id", "")
            in {
                "reveal-artifact-in-finder",
                "open-analysis-artifact-with-app",
            }
        ),
        None,
    )
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not _step_available(step):
        return []
    if tool_name not in {
        "desktop.reveal_path",
        "desktop.open_path",
        *_OPEN_PATH_WITH_APP_TOOLS,
    }:
        return []
    if tool_name not in allowed:
        return []
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    path = str(payload.get("path") or "").strip()
    if not path:
        return []
    include_step_metadata = (
        str(getattr(step, "step_id", "") or "").strip()
        == "open-analysis-artifact-with-app"
    )
    if _is_open_path_with_app_tool(tool_name):
        app_name = str(payload.get("app_name") or "").strip()
        if not app_name:
            return []
        request_input = _desktop_request_payload(
            tool_name,
            {"path": path, "app_name": app_name},
        )
        request = _request(
            tool_name,
            request_input,
            planning_reason=planning_reason,
        )
        if include_step_metadata:
            _attach_basic_step_metadata(request, step)
        return [request]
    request = _request(
        tool_name,
        {"path": path},
        planning_reason=planning_reason,
    )
    if include_step_metadata:
        _attach_basic_step_metadata(request, step)
    return [request]


def _artifact_reveal_requests_include_analysis_app_open(decision: Any) -> bool:
    return any(
        str(getattr(step, "step_id", "") or "").strip()
        == "open-analysis-artifact-with-app"
        and _step_available(step)
        for step in getattr(getattr(getattr(decision, "plan", None), "tool_plan", None), "steps", [])
    )


def _append_model_followup_requests(
    base_requests: list[dict[str, Any]],
    extra_requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not extra_requests:
        return base_requests
    if not base_requests:
        return _mark_last_request_for_model_followup(extra_requests)
    continue_to_model = bool(base_requests[-1].pop("continue_to_model", False))
    requests = [*base_requests, *extra_requests]
    if continue_to_model:
        requests[-1]["continue_to_model"] = True
    return requests


def _mark_last_request_for_model_followup(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if requests:
        requests[-1]["continue_to_model"] = True
    return requests


def _code_task_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    terminal_hint = inputs.get("terminal_command_hint")
    if isinstance(terminal_hint, Mapping):
        command = str(terminal_hint.get("command") or "").strip()
        if command and "terminal.run" in allowed:
            return [
                _request(
                    "terminal.run",
                    {"command": command},
                    planning_reason="planner_fallback_terminal_command",
                )
            ]
    terminal_execution_hint = inputs.get("terminal_execution_request_hint")
    if (
        isinstance(terminal_execution_hint, Mapping)
        and str(terminal_execution_hint.get("mode") or "").strip()
        == "model_selected_approved_command"
        and "terminal.run" in allowed
    ):
        step = _planned_step_by_id(
            decision,
            "run-model-selected-terminal-command",
        )
        input_preview = (
            getattr(step, "input_preview", None)
            if step is not None
            else None
        )
        payload = (
            dict(input_preview)
            if isinstance(input_preview, Mapping)
            else {}
        )
        if (
            str(payload.get("body_source") or "").strip()
            == "model_generated_content"
            and str(payload.get("operation") or "").strip()
            == "execute_user_delegated_terminal_task"
            and not str(payload.get("command") or "").strip()
        ):
            request = _request(
                "terminal.run",
                payload,
                planning_reason="planner_terminal_command_materialization",
            )
            request["continue_to_model"] = True
            return [request]
    diagnostic_hint = inputs.get("code_diagnostic_command_hint")
    if isinstance(diagnostic_hint, Mapping):
        command = str(diagnostic_hint.get("command") or "").strip()
        if command and "terminal.run" in allowed:
            needs_model_followup = _code_diagnostic_requires_model_followup(decision)
            prefetch_requests = []
            if needs_model_followup:
                prefetch_requests = _code_context_prefetch_tool_requests(
                    decision,
                    allowed,
                    planning_reason="planner_prefetch_code_context",
                )
                for prefetch_request in prefetch_requests:
                    prefetch_request.pop("continue_to_model", None)
            request = _request(
                "terminal.run",
                {"command": command},
                planning_reason="planner_fallback_code_diagnostic",
            )
            _attach_basic_step_metadata(
                request,
                _planned_step_by_id(decision, "run-code-diagnostic"),
            )
            if needs_model_followup:
                request["continue_to_model"] = True
            return [*prefetch_requests, request]
    return _code_context_prefetch_tool_requests(
        decision,
        allowed,
        planning_reason="planner_prefetch_code_context",
    )


def _code_context_prefetch_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    planning_reason: str,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for step in _code_context_prefetch_steps(decision):
        request = _context_prefetch_request_for_step(
            step,
            allowed,
            planning_reason=planning_reason,
        )
        if not request:
            continue
        request.pop("continue_to_model", None)
        _attach_basic_step_metadata(request, step)
        requests.append(request)
    if requests:
        requests[-1]["continue_to_model"] = True
    return requests


def _code_context_prefetch_steps(decision: Any) -> list[Any]:
    steps = list(getattr(getattr(decision.plan, "tool_plan", None), "steps", []) or [])
    ordered_ids = ["inspect-workspace"]
    ordered_ids.extend(
        str(getattr(step, "step_id", "") or "").strip()
        for step in steps
        if str(getattr(step, "step_id", "") or "").strip().startswith("inspect-code-area-")
    )
    ordered_ids.append("read-code-target-file")
    return [
        step
        for step_id in ordered_ids
        if (step := _planned_step_by_id(decision, step_id)) is not None
    ]


def _code_diagnostic_requires_model_followup(decision: Any) -> bool:
    inputs = getattr(getattr(decision, "selected_intent", None), "inputs", None)
    if isinstance(inputs, Mapping) and isinstance(inputs.get("code_change_hint"), Mapping):
        return True
    prompt = str(getattr(getattr(decision, "selected_intent", None), "user_goal", "") or "")
    clean = re.sub(r"\s+", " ", prompt).strip()
    if not clean:
        return True
    if re.search(
        r"\b(?:fix|repair|debug|diagnose|analy[sz]e|explain)\b",
        clean,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(r"failing\s+tests?|test\s+failures?", clean, flags=re.IGNORECASE):
        return True
    if re.search(r"(?:修复|修正|诊断|排查|分析|解释|失败|报错|不过|不通过)", clean):
        return True
    if re.search(
        r"\b(?:run|execute|check)\b|(?:运行|执行|跑|检查)",
        clean,
        flags=re.IGNORECASE,
    ):
        return False
    return True


def _media_tool_requests(inputs: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    tool_name, payload = media_tool_preview(inputs, allowed)
    direct_apple_query = bool(
        tool_name == "media.apple_music_play"
        and str(payload.get("query") or "").strip()
    )
    app_query_plan = (
        [] if direct_apple_query else media_app_query_search_plan(inputs, allowed)
    )
    if app_query_plan:
        requests = [
            _request(
                tool_name,
                payload,
                planning_reason="planner_fallback_media_playback",
            )
            for tool_name, payload in app_query_plan
        ]
        if _media_query_plan_needs_selected_app_followup(app_query_plan):
            if requests:
                requests[0]["continue_to_model"] = True
            return requests[:1]
        if _media_query_plan_needs_search_result_followup(app_query_plan):
            if requests:
                requests[-1]["continue_to_model"] = True
        return requests
    if not tool_name:
        prepare_plan = media_app_prepare_plan(inputs, allowed)
        if not prepare_plan:
            return []
        requests = [
            _request(
                tool_name,
                payload,
                planning_reason="planner_fallback_media_playback",
            )
            for tool_name, payload in prepare_plan
        ]
        if _media_query_plan_needs_selected_app_followup(prepare_plan):
            if requests:
                requests[0]["continue_to_model"] = True
            return requests[:1]
        return requests
    requests = [
        _request(
            tool_name,
            payload,
            planning_reason="planner_fallback_media_playback",
        )
    ]
    verify_request = _media_playback_verify_request(
        inputs,
        allowed,
        control_tool_name=tool_name,
    )
    if verify_request:
        requests.append(verify_request)
    return requests


def _media_query_plan_needs_selected_app_followup(
    app_query_plan: list[tuple[str, dict[str, Any]]],
) -> bool:
    return any(
        _selected_discovered_app_payload_requires_model(payload)
        for _tool_name, payload in app_query_plan
        if isinstance(payload, Mapping)
    )


def _media_query_plan_needs_search_result_followup(
    app_query_plan: list[tuple[str, dict[str, Any]]],
) -> bool:
    playback_tools = {
        "media.music_app_open_and_play",
        "app.focus_and_click_ui_element",
        "app.open_and_click_ui_element",
        "desktop.click_ui_element",
        "desktop.safe_click",
        "desktop.click",
    }
    return not any(str(tool_name or "").strip() in playback_tools for tool_name, _ in app_query_plan)


def _media_playback_verify_request(
    inputs: Mapping[str, Any],
    allowed: set[str],
    *,
    control_tool_name: str,
) -> dict[str, Any]:
    action = str(inputs.get("action") or "").strip() or "play"
    if action == "status" or control_tool_name.startswith("media."):
        return {}
    tool_name = _first_allowed(("desktop.ui_elements", "desktop.active_window", "screen.capture"), allowed)
    if not tool_name:
        return {}
    payload: dict[str, Any] = {}
    if tool_name == "desktop.ui_elements":
        payload = {"role_filter": "", "limit": 80}
    elif tool_name == "screen.capture":
        payload = {"reason": "verify media playback"}
    return _request(
        tool_name,
        payload,
        planning_reason="planner_fallback_media_playback",
    )


def _system_tool_requests(inputs: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    tool_name, payload = system_tool_preview(inputs, allowed)
    if not tool_name:
        fallback_requests = _system_settings_open_fallback_requests(inputs, allowed)
        if fallback_requests:
            return fallback_requests
        return []
    requests = [
        _request(
            tool_name,
            payload,
            planning_reason="planner_fallback_system_control",
        )
    ]
    verify_request = _system_control_verify_request(tool_name, payload, allowed)
    if verify_request:
        requests.append(verify_request)
    if (
        tool_name == "system.settings_open"
        and bool(inputs.get("inspect_ui"))
        and "desktop.ui_elements" in allowed
    ):
        requests.append(
            _request(
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80},
                planning_reason="planner_fallback_system_control",
            )
        )
    return requests


def _system_control_verify_request(
    tool_name: str,
    payload: Mapping[str, Any],
    allowed: set[str],
) -> dict[str, Any]:
    if tool_name != "system.volume" or "system.volume" not in allowed:
        return {}
    action = str(payload.get("action") or "").strip()
    if action == "status":
        return {}
    request = _request(
        "system.volume",
        {"action": "status"},
        planning_reason="planner_fallback_system_control",
    )
    request["continue_to_model"] = True
    return request


_SYSTEM_VOLUME_MUTATION_ACTIONS = frozenset({"set", "up", "down", "mute", "unmute"})


_MODEL_APP_FOREGROUND_MUTATION_TOOLS = frozenset(
    {
        "app.open",
        "app.focus",
        "desktop.open_app",
        "desktop.focus_app",
    }
)


_MODEL_APP_OPERATION_MUTATION_PREFIXES = ("app.open_and_", "app.focus_and_")


_MODEL_DESKTOP_OPERATION_MUTATION_TOOLS = frozenset(
    {
        *_APP_FOREGROUND_DIRECT_OPERATION_SUFFIX.keys(),
        "desktop.search_submit",
        "desktop.submit_foreground",
    }
)


def _append_model_app_foreground_verification_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    foreground_verification_tool = _first_allowed(
        (
            "desktop.active_window",
            "desktop.running_apps",
            "desktop.ui_elements",
            "screen.capture",
        ),
        allowed,
    )
    operation_verification_tool = _first_allowed(
        (
            "desktop.ui_elements",
            "desktop.read_ui",
            "desktop.active_window",
            "screen.capture",
        ),
        allowed,
    )
    if not foreground_verification_tool and not operation_verification_tool:
        return requests
    if _has_native_tool_call_protocol(requests):
        return _append_model_app_foreground_verification_after_native_tool_calls(
            requests,
            foreground_verification_tool,
            operation_verification_tool,
        )
    normalized: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        normalized.append(request)
        verification_tool = _model_app_verification_tool_for_request(
            request,
            foreground_verification_tool,
            operation_verification_tool,
        )
        if not verification_tool:
            continue
        if _has_later_execution_verification_before_mutation(requests, index):
            continue
        normalized.append(
            _model_app_foreground_verification_request(request, verification_tool)
        )
    return normalized


def _append_model_app_foreground_verification_after_native_tool_calls(
    requests: list[dict[str, Any]],
    foreground_verification_tool: str,
    operation_verification_tool: str,
) -> list[dict[str, Any]]:
    mutation_request: dict[str, Any] | None = None
    mutation_index = -1
    verification_tool = ""
    for index, request in enumerate(requests):
        request_verification_tool = _model_app_verification_tool_for_request(
            request,
            foreground_verification_tool,
            operation_verification_tool,
        )
        if request_verification_tool:
            mutation_request = request
            mutation_index = index
            verification_tool = request_verification_tool
    if mutation_request is None:
        return requests
    if _has_later_execution_verification_before_mutation(requests, mutation_index):
        return requests
    return [
        *requests,
        _model_app_foreground_verification_request(mutation_request, verification_tool),
    ]


def _model_app_verification_tool_for_request(
    request: Mapping[str, Any],
    foreground_verification_tool: str,
    operation_verification_tool: str,
) -> str:
    if _model_desktop_operation_request_needs_verification(request):
        return operation_verification_tool
    if _model_app_operation_request_needs_verification(request):
        return operation_verification_tool
    if _model_app_foreground_request_needs_verification(request):
        return foreground_verification_tool
    return ""


def _model_app_operation_request_needs_verification(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if not tool_name.startswith(_MODEL_APP_OPERATION_MUTATION_PREFIXES):
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return bool(str(payload.get("app_name") or "").strip())


def _model_desktop_operation_request_needs_verification(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    return tool_name in _MODEL_DESKTOP_OPERATION_MUTATION_TOOLS


def _model_app_foreground_request_needs_verification(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in _MODEL_APP_FOREGROUND_MUTATION_TOOLS:
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return bool(str(payload.get("app_name") or "").strip())


def _model_app_foreground_verification_request(
    source_request: Mapping[str, Any],
    verification_tool: str,
) -> dict[str, Any]:
    payload = (
        source_request.get("input")
        if isinstance(source_request.get("input"), Mapping)
        else {}
    )
    app_name = str(payload.get("app_name") or "").strip()
    is_operation = _model_app_operation_request_needs_verification(
        source_request
    ) or _model_desktop_operation_request_needs_verification(source_request)
    if verification_tool in {"desktop.ui_elements", "desktop.read_ui"}:
        input_preview: dict[str, Any] = {"limit": 80}
        if app_name and verification_tool == "desktop.ui_elements":
            input_preview["app_name"] = app_name
    elif verification_tool == "screen.capture":
        detail = "app operation" if is_operation else "foreground"
        input_preview = {"reason": f"verify {app_name} {detail}"}
    else:
        input_preview = {}
    request = _request(
        verification_tool,
        input_preview,
        planning_reason=(
            "runtime_desktop_app_operation_verification"
            if is_operation
            else "runtime_desktop_app_foreground_verification"
        ),
    )
    request["source"] = "runtime_verification"
    request["continue_to_model"] = True
    request["requires_observation"] = True
    request["runtime_stage"] = "verify"
    request["runtime_role"] = "verify_result"
    request["replan_triggers"] = ["verification_failed"]
    if app_name:
        request["target_app_name"] = app_name
    return request


def _append_system_volume_status_verification_requests(
    requests: list[dict[str, Any]],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if "system.volume" not in allowed:
        return requests
    requests = _bind_system_volume_status_verification_requests(requests)
    if _has_native_tool_call_protocol(requests):
        return _append_system_volume_status_after_native_tool_calls(requests)
    normalized: list[dict[str, Any]] = []
    for index, request in enumerate(requests):
        normalized.append(request)
        if not _system_volume_request_needs_status_verification(request):
            continue
        if _next_request_is_system_volume_status(requests, index):
            continue
        normalized.append(_system_volume_status_verification_request(request))
    return normalized


def _has_native_tool_call_protocol(requests: list[dict[str, Any]]) -> bool:
    return any(
        str(request.get("protocol") or "").strip() == "tool_calls"
        for request in requests
    )


def _append_system_volume_status_after_native_tool_calls(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mutation_request: dict[str, Any] | None = None
    mutation_index = -1
    for index, request in enumerate(requests):
        if _system_volume_request_needs_status_verification(request):
            mutation_request = request
            mutation_index = index
    if mutation_request is None:
        return requests
    if _has_later_system_volume_status_request(requests, mutation_index):
        return requests
    return [*requests, _system_volume_status_verification_request(mutation_request)]


def _system_volume_request_needs_status_verification(request: Mapping[str, Any]) -> bool:
    if str(request.get("tool") or "").strip() != "system.volume":
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    action = str(payload.get("action") or "").strip()
    return action in _SYSTEM_VOLUME_MUTATION_ACTIONS


def _next_request_is_system_volume_status(
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    if index + 1 >= len(requests):
        return False
    next_request = requests[index + 1]
    if str(next_request.get("tool") or "").strip() != "system.volume":
        return False
    payload = (
        next_request.get("input")
        if isinstance(next_request.get("input"), Mapping)
        else {}
    )
    return str(payload.get("action") or "").strip() == "status"


def _has_later_system_volume_status_request(
    requests: list[dict[str, Any]],
    index: int,
) -> bool:
    return any(
        _request_is_system_volume_status(request)
        for request in requests[index + 1 :]
    )


def _request_is_system_volume_status(request: Mapping[str, Any]) -> bool:
    if str(request.get("tool") or "").strip() != "system.volume":
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    return str(payload.get("action") or "").strip() == "status"


def _system_volume_status_verification_request(
    source_request: Mapping[str, Any],
) -> dict[str, Any]:
    source = str(source_request.get("source") or "").strip() or "runtime_model"
    if source != "runtime_planner":
        source = "runtime_verification"
    planning_reason = str(source_request.get("planning_reason") or "").strip()
    if planning_reason != "planner_fallback_system_control":
        planning_reason = "runtime_system_control_verification"
    request = {
        "protocol": "json_fallback",
        "tool": "system.volume",
        "input": {"action": "status"},
        "source": source,
        "planning_reason": planning_reason,
        "continue_to_model": True,
    }
    return _system_volume_status_request_with_exact_source(request, source_request)


def _bind_system_volume_status_verification_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind an existing volume status read to the exact preceding mutation."""

    last_mutation: Mapping[str, Any] | None = None
    normalized: list[dict[str, Any]] = []
    for request in requests:
        if _system_volume_request_needs_status_verification(request):
            last_mutation = request
            normalized.append(request)
            continue
        if last_mutation is not None and _request_is_system_volume_status(request):
            normalized.append(
                _system_volume_status_request_with_exact_source(
                    request,
                    last_mutation,
                )
            )
            last_mutation = None
            continue
        normalized.append(request)
    return normalized


def _system_volume_status_request_with_exact_source(
    status_request: Mapping[str, Any],
    source_request: Mapping[str, Any],
) -> dict[str, Any]:
    source_step_id = str(
        source_request.get("step_id")
        or source_request.get("planner_step_id")
        or ""
    ).strip()
    if not source_step_id:
        return dict(status_request)
    if not _system_volume_verification_scope_matches(status_request, source_request):
        return dict(status_request)
    existing_dependencies = _string_list(status_request.get("depends_on"))
    if existing_dependencies and source_step_id not in existing_dependencies:
        return dict(status_request)

    request = dict(status_request)
    for key in (
        "run_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "core_id",
        "workspace_id",
        "task_id",
    ):
        value = source_request.get(key)
        if value not in (None, "", [], {}):
            request.setdefault(key, value)
    verifier_step_id = str(
        request.get("step_id") or request.get("planner_step_id") or ""
    ).strip() or f"{source_step_id}:runtime-verify"
    request["step_id"] = verifier_step_id
    request["planner_step_id"] = verifier_step_id
    request["source_step_id"] = source_step_id
    request["depends_on"] = existing_dependencies or [source_step_id]
    source_request_id = str(source_request.get("request_id") or "").strip()
    if source_request_id:
        request.setdefault("source_request_id", source_request_id)
    source_tool_call_id = str(source_request.get("tool_call_id") or "").strip()
    if source_tool_call_id:
        request.setdefault("source_tool_call_id", source_tool_call_id)
    request["source"] = "runtime_verification"
    request["runtime_doctrine"] = "discover_operate_verify"
    request["runtime_stage"] = "verify"
    request["runtime_role"] = "verify_result"
    request["requires_observation"] = True
    request["requires_post_action_verification"] = False
    request.setdefault("replan_triggers", ["verification_failed"])

    verification_targets = request.get("verification_targets")
    if not isinstance(verification_targets, list) or not any(
        isinstance(target, Mapping)
        and str(target.get("step_id") or "").strip() == source_step_id
        for target in verification_targets
    ):
        target: dict[str, Any] = {"step_id": source_step_id}
        source_todo = source_request.get("task_todo")
        if isinstance(source_todo, Mapping) and source_todo:
            target["todo"] = dict(source_todo)
        source_checkpoints = source_request.get("task_checkpoints")
        if isinstance(source_checkpoints, list):
            checkpoints = [
                dict(checkpoint)
                for checkpoint in source_checkpoints
                if isinstance(checkpoint, Mapping)
            ]
            if checkpoints:
                target["checkpoints"] = checkpoints
        source_workspace_items = source_request.get("task_workspace_items")
        if isinstance(source_workspace_items, list):
            workspace_items = [
                dict(item)
                for item in source_workspace_items
                if isinstance(item, Mapping)
            ]
            if workspace_items:
                target["workspace_items"] = workspace_items
        verification_targets = [target]
        request["verification_targets"] = verification_targets
    request["task_verification_targets"] = [
        dict(target)
        for target in verification_targets
        if isinstance(target, Mapping)
    ]
    return request


def _system_volume_verification_scope_matches(
    status_request: Mapping[str, Any],
    source_request: Mapping[str, Any],
) -> bool:
    for key in ("run_id", "decision_id", "plan_id"):
        status_value = str(status_request.get(key) or "").strip()
        source_value = str(source_request.get(key) or "").strip()
        if status_value and source_value and status_value != source_value:
            return False
    return True


def _system_settings_open_fallback_requests(
    inputs: Mapping[str, Any],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if str(inputs.get("kind") or "").strip() != "settings_open":
        return []
    payload = inputs.get("payload") if isinstance(inputs.get("payload"), Mapping) else {}
    target = str(payload.get("target") or "").strip()
    tool_name = _first_allowed(("app.open", "desktop.open_app", "app.show"), allowed)
    if not tool_name:
        return []
    requests = [
        _request(
            tool_name,
            _desktop_request_payload(tool_name, {"app_name": "System Settings"}),
            planning_reason="planner_fallback_system_control",
        )
    ]
    read_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    if read_tool and (bool(inputs.get("inspect_ui")) or target):
        read_payload = {"role_filter": "", "limit": 80}
        requests.append(
            _request(
                read_tool,
                _desktop_request_payload(read_tool, read_payload),
                planning_reason="planner_fallback_system_control",
            )
        )
        requests[-1]["continue_to_model"] = True
    elif target:
        requests[-1]["continue_to_model"] = True
    return requests


def _file_access_tool_requests(inputs: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    tool_name, payload = file_access_tool_preview(inputs, allowed)
    if not tool_name:
        return []
    return [_request(tool_name, payload, planning_reason="planner_fallback_file_access")]


def _direct_communication_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    direct_hint = inputs.get("direct_message_hint")
    if not isinstance(direct_hint, Mapping):
        return []
    body_source = str(direct_hint.get("body_source") or "").strip()
    send_action = str(direct_hint.get("send_action") or "send").strip() or "send"
    if body_source == "current_page_link":
        return []
    if _direct_communication_requires_model_body(direct_hint):
        return []
    steps_by_id = {
        str(getattr(step, "step_id", "") or "").strip(): step
        for step in decision.plan.tool_plan.steps
    }
    selected_app_requests = _selected_communication_tool_requests(
        direct_hint,
        steps_by_id,
        allowed,
    )
    if selected_app_requests:
        return selected_app_requests
    observed_prepare_requests = _observed_direct_communication_prepare_requests(
        direct_hint,
        allowed,
    )
    if body_source in {"selection", "current_page_link"}:
        required_step_ids = ("copy-communication-body-source",)
    else:
        required_step_ids = ()
    if "open-or-focus-app" in steps_by_id:
        required_step_ids += ("open-or-focus-app",)
    required_step_ids += (
        "focus-communication-recipient-search",
        "type-communication-recipient",
        "submit-communication-recipient-search",
    )
    if body_source in {"clipboard", "selection", "current_page_link"}:
        required_step_ids += ("paste-communication-message",)
    else:
        required_step_ids += ("draft-communication-message",)
    if send_action != "draft":
        required_step_ids += (
            "send-communication-message",
        )
    requests: list[dict[str, Any]] = []
    for step_id in required_step_ids:
        step = steps_by_id.get(step_id)
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not _step_available(step):
            return observed_prepare_requests
        if not tool_name or tool_name not in allowed:
            return observed_prepare_requests
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        direct_mode = str(direct_hint.get("mode") or "").strip()
        if (
            step_id == "open-or-focus-app"
            and tool_name == "app.open"
            and "app.focus" in allowed
            and direct_mode == "open"
        ):
            requests.append(
                _request(
                    "app.open",
                    _desktop_request_payload("app.open", payload),
                    planning_reason="planner_fallback_communication_send",
                )
            )
            requests.append(
                _request(
                    "app.focus",
                    _desktop_request_payload("app.focus", payload),
                    planning_reason="planner_fallback_communication_send",
                )
            )
            continue
        if step_id == "open-or-focus-app" and tool_name == "app.open" and "app.focus" in allowed:
            tool_name = "app.focus"
        requests.append(
            _request(
                tool_name,
                _desktop_request_payload(tool_name, payload),
                planning_reason="planner_fallback_communication_send",
            )
        )
    return requests


def _observed_direct_communication_prepare_requests(
    direct_hint: Mapping[str, Any],
    allowed: set[str],
) -> list[dict[str, Any]]:
    app_name = str(direct_hint.get("app_name") or "").strip()
    recipient = str(direct_hint.get("recipient") or "").strip()
    body = str(direct_hint.get("body") or "").strip()
    body_source = str(direct_hint.get("body_source") or "").strip()
    if not app_name or not recipient or not body or body_source:
        return []
    if not _first_allowed(
        ("desktop.safe_type_text", "desktop.type_text", "desktop.type"),
        allowed,
    ):
        return []
    if not _first_allowed(("desktop.safe_click", "desktop.click"), allowed):
        return []
    if not _first_allowed(("desktop.submit_foreground",), allowed):
        return []
    observe_tool = _first_allowed(("desktop.ui_elements", "desktop.read_ui"), allowed)
    if not observe_tool:
        return []
    mode = str(direct_hint.get("mode") or "focus").strip() or "focus"
    open_tool = ""
    if mode == "open":
        open_tool = _first_allowed(
            ("app.open", "desktop.open_app", "app.focus", "desktop.focus_app"),
            allowed,
        )
    else:
        open_tool = _first_allowed(
            ("app.focus", "desktop.focus_app", "app.open", "desktop.open_app"),
            allowed,
        )
    if not open_tool:
        return []
    observe_input = {"app_name": app_name, "role_filter": "text", "limit": 80}
    observe_request = _request(
        observe_tool,
        _desktop_request_payload(observe_tool, observe_input),
        planning_reason="planner_fallback_communication_send",
    )
    observe_request["continue_to_model"] = True
    return [
        _request(
            open_tool,
            _desktop_request_payload(open_tool, {"app_name": app_name}),
            planning_reason="planner_fallback_communication_send",
        ),
        observe_request,
    ]


def _selected_communication_tool_requests(
    direct_hint: Mapping[str, Any],
    steps_by_id: Mapping[str, Any],
    allowed: set[str],
) -> list[dict[str, Any]]:
    if str(direct_hint.get("body_source") or "").strip() in {
        "clipboard",
        "current_page_link",
        "selection",
    }:
        return []
    if (
        "discover_apps-desktop-state" not in steps_by_id
        or "open-selected-discovered-app" not in steps_by_id
    ):
        return []
    send_action = str(direct_hint.get("send_action") or "send").strip() or "send"
    required_step_ids = [
        "discover_apps-desktop-state",
        "open-selected-discovered-app",
        "inspect-selected-communication-compose-ui",
        "fill-selected-communication-recipient",
        "submit-selected-communication-recipient",
        "draft-selected-communication-message",
    ]
    if send_action != "draft":
        required_step_ids.append("send-selected-communication-message")
    selected_query = _selected_communication_app_query(steps_by_id)
    requests: list[dict[str, Any]] = []
    for step_id in required_step_ids:
        step = steps_by_id.get(step_id)
        if not _step_available(step):
            return []
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed:
            return []
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        payload = _selected_communication_payload(payload, selected_query)
        requests.append(
            _request(
                tool_name,
                _desktop_request_payload(tool_name, payload),
                planning_reason="planner_fallback_communication_send",
            )
        )
    return requests


def _selected_communication_app_query(steps_by_id: Mapping[str, Any]) -> str:
    for step_id in ("open-selected-discovered-app", "discover_apps-desktop-state"):
        step = steps_by_id.get(step_id)
        input_preview = getattr(step, "input_preview", None)
        payload = input_preview if isinstance(input_preview, Mapping) else {}
        query = str(payload.get("query") or "").strip()
        if query:
            return query
    return ""


def _selected_communication_payload(
    payload: dict[str, Any],
    selected_query: str,
) -> dict[str, Any]:
    if str(payload.get("app_name") or "").strip() != "<selected app from desktop.list_apps>":
        return payload
    next_payload = dict(payload)
    if selected_query:
        next_payload.setdefault("selection_source", "desktop.list_apps")
        next_payload.setdefault("query", selected_query)
    return next_payload


def _direct_communication_requires_model_body(direct_hint: Mapping[str, Any]) -> bool:
    body_source = str(direct_hint.get("body_source") or "").strip()
    transform = str(direct_hint.get("content_transform_hint") or "").strip()
    if transform and body_source:
        return True
    return body_source in {
        "app_search_result",
        "screen_capture",
        "current_page_content",
        "visible_text",
        "file",
    }


def _direct_communication_context_tool_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    direct_hint = inputs.get("direct_message_hint")
    if not isinstance(direct_hint, Mapping):
        return []
    source = str(direct_hint.get("body_source") or inputs.get("context_source") or "").strip()
    planning_reason = "planner_prefetch_communication_context"

    if source == "app_search_result":
        return _direct_app_search_result_context_tool_requests(
            decision,
            allowed,
            planning_reason=planning_reason,
        )

    if source == "selection":
        if "desktop.safe_shortcut" not in allowed or "clipboard.read" not in allowed:
            return []
        requests = [
            _request(
                "desktop.safe_shortcut",
                {"action": "copy"},
                planning_reason=planning_reason,
            ),
            _request("clipboard.read", {}, planning_reason=planning_reason),
        ]
        requests[-1]["continue_to_model"] = True
        return requests

    if source == "clipboard":
        if "clipboard.read" not in allowed:
            return []
        request = _request("clipboard.read", {}, planning_reason=planning_reason)
        request["continue_to_model"] = True
        return [request]

    if source == "current_page_link":
        if "browser.current_page" not in allowed:
            return []
        request = _request("browser.current_page", {}, planning_reason=planning_reason)
        request["continue_to_model"] = True
        return [request]

    if source == "screen_capture":
        if "screen.capture" not in allowed:
            return []
        request = _request(
            "screen.capture",
            {"reason": "Capture the screen before sending it."},
            planning_reason=planning_reason,
        )
        request["continue_to_model"] = True
        return [request]

    if source == "current_page_content":
        tool_name = _first_allowed(
            ("browser.extract_text", "browser.current_page", "desktop.ui_elements", "screen.capture"),
            allowed,
        )
        if not tool_name:
            return []
        request_payload = _context_prefetch_payload(tool_name, {})
        if request_payload is None:
            return []
        request = _request(tool_name, request_payload, planning_reason=planning_reason)
        request["continue_to_model"] = True
        return [request]

    return []


def _direct_app_search_result_context_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    planning_reason: str,
) -> list[dict[str, Any]]:
    source_step_ids = {
        "discover-app-search-source",
        "open-app-search-source",
        "focus-app-search-source",
        "focus-app-search-field",
        "type-app-search-query",
        "submit-app-search",
        "read-communication-context",
    }
    requests: list[dict[str, Any]] = []
    tool_plan = getattr(getattr(decision, "plan", None), "tool_plan", None)
    for step in getattr(tool_plan, "steps", []):
        step_id = str(getattr(step, "step_id", "") or "").strip()
        if step_id not in source_step_ids:
            continue
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed:
            return []
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        request = _request(
            tool_name,
            _desktop_request_payload(tool_name, payload),
            planning_reason=planning_reason,
        )
        if step_id == "read-communication-context":
            request["continue_to_model"] = True
        requests.append(request)
        if step_id == "read-communication-context":
            break
    return requests


def _web_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_unavailable_context: bool = False,
) -> list[dict[str, Any]]:
    browser_action = str(decision.selected_intent.inputs.get("browser_action") or "").strip()
    prepare_requests = _web_browser_prepare_requests(decision, allowed)
    if browser_action == "find_current_page":
        return _current_page_find_tool_requests(decision, allowed)
    if browser_action == "click":
        if "browser.click" not in allowed:
            fallback_requests = _browser_click_desktop_fallback_requests(decision, allowed)
            if fallback_requests:
                return fallback_requests
            return []
        url = str(decision.selected_intent.inputs.get("url_hint") or "").strip()
        if url and "browser.open_url" not in allowed:
            return []
        selector = str(decision.selected_intent.inputs.get("selector") or "").strip()
        if not selector:
            return []
        payload: dict[str, Any] = {"selector": selector}
        click_count = decision.selected_intent.inputs.get("click_count")
        if click_count not in (None, ""):
            payload["click_count"] = click_count
        for key in ("fallback_x", "fallback_y"):
            value = decision.selected_intent.inputs.get(key)
            if value not in (None, ""):
                payload[key] = value
        requests = [*prepare_requests]
        if url:
            requests.append(
                _request(
                    "browser.open_url",
                    {"url": url},
                    planning_reason="planner_fallback_web_research",
                )
            )
        requests.append(
            _request(
                "browser.click",
                payload,
                planning_reason="planner_fallback_web_research",
            )
        )
        return requests
    if browser_action == "type_text":
        if "browser.type_text" not in allowed:
            fallback_requests = _browser_type_desktop_fallback_requests(decision, allowed)
            if fallback_requests:
                return [*prepare_requests, *fallback_requests]
            return []
        selector = str(decision.selected_intent.inputs.get("selector") or "").strip()
        text = str(decision.selected_intent.inputs.get("text") or "")
        if not selector or not text:
            return []
        payload: dict[str, Any] = {"selector": selector, "text": text}
        for key in ("fallback_x", "fallback_y"):
            value = decision.selected_intent.inputs.get(key)
            if value not in (None, ""):
                payload[key] = value
        return [
            *prepare_requests,
            _request(
                "browser.type_text",
                payload,
                planning_reason="planner_fallback_web_research",
            )
        ]
    if (
        browser_action == "open_search"
        and str(decision.selected_intent.inputs.get("followup_action") or "").strip()
        == "click_search_result"
    ):
        if "browser.open_url" not in allowed or "browser.click" not in allowed:
            return []
        url = str(decision.selected_intent.inputs.get("url_hint") or "").strip()
        selector = str(decision.selected_intent.inputs.get("selector") or "").strip()
        if not url or not selector:
            return []
        click_payload: dict[str, Any] = {"selector": selector}
        click_count = decision.selected_intent.inputs.get("click_count")
        if click_count not in (None, ""):
            click_payload["click_count"] = click_count
        requests = [
            *prepare_requests,
            _request(
                "browser.open_url",
                {"url": url},
                planning_reason="planner_fallback_web_research",
            ),
            _request(
                "browser.click",
                click_payload,
                planning_reason="planner_fallback_web_research",
            ),
        ]
        if (
            str(decision.selected_intent.inputs.get("post_followup_action") or "").strip()
            == "extract_text"
        ):
            post_step = next(
                (
                    item
                    for item in decision.plan.tool_plan.steps
                    if getattr(item, "step_id", "") == "extract-clicked-web-result-text"
                ),
                None,
            )
            post_tool_name = str(getattr(post_step, "tool_name", "") or "").strip()
            if (
                post_tool_name in {"browser.extract_text", "browser.current_page"}
                and post_tool_name in allowed
                and _step_available(post_step)
            ):
                post_request = _request(
                    post_tool_name,
                    {},
                    planning_reason="planner_fallback_web_research",
                )
                presentation = str(
                    decision.selected_intent.inputs.get("presentation") or ""
                ).strip()
                if presentation:
                    post_request["presentation"] = presentation
                if _web_request_needs_model_followup(
                    _decision_planning_goal(decision)
                ) or any(
                    str(getattr(item, "tool_name", "") or "").strip()
                    in {"artifact.write", "clipboard.write"}
                    for item in decision.plan.tool_plan.steps
                ):
                    post_request["continue_to_model"] = True
                requests.append(post_request)
        return requests
    if _dynamic_context_browser_action(decision):
        return _dynamic_context_browser_tool_requests(
            decision,
            allowed,
            allow_unavailable=allow_unavailable_context,
        )
    if str(decision.selected_intent.inputs.get("context_source") or "").strip() and not str(
        decision.selected_intent.inputs.get("url_hint") or ""
    ).strip():
        return _context_source_tool_requests(
            decision,
            allowed,
            step_ids=("copy-selected-web-context", "read-web-context"),
            planning_reason="planner_prefetch_web_context",
            allow_unavailable=allow_unavailable_context,
        )
    url = str(decision.selected_intent.inputs.get("url_hint") or "").strip()
    if browser_action in {"current_page", "extract_text", "screenshot"} and url:
        read_step = next(
            (
                item
                for item in decision.plan.tool_plan.steps
                if getattr(item, "step_id", "")
                in {"read-current-page", "extract-current-page-text", "capture-current-page"}
            ),
            None,
        )
        read_tool_name = str(getattr(read_step, "tool_name", "") or "").strip()
        if (
            "browser.open_url" not in allowed
            or read_tool_name not in allowed
            or not _step_available(read_step)
        ):
            return []
        requests = [
            _request(
                "browser.open_url",
                {"url": url},
                planning_reason="planner_fallback_web_research",
            )
        ]
        payload: dict[str, Any] = {}
        if read_tool_name == "browser.screenshot":
            reason = str(decision.selected_intent.inputs.get("reason") or "").strip()
            payload = {"reason": reason} if reason else {}
        request = _request(
            read_tool_name,
            payload,
            planning_reason="planner_fallback_web_research",
        )
        presentation = str(decision.selected_intent.inputs.get("presentation") or "").strip()
        if presentation:
            request["presentation"] = presentation
        if _web_read_request_needs_model_followup(decision, read_tool_name, presentation):
            request["continue_to_model"] = True
        requests.append(request)
        return [*prepare_requests, *requests]
    step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "step_id", "")
            in {
                "open-or-read-web",
                "read-current-page",
                "extract-current-page-text",
                "capture-current-page",
                "open-web-search",
                "open-web-url",
                "extract-web-url-text",
                "capture-web-url",
            }
        ),
        None,
    )
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not _step_available(step):
        return []
    if tool_name not in allowed:
        return []
    payload: dict[str, Any] = {}
    if tool_name == "browser.search":
        query = str(decision.selected_intent.inputs.get("query") or "").strip()
        if not query:
            return []
        payload = {"query": query}
    elif tool_name in {
        "browser.open_url",
        "browser.open",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
    }:
        if not url:
            return []
        payload = {"url": url}
        if tool_name == "browser.open_url_and_screenshot":
            reason = str(decision.selected_intent.inputs.get("reason") or "").strip()
            if reason:
                payload["reason"] = reason
    elif tool_name not in {
        "browser.current_page",
        "browser.extract_text",
        "browser.extract",
        "browser.screenshot",
    }:
        return []
    elif not browser_action and not _looks_like_current_page_request(
        _decision_planning_goal(decision)
    ):
        return []
    elif tool_name == "browser.screenshot":
        reason = str(decision.selected_intent.inputs.get("reason") or "").strip()
        payload = {"reason": reason} if reason else {}

    request = _request(
        tool_name,
        payload,
        planning_reason="planner_fallback_web_research",
    )
    presentation = str(decision.selected_intent.inputs.get("presentation") or "").strip()
    if presentation and _browser_tool_result_can_feed_model(tool_name):
        request["presentation"] = presentation
    if _web_read_request_needs_model_followup(decision, tool_name, presentation) and (
        not browser_action
        or _browser_tool_result_can_feed_model(tool_name)
        or any(
            str(getattr(item, "tool_name", "") or "").strip()
            in {"artifact.write", "clipboard.write"}
            for item in decision.plan.tool_plan.steps
        )
    ):
        request["continue_to_model"] = True
    requests = [*prepare_requests, request]
    if browser_action in {"open_search", "open_url", "open_url_screenshot"}:
        requests.extend(_web_open_followup_tool_requests(decision, allowed))
    if browser_action in {"open_search", "open_url"}:
        requests.extend(_web_open_readback_tool_requests(decision, allowed))
    return requests


def _web_open_followup_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for step_id in (
        "click-opened-web-page",
        "key-opened-web-page",
        "scroll-opened-web-page",
        "capture-opened-web-page",
        "verify-opened-web-page",
    ):
        step = _tool_plan_step(decision, step_id)
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not tool_name or tool_name not in allowed or not _step_available(step):
            continue
        raw_input = getattr(step, "input_preview", {})
        payload = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        requests.append(
            _request(
                tool_name,
                payload,
                planning_reason="planner_fallback_web_research",
            )
        )
    return requests


def _web_open_readback_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    step = _tool_plan_step(decision, "extract-opened-web-content")
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not tool_name or tool_name not in allowed or not _step_available(step):
        return []
    if tool_name not in {"browser.extract_text", "browser.current_page", "browser.extract"}:
        return []
    request = _request(
        tool_name,
        {},
        planning_reason="planner_fallback_web_research",
    )
    presentation = str(decision.selected_intent.inputs.get("presentation") or "").strip()
    if presentation:
        request["presentation"] = presentation
    if _web_read_request_needs_model_followup(decision, tool_name, presentation):
        request["continue_to_model"] = True
    return [request]


def _report_research_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_unavailable: bool = False,
) -> list[dict[str, Any]]:
    step = _tool_plan_step(decision, "research-report-context")
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not allow_unavailable and not _step_available(step):
        return []
    if tool_name not in allowed:
        return []
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    request_payload: dict[str, Any]
    if tool_name == "browser.search":
        query = str(payload.get("query") or "").strip()
        if not query:
            return []
        request_payload = {"query": query}
    elif tool_name in {"browser.open_url", "browser.open_url_and_extract_text"}:
        url = str(payload.get("url") or "").strip()
        if not url:
            return []
        request_payload = {"url": url}
    else:
        return []
    request = _request(
        tool_name,
        request_payload,
        planning_reason="planner_prefetch_report_context",
    )
    request["continue_to_model"] = True
    return [request]


def _tool_plan_step(decision: Any, step_id: str) -> Any | None:
    clean_step_id = str(step_id or "").strip()
    if not clean_step_id:
        return None
    return next(
        (
            item
            for item in getattr(getattr(decision.plan, "tool_plan", None), "steps", [])
            if str(getattr(item, "step_id", "") or "").strip() == clean_step_id
        ),
        None,
    )


def _decision_planning_goal(decision: Any) -> str:
    """Return Runtime-validated planning text without changing root authority."""

    intent = getattr(decision, "selected_intent", None)
    inputs = getattr(intent, "inputs", None)
    if isinstance(inputs, Mapping):
        normalized = str(inputs.get("runtime_model_planning_goal") or "").strip()
        if normalized:
            return normalized
    return str(getattr(intent, "user_goal", "") or "").strip()


def _web_read_request_needs_model_followup(
    decision: Any,
    tool_name: str,
    presentation: str,
) -> bool:
    if _web_read_request_can_direct_present(decision, tool_name, presentation):
        return False
    inputs = decision.selected_intent.inputs
    return bool(
        _web_request_needs_model_followup(_decision_planning_goal(decision))
        or str(inputs.get("output_target_hint") or "").strip() == "clipboard"
        or any(
            str(getattr(item, "tool_name", "") or "").strip()
            in {"artifact.write", "clipboard.write"}
            for item in decision.plan.tool_plan.steps
        )
        or str(presentation or "").strip()
    )


def _web_read_request_can_direct_present(
    decision: Any,
    tool_name: str,
    presentation: str,
) -> bool:
    if str(presentation or "").strip() != "summary":
        return False
    if tool_name not in {
        "browser.current_page",
        "browser.extract_text",
        "browser.extract",
        "browser.open_url_and_extract_text",
    }:
        return False
    inputs = decision.selected_intent.inputs
    if str(inputs.get("output_target_hint") or "").strip():
        return False
    if (
        str(inputs.get("query") or "").strip()
        and not _looks_like_current_page_request(_decision_planning_goal(decision))
    ):
        # A search-results summary is synthesis, not deterministic formatting
        # of one already-open page. Keep it in the model loop.
        return False
    return not any(
        str(getattr(item, "tool_name", "") or "").strip()
        in {"artifact.write", "clipboard.write"}
        for item in decision.plan.tool_plan.steps
    )


def _browser_tool_result_can_feed_model(tool_name: str) -> bool:
    return tool_name in {
        "browser.current_page",
        "browser.extract_text",
        "browser.extract",
        "browser.screenshot",
        "browser.search",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
    }


def _web_browser_prepare_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    native_browser_tool = next(
        (
            str(getattr(item, "tool_name", "") or "").strip()
            for item in getattr(getattr(decision.plan, "tool_plan", None), "steps", [])
            if str(getattr(item, "tool_name", "") or "").strip().startswith("browser.")
        ),
        "",
    )
    if native_browser_tool:
        # Browser tools operate on a run-owned target. Current-page tools
        # without one fail closed in the broker; foreground app prep would
        # only steal the user's Chrome/Safari session.
        return []

    step = _planned_step_by_id(decision, "open-or-focus-browser")
    step_tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if step_tool_name and step_tool_name in allowed and _step_available(step):
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        return [
            _request(
                step_tool_name,
                _desktop_request_payload(step_tool_name, payload),
                planning_reason="planner_fallback_web_research",
            )
        ]
    app_name = str(inputs.get("app_name") or "").strip()
    if not app_name:
        return []
    mode = str(inputs.get("app_mode") or "focus").strip() or "focus"
    tool_name = _first_allowed(app_control_tool_candidates(mode), allowed)
    if not tool_name:
        return []
    return [
        _request(
            tool_name,
            _desktop_request_payload(tool_name, {"app_name": app_name}),
            planning_reason="planner_fallback_web_research",
        )
    ]


def _browser_click_desktop_fallback_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    app_name = str(inputs.get("app_name") or "").strip()
    selector = str(inputs.get("selector") or "").strip()
    point_payload = _browser_point_desktop_payload(inputs)
    if point_payload and "desktop.click" in allowed:
        return [
            _request(
                "desktop.click",
                point_payload,
                planning_reason="planner_desktop_operation",
            )
        ]
    if not selector.startswith("text="):
        return []
    target = selector.removeprefix("text=").strip()
    if not target:
        return []
    payload = {
        "app_name": app_name,
        "target": target,
        "role_filter": "button",
        "click_count": int(inputs.get("click_count") or 1),
        "limit": 80,
    }
    app_click_tool = _first_allowed(
        ("app.focus_and_click_ui_element", "app.open_and_click_ui_element"),
        allowed,
    )
    requests: list[dict[str, Any]] = []
    if app_click_tool:
        requests.append(
            _request(
                app_click_tool,
                _desktop_request_payload(app_click_tool, payload),
                planning_reason="planner_desktop_operation",
            )
        )
    elif "app.focus" in allowed and "desktop.click_ui_element" in allowed:
        requests.extend(
            [
                _request(
                    "app.focus",
                    _desktop_request_payload("app.focus", {"app_name": app_name}),
                    planning_reason="planner_desktop_operation",
                ),
                _request(
                    "desktop.click_ui_element",
                    _desktop_request_payload(
                        "desktop.click_ui_element",
                        {
                            "target": target,
                            "role_filter": "button",
                            "click_count": int(inputs.get("click_count") or 1),
                            "limit": 80,
                        },
                    ),
                    planning_reason="planner_desktop_operation",
                ),
            ]
        )
    elif "desktop.click_ui_element" in allowed:
        requests.append(
            _request(
                "desktop.click_ui_element",
                _desktop_request_payload(
                    "desktop.click_ui_element",
                    {
                        "target": target,
                        "role_filter": "button",
                        "click_count": _safe_click_count(inputs.get("click_count")),
                        "limit": 80,
                    },
                ),
                planning_reason="planner_desktop_operation",
            )
        )
    elif "desktop.click" in allowed:
        observation_request = _browser_desktop_observation_request(
            allowed,
            role_filter=_desktop_role_filter_from_browser_selector(
                selector,
                default="button",
            ),
            reason="Inspect the foreground page before resolving the requested click target.",
        )
        if observation_request:
            return [observation_request]
    if requests and "desktop.ui_elements" in allowed:
        requests.append(
            _request(
                "desktop.ui_elements",
                {"role_filter": "button", "limit": 80},
                planning_reason="planner_desktop_operation",
            )
        )
    return requests


def _browser_type_desktop_fallback_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    selector = str(inputs.get("selector") or "").strip()
    text = str(inputs.get("text") or "")
    if not text:
        return []
    if "desktop.type_into_ui_element" not in allowed:
        if "desktop.type" not in allowed and "desktop.type_text" not in allowed:
            return []
        observation_request = _browser_desktop_observation_request(
            allowed,
            role_filter=_desktop_role_filter_from_browser_selector(
                selector,
                default="text field",
            ),
            reason="Inspect the foreground page before resolving where to type.",
        )
        return [observation_request] if observation_request else []
    target = _desktop_target_from_browser_selector(selector) or "text input"
    payload = {
        "target": target,
        "text": text,
        "role_filter": _desktop_role_filter_from_browser_selector(
            selector,
            default="text field",
        ),
        "limit": 80,
    }
    requests = [
        _request(
            "desktop.type_into_ui_element",
            _desktop_request_payload("desktop.type_into_ui_element", payload),
            planning_reason="planner_desktop_operation",
        )
    ]
    if "desktop.ui_elements" in allowed:
        requests.append(
            _request(
                "desktop.ui_elements",
                {"role_filter": "text field", "limit": 80},
                planning_reason="planner_desktop_operation",
            )
        )
    return requests


def _browser_desktop_observation_request(
    allowed: set[str],
    *,
    role_filter: str,
    reason: str,
) -> dict[str, Any] | None:
    tool_name = _first_allowed(
        ("desktop.ui_elements", "desktop.read_ui", "screen.capture"),
        allowed,
    )
    if not tool_name:
        return None
    if tool_name == "screen.capture":
        payload = {"reason": reason}
    else:
        payload = {
            "role_filter": role_filter,
            "limit": 80,
        }
    request = _request(
        tool_name,
        _desktop_request_payload(tool_name, payload),
        planning_reason="planner_desktop_operation",
    )
    request["continue_to_model"] = True
    return request


def _browser_point_desktop_payload(inputs: Mapping[str, Any]) -> dict[str, Any]:
    x = inputs.get("fallback_x")
    y = inputs.get("fallback_y")
    if x in (None, "") or y in (None, ""):
        return {}
    return {
        "x": x,
        "y": y,
        "click_count": _safe_click_count(inputs.get("click_count")),
    }


def _desktop_target_from_browser_selector(selector: str) -> str:
    value = str(selector or "").strip()
    if value.startswith("text="):
        return value.removeprefix("text=").strip()
    lowered = value.lower()
    if value.startswith("input:not") or (
        "textarea" in lowered and "contenteditable" in lowered
    ):
        return "text input"
    if "search" in lowered or "搜索" in lowered:
        return "search"
    if "password" in lowered or "密码" in lowered:
        return "password"
    if "email" in lowered or "邮箱" in lowered or "邮件" in lowered:
        return "email"
    if (
        "user" in lowered
        or "username" in lowered
        or "login" in lowered
        or "用户名" in lowered
        or "账号" in lowered
    ):
        return "username"
    if (
        value.startswith("input")
        or value.startswith("textarea")
        or "contenteditable" in lowered
    ):
        return "text input"
    return value


def _desktop_role_filter_from_browser_selector(selector: str, *, default: str) -> str:
    value = str(selector or "").strip().lower()
    if value.startswith("text="):
        return default
    if (
        value.startswith("input")
        or value.startswith("textarea")
        or "contenteditable" in value
        or "search" in value
        or "password" in value
        or "email" in value
    ):
        return "text field"
    return default


def _safe_click_count(value: Any) -> int:
    try:
        count = int(value or 1)
    except (TypeError, ValueError):
        return 1
    return count if count > 0 else 1


def _current_page_find_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    inputs = decision.selected_intent.inputs
    source = str(inputs.get("context_source") or "").strip()
    query = str(inputs.get("query") or "").strip()
    if source == "selection":
        required_step_ids = (
            "copy-selected-page-find-query",
            "open-current-page-find",
            "paste-current-page-find-query",
        )
    elif source == "clipboard":
        required_step_ids = (
            "open-current-page-find",
            "paste-current-page-find-query",
        )
    elif query:
        required_step_ids = (
            "open-current-page-find",
            "type-current-page-find-query",
        )
    else:
        return []

    steps_by_id = {
        str(getattr(step, "step_id", "") or "").strip(): step
        for step in decision.plan.tool_plan.steps
    }
    requests: list[dict[str, Any]] = []
    for step_id in required_step_ids:
        step = steps_by_id.get(step_id)
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not _step_available(step):
            return []
        if not tool_name or tool_name not in allowed:
            return []
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        if step_id == "type-current-page-find-query" and not str(payload.get("text") or "").strip():
            return []
        requests.append(
            _request(
                tool_name,
                _desktop_request_payload(tool_name, payload),
                planning_reason="planner_fallback_current_page_find",
            )
        )
    return requests


def _dynamic_context_browser_action(decision: Any) -> bool:
    inputs = decision.selected_intent.inputs
    browser_action = str(inputs.get("browser_action") or "").strip()
    source = str(inputs.get("context_source") or "").strip()
    url_hint = str(inputs.get("url_hint") or "").strip()
    return browser_action in {"open_search", "open_url"} and source in {"selection", "clipboard"} and not url_hint


def _dynamic_context_browser_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_unavailable: bool = False,
) -> list[dict[str, Any]]:
    source = str(decision.selected_intent.inputs.get("context_source") or "").strip()
    if source == "selection":
        required_step_ids = (
            "copy-selected-browser-context",
            "focus-browser-address-bar",
            "paste-browser-context",
            "submit-browser-context",
        )
    elif source == "clipboard":
        required_step_ids = (
            "focus-browser-address-bar",
            "paste-browser-context",
            "submit-browser-context",
        )
    else:
        return []

    steps_by_id = {
        str(getattr(step, "step_id", "") or "").strip(): step
        for step in decision.plan.tool_plan.steps
    }
    requests: list[dict[str, Any]] = []
    for step_id in required_step_ids:
        step = steps_by_id.get(step_id)
        tool_name = str(getattr(step, "tool_name", "") or "").strip()
        if not allow_unavailable and not _step_available(step):
            return []
        if not tool_name or tool_name not in allowed:
            return []
        input_preview = getattr(step, "input_preview", None)
        payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
        requests.append(
            _request(
                tool_name,
                _desktop_request_payload(tool_name, payload),
                planning_reason="planner_fallback_dynamic_browser_context",
            )
        )
    return requests


def _context_prefetch_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    step_ids: tuple[str, ...],
    planning_reason: str,
    allow_unavailable: bool = False,
) -> list[dict[str, Any]]:
    for step_id in step_ids:
        step = next(
            (
                item
                for item in decision.plan.tool_plan.steps
                if getattr(item, "step_id", "") == step_id
            ),
            None,
        )
        request = _context_prefetch_request_for_step(
            step,
            allowed,
            planning_reason=planning_reason,
            allow_unavailable=allow_unavailable,
        )
        if request:
            return [request]
    return []


def _context_prefetch_request_for_step(
    step: Any,
    allowed: set[str],
    *,
    planning_reason: str,
    allow_unavailable: bool = False,
) -> dict[str, Any] | None:
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not allow_unavailable and not _step_available(step):
        return None
    if tool_name not in allowed:
        return None
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    request_payload = _context_prefetch_payload(tool_name, payload)
    if request_payload is None:
        return None
    request = _request(
        tool_name,
        request_payload,
        planning_reason=planning_reason,
    )
    request["continue_to_model"] = True
    return request


def _context_prefetch_payload(
    tool_name: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    if tool_name in {"workspace.list", "fs.find_files", "file.search"}:
        path = str(payload.get("path") or "").strip()
        request_payload: dict[str, Any] = {"path": path} if path else {}
        pattern = str(payload.get("pattern") or "").strip()
        file_type = str(payload.get("file_type") or "").strip()
        selection = str(payload.get("selection") or "").strip()
        if pattern:
            request_payload["pattern"] = pattern
        if file_type:
            request_payload["file_type"] = file_type
        if selection:
            request_payload["selection"] = selection
        if bool(payload.get("include_metadata")):
            request_payload["include_metadata"] = True
        return request_payload
    if tool_name in {"workspace.read", "fs.read_file", "file.read"}:
        path = str(payload.get("path") or "").strip()
        return {"path": path} if path else None
    if tool_name in {"browser.current_page", "browser.extract_text", "browser.screenshot"}:
        return {}
    if tool_name == "clipboard.read":
        return {}
    if tool_name == "desktop.safe_shortcut":
        action = str(payload.get("action") or "").strip()
        return {"action": action} if action else None
    if tool_name in {"desktop.ui_elements", "desktop.read_ui"}:
        request_payload = {
            key: payload[key]
            for key in ("role_filter", "limit")
            if key in payload and payload[key] not in (None, "")
        }
        return request_payload
    if tool_name in {"desktop.active_window", "desktop.running_apps"}:
        return {}
    if tool_name == "desktop.list_apps":
        query = str(payload.get("query") or "").strip()
        request_payload: dict[str, Any] = {"query": query, "limit": 20} if query else {}
        limit = payload.get("limit")
        if isinstance(limit, int) and limit > 0:
            request_payload["limit"] = limit
        return request_payload
    if tool_name == "screen.capture":
        reason = str(payload.get("reason") or "").strip()
        return {"reason": reason} if reason else {}
    if tool_name in {"desktop.reveal_path", "desktop.open_path"}:
        path = str(payload.get("path") or "").strip()
        return {"path": path} if path else None
    return None


def _schedule_tool_requests(decision: Any, allowed: set[str]) -> list[dict[str, Any]]:
    tool_name, payload = schedule_tool_preview(
        _decision_planning_goal(decision),
        allowed,
    )
    if not tool_name or not payload:
        return _context_source_tool_requests(
            decision,
            allowed,
            step_ids=("copy-selected-schedule-context", "read-schedule-context"),
            planning_reason="planner_prefetch_schedule_context",
        )
    return [
        _request(
            tool_name,
            payload,
            planning_reason="planner_fallback_schedule",
        )
    ]


def _direct_schedule_context_app_item_tool_requests(
    decision: Any,
    allowed: set[str],
) -> list[dict[str, Any]]:
    source = str(decision.selected_intent.inputs.get("context_source") or "").strip()
    if source not in {"selection", "clipboard", "current_page_link", "current_page_content"}:
        return []
    app_name, shortcut_action = _schedule_context_app_item_target(
        _decision_planning_goal(decision)
    )
    if not app_name or not shortcut_action:
        return []
    app_tool = _first_allowed(("app.open",), allowed)
    shortcut_tool = _first_allowed(("desktop.safe_shortcut",), allowed)
    app_shortcut_tool = _first_allowed(("app.open_and_safe_shortcut",), allowed)
    if not shortcut_tool or not (app_tool or app_shortcut_tool):
        return []
    planning_reason = "planner_fallback_schedule_context_app_item"
    source_requests = _direct_context_clipboard_copy_requests(
        source,
        allowed,
        planning_reason=planning_reason,
    )
    if source_requests is None:
        return []
    if app_tool:
        return [
            *source_requests,
            _request(
                app_tool,
                {"app_name": app_name},
                planning_reason=planning_reason,
            ),
            _request(
                shortcut_tool,
                {"action": shortcut_action},
                planning_reason=planning_reason,
            ),
            _request(
                shortcut_tool,
                {"action": "paste"},
                planning_reason=planning_reason,
            ),
        ]
    return [
        *source_requests,
        _request(
            app_shortcut_tool,
            {"app_name": app_name, "action": shortcut_action},
            planning_reason=planning_reason,
        ),
        _request(
            shortcut_tool,
            {"action": "paste"},
            planning_reason=planning_reason,
        ),
    ]


def _schedule_context_app_item_target(text: str) -> tuple[str, str]:
    lowered = str(text or "").lower()
    if any(term in lowered for term in ("calendar", "日历", "日程", "事件", "event")):
        return "Calendar", "new_event"
    if any(term in lowered for term in ("reminder", "reminders", "提醒", "提醒事项")):
        return "Reminders", "new_reminder"
    return "", ""


def _direct_context_clipboard_copy_requests(
    source: str,
    allowed: set[str],
    *,
    planning_reason: str,
) -> list[dict[str, Any]] | None:
    if source == "clipboard":
        return []
    if "desktop.safe_shortcut" not in allowed:
        return None
    if source == "selection":
        return [
            _request(
                "desktop.safe_shortcut",
                {"action": "copy"},
                planning_reason=planning_reason,
            )
        ]
    if source == "current_page_link":
        return [
            _request(
                "desktop.safe_shortcut",
                {"action": "copy_current_page_link"},
                planning_reason=planning_reason,
            )
        ]
    if source == "current_page_content":
        return [
            _request(
                "desktop.safe_shortcut",
                {"action": "select_all"},
                planning_reason=planning_reason,
            ),
            _request(
                "desktop.safe_shortcut",
                {"action": "copy"},
                planning_reason=planning_reason,
            ),
        ]
    return None


def _information_capture_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    allow_unavailable_context: bool = False,
) -> list[dict[str, Any]]:
    artifact_request = _information_capture_artifact_request(decision, allowed)
    if artifact_request:
        return [artifact_request]
    tool_name, payload = capture_tool_preview(decision.selected_intent.inputs, allowed)
    if tool_name != "notes.create" or not payload.get("body"):
        return _context_source_tool_requests(
            decision,
            allowed,
            step_ids=("copy-selected-note-context", "read-note-context"),
            planning_reason="planner_prefetch_information_capture_context",
            allow_unavailable=allow_unavailable_context,
        )
    return [
        _request(
            tool_name,
            payload,
            planning_reason="planner_fallback_information_capture",
        )
    ]


def _information_capture_artifact_request(
    decision: Any,
    allowed: set[str],
) -> dict[str, Any] | None:
    step = next(
        (
            item
            for item in decision.plan.tool_plan.steps
            if getattr(item, "step_id", "") == "write-note-artifact"
        ),
        None,
    )
    tool_name = str(getattr(step, "tool_name", "") or "").strip()
    if not _step_available(step):
        return None
    if tool_name != "artifact.write" or tool_name not in allowed:
        return None
    input_preview = getattr(step, "input_preview", None)
    payload = dict(input_preview) if isinstance(input_preview, Mapping) else {}
    if not str(payload.get("path") or "").strip() or not str(payload.get("content") or "").strip():
        return None
    return _request(
        tool_name,
        {
            "path": str(payload.get("path") or ""),
            "content": str(payload.get("content") or ""),
        },
        planning_reason="planner_fallback_information_capture",
    )


def _context_source_tool_requests(
    decision: Any,
    allowed: set[str],
    *,
    step_ids: tuple[str, ...],
    planning_reason: str,
    allow_unavailable: bool = False,
) -> list[dict[str, Any]]:
    requests: list[dict[str, Any]] = []
    for step_id in step_ids:
        step = next(
            (
                item
                for item in decision.plan.tool_plan.steps
                if getattr(item, "step_id", "") == step_id
            ),
            None,
        )
        request = _context_prefetch_request_for_step(
            step,
            allowed,
            planning_reason=planning_reason,
            allow_unavailable=allow_unavailable,
        )
        if request:
            request.pop("continue_to_model", None)
            requests.append(request)
    if requests:
        requests[-1]["continue_to_model"] = True
    return requests


def _clipboard_tool_requests(inputs: dict[str, Any], allowed: set[str]) -> list[dict[str, Any]]:
    action = str(inputs.get("action") or "").strip()
    if action == "copy_selection_read":
        if "desktop.safe_shortcut" not in allowed or "clipboard.read" not in allowed:
            return []
        return [
            _request(
                "desktop.safe_shortcut",
                {"action": "copy"},
                planning_reason="planner_fallback_clipboard",
            ),
            _request(
                "clipboard.read",
                {},
                planning_reason="planner_fallback_clipboard",
            ),
        ]
    tool_name, payload = clipboard_tool_preview(inputs, allowed)
    if not tool_name:
        return []
    if tool_name == "clipboard.write" and not payload.get("text"):
        return []
    requests = [
        _request(
            tool_name,
            payload,
            planning_reason="planner_fallback_clipboard",
        )
    ]
    if tool_name == "clipboard.write" and "clipboard.read" in allowed:
        requests.append(
            _request(
                "clipboard.read",
                {},
                planning_reason="planner_fallback_clipboard_verify_write",
            )
        )
    return requests


def _looks_like_current_page_request(prompt: str) -> bool:
    return _contains_any(
        prompt,
        (
            "current page",
            "this page",
            "current tab",
            "当前页面",
            "当前网页",
            "当前标签",
            "页面正文",
            "网页正文",
        ),
    )


def _web_request_needs_model_followup(prompt: str) -> bool:
    return _contains_any(
        prompt,
        (
            "summary",
            "summarize",
            "report",
            "research",
            "analyze",
            "总结",
            "报告",
            "调研",
            "分析",
            "摘要",
            "输出",
            "导出",
            "生成",
            "markdown",
            "表格",
            "列出",
            "所有链接",
            "全部链接",
            "链接清单",
            "链接列表",
            "all links",
            "all urls",
            "markdown table",
            "来源清单",
            "来源列表",
            "link list",
            "links list",
            "list links",
            "source list",
            "sources",
            "最像",
            "最接近",
            "相关",
            "有关",
            "匹配",
            "合适",
            "适合",
            "应该",
            "可能",
            "哪个",
            "哪里",
            "哪一个",
            "which",
            "where",
            "closest",
            "similar",
            "related",
            "matching",
            "appropriate",
            "suitable",
        ),
    )


def _workspace_readable_data_source(source_hint: str, inputs: Mapping[str, Any]) -> bool:
    if not source_hint or source_hint.startswith(("/", "~")):
        return False
    if re.search(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", source_hint):
        return False
    if any(part == ".." for part in source_hint.replace("\\", "/").split("/")):
        return False
    source_kind = str(inputs.get("data_source_kind") or "").strip()
    if not source_kind or source_kind == "unknown":
        source_kind = data_source_kind_hint(source_hint)
    return source_kind in {"csv", "tsv", "json", "jsonl", "text", "text_table"}


def _workspace_listable_data_scope(scope_hint: str) -> bool:
    if not scope_hint or scope_hint.startswith(("/", "~")):
        return False
    if re.search(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", scope_hint):
        return False
    return not any(part == ".." for part in scope_hint.replace("\\", "/").split("/"))


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    lowered = str(text or "").lower()
    return any(str(term or "").lower() in lowered for term in terms)
