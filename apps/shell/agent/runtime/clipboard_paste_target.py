"""Private target binding for an explicit, Runtime-compiled clipboard paste."""

from collections.abc import Mapping, Sequence
from typing import Any

_AUTHORITY = object()
_KEY = "_runtime_private_clipboard_target"


def _step(request: Mapping[str, Any]) -> str:
    return str(request.get("step_id") or request.get("planner_step_id") or "")


def prepare_clipboard_paste_targets(
    requests: list[dict[str, Any]],
    *,
    user_goal: str,
    allowed_tools: list[str],
    timeline: Sequence[Mapping[str, Any]] = (),
    run_id: str = "",
) -> None:
    """Public step names or metadata cannot mint target-selection authority."""
    for request in requests:
        request.pop(_KEY, None)
    if not any(_step(r).startswith("inspect-clipboard-paste-target-") for r in requests):
        return
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )
    from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

    from .goal_runtime import runtime_goal_contract

    try:
        contract = (
            runtime_goal_contract(
                run_id=run_id,
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=timeline,
            )
            if run_id
            else None
        )
    except ValueError:
        return
    if run_id and contract is None:
        return
    if contract is not None:
        if user_goal and user_goal != contract.original_goal:
            return
        user_goal = contract.original_goal
    if not user_goal:
        return
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    try:
        selection = planner_first_direct_tool_selection(user_goal, allowed_tools)
        if planner_selection_needs_model_assistance(selection, user_goal):
            return
        decision = RuntimePlanner().decision(user_goal, allowed_tools=allowed_tools)
        canonical = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                decision, allowed_tools=allowed_tools, full_plan=True
            ),
            allowed_tools=allowed_tools,
        )
    except ValueError:
        return
    all_expected = {_step(r): r for r in canonical}
    if len(requests) != len(canonical) or len(all_expected) != len(canonical):
        return
    if len({_step(r) for r in requests}) != len(requests):
        return
    if any(
        _step(r) not in all_expected
        or any(
            r.get(k) != all_expected[_step(r)].get(k)
            for k in ("tool", "plan_id", "request_id", "depends_on")
        )
        or dict(r.get("input") or {}) != dict(all_expected[_step(r)].get("input") or {})
        for r in requests
    ):
        return
    for expected_target in canonical:
        target_id = _step(expected_target)
        if not target_id.startswith("inspect-clipboard-paste-target-"):
            continue
        paste_id = target_id.removeprefix("inspect-clipboard-paste-target-")
        source_id = f"read-clipboard-before-{paste_id}"
        verifier_id = f"verify-clipboard-paste-{paste_id}"
        ids = {paste_id, source_id, target_id, verifier_id}
        if not ids.issubset(all_expected):
            continue
        expected = all_expected
        actual = requests
        paste = next(r for r in actual if _step(r) == paste_id)
        hint = decision.selected_intent.inputs.get("direct_message_hint")
        recipient = str(hint.get("recipient") or "") if isinstance(hint, Mapping) else ""
        send_required = any(
            r.get("tool") == "desktop.submit_foreground"
            and (r.get("input") or {}).get("action") == "send"
            for r in canonical
        )
        paste[_KEY] = {
            "_authority": _AUTHORITY,
            "target_request": expected[target_id],
            "recipient": recipient,
            "send_required": send_required,
        }


def observed_clipboard_paste_target(
    request: Mapping[str, Any],
    result: Mapping[str, Any],
    timeline: Sequence[Mapping[str, Any]],
    *,
    run_id: str,
    before_dispatch: bool = False,
) -> dict[str, Any]:
    """Bind one recent focused editable AX object, with exact source lineage."""
    binding = request.get(_KEY)
    if not isinstance(binding, Mapping) or binding.get("_authority") is not _AUTHORITY:
        return {}
    from . import tool_execution as te

    expected = binding["target_request"]
    target_id = _step(expected)
    if target_id not in te._string_list(request.get("depends_on")):
        return {}
    matches = [
        (i, e)
        for i, e in enumerate(timeline)
        if e.get("event") == "agent.tool.call"
        and str(e.get("detail") or e.get("tool") or "") == "desktop.ui_elements"
        and _step(e) == target_id
    ]
    if len(matches) != 1:
        return {}
    index, event = matches[0]
    observed = event.get("result")
    if not isinstance(observed, Mapping) or observed.get("ok") is not True:
        return {}
    if event.get("run_id") != run_id or not event.get("tool_call_id"):
        return {}
    if (
        event.get("actor") != "native_runtime"
        or event.get("execution_authority") != "runtime_tool_executor"
    ):
        return {}
    if event.get("request_id") != expected.get(
        "request_id"
    ) or not te._runtime_request_plan_identity_matches(request, event):
        return {}
    preview = event.get("input_preview") or {}
    executable_input = {
        k: v
        for k, v in (expected.get("input") or {}).items()
        if k not in {"selection_source", "query"}
    }
    if any(preview.get(k) != v for k, v in executable_input.items()):
        return {}
    later = [
        e
        for e in timeline[index + 1 :]
        if e.get("event") in {"agent.tool.call", "agent.tool.failed", "agent.tool.skipped"}
    ]
    if before_dispatch:
        if later:
            return {}
        result = observed
    elif (
        len(later) != 1
        or _step(later[0]) != _step(request)
        or later[0].get("tool_call_id") != request.get("tool_call_id")
    ):
        return {}
    provider = te._trusted_runtime_execution_provider_identity(request, result)
    if not all(provider) or provider != te._trusted_runtime_execution_provider_identity(
        event, observed
    ):
        return {}
    data = observed.get("data")
    if not isinstance(data, Mapping) or data.get("truncated") is True:
        return {}
    recipient = str(binding.get("recipient") or "")
    if recipient:
        from .communication_target import conversation_recipient_matches

        if not conversation_recipient_matches(data, recipient):
            return {}
    expected_app = te._approval_dependency_request_app_name(request)
    window = te._trusted_ui_window_identity(observed, expected_app_name=expected_app)
    if not window:
        return {}
    action_window = te._trusted_ui_window_identity(result, expected_app_name=window["app_name"])
    if action_window and not te._same_trusted_ui_window_identity(window, action_window):
        return {}
    for source in te._structured_result_sources(result):
        app = str(source.get("app_name") or source.get("active_app_name") or "")
        if app and not te._app_lookups_same_identity(window["app_name"], app):
            return {}
    focused = data.get("focused_element")
    elements = [
        e
        for e in data.get("elements", [])
        if isinstance(e, Mapping)
        and e.get("focused") is True
        and te._trusted_editable_ui_target_identity(e)
    ]
    if isinstance(focused, Mapping):
        if focused.get("focused") is not True or not te._trusted_editable_ui_target_identity(
            focused
        ):
            return {}
        if any(
            te._trusted_editable_ui_target_identity(e)
            != te._trusted_editable_ui_target_identity(focused)
            for e in elements
        ):
            return {}
        target = focused
    elif len(elements) == 1:
        target = elements[0]
    else:
        return {}
    identity = te._trusted_editable_ui_target_identity(target)
    label = te._trusted_ui_element_identity(target)
    if not identity or not label or target.get("enabled") is False:
        return {}
    if binding.get("send_required"):
        from .communication_target import is_message_composer

        if not is_message_composer(target):
            return {}
    return {
        "target_app_name": window["app_name"],
        "target_window": window,
        "target_ui_element": label,
        "pre_paste_target_identity": identity,
        "pre_paste_tool_call_id": event["tool_call_id"],
        **({"target_recipient": recipient} if recipient else {}),
    }


def clipboard_paste_target_is_bound(request: Mapping[str, Any]) -> bool:
    binding = request.get(_KEY)
    return isinstance(binding, Mapping) and binding.get("_authority") is _AUTHORITY
