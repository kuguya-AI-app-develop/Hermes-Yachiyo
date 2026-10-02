"""Current-window searches use a frozen native plan and independent AX evidence."""

from collections.abc import Mapping, Sequence
from typing import Any

from .query_typing_receipts import _focused_search, _native_event_matches, _step

PRE = "read-foreground-search-field"
READY = "read-foreground-search-ready"
POST = "verify-foreground-search-result"
_SCOPE = ("decision_id", "tool_plan_id", "plan_id", "request_id")
_NAMES = frozenset({"search", "search field", "search box", "搜索", "搜索框", "搜索栏"})


def _specs(timeline, run_id):
    from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    from .goal_runtime import runtime_goal_contract
    from .model_intent_planning import planner_selection_needs_model_assistance

    try:
        contract = runtime_goal_contract(
            run_id=run_id,
            timeline=timeline,
            messages=[],
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
        )
        if contract is None or contract.run_id != run_id:
            return {}
        selected = planner_first_direct_tool_selection(
            contract.original_goal, DAILY_DESKTOP_TOOL_NAMES
        )
        if planner_selection_needs_model_assistance(selected, contract.original_goal):
            return {}
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                selected.decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
            ),
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        )
    except ValueError:
        return {}
    specs = {_step(r): r for r in requests}
    if len(specs) != len(requests) or not {PRE, POST}.issubset(specs):
        return {}
    return specs


def _same_request(request, spec):
    if (
        not isinstance(request, Mapping)
        or not isinstance(spec, Mapping)
        or not isinstance(request.get("input", {}), Mapping)
    ):
        return False
    return bool(
        spec
        and request.get("tool", request.get("tool_name")) == spec.get("tool")
        and all(request.get(k) == spec.get(k) and bool(spec.get(k)) for k in _SCOPE)
        and _step(request) == _step(spec)
        and request.get("depends_on") == spec.get("depends_on")
        and dict(request.get("input") or {}) == dict(spec.get("input") or {})
    )


def _event_ok(event, spec, run_id, te):
    if (
        not isinstance(event, Mapping)
        or not isinstance(spec, Mapping)
        or not isinstance(event.get("input_preview", {}), Mapping)
    ):
        return False
    result = event.get("result") or {}
    if not isinstance(result, Mapping) or not isinstance(result.get("data", {}), Mapping):
        return False
    return bool(
        _native_event_matches(event, spec, run_id, te)
        and result.get("fallback_used") is not True
        and result.get("truncated") is not True
        and (result.get("data") or {}).get("truncated") is not True
    )


def _one(timeline, spec, run_id, te):
    candidates = [
        (i, e)
        for i, e in enumerate(timeline)
        if e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
        and _step(e) == _step(spec)
    ]
    if len(candidates) != 1 or not _event_ok(candidates[0][1], spec, run_id, te):
        return None
    return candidates[0]


def _search(result, te, *, focused):
    if (
        not isinstance(result, Mapping)
        or result.get("ok") is not True
        or result.get("permission_error")
        or result.get("approval_required")
        or result.get("verification_failed")
    ):
        return None
    data = result.get("data") or {}
    if not isinstance(data, Mapping) or not isinstance(data.get("elements", []), list):
        return None
    app = data.get("app_name")
    if (
        not isinstance(app, str)
        or not app
        or result.get("fallback_used") is True
        or result.get("truncated") is True
        or data.get("truncated") is True
    ):
        return None
    if focused:
        return _focused_search(result, app, te)
    window = te._trusted_ui_window_identity(result, expected_app_name=app)
    fields = [
        e
        for e in data.get("elements", [])
        if isinstance(e, Mapping)
        and te._trusted_ui_element_is_editable(e)
        and any(
            str(e.get(k) or "").strip().casefold() in _NAMES
            for k in ("name", "description", "identifier")
        )
    ]
    if not window or len(fields) != 1:
        return None
    identity = te._trusted_editable_ui_target_identity(fields[0])
    focused_search = _focused_search(result, app, te)
    if focused_search:
        if fields[0].get("value") != focused_search[2] or any(
            identity.get(k) and focused_search[1].get(k) != identity[k]
            for k in ("role", "name", "description", "identifier")
        ):
            return None
        return focused_search
    return (window, identity, fields[0].get("value")) if identity else None


def _has_result(result, query, te):
    elements = (result.get("data") or {}).get("elements", [])
    names = {"search results", "find results", "搜索结果", "查询结果", "检索结果"}
    for i, e in enumerate(elements):
        if not isinstance(e, Mapping) or e.get("role") not in {
            "AXTable",
            "AXOutline",
            "AXList",
            "AXGroup",
        }:
            continue
        if not any(
            str(e.get(k) or "").strip().casefold() in names
            for k in ("name", "description", "identifier")
        ):
            continue
        depth = e.get("depth")
        if type(depth) is not int or depth < 0:
            continue
        for child in elements[i + 1 :]:
            if (
                not isinstance(child, Mapping)
                or type(child.get("depth")) is not int
                or child["depth"] <= depth
            ):
                break
            if (
                child.get("role") in {"AXRow", "AXCell", "AXStaticText"}
                and not te._trusted_ui_element_is_editable(child)
                and query in (child.get("value"), child.get("name"))
            ):
                return True
    return False


def _binding(request, timeline, run_id):
    from . import tool_execution as te

    specs = _specs(timeline, run_id)
    if request.get("run_id") != run_id or not _same_request(request, specs.get(_step(request))):
        return None
    before = _one(timeline, specs[PRE], run_id, te)
    preceding = list(specs)[: list(specs).index(PRE)]
    prefix = [_one(timeline, specs[k], run_id, te) for k in preceding]
    if any(not e for e in prefix) or (
        before
        and prefix
        and not all(a[0] < b[0] for a, b in zip([*prefix, before], [*prefix, before][1:]))
    ):
        return None
    prior = [*prefix, before] if before else prefix
    call_ids = [e[1].get("tool_call_id") for e in prior]
    if len(call_ids) != len(set(call_ids)):
        return None
    if not before:
        return None
    search = _search(before[1]["result"], te, focused=True)
    if not search:
        return None
    typing = specs.get("prepare-foreground-search-query")
    query = (typing.get("input") or {}).get("text") if typing else search[2]
    if not isinstance(query, str) or not query or len(query) > 2000:
        return None
    if _has_result(before[1]["result"], query, te):
        return None
    return specs, before, search, query


def foreground_search_request_requires_binding(request, timeline, *, run_id):
    if {PRE, READY} & set(request.get("depends_on") or []):
        return True
    step = _step(request)
    return step in {"prepare-foreground-search-query", "submit-foreground-search"} or (
        step == "submit-app-search" and bool(_specs(timeline, run_id))
    )


def foreground_search_dispatch_ready(request, timeline, *, run_id):
    """Check native field identity before typing or submitting; no completion flag."""
    if not foreground_search_request_requires_binding(request, timeline, run_id=run_id):
        return True
    bound = _binding(request, timeline, run_id)
    if not bound:
        return False
    specs, before, search, query = bound
    from . import tool_execution as te

    if request.get("tool") == "desktop.safe_type_text":
        between = timeline[before[0] + 1 :]
    elif READY in specs:
        typing = _one(timeline, specs["prepare-foreground-search-query"], run_id, te)
        ready = _one(timeline, specs[READY], run_id, te)
        if not typing or not ready or not before[0] < typing[0] < ready[0]:
            return False
        actual = _search(ready[1]["result"], te, focused=True)
        if not actual or actual[:2] != search[:2] or actual[2] != query:
            return False
        between = timeline[ready[0] + 1 :]
    else:
        between = timeline[before[0] + 1 :]
    return not any(
        e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
        for e in between
    )


def trusted_foreground_search_receipt(
    action_tool,
    action_event,
    verifier_request,
    verifier_result,
    timeline: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    if (
        action_tool != "desktop.search_submit"
        or _step(verifier_request) != POST
        or verifier_request.get("actor") != "native_runtime"
        or verifier_request.get("execution_authority") != "runtime_tool_executor"
        or not verifier_request.get("tool_call_id")
    ):
        return {}
    from . import tool_execution as te

    bound = _binding(verifier_request, timeline, run_id)
    if not bound:
        return {}
    specs, before, search, query = bound
    source_spec = specs.get(_step(action_event))
    source = _one(timeline, source_spec, run_id, te) if source_spec else None
    if (
        not source
        or source[1].get("tool_call_id") != action_event.get("tool_call_id")
        or not _event_ok(action_event, source_spec, run_id, te)
        or verifier_request.get("source_tool_call_id") != action_event.get("tool_call_id")
        or verifier_request.get("source_step_id") != _step(action_event)
        or verifier_request.get("source_request_id") != action_event.get("request_id")
    ):
        return {}
    if not foreground_search_dispatch_ready(
        {**source_spec, "run_id": run_id}, timeline[: source[0]], run_id=run_id
    ):
        return {}
    ack = action_event["result"].get("data") or {}
    if str(ack.get("key") or "").casefold() not in {"return", "enter"} or ack.get(
        "modifiers"
    ) not in (None, [], ()):
        return {}
    after_calls = [
        e
        for e in timeline[source[0] + 1 :]
        if e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
    ]
    if after_calls and (
        len(after_calls) != 1
        or after_calls[0].get("tool_call_id") != verifier_request.get("tool_call_id")
        or not _event_ok(after_calls[0], specs[POST], run_id, te)
    ):
        return {}
    prefix_steps = [k for k in specs if k != POST]
    prefix = [_one(timeline, specs[k], run_id, te) for k in prefix_steps]
    if any(not e for e in prefix) or not all(a[0] < b[0] for a, b in zip(prefix, prefix[1:])):
        return {}
    if len({e[1].get("tool_call_id") for e in prefix}) != len(prefix):
        return {}
    if verifier_request["tool_call_id"] in {e[1].get("tool_call_id") for e in prefix}:
        return {}
    actual_calls = [
        e
        for e in timeline[prefix[0][0] : source[0] + 1]
        if e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
    ]
    if len(actual_calls) != len(prefix_steps) or [_step(e) for e in actual_calls] != prefix_steps:
        return {}
    after = _search(verifier_result, te, focused=False)
    if (
        not after
        or after[:2] != search[:2]
        or after[2] != query
        or not _has_result(verifier_result, query, te)
        or te._trusted_runtime_execution_provider_identity(verifier_request, verifier_result)
        != (te.LOCAL_DESKTOP_PROVIDER_KIND, te.LOCAL_DESKTOP_PROVIDER_ID)
    ):
        return {}
    return {
        "verification_predicate_kind": "exact_app_search_result_present",
        "verified_observed_state": "sent",
        "observed_app_name": search[0]["app_name"],
        "observed_query": query,
        "target_window": search[0],
    }


def foreground_search_live_binding(request, timeline, *, run_id):
    """Return internal expectations only after validating a real canonical PRE."""
    if not foreground_search_request_requires_binding(request, timeline, run_id=run_id):
        return None
    if not foreground_search_dispatch_ready(request, timeline, run_id=run_id):
        return None
    specs, before, search, query = _binding(request, timeline, run_id)
    from . import tool_execution as te

    if request.get("tool") == "desktop.safe_type_text":
        expected = search
    elif READY in specs:
        event = _one(timeline, specs[READY], run_id, te)
        expected = _search(event[1]["result"], te, focused=True)
    else:
        expected = search
    return expected


def foreground_search_live_matches(snapshot, expected):
    from . import tool_execution as te

    actual = _search(snapshot, te, focused=True)
    return bool(actual and actual == expected)
