"""Native composer bindings leave ordinary and model-planned paste intact."""

import pytest

from apps.shell.agent.runtime.clipboard_paste_target import (
    clipboard_paste_target_is_bound,
    prepare_clipboard_paste_targets,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner


def _requests(decision):
    return runtime_execution_requests_from_envelope_payload(
        runtime_execution_envelope_payload(
            decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True,
        ),
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )


@pytest.mark.parametrize("mode,verb", [("focus", "切到"), ("open", "打开")])
@pytest.mark.parametrize("recovery", [False, True])
def test_ordinary_app_paste_keeps_compound_dispatch_and_original_verifier(mode, verb, recovery):
    goal = f"{verb}Google Chrome并粘贴"
    tool = f"app.{mode}_and_safe_shortcut"
    metadata = {
        "allow_user_foreground_takeover": True,
        **({
            "desktop_permission_recovery": True,
            "recovery_tool": tool,
            "recovery_input": {"app_name": "Google Chrome", "action": "paste"},
            "recovery_permission_target": "foreground_input",
            "recovery_risk_level": "low",
        } if recovery else {}),
    }
    decision = RuntimePlanner().decision(
        goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, metadata=metadata,
    )
    requests = _requests(decision)
    primary = next(r for r in requests if r["tool"] == tool)
    assert primary["input"] == {
        "app_name": "Google Chrome", "action": "paste",
        "selection_source": "desktop.list_apps", "query": "Google Chrome",
    }
    assert primary["approval_required"] is False
    assert not any(r["tool"] == "clipboard.read" for r in requests)
    assert not any(str(r["step_id"]).startswith("inspect-clipboard-paste-target-")
                   for r in requests)
    assert any(primary["step_id"] in r.get("depends_on", [])
               and r["tool"] in {"desktop.ui_elements", "desktop.verify"}
               for r in requests)
    prepare_clipboard_paste_targets(
        requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    assert not any(clipboard_paste_target_is_bound(r) for r in requests)
    assert decision.plan.task_core.goal_contract.original_goal == goal


@pytest.mark.parametrize("goal", [
    "当前输入框粘贴并发送", "把剪贴板内容发给微信文件传输助手",
])
def test_native_clipboard_send_keeps_source_target_and_readback_before_approval(goal):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    requests = _requests(decision)
    prepare_clipboard_paste_targets(
        requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    paste = next(r for r in requests if clipboard_paste_target_is_bound(r))
    source = next(r for r in requests if r["tool"] == "clipboard.read")
    before = next(
        r for r in requests if str(r["step_id"]).startswith("inspect-clipboard-paste-target-")
    )
    after = next(r for r in requests if str(r["step_id"]).startswith("verify-clipboard-paste-"))
    send = next(r for r in requests if r["tool"] == "desktop.submit_foreground")
    assert set(paste["depends_on"]) == {source["step_id"], before["step_id"]}
    assert after["depends_on"] == [paste["step_id"]]
    assert {paste["step_id"], after["step_id"]}.issubset(send["depends_on"])
    assert send["approval_required"] is True
    assert decision.plan.task_core.goal_contract.original_goal == goal


@pytest.mark.parametrize("goal,kind", [
    ("当前输入框粘贴并发送", "desktop_operation"),
    ("把剪贴板内容发给微信文件传输助手", "communication"),
])
def test_model_compiled_paste_does_not_acquire_original_literal_native_binding(goal, kind):
    decision = RuntimePlanner().decision_from_model_intent_hint(
        goal, goal, kind, allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    requests = _requests(decision)
    assert not any(str(r["step_id"]).startswith("inspect-clipboard-paste-target-")
                   for r in requests)
    assert any((r.get("input") or {}).get("action") == "paste" for r in requests)
    assert any(r["tool"] == "desktop.submit_foreground" and r["approval_required"]
               for r in requests)
    assert decision.plan.task_core.goal_contract.original_goal == goal
