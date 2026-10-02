"""A native URL copy needs actual address-control and pasteboard observations."""

import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from .clipboard_copy_transaction import _COPY_AUTHORITY, COPY_TRANSACTION_KEY, _revision, _step
from .private_native_observation import PrivateNativeObservationChannel

PAGE_LINK_PREDICATE = "exact_current_page_link_copied"
PAGE_LINK_COPY_STEP = "copy-current-page-link"
PAGE_LINK_VERIFY_STEP = "verify-copied-page-link"
PAGE_LINK_STEPS = (
    "read-page-link-pasteboard-before",
    "read-page-link-source-ui",
    PAGE_LINK_COPY_STEP,
    "read-page-link-target-ui",
    PAGE_LINK_VERIFY_STEP,
)
_ADDRESS = re.compile(
    r"address(?:\s+and\s+search)?(?:\s+(?:bar|field))?|location(?:\s+(?:bar|field))?|"
    r"url(?:bar|\s+(?:bar|field))|smart\s+search\s+field|地址(?:栏|和搜索栏)?|网址(?:栏|框)",
    re.I,
)
_ROLES = frozenset({"AXTextField", "AXComboBox"})
_PLAN_SCOPE = ("plan_id", "request_id", "decision_id", "tool_plan_id")


def bounded_page_link_copy_goal(goal: str) -> bool:
    return bool(
        re.fullmatch(
            r"(?:please\s+)?copy\s+(?:the\s+)?current\s+page\s+link[.!?]?|"
            r"(?:请|帮我)?复制当前(?:网页|页面|页)链接[。！!]?",
            goal.strip(),
            re.I,
        )
    )


def prepare_page_link_copy(
    requests: list[dict[str, Any]],
    *,
    user_goal: str,
    allowed_tools: list[str],
    run_id: str = "",
    timeline: list[dict[str, Any]] | None = None,
) -> None:
    if run_id:
        from .goal_runtime import runtime_goal_contract

        try:
            contract = runtime_goal_contract(
                run_id=run_id,
                original_goal=user_goal or None,
                timeline=timeline or [],
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
            )
        except ValueError:
            return
        if contract is None:
            return
        user_goal = contract.original_goal
    if not bounded_page_link_copy_goal(user_goal):
        return
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    from .model_intent_planning import planner_selection_needs_model_assistance

    selection = planner_first_direct_tool_selection(user_goal, allowed_tools)
    if planner_selection_needs_model_assistance(selection, user_goal):
        return
    try:
        canonical = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                selection.decision,
                allowed_tools=allowed_tools,
                full_plan=True,
            ),
            allowed_tools=allowed_tools,
        )
    except ValueError:
        return
    if len({_step(r) for r in canonical}) != len(canonical):
        return
    if len(requests) != len(canonical) or [_step(r) for r in requests] != [
        _step(r) for r in canonical
    ]:
        return
    expected = canonical
    if any(
        any(actual.get(k) != expected.get(k) for k in ("tool", *_PLAN_SCOPE, "depends_on"))
        or dict(actual.get("input") or {}) != dict(expected.get("input") or {})
        for actual, expected in zip(requests, expected, strict=True)
    ):
        return
    specs = {_step(r): r for r in canonical if _step(r) in PAGE_LINK_STEPS}
    if set(specs) != set(PAGE_LINK_STEPS):
        return
    binding = {"_authority": _COPY_AUTHORITY, "family": "page_link", "specs": specs}
    for request in requests:
        if _step(request) in specs:
            request[COPY_TRANSACTION_KEY] = binding


def page_link_copy_bound(request: Mapping[str, Any]) -> bool:
    binding = request.get(COPY_TRANSACTION_KEY)
    if (
        not isinstance(binding, Mapping)
        or binding.get("_authority") is not _COPY_AUTHORITY
        or binding.get("family") != "page_link"
    ):
        return False
    spec = (binding.get("specs") or {}).get(_step(request))
    return bool(
        isinstance(spec, Mapping)
        and all(request.get(k) == spec.get(k) for k in ("tool", *_PLAN_SCOPE, "depends_on"))
        and dict(request.get("input") or request.get("input_preview") or {})
        == dict(spec.get("input") or {})
    )


_CHANNEL = PrivateNativeObservationChannel(
    page_link_copy_bound,
    authority=_COPY_AUTHORITY,
    tools=frozenset({"clipboard.read", "desktop.ui_elements"}),
    require_focused_element=False,
)


def capture_page_link_observation(request, result, *, local_broker_executed):
    return _CHANNEL.capture(request, result, local_broker_executed=local_broker_executed)


def consume_page_link_observation(token, request, *, run_id):
    return _CHANNEL.consume(token, request, run_id=run_id)


def _url(value: Any) -> bool:
    if not isinstance(value, str) or not value or any(c.isspace() or ord(c) < 32 for c in value):
        return False
    try:
        value.encode("utf-8")
        parsed = urlsplit(value)
        return bool(
            parsed.scheme in {"http", "https"}
            and parsed.hostname
            and (parsed.port is None or 0 < parsed.port <= 65535)
        )
    except (ValueError, UnicodeEncodeError):
        return False


def _address_identity(element: Mapping[str, Any]) -> tuple[str, str, str] | None:
    role = element.get("role")
    name, description = element.get("name") or "", element.get("description") or ""
    if (
        role not in _ROLES
        or not isinstance(name, str)
        or not isinstance(description, str)
        or element.get("enabled") is False
        or not any(_ADDRESS.fullmatch(label.strip()) for label in (name, description))
    ):
        return None
    return role, name, description


def _address(data: Mapping[str, Any], *, focused: bool) -> tuple[tuple, str] | None:
    app, pid, window = data.get("app_name"), data.get("pid"), data.get("window_id")
    elements = data.get("elements")
    if (
        not isinstance(app, str)
        or not app
        or data.get("truncated") is True
        or type(pid) is not int
        or pid <= 0
        or type(window) is not int
        or window <= 0
        or not isinstance(elements, list)
    ):
        return None
    candidates = [e for e in elements if isinstance(e, Mapping) and _address_identity(e)]
    if len(candidates) != 1:
        return None
    candidate = candidates[0]
    identity = _address_identity(candidate)
    value = candidate.get("value")
    active = data.get("focused_element")
    if isinstance(active, Mapping) and _address_identity(active) == identity:
        if active.get("focused") is not True or active.get("value") != value:
            return None
        value = active.get("value")
    elif focused:
        return None
    if not _url(value):
        return None
    return (app, pid, window, identity), value


def _raw(request, private, *, run_id):
    if request.get("run_id") != run_id:
        return {}
    record = (private or {}).get(str(request.get("tool_call_id") or ""))
    if not isinstance(record, Mapping) or record.get("_authority") is not _COPY_AUTHORITY:
        return {}
    scope = record.get("scope") or {}
    if any(
        scope.get(k) != str(request.get(k) or "")
        for k in ("run_id", "plan_id", "request_id", "tool_call_id", "step_id")
    ) or scope.get("tool") != str(request.get("tool") or request.get("detail") or ""):
        return {}
    return record.get("data") or {}


def _events(request, timeline, steps, *, run_id, provider_identity):
    specs = request[COPY_TRANSACTION_KEY]["specs"]
    found, provider = {}, None
    for index, event in enumerate(timeline):
        if event.get("event") != "agent.tool.call" or _step(event) not in steps:
            continue
        step, result = _step(event), event.get("result") or {}
        if (
            step in found
            or event.get("run_id") != run_id
            or event.get("actor") != "native_runtime"
            or event.get("execution_authority") != "runtime_tool_executor"
            or event.get("detail", event.get("tool")) != specs[step]["tool"]
            or any(event.get(k) != specs[step].get(k) for k in _PLAN_SCOPE)
            or dict(event.get("input_preview") or {}) != dict(specs[step].get("input") or {})
            or result.get("ok") is not True
            or result.get("permission_error")
            or result.get("approval_required")
            or result.get("fallback_used") is True
            or result.get("truncated") is True
        ):
            return {}
        actual_provider = provider_identity(event, result)
        if not all(actual_provider) or (provider is not None and provider != actual_provider):
            return {}
        provider = actual_provider
        found[step] = (index, event)
    if set(found) != set(steps) or [found[s][0] for s in steps] != sorted(
        found[s][0] for s in steps
    ):
        return {}
    # A second action or observation cannot be inserted into this frozen
    # transaction and then be mistaken for its source or final readback.
    first, last = found[steps[0]][0], found[steps[-1]][0]
    if any(
        event.get("event") == "agent.tool.call" and _step(event) not in steps
        for event in timeline[first : last + 1]
    ):
        return {}
    tail_calls = [
        event for event in timeline[last + 1 :] if event.get("event") == "agent.tool.call"
    ]
    if tail_calls and (
        _step(request) != PAGE_LINK_VERIFY_STEP
        or len(tail_calls) != 1
        or any(
            tail_calls[0].get(key) != request.get(key)
            for key in ("run_id", *_PLAN_SCOPE, "tool_call_id", "step_id")
        )
        or tail_calls[0].get("detail", tail_calls[0].get("tool")) != request.get("tool")
    ):
        return {}
    return found


def page_link_copy_source_ready(request, timeline, private, *, run_id, provider_identity):
    if not page_link_copy_bound(request) or _step(request) != PAGE_LINK_COPY_STEP:
        return False
    events = _events(
        request, timeline, PAGE_LINK_STEPS[:2], run_id=run_id, provider_identity=provider_identity
    )
    if not events:
        return False
    before = _raw(events[PAGE_LINK_STEPS[0]][1], private, run_id=run_id)
    source = _raw(events[PAGE_LINK_STEPS[1]][1], private, run_id=run_id)
    return _revision({"data": before}) is not None and _address(source, focused=False) is not None


def exact_page_link_copy_observation(
    action_event,
    verifier_request,
    verifier_result,
    timeline,
    *,
    provider_identity: Callable,
    private_observations=None,
):
    if (
        not page_link_copy_bound(verifier_request)
        or _step(verifier_request) != PAGE_LINK_VERIFY_STEP
    ):
        return {}
    run_id = str(verifier_request.get("run_id") or "")
    events = _events(
        verifier_request,
        timeline,
        PAGE_LINK_STEPS[:-1],
        run_id=run_id,
        provider_identity=provider_identity,
    )
    if not events or events[PAGE_LINK_COPY_STEP][1] is not action_event:
        return {}
    result = action_event.get("result") or {}
    if (
        (action_event.get("input_preview") or {}).get("action") != "copy_current_page_link"
        or result.get("action") != "desktop.safe_shortcut"
        or (result.get("data") or {}).get("shortcut_action") != "copy_current_page_link"
    ):
        return {}
    raw = {
        step: _raw(
            verifier_request if step == PAGE_LINK_VERIFY_STEP else events[step][1],
            private_observations,
            run_id=run_id,
        )
        for step in PAGE_LINK_STEPS
        if step != PAGE_LINK_COPY_STEP
    }
    before = _address(raw[PAGE_LINK_STEPS[1]], focused=False)
    after = _address(raw[PAGE_LINK_STEPS[3]], focused=True)
    if before is None or before != after:
        return {}
    before_rev = _revision({"data": raw[PAGE_LINK_STEPS[0]]})
    observed = raw[PAGE_LINK_VERIFY_STEP]
    after_rev = _revision({"data": observed})
    identity, url = before
    if (
        before_rev is None
        or after_rev is None
        or after_rev <= before_rev
        or observed.get("text") != url
        or observed.get("text_length") != len(url)
        or observed.get("truncated") is not False
    ):
        return {}
    for step in raw:
        request = verifier_request if step == PAGE_LINK_VERIFY_STEP else events[step][1]
        private_observations.pop(str(request.get("tool_call_id") or ""), None)
    app, pid, window, ui = identity
    encoded = url.encode("utf-8")
    return {
        "verification_predicate_kind": PAGE_LINK_PREDICATE,
        "verified_observed_state": "fulfilled",
        "clipboard_source_verified": True,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_length": len(url),
        "content_byte_length": len(encoded),
        "pasteboard_revision_before": before_rev,
        "pasteboard_revision_after": after_rev,
        "target_app_name": app,
        "target_window": {"app_name": app, "pid": pid, "window_id": window},
        "target_ui_identity": {"role": ui[0], "name": ui[1], "description": ui[2]},
    }
