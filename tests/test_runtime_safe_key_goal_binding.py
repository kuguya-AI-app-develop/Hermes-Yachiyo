"""Recovery keys bind to the immutable goal while retaining effect verification."""

import pytest

from apps.shell.agent.runtime.goal_contract import GoalContract
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment
from apps.shell.yachiyo_agent.daily_desktop import daily_desktop_entrypoint_runtime_plan


@pytest.mark.parametrize("prefix", ["", "打开Google Chrome并", "切到Google Chrome并"])
@pytest.mark.parametrize(("action", "label"), [("tab", "Tab"), ("arrow_down", "下箭头")])
@pytest.mark.parametrize("count", [1, 2])
@pytest.mark.parametrize("provider_claims_verified", [True, False])
def test_key_recovery_binds_without_promoting_dispatch(
    prefix, action, label, count, provider_claims_verified
):
    goal = prefix + f"按{label}" + (f"{count}次" if count > 1 else "")
    tool = (
        "app.open_and_safe_key" if prefix.startswith("打开") else
        "app.focus_and_safe_key" if prefix else "desktop.safe_key"
    )
    inputs = {"action": action, "repeat_count": count}
    if prefix:
        inputs["app_name"] = "Google Chrome"
    plan = daily_desktop_entrypoint_runtime_plan(goal, metadata={
        "desktop_permission_recovery": True,
        "recovery_risk_level": "low",
        "recovery_tool": tool,
        "recovery_input": inputs,
        "allow_user_foreground_takeover": True,
    })

    assert plan.selected_source == "metadata_runtime_planner"
    request = next(item for item in plan.entrypoint_requests if item.get("tool") == tool)
    assert inputs.items() <= request["input"].items()
    criterion = next(
        item for item in plan.decision.plan.task_core.goal_contract.criteria
        if "desktop.ui_operation" in item.required_capabilities
    )
    assert criterion.effectful and not criterion.response_satisfiable
    assert criterion.expected["target"]["shortcut_action"] == action
    contract = GoalContract.from_payload(
        plan.decision.plan.task_core.goal_contract.model_dump(mode="json")
    ).bind_run("key-run")
    event = {
        "event": "agent.tool.call", "actor": "native_runtime",
        "source": "runtime_planner", "run_id": "key-run",
        "plan_id": plan.decision.plan.plan_id, "step_id": request["step_id"],
        "tool": tool, "tool_call_id": "key-call",
        "capability_id": "desktop.ui_operation",
        "input_preview": request["input"], "action_target": request["action_target"],
        "result": {
            "ok": True, "action": tool,
            "_runtime_execution_provenance": {"source": "local_tool_broker", "version": 1},
            "data": {**inputs, "key_action": action},
        },
    }
    if provider_claims_verified:
        event["result"].update(
            postcondition_verified=True, verified_observed_state="fulfilled"
        )
        event["result"]["data"].update(
            postcondition_verified=True, verified_observed_state="fulfilled"
        )
    assert not runtime_goal_assessment(contract, [event]).completed
