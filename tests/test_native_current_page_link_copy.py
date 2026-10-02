"""Current-page link copy observes a real address control and fresh pasteboard."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.shell.agent.runtime.clipboard_copy_transaction import (
    COPY_TRANSACTION_KEY,
    capture_copy_observation,
    consume_copy_observation,
    prepare_copy_transactions,
)
from apps.shell.agent.runtime.current_page_link_copy import (
    PAGE_LINK_COPY_STEP,
    PAGE_LINK_PREDICATE,
    PAGE_LINK_VERIFY_STEP,
    page_link_copy_bound,
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
from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

_URL = "https://example.test/路径?q=精确%20url"


def _native_ui(url=_URL, *, focused=False):
    output = (
        "META\tGoogle Chrome\t100\tPage\t200\n"
        f"1\tAXTextField\t\t\tAddress and search bar\t{url}\ttrue\t0\t0\t100\t40"
    )
    if focused:
        output += "\nFOCUSED\t" + json.dumps(
            {
                "app_name": "Google Chrome",
                "pid": 100,
                "window_id": 200,
                "role": "AXTextField",
                "description": "Address and search bar",
                "value": url,
                "focused": True,
            }
        )
    return desktop._parse_ui_elements_output(output)


def _case(goal="copy current page link"):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    envelope = runtime_execution_envelope_payload(
        decision,
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        full_plan=True,
    )
    requests = runtime_execution_requests_from_envelope_payload(
        envelope,
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    prepare_copy_transactions(requests, user_goal=goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    contract = runtime_goal_contract(
        run_id="link-run",
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
        if not page_link_copy_bound(request):
            continue
        request.update(
            run_id="link-run",
            tool_call_id=f"call:{request['step_id']}",
            actor="native_runtime",
            execution_authority="runtime_tool_executor",
        )
        tool, step = request["tool"], request["step_id"]
        data = {"shortcut_action": "copy_current_page_link"}
        if tool == "desktop.ui_elements":
            data = _native_ui(focused=step == "read-page-link-target-ui")
        if tool == "clipboard.read":
            text = _URL if step == PAGE_LINK_VERIFY_STEP else "old"
            data = {
                "text": text,
                "text_length": len(text),
                "truncated": False,
                "pasteboard_revision": 11 if step == PAGE_LINK_VERIFY_STEP else 10,
                "pasteboard_revision_stable": True,
            }
        result = {"ok": True, "action": tool, **deepcopy(provenance), "data": data}
        if step == PAGE_LINK_VERIFY_STEP:
            verifier, observed = request, result
        else:
            events.append(
                {
                    **request,
                    "event": "agent.tool.call",
                    "detail": tool,
                    "input_preview": request["input"],
                    "result": result,
                }
            )
    copy = next(e for e in events if e["step_id"] == PAGE_LINK_COPY_STEP)
    _post_action_verification_request(
        copy["tool"],
        copy,
        copy["result"],
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        remaining_requests=[verifier],
        active_window_target=None,
    )
    return contract, events, verifier, observed


def _receipt(events, verifier, observed, *, actual_broker=True):
    private = {}
    for request, result in [(e, e.get("result") or {}) for e in events] + [(verifier, observed)]:
        token = capture_copy_observation(request, result, local_broker_executed=actual_broker)
        record = consume_copy_observation(token, request, run_id="link-run")
        if record:
            private[request["tool_call_id"]] = record
    return _trusted_postcondition_observation_receipt_for_verifier(
        verifier,
        observed,
        events,
        tool_timeline_start=0,
        run_id="link-run",
        private_copy_observations=private,
    )


@pytest.mark.parametrize("goal", ["copy current page link", "复制当前网页链接"])
def test_native_address_and_new_exact_pasteboard_complete_the_original_copy_goal(goal):
    contract, events, verifier, observed = _case(goal)
    assert "focused_element" not in events[1]["result"]["data"]
    assert not runtime_goal_assessment(contract, events).completed
    receipt = _receipt(events, verifier, observed)
    assert receipt["verification_predicate_kind"] == PAGE_LINK_PREDICATE
    assert receipt["content_byte_length"] == len(_URL.encode())
    final = {
        **verifier,
        "event": "agent.tool.call",
        "detail": verifier["tool"],
        "source": "runtime_native_postcondition_receipt",
        "input_preview": verifier["input"],
        "result": _tool_result_with_trusted_observation_receipt(observed, receipt),
    }
    assert runtime_goal_assessment(contract, [*events, final]).completed


@pytest.mark.parametrize(
    "bad",
    [
        "stale",
        "missing_revision",
        "unstable_revision",
        "wrong_bytes",
        "partial",
        "truncated",
        "scheme_elided",
        "body_url",
        "ambiguous_address",
        "missing_post_focus",
        "changed_url",
        "other_window",
        "other_app",
        "missing_window",
        "cross_run",
        "cross_plan",
        "cross_request",
        "cross_decision",
        "cross_tool_plan",
        "other_provider",
        "duplicate_source",
        "wrong_order",
        "interleaved_action",
        "late_action",
        "misleading_label",
        "invalid_utf8",
        "ack_claim",
        "no_actual_broker",
        "fallback_outcome",
        "truncated_outcome",
    ],
)
def test_url_copy_rejects_unobserved_or_foreign_addresses_and_clipboard_evidence(bad):
    contract, events, verifier, observed = _case()
    before, after = events[1]["result"]["data"], events[3]["result"]["data"]
    clipboard = observed["data"]
    if bad == "stale":
        clipboard["pasteboard_revision"] = 10
    elif bad == "missing_revision":
        clipboard.pop("pasteboard_revision")
    elif bad == "unstable_revision":
        clipboard["pasteboard_revision_stable"] = False
    elif bad == "wrong_bytes":
        clipboard["text"] = _URL + "#other"
    elif bad == "partial":
        clipboard["text"] = _URL[:8]
    elif bad == "truncated":
        clipboard["truncated"] = True
    elif bad == "scheme_elided":
        before["elements"][0]["value"] = "example.test/path"
    elif bad == "body_url":
        before["elements"][0]["description"] = "Page content"
    elif bad == "ambiguous_address":
        before["elements"].append(deepcopy(before["elements"][0]))
    elif bad == "missing_post_focus":
        after.pop("focused_element")
    elif bad == "changed_url":
        after["focused_element"]["value"] = _URL + "#new"
    elif bad == "other_window":
        after["window_id"] = 201
    elif bad == "other_app":
        after["app_name"] = "Safari"
    elif bad == "missing_window":
        before.pop("window_id")
    elif bad == "cross_run":
        events[1]["run_id"] = "other"
    elif bad == "cross_plan":
        events[1]["plan_id"] = "other"
    elif bad == "cross_request":
        events[1]["request_id"] = "other"
    elif bad == "cross_decision":
        events[1]["decision_id"] = "other"
    elif bad == "cross_tool_plan":
        events[1]["tool_plan_id"] = "other"
    elif bad == "other_provider":
        events[1]["result"]["local_desktop_provider"]["provider_id"] = "other"
    elif bad == "duplicate_source":
        events.insert(1, deepcopy(events[1]))
    elif bad == "wrong_order":
        events[0], events[1] = events[1], events[0]
    elif bad in {"interleaved_action", "late_action"}:
        mutation = {
            "event": "agent.tool.call",
            "step_id": "other-action",
            "detail": "desktop.type_text",
        }
        events.insert(2 if bad == "interleaved_action" else len(events), mutation)
    elif bad == "misleading_label":
        before["elements"][0]["description"] = "Page content about Address bar"
    elif bad == "invalid_utf8":
        before["elements"][0]["value"] = "https://example.test/\ud800"
    elif bad == "ack_claim":
        after.pop("focused_element")
        events[2]["result"].update(postcondition_verified=True, clipboard_source_verified=True)
    elif bad == "fallback_outcome":
        events[1]["result"]["fallback_used"] = True
    elif bad == "truncated_outcome":
        events[1]["result"]["truncated"] = True
    assert _receipt(events, verifier, observed, actual_broker=bad != "no_actual_broker") == {}
    assert not runtime_goal_assessment(contract, events).completed


@pytest.mark.parametrize(
    "bad",
    [
        "missing",
        "reordered",
        "duplicate",
        "extra_input",
        "extra_action",
        "cross_decision",
        "cross_tool_plan",
    ],
)
def test_partial_or_changed_actual_plan_never_receives_url_copy_capability(bad):
    _, events, verifier, _ = _case()
    requests = [dict(e) for e in events] + [dict(verifier)]
    if bad == "missing":
        requests.pop(0)
    elif bad == "reordered":
        requests.reverse()
    elif bad == "duplicate":
        requests.append(dict(requests[0]))
    elif bad == "extra_input":
        requests[1]["input"] = {"limit": 80, "app_name": "other"}
    elif bad == "extra_action":
        requests.append({"tool": "desktop.submit_foreground", "input": {"action": "send"}})
    elif bad == "cross_decision":
        requests[1]["decision_id"] = "other"
    elif bad == "cross_tool_plan":
        requests[1]["tool_plan_id"] = "other"
    prepare_copy_transactions(
        requests, user_goal="copy current page link", allowed_tools=DAILY_DESKTOP_TOOL_NAMES
    )
    assert not any(COPY_TRANSACTION_KEY in r for r in requests)


@pytest.mark.parametrize(
    "goal",
    ["不要复制当前网页链接", "copy current page link then send it", "复制当前网页链接并删除文件"],
)
def test_extra_actions_and_negation_do_not_receive_the_native_page_copy_transaction(goal):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert not any(s.step_id == PAGE_LINK_COPY_STEP for s in decision.plan.tool_plan.steps)


@pytest.mark.parametrize("bad", ["mapping", "replay", "cross_run", "cross_request"])
def test_url_observation_capability_is_exact_scope_and_one_use(bad):
    _, events, _, _ = _case()
    request = events[1]
    token = capture_copy_observation(request, request["result"], local_broker_executed=True)
    changed = dict(request)
    if bad == "mapping":
        token = {"scope": request, "data": request["result"]["data"]}
    elif bad == "cross_run":
        changed["run_id"] = "other"
    elif bad == "cross_request":
        changed["request_id"] = "other"
    if bad == "replay":
        assert consume_copy_observation(token, request, run_id="link-run")
    assert consume_copy_observation(token, changed, run_id="link-run") == {}
    assert consume_copy_observation(token, request, run_id="link-run") == {}


@pytest.mark.parametrize("mode", ["normal", "secret", "scheme_elided", "wrong_clipboard"])
def test_actual_main_chat_copies_only_the_verified_native_parser_url(tmp_path, monkeypatch, mode):
    from tests.test_chat_api import _make_agent_runtime_service, _make_api, _send_foreground_message

    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    runtime.agent_runtime_service = service
    secret = "sk-proj-" + "examplefakekey" * 5
    url = _URL + ("&token=" + secret if mode == "secret" else "")
    calls, state = [], {"copied": False}
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: SimpleNamespace(get_defaults=lambda: {"chat": ""}),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *a, **kw: pytest.fail("Native URL copy cannot call a model"),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.running_apps",
        lambda **kw: {
            "ok": True,
            "action": "desktop.running_apps",
            "data": {"apps": [{"name": "Google Chrome"}]},
        },
    )

    def ui(**kw):
        calls.append("ui")
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": _native_ui(
                "example.test/path" if mode == "scheme_elided" else url,
                focused=state["copied"],
            ),
        }

    def read(max_chars=2000):
        calls.append("clipboard")
        text = ("different" if mode == "wrong_clipboard" else url) if state["copied"] else "old"
        return {
            "ok": True,
            "action": "clipboard.read",
            "data": {
                "text": text,
                "text_length": len(text),
                "truncated": False,
                "pasteboard_revision": 11 if state["copied"] else 10,
                "pasteboard_revision_stable": True,
            },
        }

    def copy(action):
        assert action == "copy_current_page_link"
        calls.append("copy")
        state["copied"] = True
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"shortcut_action": action}}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", ui)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", read)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", copy)
    try:
        response = _send_foreground_message(api, "copy current page link")
        run = service.get_run(response["run_id"])
        assert run["status"] == ("completed" if mode in {"normal", "secret"} else "failed"), run
        assert calls == (
            ["clipboard", "ui"]
            if mode == "scheme_elided"
            else ["clipboard", "ui", "copy", "ui", "clipboard"]
        )
        assert run["pending_approval"] == {}
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert not any(e["event_type"].startswith("model.request") for e in events)
        serialized = json.dumps(events, ensure_ascii=False)
        assert "_runtime_private_" not in serialized
        assert secret not in serialized
        assert (
            not any(e["event_type"] == "agent.desktop.intent_completed" for e in events)
            if mode in {"scheme_elided", "wrong_clipboard"}
            else True
        )
    finally:
        service.close()
        store.close()
