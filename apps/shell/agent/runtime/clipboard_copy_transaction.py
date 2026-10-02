"""Process-private binding for one explicit select-all/copy transaction."""

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

_COPY_AUTHORITY = object()
COPY_TRANSACTION_KEY = "_runtime_private_select_all_copy"
COPY_PREDICATE = "exact_selected_full_text_copied"
COPY_STEP = "copy-selected-full-text"
COPY_VERIFY_STEP = "verify-copied-full-text"
_COPY_STEPS = (
    "read-copy-pasteboard-before",
    "prepare-select-all-for-copy",
    "read-copy-source-ui",
    COPY_STEP,
    "read-copy-target-ui",
    COPY_VERIFY_STEP,
)


def _step(request: Mapping[str, Any]) -> str:
    return str(request.get("step_id") or request.get("planner_step_id") or "")


def prepare_copy_transactions(
    requests: list[dict[str, Any]],
    *,
    user_goal: str,
    allowed_tools: list[str],
    run_id: str = "",
    timeline: list[dict[str, Any]] | None = None,
) -> None:
    """Recompile the immutable goal before minting a nonserializable binding.

    A supplied step name, public metadata or model-authored preparation flag
    cannot waive verification. Only the exact Runtime-compiled transaction
    for this original goal receives the opaque in-process marker.
    """
    for request in requests:
        request.pop(COPY_TRANSACTION_KEY, None)
        if _step(request) == "prepare-select-all-for-copy":
            request["requires_post_action_verification"] = True
    if not any(_step(r) in _COPY_STEPS for r in requests):
        return
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    if run_id:
        from apps.shell.agent.runtime.goal_runtime import runtime_goal_contract

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
            contract = None
        if contract is None:
            return
        user_goal = contract.original_goal
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    selection = planner_first_direct_tool_selection(user_goal, allowed_tools)
    if planner_selection_needs_model_assistance(selection, user_goal):
        return
    decision = selection.decision
    try:
        payload = runtime_execution_envelope_payload(
            decision, allowed_tools=allowed_tools, full_plan=True
        )
        canonical = runtime_execution_requests_from_envelope_payload(
            payload, allowed_tools=allowed_tools
        )
    except ValueError:
        return
    specs = {_step(r): r for r in canonical if _step(r) in _COPY_STEPS}
    valid = set(specs) == set(_COPY_STEPS)
    current = [r for r in requests if _step(r) in _COPY_STEPS]
    valid = valid and [_step(r) for r in current] == list(_COPY_STEPS)
    for actual in current:
        expected = specs.get(_step(actual), {})
        if any(
            actual.get(k) != expected.get(k)
            for k in ("tool", "plan_id", "request_id", "depends_on")
        ):
            valid = False
        actual_input, expected_input = actual.get("input") or {}, expected.get("input") or {}
        if actual_input != expected_input:
            valid = False
    if len({_step(r) for r in current}) != len(current):
        valid = False
    if valid:
        binding = {"_authority": _COPY_AUTHORITY, "specs": specs}
        for request in current:
            request[COPY_TRANSACTION_KEY] = binding
            if _step(request) == "prepare-select-all-for-copy":
                request["requires_post_action_verification"] = False
    else:
        for request in current:
            if _step(request) == "prepare-select-all-for-copy":
                request["requires_post_action_verification"] = True


def copy_transaction_bound(request: Mapping[str, Any]) -> bool:
    binding = request.get(COPY_TRANSACTION_KEY)
    return isinstance(binding, Mapping) and binding.get("_authority") is _COPY_AUTHORITY


def _focused(result: Mapping[str, Any]) -> tuple[tuple[str, int, int, str], str] | None:
    data = result.get("data")
    if not isinstance(data, Mapping):
        return None
    focused = data.get("focused_element")
    if not isinstance(focused, Mapping) or focused.get("focused") is not True:
        return None
    role = str(focused.get("role") or "").casefold()
    if role not in {"axtextfield", "axtextarea", "axcombobox", "textfield", "textarea", "combobox"}:
        return None
    value = focused.get("value")
    if not isinstance(value, str) or not value or focused.get("enabled") is False:
        return None
    app, pid, window = data.get("app_name"), data.get("pid"), data.get("window_id")
    if (
        not isinstance(app, str)
        or not app
        or any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in (pid, window))
    ):
        return None
    return (app, pid, window, role), value


def _revision(result: Mapping[str, Any]) -> int | None:
    data = result.get("data")
    if not isinstance(data, Mapping) or data.get("pasteboard_revision_stable") is not True:
        return None
    revision = data.get("pasteboard_revision")
    return (
        revision
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0
        else None
    )


def exact_copy_observation(
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
    *,
    provider_identity: Callable[[Mapping[str, Any], Mapping[str, Any]], tuple[str, str]],
    private_observations: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not copy_transaction_bound(verifier_request) or _step(verifier_request) != COPY_VERIFY_STEP:
        return {}
    binding = verifier_request[COPY_TRANSACTION_KEY]
    specs = binding["specs"]
    copy_spec = specs[COPY_STEP]
    if (
        any(action_event.get(k) != copy_spec.get(k) for k in ("plan_id", "request_id"))
        or _step(action_event) != COPY_STEP
    ):
        return {}
    if action_event.get("detail", action_event.get("tool")) != "desktop.safe_shortcut":
        return {}
    if (action_event.get("input_preview") or {}).get("action") != "copy":
        return {}
    run_id = str(action_event.get("run_id") or "")
    action_result = action_event.get("result") or {}
    action_data = action_result.get("data") or {}
    if (
        action_result.get("action") != "desktop.safe_shortcut"
        or action_data.get("shortcut_action") != "copy"
    ):
        return {}
    provider = provider_identity(action_event, action_result)
    if not run_id or not all(provider):
        return {}
    events: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, event in enumerate(timeline):
        if (event.get("event") or event.get("event_type")) != "agent.tool.call":
            continue
        step = _step(event)
        spec = specs.get(step)
        if spec is None or step == COPY_VERIFY_STEP:
            continue
        if step in events:
            return {}
        result = event.get("result") or {}
        if (event.get("detail") or event.get("tool")) != spec["tool"] or any(
            event.get(k) != spec.get(k) for k in ("plan_id", "request_id")
        ):
            return {}
        if (
            event.get("run_id") != run_id
            or result.get("ok") is not True
            or result.get("approval_required")
            or result.get("permission_error")
        ):
            return {}
        if provider_identity(event, result) != provider:
            return {}
        events[step] = (index, event)
    needed = _COPY_STEPS[:-1]
    if set(events) != set(needed) or [events[s][0] for s in needed] != sorted(
        events[s][0] for s in needed
    ):
        return {}
    private = private_observations if isinstance(private_observations, dict) else {}
    raw_results = {}
    for step in (
        "read-copy-pasteboard-before",
        "read-copy-source-ui",
        "read-copy-target-ui",
        COPY_VERIFY_STEP,
    ):
        context = verifier_request if step == COPY_VERIFY_STEP else events[step][1]
        call_id = str(context.get("tool_call_id") or "")
        observation = private.pop(call_id, None)
        if (
            not isinstance(observation, Mapping)
            or observation.get("_authority") is not _COPY_AUTHORITY
        ):
            return {}
        scope = observation.get("scope") or {}
        if any(
            scope.get(k) != str(context.get(k) or "")
            for k in ("run_id", "plan_id", "request_id", "tool_call_id", "step_id")
        ):
            return {}
        if scope.get("tool") != specs[step]["tool"]:
            return {}
        raw_results[step] = {"data": observation.get("data") or {}}
    before_revision = _revision(raw_results[needed[0]])
    after_revision = _revision(raw_results[COPY_VERIFY_STEP])
    if before_revision is None or after_revision is None or after_revision <= before_revision:
        return {}
    before = _focused(raw_results["read-copy-source-ui"])
    after = _focused(raw_results["read-copy-target-ui"])
    if before is None or before != after:
        return {}
    identity, value = before
    app, pid, window, role = identity
    expected_app = str((specs["read-copy-source-ui"].get("input") or {}).get("app_name") or "")
    if app != expected_app:
        return {}
    select_event = events["prepare-select-all-for-copy"][1]
    if (select_event.get("input_preview") or {}).get("action") != "select_all":
        return {}
    observed = raw_results[COPY_VERIFY_STEP]["data"]
    if (
        observed.get("text") != value
        or observed.get("text_length") != len(value)
        or observed.get("truncated") is not False
    ):
        return {}
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return {}
    return {
        "verification_predicate_kind": COPY_PREDICATE,
        "verified_observed_state": "fulfilled",
        "clipboard_source_verified": True,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content_length": len(value),
        "content_byte_length": len(encoded),
        "pasteboard_revision_before": before_revision,
        "pasteboard_revision_after": after_revision,
        "target_app_name": app,
        "target_window": {"app_name": app, "pid": pid, "window_id": window},
        "target_ui_identity": {"role": role},
    }


class _CopyObservationToken:
    """A live executor capability; JSON-like output cannot reproduce it."""

    def __init__(self, request: Mapping[str, Any], data: Mapping[str, Any]):
        self.scope = {
            key: str(request.get(key) or "")
            for key in (
                "run_id",
                "plan_id",
                "request_id",
                "tool_call_id",
                "step_id",
                "tool",
            )
        }
        self.data = dict(data)
        self.used = False


COPY_OBSERVATION_RESULT_KEY = "_runtime_private_copy_observation"


def capture_copy_observation(
    request: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    *,
    local_broker_executed: bool,
) -> Any:
    if not local_broker_executed or not copy_transaction_bound(request):
        return None
    if raw_result.get("ok") is not True or raw_result.get("permission_error"):
        return None
    tool = request.get("tool")
    if tool not in {"desktop.ui_elements", "clipboard.read"}:
        return None
    data = raw_result.get("data")
    if not isinstance(data, Mapping):
        return None
    if tool == "desktop.ui_elements":
        focused = data.get("focused_element")
        if not isinstance(focused, Mapping):
            return None
        raw = {key: data.get(key) for key in ("app_name", "pid", "window_id")}
        raw["focused_element"] = dict(focused)
    else:
        raw = {
            key: data.get(key)
            for key in (
                "text",
                "text_length",
                "truncated",
                "pasteboard_revision",
                "pasteboard_revision_stable",
            )
        }
    return _CopyObservationToken(request, raw)


def consume_copy_observation(
    token: Any,
    request: Mapping[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    if not isinstance(token, _CopyObservationToken) or token.used:
        return {}
    token.used = True
    expected = {key: str(request.get(key) or "") for key in token.scope}
    expected["run_id"] = run_id
    if token.scope != expected or not all(expected.values()) or not copy_transaction_bound(request):
        return {}
    return {"_authority": _COPY_AUTHORITY, "scope": token.scope, "data": token.data}
