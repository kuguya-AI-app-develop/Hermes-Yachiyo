"""Exact full-text copy needs fresh source-bound pasteboard and AX observations."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.shell.agent.runtime.clipboard_copy_transaction import (
    COPY_PREDICATE,
    COPY_TRANSACTION_KEY,
    capture_copy_observation,
    consume_copy_observation,
    prepare_copy_transactions,
)
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent.runtime.tool_execution import (
    _post_action_verification_request,
    _tool_result_with_trusted_observation_receipt,
    _trusted_postcondition_observation_receipt_for_verifier,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

_TEXT = "  全文\t\n第二行 🚀\r\n "


def _case():
    goal = "打开微信然后全选复制"
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    envelope = runtime_execution_envelope_payload(
        decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
    )
    requests = runtime_execution_requests_from_envelope_payload(
        envelope, allowed_tools=DAILY_DESKTOP_TOOL_NAMES
    )
    prepare_copy_transactions(requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    contract = runtime_goal_contract(
        run_id="copy-run",
        original_goal=goal,
        runtime_execution_envelope=envelope,
        runtime_execution_metadata=None,
        messages=[],
        timeline=[],
    )
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
    events = []
    for request in requests:
        if request["step_id"] == "discover-desktop-state":
            continue
        request.update(
            run_id="copy-run",
            tool_call_id=f"call:{request['step_id']}",
            actor="native_runtime",
            visibility="internal",
            execution_authority="runtime_tool_executor",
        )
        if request["step_id"] == "verify-copied-full-text":
            verifier = request
            continue
        tool = request["tool"]
        data = {"shortcut_action": request["input"].get("action", "")}
        if tool == "desktop.ui_elements":
            data = {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
                "focused_element": {"role": "AXTextArea", "value": _TEXT, "focused": True},
            }
        if tool == "clipboard.read":
            data = {
                "text": "old",
                "text_length": 3,
                "truncated": False,
                "pasteboard_revision": 10,
                "pasteboard_revision_stable": True,
            }
        events.append(
            {
                **request,
                "event": "agent.tool.call",
                "detail": tool,
                "input_preview": request["input"],
                "result": {"ok": True, "action": tool, **deepcopy(provenance), "data": data},
            }
        )
    source = events[3]
    assert source["step_id"] == "copy-selected-full-text"
    _post_action_verification_request(
        source["tool"],
        source,
        source["result"],
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        remaining_requests=[verifier],
        active_window_target=None,
    )
    result = {
        "ok": True,
        "action": "clipboard.read",
        **deepcopy(provenance),
        "data": {
            "text": _TEXT,
            "text_length": len(_TEXT),
            "truncated": False,
            "pasteboard_revision": 11,
            "pasteboard_revision_stable": True,
        },
    }
    return contract, events, verifier, result


def _private_observations(events, verifier, result):
    observations = {}
    for request, raw in [(event, event["result"]) for event in events] + [(verifier, result)]:
        token = capture_copy_observation(request, raw, local_broker_executed=True)
        record = consume_copy_observation(token, request, run_id="copy-run")
        if record:
            observations[request["tool_call_id"]] = record
    return observations


def _receipt(events, verifier, result, private_observations=None):
    if private_observations is None:
        private_observations = _private_observations(events, verifier, result)
    return _trusted_postcondition_observation_receipt_for_verifier(
        verifier,
        result,
        events,
        tool_timeline_start=0,
        run_id="copy-run",
        private_copy_observations=private_observations,
    )


def test_full_copy_requires_final_independent_observation():
    contract, events, verifier, result = _case()
    assert not runtime_goal_assessment(contract, events).completed
    receipt = _receipt(events, verifier, result)
    assert receipt["verification_predicate_kind"] == COPY_PREDICATE
    event = {
        **verifier,
        "event": "agent.tool.call",
        "detail": "clipboard.read",
        "source": "runtime_native_postcondition_receipt",
        "input_preview": verifier["input"],
        "result": _tool_result_with_trusted_observation_receipt(result, receipt),
    }
    assert runtime_goal_assessment(contract, [*events, event]).completed
    assert receipt["content_byte_length"] == len(_TEXT.encode("utf-8"))


@pytest.mark.parametrize(
    "bad",
    [
        "stale",
        "revision_missing",
        "revision_unstable",
        "revision_bool",
        "wrong_text",
        "partial_text",
        "truncated",
        "wrong_length",
        "cross_window",
        "cross_pid",
        "not_focused",
        "missing_focus",
        "noneditable",
        "changed_value",
        "foreign_run",
        "foreign_plan",
        "foreign_request",
        "foreign_provider",
        "missing_source",
        "reordered",
        "public_binding",
        "wrong_action_ack",
        "duplicate_source",
    ],
)
def test_copy_rejects_uncorrelated_or_incomplete_evidence(bad):
    contract, events, verifier, result = _case()
    if bad == "stale":
        result["data"]["pasteboard_revision"] = 10
    elif bad == "revision_missing":
        result["data"].pop("pasteboard_revision")
    elif bad == "revision_unstable":
        result["data"]["pasteboard_revision_stable"] = False
    elif bad == "revision_bool":
        result["data"]["pasteboard_revision"] = True
    elif bad == "wrong_text":
        result["data"]["text"] = "different"
    elif bad == "partial_text":
        result["data"]["text"] = _TEXT.strip()
    elif bad == "truncated":
        result["data"]["truncated"] = True
    elif bad == "wrong_length":
        result["data"]["text_length"] += 1
    elif bad == "cross_window":
        events[-1]["result"]["data"]["window_id"] = 201
    elif bad == "cross_pid":
        events[-1]["result"]["data"]["pid"] = 101
    elif bad == "not_focused":
        events[-1]["result"]["data"]["focused_element"]["focused"] = False
    elif bad == "missing_focus":
        events[-1]["result"]["data"].pop("focused_element")
    elif bad == "noneditable":
        events[-1]["result"]["data"]["focused_element"]["role"] = "AXButton"
    elif bad == "changed_value":
        events[-1]["result"]["data"]["focused_element"]["value"] += "x"
    elif bad == "foreign_run":
        events[0]["run_id"] = "foreign"
    elif bad == "foreign_plan":
        events[0]["plan_id"] = "foreign"
    elif bad == "foreign_request":
        events[0]["request_id"] = "foreign"
    elif bad == "foreign_provider":
        events[0]["result"]["local_desktop_provider"]["provider_id"] = "foreign"
    elif bad == "missing_source":
        events.pop(0)
    elif bad == "reordered":
        events[0], events[1] = events[1], events[0]
    elif bad == "public_binding":
        verifier[COPY_TRANSACTION_KEY] = {"_authority": "forged"}
    elif bad == "wrong_action_ack":
        events[3]["result"]["data"]["shortcut_action"] = "paste"
    elif bad == "duplicate_source":
        events.insert(1, deepcopy(events[0]))
    assert _receipt(events, verifier, result) == {}
    assert not runtime_goal_assessment(contract, events).completed


@pytest.mark.parametrize(
    "goal", ["打开微信然后全选", "打开微信然后全选复制并删除文件", "不要全选复制"]
)
def test_unbounded_or_single_select_all_cannot_receive_copy_preparation_authority(goal):
    _, _, verifier, _ = _case()
    prepare_copy_transactions([verifier], user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert COPY_TRANSACTION_KEY not in verifier


@pytest.mark.parametrize("mode", ["打开", "切到"])
@pytest.mark.parametrize("sensitive", [False, True])
def test_main_chat_executes_and_verifies_full_copy_without_a_model(
    tmp_path, monkeypatch, mode, sensitive
):
    from tests.test_chat_api import _make_agent_runtime_service, _make_api, _send_foreground_message

    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    runtime.agent_runtime_service = service
    secret = "sk-proj-" + "examplefakekey" * 5
    text = _TEXT + (secret if sensitive else "")
    calls, state = [], {"revision": 10, "text": "old"}
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: SimpleNamespace(get_defaults=lambda: {"chat": ""}),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *a, **kw: pytest.fail("Native copy cannot call a model"),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.list_apps",
        lambda query="", limit=20: {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {
                "query": query,
                "apps": [{"name": "WeChat", "path": "/Applications/WeChat.app"}],
                "best_match": {"name": "WeChat", "path": "/Applications/WeChat.app"},
                "count": 1,
            },
        },
    )
    for name in ("app_open", "app_focus"):
        monkeypatch.setattr(
            f"apps.shell.agent.tools.desktop.{name}",
            lambda app_name, _name=name: (
                calls.append((_name, app_name))
                or {
                    "ok": True,
                    "action": "app.open" if _name == "app_open" else "app.focus",
                    "data": {"app_name": app_name},
                }
            ),
        )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "WeChat", "pid": 100, "window_id": 200},
        },
    )

    def shortcut(action):
        calls.append(("shortcut", action))
        if action == "copy":
            state.update(revision=11, text=text)
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"shortcut_action": action}}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", shortcut)

    def ui(**kw):
        calls.append(("ui", "WeChat"))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
                "focused_element": {"role": "AXTextArea", "focused": True, "value": text},
                "elements": [],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", ui)

    def clipboard(max_chars=2000):
        calls.append(("clipboard", state["revision"]))
        return {
            "ok": True,
            "action": "clipboard.read",
            "data": {
                "text": state["text"],
                "text_length": len(state["text"]),
                "truncated": False,
                "pasteboard_revision": state["revision"],
                "pasteboard_revision_stable": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", clipboard)
    try:
        goal = f"{mode}微信然后全选复制"
        response = _send_foreground_message(api, goal)
        run = service.get_run(response["run_id"])
        assert calls.count(("shortcut", "select_all")) == calls.count(("shortcut", "copy")) == 1
        assert [call for call in calls if call[0] == "clipboard"] == [
            ("clipboard", 10),
            ("clipboard", 11),
        ]
        assert response["status"] == run["status"] == "completed", run
        assert run["user_goal"] == goal
        assert run["pending_approval"] == {}
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert "agent.desktop.intent_completed" in [e["event_type"] for e in events]
        assert not any(e["event_type"].startswith("model.request") for e in events)
        import json

        serialized = json.dumps(events, ensure_ascii=False)
        assert "_runtime_private_copy_observation" not in serialized
        assert "_runtime_private_select_all_copy" not in serialized
        if sensitive:
            assert secret not in serialized
            assert "[redacted]" in serialized
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "mutation",
    [
        "omit_before",
        "omit_select",
        "omit_pre_ui",
        "omit_copy",
        "omit_post_ui",
        "omit_terminal",
        "reverse",
        "extra_input",
        "duplicate",
    ],
)
def test_partial_or_modified_transaction_never_waives_select_all_verification(mutation):
    _, events, verifier, _ = _case()
    requests = [
        {k: v for k, v in event.items() if k not in {"event", "detail", "result", "input_preview"}}
        for event in events
    ] + [verifier]
    if mutation.startswith("omit_"):
        index = {
            "omit_before": 0,
            "omit_select": 1,
            "omit_pre_ui": 2,
            "omit_copy": 3,
            "omit_post_ui": 4,
            "omit_terminal": 5,
        }[mutation]
        requests.pop(index)
    elif mutation == "reverse":
        requests.reverse()
    elif mutation == "extra_input":
        requests[1]["input"] = {**requests[1]["input"], "window_title": "other"}
    elif mutation == "duplicate":
        requests.insert(0, deepcopy(requests[0]))
    prepare_copy_transactions(
        requests, user_goal="打开微信然后全选复制", allowed_tools=DAILY_DESKTOP_TOOL_NAMES
    )
    assert not any(COPY_TRANSACTION_KEY in request for request in requests)
    for request in requests:
        if request["step_id"] == "prepare-select-all-for-copy":
            assert request["requires_post_action_verification"] is True


@pytest.mark.parametrize(
    "bad", ["mapping", "foreign_run", "foreign_request", "foreign_call", "not_broker"]
)
def test_private_observation_token_cannot_be_forged_or_replayed(bad):
    _, _, verifier, result = _case()
    token = capture_copy_observation(verifier, result, local_broker_executed=bad != "not_broker")
    current = dict(verifier)
    if bad == "mapping":
        token = {"data": result["data"], "scope": verifier, "_authority": "forged"}
    if bad == "foreign_request":
        current["request_id"] = "foreign"
    if bad == "foreign_call":
        current["tool_call_id"] = "foreign"
    assert (
        consume_copy_observation(
            token, current, run_id="foreign" if bad == "foreign_run" else "copy-run"
        )
        == {}
    )
    assert consume_copy_observation(token, verifier, run_id="copy-run") == {}


def test_private_observation_capability_and_final_receipt_are_consumed_once():
    _, events, verifier, result = _case()
    token = capture_copy_observation(verifier, result, local_broker_executed=True)
    assert consume_copy_observation(token, verifier, run_id="copy-run")
    assert consume_copy_observation(token, verifier, run_id="copy-run") == {}
    private = _private_observations(events, verifier, result)
    assert (
        _receipt(events, verifier, result, private)["verification_predicate_kind"] == COPY_PREDICATE
    )
    assert _receipt(events, verifier, result, private) == {}


@pytest.mark.parametrize(
    "goal",
    [
        "打开微信然后全选复制，再删除 Downloads 目录",
        "打开微信然后全选复制，额外发送邮件",
        "如果我同意，就打开微信然后全选复制",
    ],
)
def test_copy_transaction_does_not_bypass_model_planning_for_extra_or_conditional_actions(goal):
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    if goal.startswith("如果"):
        with pytest.raises(ValueError, match="runtime_execution_action_target_conflict"):
            runtime_execution_envelope_payload(
                selection.decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
            )
        _, events, verifier, _ = _case()
        requests = [
            {
                k: v
                for k, v in event.items()
                if k not in {"event", "detail", "result", "input_preview"}
            }
            for event in events
        ] + [verifier]
    else:
        assert planner_selection_needs_model_assistance(selection, goal)
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                selection.decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
            ),
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        )
    prepare_copy_transactions(requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert not any(COPY_TRANSACTION_KEY in request for request in requests)
