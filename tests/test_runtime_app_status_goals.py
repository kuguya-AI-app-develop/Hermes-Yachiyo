"""A native app-status readback answers a question without mutating an app."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.goal_contract import GoalContract
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment
from apps.shell.yachiyo_agent import RuntimePlanner


def _status_contract_and_event(running: bool):
    decision = RuntimePlanner().decision(
        "Finder 是否运行", allowed_tools=["app.status", "desktop.running_apps"]
    )
    contract = GoalContract.from_payload(
        decision.plan.task_core.goal_contract.model_dump(mode="json")
    ).bind_run("status-run")
    criterion = next(
        item for item in contract.criteria if "desktop.app_control" in item.required_capabilities
    )
    event = {
        "event": "agent.tool.call",
        "run_id": contract.run_id,
        "plan_id": decision.plan.plan_id,
        "step_id": criterion.source_step_ids[0],
        "tool": "app.status",
        "tool_call_id": "status-call",
        "capability_id": "desktop.app_control",
        "actor": "native_runtime",
        "source": "runtime_planner",
        "action_target": dict(criterion.expected["target"]),
        "input_preview": {"app_name": "Finder"},
        "result": {
            "ok": True,
            "action": "app.status",
            "data": {"app_name": "Finder", "running": running},
        },
    }
    discovery_step = next(
        step for step in decision.plan.tool_plan.steps if step.tool_name == "desktop.running_apps"
    )
    discovery = {
        **event,
        "step_id": discovery_step.step_id,
        "tool": "desktop.running_apps",
        "tool_call_id": "discovery-call",
        "capability_id": "desktop.app_discovery",
        "action_target": next(
            dict(item.expected["target"])
            for item in contract.criteria
            if "desktop.app_discovery" in item.required_capabilities
        ),
        "input_preview": {},
        "result": {
            "ok": True,
            "action": "desktop.running_apps",
            "data": {"apps": [{"name": "Finder"}] if running else []},
        },
    }
    return contract, [discovery, event]


@pytest.mark.parametrize("running", [True, False])
def test_native_status_readback_fulfils_both_running_and_stopped_answers(running):
    contract, events = _status_contract_and_event(running)

    assert all(not item.effectful for item in contract.criteria)
    assert all(not item.response_satisfiable for item in contract.criteria)
    assert not runtime_goal_assessment(contract, []).completed
    assert not runtime_goal_assessment(contract, events[:1]).completed
    assert runtime_goal_assessment(contract, events).completed


@pytest.mark.parametrize("mutation", ["foreign_run", "wrong_step", "wrong_app"])
def test_status_readback_must_still_match_the_requested_run_and_target(mutation):
    contract, events = _status_contract_and_event(True)
    altered = deepcopy(events[-1])
    if mutation == "foreign_run":
        altered["run_id"] = "another-run"
    elif mutation == "wrong_step":
        altered["step_id"] = "another-step"
    else:
        altered["action_target"]["app_name"] = "Safari"

    assert not runtime_goal_assessment(contract, [events[0], altered]).completed


def test_app_launch_still_requires_evidence_of_its_effect():
    decision = RuntimePlanner().decision(
        "打开 Finder", allowed_tools=["app.open", "app.status", "desktop.list_apps"]
    )

    assert any(criterion.effectful for criterion in decision.plan.task_core.goal_contract.criteria)
