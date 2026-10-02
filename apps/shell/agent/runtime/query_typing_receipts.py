"""Read-only completion proof for a frozen, explicitly clicked search field."""

from collections.abc import Mapping, Sequence
from typing import Any

_SEARCH_NAMES = frozenset({"search", "search field", "search box", "搜索", "搜索框", "搜索栏"})


def _step(request: Mapping[str, Any]) -> str:
    return str(request.get("step_id") or request.get("planner_step_id") or "")


def _native_event_matches(event, spec, run_id, te):
    result = event.get("result") or {}
    if not isinstance(result, Mapping):
        return False
    return bool(
        event.get("event") == "agent.tool.call"
        and event.get("actor") == "native_runtime"
        and event.get("execution_authority") == "runtime_tool_executor"
        and event.get("run_id") == run_id
        and event.get("tool_call_id")
        and event.get("detail", event.get("tool")) == spec.get("tool")
        and all(
            event.get(k) == spec.get(k)
            for k in ("decision_id", "tool_plan_id", "plan_id", "request_id")
        )
        and _step(event) == _step(spec)
        and dict(event.get("input_preview") or {}) == dict(spec.get("input") or {})
        and result.get("ok") is True
        and result.get("action") == spec.get("tool")
        and not result.get("verification_failed")
        and not result.get("approval_required")
        and not result.get("permission_error")
        and te._trusted_runtime_execution_provider_identity(event, result)
        == (te.LOCAL_DESKTOP_PROVIDER_KIND, te.LOCAL_DESKTOP_PROVIDER_ID)
    )


def _focused_search(result, expected_app, te):
    if (
        result.get("ok") is not True
        or result.get("permission_error")
        or result.get("approval_required")
    ):
        return None
    data = result.get("data") or {}
    if not isinstance(data, Mapping) or data.get("truncated") is True:
        return None
    window = te._trusted_ui_window_identity(result, expected_app_name=expected_app)
    if not window:
        return None
    target = data.get("focused_element")
    if not isinstance(target, Mapping) or target.get("focused") is not True:
        return None
    identity = te._trusted_editable_ui_target_identity(target)
    if not identity or not any(
        str(target.get(k) or "").strip().casefold() in _SEARCH_NAMES
        for k in ("name", "description", "identifier")
    ):
        return None
    fields = [
        element
        for element in data.get("elements", [])
        if isinstance(element, Mapping)
        and te._trusted_ui_element_is_editable(element)
        and any(
            str(element.get(k) or "").strip().casefold() in _SEARCH_NAMES
            for k in ("name", "description", "identifier")
        )
    ]
    if len(fields) > 1 or any(
        element.get("focused") is True
        and te._trusted_editable_ui_target_identity(element) != identity
        for element in data.get("elements", [])
        if isinstance(element, Mapping)
    ):
        return None
    return window, identity, target.get("value")


def trusted_query_typing_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    """Prove query input only; this never prepares or authorizes a send."""
    if action_tool != "desktop.safe_type_text" or _step(action_event) != "type-app-search-query":
        return {}
    from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    from . import tool_execution as te
    from .goal_runtime import runtime_goal_contract
    from .model_intent_planning import planner_selection_needs_model_assistance

    try:
        contract = runtime_goal_contract(
            run_id=run_id,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=[],
            timeline=timeline,
        )
        if contract is None or contract.run_id != run_id:
            return {}
        selection = planner_first_direct_tool_selection(
            contract.original_goal, DAILY_DESKTOP_TOOL_NAMES
        )
        if planner_selection_needs_model_assistance(selection, contract.original_goal):
            return {}
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                selection.decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
            ),
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        )
    except ValueError:
        return {}
    specs = {_step(request): request for request in requests}
    source, click, post = (
        specs.get(k)
        for k in ("type-app-search-query", "focus-app-search-field", _step(verifier_request))
    )
    if (
        not source
        or not click
        or not post
        or click.get("tool")
        not in {"app.focus_and_click_ui_element", "app.open_and_click_ui_element"}
        or click.get("approval_required") is not True
        or any(
            r.get("tool") in {"desktop.submit_foreground", "desktop.search_submit"}
            for r in requests
        )
        or not _native_event_matches(action_event, source, run_id, te)
        or any(
            verifier_request.get(k) != post.get(k)
            for k in ("tool", "decision_id", "tool_plan_id", "plan_id", "request_id", "depends_on")
        )
        or dict(verifier_request.get("input") or {}) != dict(post.get("input") or {})
        or verifier_request.get("run_id") != run_id
        or verifier_request.get("source_tool_call_id") != action_event.get("tool_call_id")
        or verifier_request.get("source_step_id") != _step(source)
    ):
        return {}
    expected_app = str((click.get("input") or {}).get("app_name") or "")
    expected_field = str((click.get("input") or {}).get("target") or "").casefold()
    text = (source.get("input") or {}).get("text")
    if (
        not expected_app
        or expected_field not in _SEARCH_NAMES
        or not isinstance(text, str)
        or not text
    ):
        return {}
    source_indices = [
        i
        for i, e in enumerate(timeline)
        if _native_event_matches(e, source, run_id, te)
        and e.get("tool_call_id") == action_event.get("tool_call_id")
    ]
    if len(source_indices) != 1:
        return {}
    source_index = source_indices[0]
    clicks = [e for e in timeline[:source_index] if _native_event_matches(e, click, run_id, te)]
    click_calls = {str(e.get("tool_call_id")) for e in clicks}
    if len(click_calls) != 1:
        return {}
    click_call = next(iter(click_calls))
    pre_spec = {
        "tool": "desktop.ui_elements",
        "decision_id": click["decision_id"],
        "tool_plan_id": click["tool_plan_id"],
        "plan_id": click["plan_id"],
        "request_id": (
            f"{click['request_id']}:verify:{_step(click)}:runtime-verify:desktop.ui_elements"
        ),
        "step_id": f"{_step(click)}:runtime-verify",
        "input": {"app_name": expected_app},
    }
    pre_events = [
        (i, e)
        for i, e in enumerate(timeline[:source_index])
        if _native_event_matches(e, pre_spec, run_id, te)
        and e.get("source_step_id") == _step(click)
        and e.get("source_tool_call_id") == click_call
    ]
    if len(pre_events) != 1:
        return {}
    index, pre = pre_events[0]
    if any(
        e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
        for e in timeline[index + 1 : source_index]
    ):
        return {}
    before = _focused_search(pre["result"], expected_app, te)
    after = _focused_search(verifier_result, expected_app, te)
    if not before or not after or before[:2] != after[:2] or after[2] != text:
        return {}
    if te._trusted_runtime_execution_provider_identity(verifier_request, verifier_result) != (
        te.LOCAL_DESKTOP_PROVIDER_KIND,
        te.LOCAL_DESKTOP_PROVIDER_ID,
    ):
        return {}
    import hashlib

    return {
        "verification_predicate_kind": te.EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "fulfilled",
        "observed_app_name": expected_app,
        "content_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "content_length": len(text),
    }
