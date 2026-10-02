"""Native Music state is correlated to the full original search/query plan."""

import json
from copy import deepcopy

import pytest

from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent.runtime.native_music_receipts import native_music_search_receipt
from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES as ALLOWED
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)


def _case(tmp_path, monkeypatch):
    goal = "我想听超时空辉夜姬吧"
    selection = planner_first_direct_tool_selection(goal, ALLOWED)
    payload = runtime_execution_envelope_payload(
        selection.decision, allowed_tools=ALLOWED, full_plan=True
    )
    requests = runtime_execution_requests_from_envelope_payload(payload, allowed_tools=ALLOWED)
    contract = runtime_goal_contract(
        run_id="music-run",
        original_goal=goal,
        runtime_execution_envelope=payload,
        runtime_execution_metadata=None,
        messages=[],
        timeline=[],
    )
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop,
        "app_open",
        lambda name: {
            "ok": True,
            "action": "app.open",
            "data": {"app_name": name},
        },
    )
    monkeypatch.setattr(
        desktop,
        "_run_osascript",
        lambda *args, **kwargs: {
            "ok": True,
            "stdout": "controlled|play|playing|超时空辉夜姬|Artist",
            "stderr": "",
        },
    )
    broker_result = ToolBroker(
        workspace_policy={"default_workdir": str(tmp_path)},
        artifact_root=tmp_path / "artifacts",
    ).media_music_app_open_and_play("Music")
    assert broker_result["postcondition_verified"] is True
    provenance = {
        RUNTIME_EXECUTION_PROVENANCE_KEY: {
            "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
            "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
        },
        "local_desktop_provider": {
            "provider_kind": "local_desktop",
            "provider_id": "local-native-desktop",
        },
    }
    scope = {
        "run_id": "music-run",
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
    }
    timeline = [
        {
            "event": "agent.goal.contract",
            "run_id": "music-run",
            "goal_contract": contract.to_payload(),
        }
    ]
    for index, request in enumerate(requests[:-1]):
        actual_input = dict(request["input"])
        if actual_input.get("selection_source") == "desktop.list_apps":
            actual_input.pop("selection_source")
            actual_input.pop("query")
            actual_input["app_name"] = "Music"
        data = {}
        if index == 0:
            chosen = {
                "name": "Music",
                "path": "/System/Applications/Music.app",
                "match_confidence": "high",
            }
            data = {"query": "music", "best_match": chosen, "apps": [chosen]}
        result = {"ok": True, "action": request["tool"], "data": data}
        if index == 4:
            result = broker_result
        timeline.append(
            {
                **deepcopy(request),
                **scope,
                "event": "agent.tool.call",
                "detail": request["tool"],
                "tool_call_id": f"music-call-{index}",
                "input_preview": actual_input,
                "result": {**deepcopy(result), **deepcopy(provenance)},
            }
        )
    verifier = {**requests[-1], **scope, "tool_call_id": "music-verifier"}
    return contract, timeline, verifier


def _projection(verifier, receipt):
    return {
        **verifier,
        "event": "agent.tool.call",
        "detail": "desktop.ui_elements",
        "source": "runtime_native_postcondition_receipt",
        "input_preview": verifier["input"],
        "result": {
            "ok": True,
            "action": "desktop.ui_elements",
            "postcondition_verified": True,
            "verification_satisfied_by_native_receipt": True,
            **receipt,
        },
    }


def test_real_broker_music_readback_finishes_exact_declared_goal(tmp_path, monkeypatch):
    contract, timeline, verifier = _case(tmp_path, monkeypatch)
    receipt = native_music_search_receipt(timeline[-1], verifier, timeline)
    assert receipt["source_tool_call_id"] == "music-call-4"
    assert receipt["source_request_id"] == timeline[-1]["request_id"]
    assert receipt["verified_observed_state"] == "playing"
    assert runtime_goal_assessment(contract, [*timeline, _projection(verifier, receipt)]).completed
    assert not runtime_goal_assessment(contract, timeline).completed


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_query",
        "missing_track",
        "unknown_state",
        "wrong_app",
        "failed_open",
        "failed_play",
        "fallback",
        "provider_flags_only",
        "foreign_provider",
        "model_actor",
        "cross_run",
        "cross_plan",
        "cross_decision",
        "cross_tool_plan",
        "extra_source_input",
        "changed_typed_query",
        "extra_type_input",
        "missing_discovery",
        "foreign_discovery_query",
        "ambiguous_discovery",
        "missing_submit",
        "wrong_order",
        "duplicate_source",
        "interleaved_action",
        "late_action",
        "foreign_verifier",
        "extra_verifier_input",
        "malformed_verifier_input",
        "malformed_source_input",
        "malformed_source_data",
        "malformed_catalog",
    ],
)
def test_music_receipt_rejects_noncanonical_or_unobserved_playback(tmp_path, monkeypatch, mutation):
    _, timeline, verifier = _case(tmp_path, monkeypatch)
    action = timeline[-1]
    data = action["result"]["data"]
    if mutation == "wrong_query":
        data["track"] = "Wrong song"
    elif mutation == "missing_track":
        data.pop("track")
    elif mutation == "unknown_state":
        data["player_state"] = "unknown"
    elif mutation == "wrong_app":
        data["app_name"] = "Spotify"
    elif mutation == "failed_open":
        data["open_ok"] = False
    elif mutation == "failed_play":
        data["playback_ok"] = False
    elif mutation == "fallback":
        action["result"]["fallback_used"] = True
    elif mutation == "provider_flags_only":
        action["result"]["data"] = {"verified": True}
    elif mutation == "foreign_provider":
        action["result"]["local_desktop_provider"]["provider_id"] = "foreign"
    elif mutation == "model_actor":
        action["actor"] = "model"
    elif mutation.startswith("cross_"):
        key = {
            "cross_run": "run_id",
            "cross_plan": "plan_id",
            "cross_decision": "decision_id",
            "cross_tool_plan": "tool_plan_id",
        }[mutation]
        action[key] = "foreign"
    elif mutation == "extra_source_input":
        action["input_preview"]["action"] = "send"
    elif mutation == "changed_typed_query":
        timeline[3]["input_preview"]["text"] = "Wrong song"
    elif mutation == "extra_type_input":
        timeline[3]["input_preview"]["submit"] = True
    elif mutation == "missing_discovery":
        timeline.pop(1)
    elif mutation == "foreign_discovery_query":
        timeline[1]["result"]["data"]["query"] = "Spotify"
    elif mutation == "ambiguous_discovery":
        timeline[1]["result"]["data"]["best_match"]["match_confidence"] = "medium"
    elif mutation == "missing_submit":
        timeline.pop(4)
    elif mutation == "wrong_order":
        timeline[2], timeline[3] = timeline[3], timeline[2]
    elif mutation == "duplicate_source":
        timeline.append(deepcopy(action))
    elif mutation in {"interleaved_action", "late_action"}:
        timeline.insert(
            3 if mutation == "interleaved_action" else len(timeline),
            {"event": "agent.tool.call", "detail": "desktop.submit_foreground"},
        )
    elif mutation == "foreign_verifier":
        verifier["request_id"] = "foreign"
    elif mutation == "extra_verifier_input":
        verifier["input"]["app_name"] = "Spotify"
    elif mutation == "malformed_verifier_input":
        verifier["input"] = ["not", "a", "mapping"]
    elif mutation == "malformed_source_input":
        action["input_preview"] = 7
    elif mutation == "malformed_source_data":
        action["result"]["data"] = "not a mapping"
    elif mutation == "malformed_catalog":
        timeline[1]["result"]["data"] = ["not a mapping"]
    assert native_music_search_receipt(action, verifier, timeline) == {}


@pytest.mark.parametrize(
    "mutation",
    [
        "source_call",
        "source_request",
        "source_step",
        "provider",
        "state",
        "wrong_query",
        "verification_input",
        "verification_dependency",
    ],
)
def test_goal_rejects_forged_or_rebound_music_receipts(tmp_path, monkeypatch, mutation):
    contract, timeline, verifier = _case(tmp_path, monkeypatch)
    receipt = native_music_search_receipt(timeline[-1], verifier, timeline)
    event = _projection(verifier, receipt)
    key = {
        "source_call": "source_tool_call_id",
        "source_request": "source_request_id",
        "source_step": "source_step_id",
        "provider": "provider_id",
        "state": "verified_observed_state",
    }.get(mutation)
    if key:
        event["result"][key] = "foreign"
    elif mutation == "verification_input":
        event["result"]["verification_input"] = {"app_name": "Spotify"}
    elif mutation == "verification_dependency":
        event["result"]["verification_depends_on"] = ["foreign"]
    else:
        timeline[-1]["result"]["data"]["track"] = "Wrong song"
    assert not runtime_goal_assessment(contract, [*timeline, event]).completed


@pytest.mark.parametrize("mode", ["correct", "wrong_query", "unknown_state"])
def test_actual_chat_music_uses_native_state_and_saved_goal_proof(tmp_path, monkeypatch, mode):
    from tests.test_chat_bridge import _run_launcher_daily_desktop_quick_message

    calls, state = [], {"query": ""}
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")

    def script(*args, **kwargs):
        calls.append("native_music_state")
        track = "Wrong song" if mode == "wrong_query" else state["query"]
        playing = "unknown" if mode == "unknown_state" else "playing"
        return {"ok": True, "stdout": f"controlled|play|{playing}|{track}|Artist", "stderr": ""}

    def typed(text):
        calls.append("type")
        state["query"] = text
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "data": {"character_count": len(text)},
        }

    def shortcut(action):
        calls.append(action)
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"shortcut_action": action}}

    def submit():
        calls.append("submit")
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "data": {"key": "return", "modifiers": []},
        }

    def discover(query="", limit=200):
        calls.append("catalog")
        chosen = {
            "name": "Music",
            "path": "/System/Applications/Music.app",
            "match_confidence": "high",
        }
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {"query": query, "apps": [chosen], "best_match": chosen},
        }

    monkeypatch.setattr(desktop, "_run_osascript", script)
    monkeypatch.setattr(
        desktop,
        "app_open",
        lambda name: {"ok": True, "action": "app.open", "data": {"app_name": name}},
    )
    monkeypatch.setattr(
        desktop,
        "app_focus",
        lambda name: {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": name, "focus_verified": True},
        },
    )
    monkeypatch.setattr(
        desktop,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "pid": 100, "window_id": 200},
        },
    )
    monkeypatch.setattr(
        desktop,
        "ui_elements",
        lambda **kwargs: {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": desktop._parse_ui_elements_output(
                "META\tMusic\t100\tMusic\t200\n1\tAXButton\t\tPlay\t\t\ttrue\t0\t0\t100\t100"
            ),
        },
    )
    monkeypatch.setattr(desktop, "list_apps", discover)
    monkeypatch.setattr(desktop, "desktop_safe_shortcut", shortcut)
    monkeypatch.setattr(desktop, "desktop_safe_type_text", typed)
    monkeypatch.setattr(desktop, "desktop_search_submit", submit)
    result, task, run, events = _run_launcher_daily_desktop_quick_message(
        tmp_path, monkeypatch, "我想听超时空辉夜姬吧"
    )
    assert calls == ["catalog", "find", "type", "submit", "native_music_state"]
    assert task["status"] == ("completed" if mode == "correct" else "failed")
    assert "model.request.started" not in events
    stored = json.loads(json.dumps(result["_events"]))
    frozen = runtime_goal_contract(
        run_id=run["run_id"],
        timeline=stored,
        runtime_execution_envelope=None,
        runtime_execution_metadata=None,
        messages=[],
    )
    assert runtime_goal_assessment(frozen, stored).completed is (mode == "correct")
    assert all(
        event.get("visibility") != "internal" for event in result["_task_timeline"]["events"]
    )
