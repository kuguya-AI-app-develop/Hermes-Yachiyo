"""Explicit paste uses a private source and one observed editable target."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.shell.agent.runtime import clipboard_paste_target as pt
from apps.shell.agent.runtime import tool_execution as te
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner


def _case(goal="当前输入框粘贴并发送"):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    requests = runtime_execution_requests_from_envelope_payload(
        runtime_execution_envelope_payload(
            decision,
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
            full_plan=True,
            metadata={"allow_user_foreground_takeover": True},
        ),
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    for i, r in enumerate(requests):
        r.update(
            run_id="run-paste",
            tool_call_id=f"call-{i}",
            actor="native_runtime",
            execution_authority="runtime_tool_executor",
        )
    te._prepare_runtime_private_clipboard_source_requests(requests)
    pt.prepare_clipboard_paste_targets(
        requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES
    )
    paste = next(r for r in requests if r["step_id"] == "operate-foreground-ui")
    target = next(r for r in requests if r["step_id"].startswith("inspect-clipboard-paste-target-"))
    source = next(r for r in requests if r["tool"] == "clipboard.read")
    verifier = next(r for r in requests if r["step_id"].startswith("verify-clipboard-paste-"))
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
    before = {
        "ok": True,
        "action": "desktop.ui_elements",
        **provenance,
        "data": {
            "app_name": "WeChat",
            "pid": 100,
            "window_id": 200,
            "truncated": False,
            "elements": [
                {
                    "role": "AXTextArea",
                    "identifier": "composer-1",
                    "name": "Message",
                    "value": "",
                    "focused": True,
                    "editable": True,
                }
            ],
        },
    }
    dispatched = {
        "ok": True,
        "action": "desktop.safe_shortcut",
        **provenance,
        "data": {"shortcut_action": "paste"},
    }
    events = [
        {
            **target,
            "event": "agent.tool.call",
            "detail": target["tool"],
            "input_preview": target["input"],
            "result": before,
        },
        {
            **paste,
            "event": "agent.tool.call",
            "detail": paste["tool"],
            "input_preview": paste["input"],
            "result": dispatched,
        },
    ]
    clipboard = {
        "ok": True,
        "action": "clipboard.read",
        **provenance,
        "data": {
            "text": "原文\n  literal  ",
            "text_length": 15,
            "max_chars": 12000,
            "truncated": False,
        },
    }
    clipboard["data"]["text_length"] = len(clipboard["data"]["text"])
    source_receipt = te._private_clipboard_source_receipt_from_result(
        source, clipboard, run_id="run-paste", tool_sequence=1
    )
    return requests, paste, verifier, events, source_receipt


def test_compiled_paste_retains_source_and_verifier_before_send_approval():
    requests, paste, verifier, events, source = _case()
    assert paste.get(pt._KEY)
    assert len(paste["depends_on"]) == 2
    send = next(r for r in requests if r["tool"] == "desktop.submit_foreground")
    assert paste["step_id"] in send["depends_on"]
    assert verifier["step_id"] in send["depends_on"]
    assert send["approval_required"] is True
    assert verifier["continue_to_model"] is False
    binding = te._private_clipboard_paste_binding_from_action(
        source,
        paste,
        events[-1]["result"],
        [verifier],
        run_id="run-paste",
        tool_sequence=3,
        timeline=events,
    )
    assert binding["target_app_name"] == "WeChat"
    assert binding["target_window"] == {"app_name": "WeChat", "pid": 100, "window_id": 200}
    observation = deepcopy(events[0]["result"])
    observation["data"]["elements"][0]["value"] = source["content"]
    receipt = te._trusted_postcondition_observation_receipt_for_verifier(
        verifier,
        observation,
        events,
        tool_timeline_start=0,
        run_id="run-paste",
        private_clipboard_paste_binding=binding,
    )
    assert receipt["verification_predicate_kind"] == "exact_pasted_content_present"
    assert receipt["content_length"] == len(source["content"])
    assert receipt["target_ui_identity"]["identifier"] == "composer-1"


@pytest.mark.parametrize(
    "mutation",
    [
        "public_marker",
        "wrong_run",
        "wrong_plan",
        "wrong_request",
        "wrong_call",
        "missing_dependency",
        "no_focus",
        "multiple_focus",
        "non_editable",
        "no_identity",
        "truncated",
        "foreign_provider",
        "wrong_window",
        "expired",
        "duplicate_source",
    ],
)
def test_focused_paste_target_rejects_unbound_or_ambiguous_observation(mutation):
    requests, paste, verifier, events, source = _case()
    event, result = events[0], events[-1]["result"]
    element = event["result"]["data"]["elements"][0]
    if mutation == "public_marker":
        paste[pt._KEY] = {"target_request": paste[pt._KEY]["target_request"]}
    elif mutation == "wrong_run":
        event["run_id"] = "foreign"
    elif mutation == "wrong_plan":
        event["plan_id"] = "foreign"
    elif mutation == "wrong_request":
        event["request_id"] = "foreign"
    elif mutation == "wrong_call":
        event["tool_call_id"] = ""
    elif mutation == "missing_dependency":
        paste["depends_on"] = []
    elif mutation == "no_focus":
        element["focused"] = False
    elif mutation == "multiple_focus":
        event["result"]["data"]["elements"].append({**element, "identifier": "other"})
    elif mutation == "non_editable":
        element.update(role="AXStaticText", editable=False)
    elif mutation == "no_identity":
        element.pop("name")
        element.pop("identifier")
    elif mutation == "truncated":
        event["result"]["data"]["truncated"] = True
    elif mutation == "foreign_provider":
        event["result"].pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    elif mutation == "wrong_window":
        result["data"].update(app_name="WeChat", pid=100, window_id=999)
    elif mutation == "expired":
        events.insert(1, {**event, "step_id": "other-read", "tool_call_id": "later"})
    elif mutation == "duplicate_source":
        events.insert(0, deepcopy(event))
    assert pt.observed_clipboard_paste_target(paste, result, events, run_id="run-paste") == {}


@pytest.mark.parametrize(
    "mutation", ["wrong_bytes", "no_focus", "other_identity", "wrong_app", "wrong_window"]
)
def test_after_paste_readback_must_match_same_focused_editable_identity_and_bytes(mutation):
    requests, paste, verifier, events, source = _case()
    binding = te._private_clipboard_paste_binding_from_action(
        source,
        paste,
        events[-1]["result"],
        [verifier],
        run_id="run-paste",
        tool_sequence=3,
        timeline=events,
    )
    observation = deepcopy(events[0]["result"])
    element = observation["data"]["elements"][0]
    element["value"] = source["content"]
    if mutation == "wrong_bytes":
        element["value"] = source["content"].strip()
    elif mutation == "no_focus":
        element["focused"] = False
    elif mutation == "other_identity":
        element["identifier"] = "foreign"
    elif mutation == "wrong_app":
        observation["data"]["app_name"] = "Slack"
    elif mutation == "wrong_window":
        observation["data"]["window_id"] = 999
    assert (
        te._trusted_postcondition_observation_receipt_for_verifier(
            verifier,
            observation,
            events,
            tool_timeline_start=0,
            run_id="run-paste",
            private_clipboard_paste_binding=binding,
        )
        == {}
    )


@pytest.mark.parametrize("exact", ["clipboard content", "原文\n  literal  "])
def test_real_main_chat_paste_reads_exact_source_and_waits_before_actual_approved_send(
    tmp_path, monkeypatch, exact
):
    from tests.test_chat_api import _make_agent_runtime_service, _make_api, _send_foreground_message

    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    runtime.agent_runtime_service = service
    state = {"value": ""}
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: SimpleNamespace(get_defaults=lambda: {"chat": ""}),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *a, **kw: pytest.fail("Paste must not ask a model"),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.running_apps",
        lambda: {
            "ok": True,
            "action": "desktop.running_apps",
            "data": {"apps": [{"name": "WeChat", "pid": 100}]},
        },
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "WeChat", "pid": 100, "window_id": 200},
        },
    )

    def read(max_chars=2000):
        calls.append("read")
        return {
            "ok": True,
            "action": "clipboard.read",
            "data": {
                "text": exact,
                "text_length": len(exact),
                "max_chars": max_chars,
                "truncated": False,
            },
        }

    def ui(**kwargs):
        calls.append("ui")
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
                "elements": [
                    {
                        "role": "AXTextArea",
                        "name": "Message",
                        "identifier": "composer-1",
                        "value": state["value"],
                        "focused": True,
                        "editable": True,
                    }
                ],
            },
        }

    def paste(action):
        assert action == "paste"
        calls.append("paste")
        state["value"] = exact
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"shortcut_action": action}}

    def send(action):
        assert action == "send"
        calls.append("send")
        return {
            "ok": True,
            "action": "desktop.submit_foreground",
            "data": {"key": "return", "modifiers": [], "submit_action": "send"},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", read)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", ui)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", paste)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_submit_foreground", send)
    try:
        result = _send_foreground_message(api, "当前输入框粘贴并发送")
        assert result["status"] == "waiting_approval"
        assert calls == ["read", "ui", "paste", "ui"]
        assert "send" not in calls
        assert (
            result["agent_task"]["pending_approvals"][0]["tool_name"] == "desktop.submit_foreground"
        )
        approved = service.approve_run_approval(result["run_id"])
        assert calls.count("send") == 1, {
            k: approved.get(k) for k in ("status", "result", "error", "pending_approval")
        }
        assert calls.index("read") < calls.index("paste") < calls.index("send")
        assert approved["status"] == "completed"
        events = service.list_run_events(result["run_id"], include_internal=True)["events"]
        assert not any(e["event_type"] == "model.request.started" for e in events)
        public = service.list_run_events(result["run_id"])["events"]
        assert all(e.get("visibility") != "internal" for e in public)
        assert not any(
            (e.get("payload") or {}).get("source") == "runtime_native_postcondition_receipt"
            for e in public
        )
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "mutation", ["missing_send", "extra_step", "changed_input", "wrong_plan", "extra_goal_action"]
)
def test_paste_target_authority_requires_complete_exact_original_goal_plan(mutation):
    requests, paste, verifier, events, source = _case()
    goal = "当前输入框粘贴并发送"
    if mutation == "missing_send":
        requests[:] = [r for r in requests if r["tool"] != "desktop.submit_foreground"]
    elif mutation == "extra_step":
        requests.append({"step_id": "foreign", "tool": "app.quit", "input": {"app_name": "Slack"}})
    elif mutation == "changed_input":
        paste["input"]["action"] = "copy"
    elif mutation == "wrong_plan":
        paste["plan_id"] = "foreign"
    elif mutation == "extra_goal_action":
        goal += "，然后删除所有文件"
    pt.prepare_clipboard_paste_targets(
        requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES
    )
    assert not pt.clipboard_paste_target_is_bound(paste)


@pytest.mark.parametrize("label", ["Search", "Recipient", "Unknown field"])
def test_send_paste_never_binds_search_recipient_or_unknown_field(label):
    requests, paste, verifier, events, source = _case()
    element = events[0]["result"]["data"]["elements"][0]
    element.update(name=label, identifier="field-1")
    assert (
        pt.observed_clipboard_paste_target(paste, events[-1]["result"], events, run_id="run-paste")
        == {}
    )


def test_compiled_submit_covers_send_and_keeps_unplanned_extra_actions_blocked():
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    for goal in ("当前输入框粘贴并发送", "打开微信粘贴后发送"):
        selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
        assert not planner_selection_needs_model_assistance(selection, goal)
        assert planner_selection_needs_model_assistance(selection, goal + "，然后删除所有文件")


@pytest.mark.parametrize(
    "mutation", ["foreign_app", "empty_app", "tampered_target", "reordered_steps"]
)
def test_named_paste_target_comes_from_immutable_goal_and_exact_order(mutation):
    requests, paste, verifier, events, source = _case("打开微信粘贴后发送")
    if mutation == "foreign_app":
        events[0]["result"]["data"]["app_name"] = "Slack"
    elif mutation == "empty_app":
        events[0]["result"]["data"]["app_name"] = ""
    elif mutation == "tampered_target":
        paste["action_target"] = {"app_name": "Slack"}
    elif mutation == "reordered_steps":
        requests[0], requests[1] = requests[1], requests[0]
        pt.prepare_clipboard_paste_targets(
            requests, user_goal="打开微信粘贴后发送", allowed_tools=DAILY_DESKTOP_TOOL_NAMES
        )
    assert (
        pt.observed_clipboard_paste_target(paste, events[-1]["result"], events, run_id="run-paste")
        == {}
    )
