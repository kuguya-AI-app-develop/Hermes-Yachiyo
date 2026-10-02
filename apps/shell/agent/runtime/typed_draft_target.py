"""Process-private pre-dispatch AX binding for explicit typed drafts."""

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

from .communication_target import conversation_recipient_matches, is_message_composer
from .private_native_observation import PrivateNativeObservationChannel

_AUTHORITY = object()
PRE_PREFIX = "inspect-typed-draft-"
POST_PREFIX = "verify-typed-draft-"


def _step(request: Mapping[str, Any]) -> str:
    return str(request.get("step_id") or request.get("planner_step_id") or "")


def prepare_typed_draft_targets(
    requests: list[dict[str, Any]],
    *,
    user_goal: str,
    allowed_tools: list[str],
    timeline: Sequence[Mapping[str, Any]],
    run_id: str,
) -> dict[str, dict[str, Any]]:
    for request in requests:
        request.pop(OBSERVATION_REQUEST_KEY, None)
    if not run_id or not any(_step(r).startswith(PRE_PREFIX) for r in requests):
        return {}
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )
    from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

    from .goal_runtime import runtime_goal_contract
    from .model_intent_planning import planner_selection_needs_model_assistance

    try:
        contract = runtime_goal_contract(
            run_id=run_id,
            original_goal=user_goal or None,
            timeline=timeline,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=[],
        )
    except ValueError:
        return {}
    if contract is None or contract.run_id != run_id:
        return {}
    decision = RuntimePlanner().decision(contract.original_goal, allowed_tools=allowed_tools)
    canonical = runtime_execution_requests_from_envelope_payload(
        runtime_execution_envelope_payload(decision, allowed_tools=allowed_tools, full_plan=True),
        allowed_tools=allowed_tools,
    )
    selection = SimpleNamespace(
        decision=decision,
        requests=canonical,
        selected_source="runtime_execution_envelope",
    )
    if planner_selection_needs_model_assistance(selection, contract.original_goal):
        return {}
    # Authority is granted only to the whole current immutable compiled plan.
    if len(requests) != len(canonical):
        return {}
    for actual, expected in zip(requests, canonical, strict=True):
        if any(
            actual.get(k) != expected.get(k)
            for k in (
                "tool",
                "step_id",
                "plan_id",
                "request_id",
                "depends_on",
                "approval_required",
            )
        ) or dict(actual.get("input") or {}) != dict(expected.get("input") or {}):
            return {}
    direct = decision.selected_intent.inputs.get("direct_message_hint")
    recipient = str(direct.get("recipient") or "") if isinstance(direct, Mapping) else ""
    specs = {_step(r): r for r in canonical}
    result = {}
    for pre in canonical:
        pre_id = _step(pre)
        if not pre_id.startswith(PRE_PREFIX):
            continue
        source_id = pre_id.removeprefix(PRE_PREFIX)
        source, post = specs.get(source_id), specs.get(POST_PREFIX + source_id)
        if not source or not post or source.get("tool") != "desktop.safe_type_text":
            continue
        result[source_id] = {
            "_authority": _AUTHORITY,
            "run_id": run_id,
            "pre": pre,
            "source": source,
            "post": post,
            "recipient": "" if source_id == "type-communication-recipient" else recipient,
            "composer_required": source_id != "type-communication-recipient",
        }
    for request in requests:
        request.pop(OBSERVATION_REQUEST_KEY, None)
        if _step(request).startswith((PRE_PREFIX, POST_PREFIX)) and result:
            request[OBSERVATION_REQUEST_KEY] = {"_authority": _AUTHORITY}
    return result


def focused_editable_target(data: Mapping[str, Any]) -> Mapping[str, Any] | None:
    from . import tool_execution as te

    focused = data.get("focused_element")
    rows = data.get("elements")
    elements = [
        e
        for e in (rows if isinstance(rows, (list, tuple)) else [])
        if isinstance(e, Mapping)
        and e.get("focused") is True
        and te._trusted_editable_ui_target_identity(e)
    ]
    if isinstance(focused, Mapping):
        if len(elements) > 1:
            return None
        if focused.get("focused") is not True or not te._trusted_editable_ui_target_identity(
            focused
        ):
            return None
        if any(
            te._trusted_editable_ui_target_identity(e)
            != te._trusted_editable_ui_target_identity(focused)
            for e in elements
        ):
            return None
        return focused
    return elements[0] if len(elements) == 1 else None


def bind_typed_source_target(
    request: Mapping[str, Any],
    specs: Mapping[str, Any],
    timeline: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    private_observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from . import tool_execution as te

    spec = specs.get(_step(request))
    if not isinstance(spec, Mapping) or spec.get("_authority") is not _AUTHORITY:
        return {}
    source = spec["source"]
    if (
        spec.get("run_id") != run_id
        or not request.get("tool_call_id")
        or any(
            request.get(k) != source.get(k) for k in ("tool", "plan_id", "request_id", "depends_on")
        )
        or dict(request.get("input") or {}) != dict(source.get("input") or {})
    ):
        return {}
    expected = spec["pre"]
    events = [
        (i, e)
        for i, e in enumerate(timeline)
        if e.get("event") == "agent.tool.call" and _step(e) == _step(expected)
    ]
    if len(events) != 1:
        return {}
    index, event = events[0]
    observed = event.get("result")
    if (
        event.get("actor") != "native_runtime"
        or event.get("execution_authority") != "runtime_tool_executor"
        or event.get("run_id") != run_id
        or not event.get("tool_call_id")
        or event.get("request_id") != expected.get("request_id")
        or not te._runtime_request_plan_identity_matches(request, event)
        or not isinstance(observed, Mapping)
        or observed.get("ok") is not True
        or observed.get("approval_required")
        or observed.get("permission_error")
        or any(
            e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
            for e in timeline[index + 1 :]
        )
    ):
        return {}
    provider = te._trusted_runtime_execution_provider_identity(event, observed)
    if provider != (te.LOCAL_DESKTOP_PROVIDER_KIND, te.LOCAL_DESKTOP_PROVIDER_ID):
        return {}
    raw = private_observations.get(str(event.get("tool_call_id") or ""))
    if not isinstance(raw, Mapping) or raw.get("_authority") is not _AUTHORITY:
        return {}
    data = raw.get("data")
    if not isinstance(data, Mapping) or data.get("truncated") is True:
        return {}
    app = str((expected.get("input") or {}).get("app_name") or "")
    window = te._trusted_ui_window_identity({"data": data}, expected_app_name=app)
    target = focused_editable_target(data)
    if not window or target is None or target.get("enabled") is False:
        return {}
    if spec["composer_required"] and not is_message_composer(target):
        return {}
    if not conversation_recipient_matches(data, spec["recipient"]):
        return {}
    return {
        "_authority": _AUTHORITY,
        "run_id": run_id,
        "source": spec["source"],
        "tool_call_id": request["tool_call_id"],
        "provider": provider,
        "target_window": window,
        "target_ui_identity": te._trusted_editable_ui_target_identity(target),
        "target_recipient": spec["recipient"],
        "composer_required": spec["composer_required"],
    }


def typed_target_receipt(
    context: Mapping[str, Any],
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    private_observations: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from . import tool_execution as te

    if context.get("_authority") is not _AUTHORITY:
        return {}
    source = context["source"]
    if (
        action_event.get("run_id") != context["run_id"]
        or action_event.get("tool_call_id") != context["tool_call_id"]
        or any(action_event.get(k) != source.get(k) for k in ("request_id", "plan_id"))
        or _step(action_event) != _step(source)
    ):
        return {}
    action_result = action_event.get("result") or {}
    if (
        te._trusted_runtime_execution_provider_identity(action_event, action_result)
        != context["provider"]
    ):
        return {}
    expected_text = (source.get("input") or {}).get("text")
    app = context["target_window"]["app_name"]
    raw = private_observations.get(str(verifier_request.get("tool_call_id") or ""))
    if not isinstance(raw, Mapping) or raw.get("_authority") is not _AUTHORITY:
        return {}
    data = raw.get("data")
    if not isinstance(data, Mapping):
        return {}
    window = te._trusted_ui_window_identity({"data": data}, expected_app_name=app)
    target = focused_editable_target(data)
    if (
        not te._same_trusted_ui_window_identity(window, context["target_window"])
        or target is None
        or target.get("value") != expected_text
        or te._trusted_editable_ui_target_identity(target) != context["target_ui_identity"]
        or not conversation_recipient_matches(data, context["target_recipient"])
        or (context["composer_required"] and not is_message_composer(target))
    ):
        return {}
    import hashlib

    return {
        "verification_predicate_kind": te.EXACT_TYPED_CONTENT_PRESENT_PREDICATE,
        "verified_observed_state": "fulfilled",
        "observed_app_name": app,
        "observed_target": te._trusted_ui_element_identity(target),
        "content_sha256": hashlib.sha256(expected_text.encode()).hexdigest(),
        "content_length": len(expected_text),
        "target_app_name": app,
        "target_window": dict(window),
        "target_ui_identity": context["target_ui_identity"],
        "target_recipient": context["target_recipient"],
        "composer_required": context["composer_required"],
    }


OBSERVATION_REQUEST_KEY = "_runtime_private_typed_observation"
OBSERVATION_RESULT_KEY = "_runtime_private_typed_raw_result"


def _typed_observation_is_bound(request: Mapping[str, Any]) -> bool:
    binding = request.get(OBSERVATION_REQUEST_KEY)
    return bool(
        isinstance(binding, Mapping)
        and binding.get("_authority") is _AUTHORITY
        and request.get("tool") == "desktop.ui_elements"
    )


_TYPED_OBSERVATIONS = PrivateNativeObservationChannel(
    _typed_observation_is_bound, authority=_AUTHORITY,
)


def capture_typed_observation(
    request: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    *,
    local_broker_executed: bool,
) -> Any:
    return _TYPED_OBSERVATIONS.capture(
        request, raw_result, local_broker_executed=local_broker_executed,
    )


def consume_typed_observation(token: Any, request: Mapping[str, Any], *, run_id: str) -> dict:
    return _TYPED_OBSERVATIONS.consume(token, request, run_id=run_id)


def owned_typed_content(context: Mapping[str, Any], receipt: Mapping[str, Any]) -> str | None:
    if (
        context.get("_authority") is not _AUTHORITY
        or context.get("run_id") != receipt.get("run_id")
        or context.get("tool_call_id") != receipt.get("source_tool_call_id")
        or context["source"].get("request_id") != receipt.get("source_request_id")
        or _step(context["source"]) != receipt.get("source_step_id")
    ):
        return None
    content = (context["source"].get("input") or {}).get("text")
    return content if isinstance(content, str) else None
