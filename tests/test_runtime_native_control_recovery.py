"""Recovery presentation remains bound to concrete native control and capture goals."""

import pytest

from apps.shell.agent.runtime.model_intent_planning import planner_selection_needs_model_assistance
from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
from apps.shell.yachiyo_agent.daily_desktop import daily_desktop_entrypoint_runtime_plan
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection


def _metadata(tool, inputs):
    return {
        "desktop_permission_recovery": True,
        "recovery_tool": tool,
        "recovery_input": inputs,
        "recovery_risk_level": "low",
        "allow_user_foreground_takeover": True,
    }


@pytest.mark.parametrize("level", [0, 35, 100])
def test_recovery_volume_keeps_the_explicit_native_plan_without_model_assistance(level):
    goal = f"把音量调到 {level}%"
    inputs = {"action": "set", "level": level}
    metadata = _metadata("system.volume", inputs)
    selection = planner_first_direct_tool_selection(
        goal, list(TOOL_DISPATCH_REGISTRY), metadata=metadata,
    )
    assert selection.decision.selected_intent.kind == "system_control"
    assert not planner_selection_needs_model_assistance(selection, goal)
    assert [(r["tool"], r["input"]) for r in selection.requests] == [
        ("system.volume", inputs), ("system.volume", {"action": "status"}),
    ]
    recovery = daily_desktop_entrypoint_runtime_plan(goal, metadata=metadata)
    assert recovery.selected_source == "metadata_runtime_planner"
    assert recovery.decision.plan.task_core.goal_contract.original_goal == goal


@pytest.mark.parametrize("goal,action", [("调高屏幕亮度", "up"), ("调低屏幕亮度", "down")])
def test_recovery_brightness_metadata_does_not_suppress_the_original_control_goal(goal, action):
    selection = planner_first_direct_tool_selection(
        goal, list(TOOL_DISPATCH_REGISTRY),
        metadata=_metadata("system.brightness", {"action": action}),
    )
    assert selection.decision.selected_intent.kind == "system_control"
    assert not planner_selection_needs_model_assistance(selection, goal)
    assert any(r["tool"] == "system.brightness" and r["input"]["action"] == action
               for r in selection.requests)


def test_capture_recovery_preserves_the_original_goal_and_the_complete_atomic_tool():
    goal = "打开 https://github.com 并截图"
    metadata = _metadata("browser.open_url_and_screenshot", {"url": "https://github.com"})
    selection = planner_first_direct_tool_selection(
        goal, list(TOOL_DISPATCH_REGISTRY), metadata=metadata,
    )
    assert not planner_selection_needs_model_assistance(selection, goal)
    assert [r["tool"] for r in selection.requests] == ["browser.open_url_and_screenshot"]
    recovery = daily_desktop_entrypoint_runtime_plan(goal, metadata=metadata)
    assert recovery.selected_source == "metadata_runtime_planner"
    assert recovery.decision.plan.task_core.goal_contract.original_goal == goal
    assert recovery.entrypoint_requests[0]["input"]["url"] == "https://github.com"


def test_atomic_browser_capture_does_not_cover_an_additional_destructive_clause():
    goal = "打开 https://github.com 并截图，然后删除全部文件"
    selection = planner_first_direct_tool_selection(goal, list(TOOL_DISPATCH_REGISTRY))
    assert planner_selection_needs_model_assistance(selection, goal)


@pytest.mark.parametrize("goal,tool,inputs", [
    ("把音量调到 35%", "system.volume", {"action": "set", "level": 36}),
    ("打开 https://github.com 并截图", "browser.open_url_and_screenshot", {"url": "https://example.com"}),
])
def test_recovery_metadata_cannot_change_the_selected_level_or_capture_url(goal, tool, inputs):
    plan = daily_desktop_entrypoint_runtime_plan(goal, metadata=_metadata(tool, inputs))
    assert plan.selected_source == "metadata_unbound"
    assert plan.entrypoint_requests == []


@pytest.mark.parametrize("prefix", ["不要", "如果有必要，", "解释这句话："])
def test_recovery_metadata_does_not_authorize_a_nonexecuting_volume_goal(prefix):
    goal = prefix + "把音量调到 35%"
    selection = planner_first_direct_tool_selection(
        goal, list(TOOL_DISPATCH_REGISTRY),
        metadata=_metadata("system.volume", {"action": "set", "level": 35}),
    )
    assert all(r["tool"] != "system.volume" for r in selection.requests)
    recovery = daily_desktop_entrypoint_runtime_plan(
        goal, metadata=_metadata("system.volume", {"action": "set", "level": 35}),
    )
    assert recovery.selected_source == "metadata_unbound"
    assert recovery.entrypoint_requests == []


@pytest.mark.parametrize("goal", [
    "不要把音量调到 35%，然后把音量调到 40%",
    "请说‘把音量调到 90%’，再把音量调到 40%",
    "解释如何把音量调到 90%，然后把音量调到 40%",
    "把音量调到 40%，并解释如何把音量调到 90%",
    "打开 Finder 2，然后把音量调到 40%",
])
def test_volume_operand_comes_from_the_authorized_sibling_clause(goal):
    selection = planner_first_direct_tool_selection(goal, list(TOOL_DISPATCH_REGISTRY))
    volume_requests = [r for r in selection.requests if r["tool"] == "system.volume"]
    assert volume_requests[0]["input"] == {"action": "set", "level": 40}


@pytest.mark.parametrize("goal,tool,inputs", [
    ("不要静音", "system.volume", {"action": "mute"}),
    ("如果有必要，调高屏幕亮度", "system.brightness", {"action": "up"}),
    ("解释这句话：调低亮度", "system.brightness", {"action": "down"}),
    ("不要音量35%", "system.volume", {"action": "set", "level": 35}),
    ("不要大点声", "system.volume", {"action": "up"}),
    ("不要亮一点", "system.brightness", {"action": "up"}),
])
def test_nonexecuting_system_recovery_cannot_supply_a_replacement_goal(goal, tool, inputs):
    selection = planner_first_direct_tool_selection(
        goal, list(TOOL_DISPATCH_REGISTRY), metadata=_metadata(tool, inputs),
    )
    assert all(r["tool"] != tool for r in selection.requests)
    recovery = daily_desktop_entrypoint_runtime_plan(goal, metadata=_metadata(tool, inputs))
    assert recovery.selected_source == "metadata_unbound"
