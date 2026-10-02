"""A foreground key delivery must not attest its own UI postcondition."""

from __future__ import annotations

import pytest

from apps.shell.agent.runtime.dispatch_semantics import exact_native_dispatch_receipt_matches
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment
from apps.shell.agent.runtime.outcome_evaluator import evaluate_main_chat_outcome


@pytest.mark.parametrize(
    "tool", ("desktop.safe_key", "app.open_and_safe_key", "app.focus_and_safe_key")
)
@pytest.mark.parametrize("action", ("tab", "shift_tab", "arrow_down"))
@pytest.mark.parametrize("claims_verified", (False, True))
def test_key_dispatch_without_independent_observation_cannot_complete(
    tool: str, action: str, claims_verified: bool
) -> None:
    request = {"action": action, "repeat_count": 1}
    data = {"key_action": action, "repeat_count": 1}
    target = {"kind": "desktop_foreground", "action": "dispatch_shortcut", "shortcut_action": action}
    if tool.startswith("app."):
        request["app_name"] = "Notes"
        data["app_name"] = "Notes"
        target["app_name"] = "Notes"
    result = {"ok": True, "action": tool, "data": data}
    if claims_verified:
        # Provider-supplied flags are not a separate UI observation.
        result.update(postcondition_verified=True, verified_observed_state="fulfilled")
        data.update(postcondition_verified=True, verified_observed_state="fulfilled")
    assert exact_native_dispatch_receipt_matches(tool, request, result) is True
    event = {
        "event": "agent.tool.call",
        "run_id": "run-key",
        "plan_id": "plan-key",
        "step_id": "press-key",
        "detail": tool,
        "tool": tool,
        "tool_call_id": "call-key",
        "capability_id": "desktop.ui_operation",
        "input_preview": request,
        "action_target": target,
        "result": result,
    }
    contract = GoalContract(
        contract_id="goal-key",
        run_id="run-key",
        original_goal="Move to the next input field",
        criteria=(
            GoalCriterion(
                criterion_id="focus-next-input",
                description="Observe the requested keyboard effect",
                # Legacy planner contracts used False here. They must not
                # elevate an acknowledgement to the expected UI state.
                effectful=False,
                required_capabilities=("desktop.ui_operation",),
                expected={"state": "fulfilled", "target": target},
                source_step_ids=("press-key",),
            ),
        ),
    )
    assert runtime_goal_assessment(contract, [event]).completed is False
    legacy_event = {"event_type": "agent.tool.call", "run_id": "run-key", "payload": event}
    assert evaluate_main_chat_outcome({"run_id": "run-key"}, [legacy_event]).allows_completion is False
