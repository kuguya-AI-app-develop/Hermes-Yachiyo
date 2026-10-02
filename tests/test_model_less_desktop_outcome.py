from copy import deepcopy

import pytest

from apps.shell.agent.runtime.model_less_desktop_outcome import executed_bounded_desktop_requests


def _dispatch(tool="app.focus_and_safe_shortcut", action="paste"):
    request = {
        "tool": tool, "plan_id": "plan-1", "request_id": "plan-1:request:1:action",
        "step_id": "action", "risk_level": "low", "approval_required": False,
        "input": {"app_name": "Google Chrome", "action": action},
    }
    event = {
        "event": "agent.tool.call", "detail": tool, "plan_id": "plan-1",
        "request_id": request["request_id"], "step_id": "action",
        "input_preview": deepcopy(request["input"]), "result": {"ok": True},
    }
    verifier = {
        "tool": "desktop.ui_elements", "plan_id": "plan-1",
        "request_id": "plan-1:request:2:verify", "step_id": "verify",
        "continue_to_model": True,
    }
    return [request, verifier], [event]


@pytest.mark.parametrize("tool,action", [
    ("app.focus_and_safe_shortcut", "paste"),
    ("app.open_and_safe_shortcut", "copy"),
    ("app.open_and_safe_key", "tab"),
])
def test_dispatched_action_can_preserve_partial_result_without_verifier_authority(tool, action):
    requests, timeline = _dispatch(tool, action)
    preserved = executed_bounded_desktop_requests(requests, timeline, tool_timeline_start=0)
    assert preserved == requests
    assert timeline == [{
        "event": "agent.tool.call", "detail": tool, "plan_id": "plan-1",
        "request_id": "plan-1:request:1:action", "step_id": "action",
        "input_preview": {"app_name": "Google Chrome", "action": action}, "result": {"ok": True},
    }]
    assert not any("postcondition_verified" in r or "completed" in r for r in preserved)


@pytest.mark.parametrize("change", [
    "no_event", "outside_start", "foreign_plan", "foreign_request", "foreign_step",
    "wrong_app", "wrong_action", "failed_dispatch", "pending_approval", "unresolved_app",
    "model_generated_input", "model_action", "higher_risk", "incomplete_chain", "no_primary",
])
def test_missing_model_remains_blocker_for_unexecuted_or_unbound_chain(change):
    requests, timeline = _dispatch()
    start = 0
    if change == "no_event": timeline = []
    elif change == "outside_start": start = 1
    elif change == "foreign_plan": timeline[0]["plan_id"] = "other-plan"
    elif change == "foreign_request": timeline[0]["request_id"] = "plan-1:request:99:action"
    elif change == "foreign_step": timeline[0]["step_id"] = "other-step"
    elif change == "wrong_app": timeline[0]["input_preview"]["app_name"] = "Terminal"
    elif change == "wrong_action": timeline[0]["input_preview"]["action"] = "copy"
    elif change == "failed_dispatch": timeline[0]["result"]["ok"] = False
    elif change == "pending_approval": timeline[0]["result"]["approval_required"] = True
    elif change == "unresolved_app": requests[0]["input"]["app_name"] = "<selected app from desktop.list_apps>"
    elif change == "model_generated_input": requests[0]["input"]["body_source"] = "model_generated"
    elif change == "model_action": requests[0]["continue_to_model"] = True
    elif change == "higher_risk": requests[0]["risk_level"] = "high"
    elif change == "incomplete_chain": requests.insert(1, {**requests[0], "request_id": "plan-1:request:2:action", "step_id": "second-action"})
    elif change == "no_primary": requests = [requests[-1]]
    assert executed_bounded_desktop_requests(requests, timeline, tool_timeline_start=start) == []


def _catalog_bound_dispatch():
    requests, timeline = _dispatch(action="new_window")
    requests[0]["input"].update({
        "app_name": "PixelForge", "selection_source": "desktop.list_apps", "query": "PixelForge",
    })
    timeline[0]["input_preview"].update({
        "app_name": "PixelForge Studio", "requested_app_name": "PixelForge",
        "resolved_app_name": "PixelForge Studio", "app_resolution_source": "desktop.list_apps",
    })
    discovery = {
        "tool": "desktop.list_apps", "input": {"query": "PixelForge"},
        "plan_id": "plan-1", "request_id": "plan-1:request:0:discovery", "step_id": "discovery",
    }
    observation = {
        "event": "agent.tool.call", "detail": "desktop.list_apps", "plan_id": "plan-1",
        "request_id": discovery["request_id"], "step_id": "discovery",
        "result": {"ok": True, "data": {
            "query": "PixelForge", "apps": [{"name": "PixelForge Studio", "match_score": 96}],
        }},
    }
    return [discovery, *requests], [observation, *timeline]


def test_same_plan_catalog_resolution_preserves_truthful_partial_application_name():
    requests, timeline = _catalog_bound_dispatch()
    original = deepcopy(requests)
    result = executed_bounded_desktop_requests(requests, timeline, tool_timeline_start=0)
    assert result[1]["input"]["app_name"] == "PixelForge Studio"
    assert result[1]["input"]["action"] == "new_window"
    assert requests == original
    assert "postcondition_verified" not in result[1]


@pytest.mark.parametrize("change", [
    "foreign_plan", "foreign_request", "foreign_step", "wrong_query", "wrong_original_query",
    "wrong_requested_name", "wrong_resolved_name", "foreign_source", "missing_catalog",
    "failed_catalog", "unknown_app", "ambiguous_catalog", "tied_best_match",
])
def test_unbound_or_ambiguous_catalog_resolution_keeps_missing_model_blocker(change):
    requests, timeline = _catalog_bound_dispatch()
    observation = timeline[0]
    data = observation["result"]["data"]
    actual = timeline[1]["input_preview"]
    if change == "foreign_plan": observation["plan_id"] = "other-plan"
    elif change == "foreign_request": observation["request_id"] = "plan-1:request:99:other"
    elif change == "foreign_step": observation["step_id"] = "other-step"
    elif change == "wrong_query": data["query"] = "Terminal"
    elif change == "wrong_original_query": requests[1]["input"]["query"] = "Terminal"
    elif change == "wrong_requested_name": actual["requested_app_name"] = "Terminal"
    elif change == "wrong_resolved_name": actual["resolved_app_name"] = "Terminal"
    elif change == "foreign_source": actual["app_resolution_source"] = "user_metadata"
    elif change == "missing_catalog": timeline.pop(0)
    elif change == "failed_catalog": observation["result"]["ok"] = False
    elif change == "unknown_app": data["apps"] = [{"name": "Terminal"}]
    elif change in {"ambiguous_catalog", "tied_best_match"}:
        data["apps"].append({"name": "PixelForge Plus", "match_score": 96})
        if change == "tied_best_match": data["best_match"] = data["apps"][0]
    assert executed_bounded_desktop_requests(requests, timeline, tool_timeline_start=0) == []
