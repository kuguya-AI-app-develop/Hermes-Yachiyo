from __future__ import annotations

import pytest

from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.runtime.dispatch_semantics import intrinsic_native_postcondition_state


@pytest.mark.parametrize("quit_status,readback,verified", [
    ("quit", "not_running", True), ("not_running", "not_running", True),
    ("quit_requested_running", "running", False), ("quit", "running", False),
    ("quit", "unknown", False), ("quit", "", False),
    ("unknown", "not_running", False),
])
def test_app_quit_native_receipt_requires_independent_not_running_readback(
    tmp_path, monkeypatch, quit_status, readback, verified,
):
    calls = []
    def fake_script(script, args, **kwargs):
        calls.append((script, args))
        return {"ok": True, "stdout": f"{quit_status}|Slack" if len(calls) == 1 else readback}
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_run_osascript", fake_script)
    result = ToolBroker(workspace_policy={}, artifact_root=tmp_path).app_quit("Slack")
    assert len(calls) == 2
    assert all(args == ["Slack"] for script, args in calls)
    assert result["ok"] is True
    assert (result.get("postcondition_verified") is True) is verified
    assert intrinsic_native_postcondition_state("app.quit", {"app_name": "Slack"}, result) == ("fulfilled" if verified else "")
    if readback not in {"running", "not_running"}:
        assert result["data"]["running"] is None
        assert result["data"]["quit_verified"] is False


@pytest.mark.parametrize("change", ["other_app", "unknown_status", "missing_readback", "permission_error", "fallback"])
def test_app_quit_cannot_trust_supplied_completion_flags(tmp_path, monkeypatch, change):
    result = {"ok": True, "action": "app.quit", "postcondition_verified": True,
              "data": {"app_name": "Slack", "quit_status": "quit", "launch_status": "not_running",
                       "running": False, "quit_verified": True, "verified": True}}
    if change == "other_app": result["data"]["app_name"] = "Terminal"
    elif change == "unknown_status": result["data"]["quit_status"] = "unknown"
    elif change == "missing_readback": result["data"].pop("launch_status")
    elif change == "permission_error": result["permission_error"] = True
    elif change == "fallback": result["fallback_used"] = True
    monkeypatch.setattr(desktop, "app_quit", lambda _: result)
    receipt = ToolBroker(workspace_policy={}, artifact_root=tmp_path).app_quit("Slack")
    assert "postcondition_verified" not in receipt
    assert "verified" not in receipt["data"]
    assert intrinsic_native_postcondition_state("app.quit", {"app_name": "Slack"}, result) == ""


@pytest.mark.parametrize("mutation", [
    "trusted", "wrong_app", "unknown_readback", "permission_error",
    "wrong_contract_run", "wrong_plan", "wrong_decision", "wrong_source_step",
    "wrong_capability", "multiple_verifiers", "duplicate_plan", "missing_plan",
    "effectful_verifier", "approval_verifier", "disallowed_verifier",
])
def test_native_quit_resume_preserves_unique_declared_goal_verifier(mutation):
    from apps.shell.agent.runtime.tool_execution import _trusted_declared_exact_dispatch_verifier
    request = {
        "tool": "app.quit", "input": {"app_name": "Slack"}, "run_id": "run-quit",
        "plan_id": "plan-quit", "decision_id": "decision-quit", "step_id": "manage-app",
        "request_id": "request-quit", "tool_call_id": "call-quit",
        "capability_id": "desktop.app_control", "goal_contract_id": "goal-quit",
        "goal_criterion_id": "criterion-quit",
    }
    result = {"ok": True, "action": "app.quit", "postcondition_verified": True, "data": {
        "app_name": "Slack", "quit_status": "quit", "launch_status": "not_running",
        "quit_verified": True, "running": False,
    }}
    criterion = {"criterion_id": "criterion-quit", "effectful": True,
                 "expected": {"state": "fulfilled", "target": {"action": "quit_app", "app_name": "Slack"}}, "required_capabilities": ["desktop.app_control"],
                 "source_step_ids": ["manage-app"], "verifier_step_ids": ["verify-desktop-result"]}
    contract = {"event": "agent.goal.contract", "run_id": "run-quit", "contract_id": "goal-quit",
                "goal_contract": {"run_id": "run-quit", "contract_id": "goal-quit", "source": "goal_contract", "criteria": [criterion]}}
    step = {"step_id": "verify-desktop-result", "tool_name": "desktop.running_apps",
            "capability_id": "desktop.app_discovery", "depends_on": ["manage-app"],
            "approval_required": False,
            "execution_mode": {"mode": "read_only_observation", "keyboard_mouse_capture": False}}
    plan = {"event": "agent.plan.step", "source": "runtime_planner", "decision_id": "decision-quit", "plan_id": "plan-quit", "step": step}
    allowed = ["app.quit", "desktop.running_apps", "desktop.ui_elements"]
    plans = [plan]
    if mutation == "wrong_app": result["data"]["app_name"] = "Terminal"
    elif mutation == "unknown_readback": result["data"]["launch_status"] = "unknown"
    elif mutation == "permission_error": result["permission_error"] = True
    elif mutation == "wrong_contract_run": contract["goal_contract"]["run_id"] = "other"
    elif mutation == "wrong_plan": plan["plan_id"] = "other"
    elif mutation == "wrong_decision": plan["decision_id"] = "other"
    elif mutation == "wrong_source_step": step["depends_on"] = ["other"]
    elif mutation == "wrong_capability": criterion["required_capabilities"] = ["other"]
    elif mutation == "multiple_verifiers": criterion["verifier_step_ids"].append("other")
    elif mutation == "duplicate_plan": plans.append(plan)
    elif mutation == "missing_plan": plans = []
    elif mutation == "effectful_verifier": step["execution_mode"]["mode"] = "foreground_mutation"
    elif mutation == "approval_verifier": step["approval_required"] = True
    elif mutation == "disallowed_verifier": allowed.remove("desktop.running_apps")
    verifier = _trusted_declared_exact_dispatch_verifier("app.quit", request, result, allowed_tools=allowed, timeline=[contract, *plans])
    if mutation == "trusted":
        assert verifier["step_id"] == "verify-desktop-result"
        assert verifier["tool"] == "desktop.ui_elements"
        assert verifier["execution_mode"]["keyboard_mouse_capture"] is False
    else:
        assert verifier == {}


@pytest.mark.parametrize("change", [
    "trusted", "wrong_source_app", "wrong_source_target", "wrong_verifier_plan",
    "wrong_verifier_decision", "wrong_source_plan", "wrong_source_step",
    "duplicate_source", "wrong_source_kind", "wrong_verifier_kind",
])
def test_main_chat_quit_declared_projection_requires_same_source_app_and_plan(change):
    from apps.shell.agent.runtime.tool_execution import _trusted_declared_exact_dispatch_verifier
    target = {"kind": "desktop_app", "action": "quit_app", "app_name": "Slack", "query": "Slack", "selection_source": "direct_app_name"}
    request = {"input": {"app_name": "Slack"}, "run_id": "run", "plan_id": "plan", "decision_id": "decision",
               "step_id": "quit", "capability_id": "desktop.app_control", "goal_contract_id": "goal", "goal_criterion_id": "criterion"}
    result = {"ok": True, "action": "app.quit", "postcondition_verified": True,
              "data": {"app_name": "Slack", "quit_status": "quit", "launch_status": "not_running", "running": False, "quit_verified": True}}
    contract = {"event": "agent.goal.contract", "run_id": "run", "contract_id": "goal",
                "goal_contract": {"run_id": "run", "contract_id": "goal", "source": "goal_contract", "criteria": [
                    {"criterion_id": "criterion", "effectful": True, "expected": {"state": "fulfilled", "target": target},
                     "required_capabilities": ["desktop.app_control"], "source_step_ids": ["quit"], "verifier_step_ids": ["verify"]}]}}
    source = {"event": "agent.desktop.intent_planned", "source": "runtime_planner", "tool": "app.quit", "plan_id": "plan", "decision_id": "decision",
              "step_id": "quit", "capability_id": "desktop.app_control", "input_preview": {"app_name": "Slack"}, "action_target": dict(target)}
    verifier = {"event": "agent.desktop.intent_planned", "source": "runtime_verification", "tool": "desktop.running_apps", "plan_id": "plan", "decision_id": "decision",
                "step_id": "verify", "depends_on": ["quit"], "approval_required": False, "runtime_stage": "verify", "runtime_role": "verify_result",
                "desktop_execution_mode": {"mode": "read_only_observation", "keyboard_mouse_capture": False}}
    sources = [source]
    if change == "wrong_source_app": source["input_preview"]["app_name"] = "Terminal"
    elif change == "wrong_source_target": source["action_target"]["app_name"] = "Terminal"
    elif change == "wrong_verifier_plan": verifier["plan_id"] = "other"
    elif change == "wrong_verifier_decision": verifier["decision_id"] = "other"
    elif change == "wrong_source_plan": source["plan_id"] = "other"
    elif change == "wrong_source_step": source["step_id"] = "other"
    elif change == "duplicate_source": sources.append(dict(source))
    elif change == "wrong_source_kind": source["source"] = "model"
    elif change == "wrong_verifier_kind": verifier["runtime_stage"] = "operate"
    projected = _trusted_declared_exact_dispatch_verifier("app.quit", request, result,
        allowed_tools=["app.quit", "desktop.running_apps", "desktop.ui_elements"], timeline=[contract, *sources, verifier])
    assert bool(projected) is (change == "trusted")
    if projected:
        assert projected["step_id"] == "verify"
