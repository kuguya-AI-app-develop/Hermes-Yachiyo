"""Actual Native Bridge typed drafts require source AX and approved exact send."""

import json
from types import SimpleNamespace

import pytest

from apps.core.chat_session import ChatSession
from apps.core.chat_store import ChatStore
from apps.core.state import AppState
from apps.shell.agent.tools import desktop
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.chat_bridge import ChatBridge
from apps.shell.credential_store import MemoryCredentialStore


class _NoProfile:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def _fixture(tmp_path, monkeypatch, *, mode="good", native_shape=False):
    state = {
        "find": False,
        "selected": False,
        "typed": "",
        "body": "",
        "sent": 0,
        "calls": [],
        "mode": mode,
    }

    def focus(app):
        state["calls"].append(("focus", app))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app}}

    def shortcut(action):
        state["find"] = action == "find"
        state["calls"].append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "data": {"shortcut_action": action, "key": "f", "modifiers": ["command"]},
        }

    def typing(text):
        state["calls"].append(("type", text))
        state["typed"] = text
        if state["selected"]:
            state["body"] = text
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def search():
        state["selected"] = True
        state["find"] = False
        state["calls"].append(("search", ""))
        return {"ok": True, "action": "desktop.search_submit", "data": {"key": "return"}}

    def ui(**kwargs):
        selected = state["selected"]
        role = "AXTextArea" if selected else "AXTextField"
        name = "Message" if selected else "Search"
        value = state["body"] if selected else state["typed"]
        if selected and state["mode"] in {"search_composer", "unknown_composer"}:
            name = "Search" if state["mode"] == "search_composer" else "Unknown"
        focused = {
            "role": role,
            "name": name,
            "value": value,
            "focused": True,
            "editable": True,
            "depth": 1,
        }
        elems = [focused]
        if selected:
            if state["mode"] == "search_results":
                elems = [
                    {"role": "AXTable", "name": "Search Results", "depth": 0},
                    {"role": "AXRow", "name": "张三", "depth": 1},
                    focused,
                ]
            else:
                recipient = "李四" if state["mode"] == "wrong_recipient" else "张三"
                elems = [
                    {
                        "role": "AXStaticText",
                        "name": recipient,
                        "value": recipient,
                        "description": "Conversation header",
                        "depth": 1,
                    },
                    focused,
                ]
        data = {
            "app_name": "WeChat",
            "pid": 100,
            "window_id": 200,
            "elements": elems,
            "focused_element": focused,
        }
        if selected and state["mode"] == "missing_window":
            data.pop("window_id")
        if selected and state["body"] and state["mode"] == "post_window_drift":
            data["window_id"] = 201
        if selected and state["body"] and state["mode"] == "post_target_drift":
            focused["name"] = "Other Message"
        if native_shape:
            native_focus = {
                **focused,
                "app_name": data["app_name"],
                "pid": data["pid"],
                "window_id": data.get("window_id"),
            }
            lines = [
                "META\tWeChat\t100\tWeChat\t" + str(data.get("window_id") or 0),
                "FOCUSED\t" + json.dumps(native_focus),
            ]
            for element in elems:
                clean_value = " ".join(str(element.get("value") or "").split())
                lines.append(
                    "\t".join(
                        (
                            str(element.get("depth", 1)),
                            element["role"],
                            "",
                            element.get("name", ""),
                            element.get("description", ""),
                            clean_value,
                            "true",
                            "",
                            "",
                            "",
                            "",
                        )
                    )
                )
            data = desktop._parse_ui_elements_output("\n".join(lines))
            assert all("focused" not in element for element in data["elements"])
        return {"ok": True, "action": "desktop.ui_elements", "data": data}

    def active():
        return {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "WeChat", "pid": 100, "window_id": 200},
        }

    def send(*_a, **_kw):
        state["sent"] += 1
        state["body"] = ""
        state["calls"].append(("send", ""))
        return {
            "ok": True,
            "action": "desktop.submit_foreground",
            "data": {"key": "return", "modifiers": [], "submit_action": "send"},
        }

    for key, fn in {
        "app_focus": focus,
        "desktop_safe_shortcut": shortcut,
        "desktop_safe_type_text": typing,
        "desktop_search_submit": search,
        "ui_elements": ui,
        "active_window": active,
        "desktop_submit_foreground": send,
    }.items():
        monkeypatch.setattr(desktop, key, fn)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _NoProfile())
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_a, **_kw: pytest.fail("No model for explicit bound draft"),
    )
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    session = ChatSession(session_id="current")
    session.attach_store(store, load_existing=False)
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime = SimpleNamespace(
        state=AppState(),
        chat_session=session,
        task_runner=None,
        agent_runtime_service=service,
        store=store,
    )
    return ChatBridge(runtime), service, store, state


def _send(bridge):
    return bridge.send_quick_message(
        "微信给张三说你好",
        metadata={
            "source": "launcher",
            "launcher_mode": "bubble",
            "launcher_surface": "quick_message",
            "allow_user_foreground_takeover": True,
        },
    )


def test_real_bridge_typed_source_waits_for_approval_and_sends_only_after_actual_approval(
    tmp_path, monkeypatch
):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        run = service.get_run(run_id)
        assert run["user_goal"] == "微信给张三说你好"
        assert run["status"] == "approval_required", run
        assert state["sent"] == 0
        assert ("type", "你好") in state["calls"]
        pending = run["pending_approval"]
        approved = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 1
        assert approved["status"] == "completed", approved
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "mode",
    ["search_results", "wrong_recipient", "search_composer", "unknown_composer", "missing_window"],
)
def test_unbound_recipient_or_composer_blocks_body_before_typing(tmp_path, monkeypatch, mode):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, mode=mode)
    try:
        result = _send(bridge)
        run = service.get_run(service.get_task_run_link(result["task_id"])["run_id"])
        assert run["status"] == "failed"
        assert not run["pending_approval"]
        assert ("type", "你好") not in state["calls"]
        assert state["sent"] == 0
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("mode", ["post_window_drift", "post_target_drift"])
def test_typing_observation_drift_does_not_authorize_send(tmp_path, monkeypatch, mode):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, mode=mode)
    try:
        result = _send(bridge)
        run = service.get_run(service.get_task_run_link(result["task_id"])["run_id"])
        assert ("type", "你好") in state["calls"]
        assert run["status"] == "failed" and not run["pending_approval"]
        assert state["sent"] == 0
    finally:
        service.close()
        store.close()


def test_recipient_changed_after_approval_prompt_is_not_sent(tmp_path, monkeypatch):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        pending = service.get_run(run_id)["pending_approval"]
        state["mode"] = "wrong_recipient"
        approved = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 0
        assert approved["status"] == "failed"
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("opening,closing", [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")])
def test_quoted_body_bytes_survive_native_typing_approval_and_send(
    tmp_path, monkeypatch, opening, closing
):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    body = "  literal\n\t汉字🤖 tail  "
    goal = f"微信给张三说{opening}{body}{closing}"
    try:
        result = bridge.send_quick_message(
            goal,
            metadata={
                "source": "launcher",
                "allow_user_foreground_takeover": True,
            },
        )
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        run = service.get_run(run_id)
        assert run["user_goal"] == goal
        assert run["status"] == "approval_required", run["result"]
        assert state["body"] == body, (run["status"], run["result"], state["calls"])
        pending = run["pending_approval"]
        approved = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 1
        assert approved["status"] == "completed", approved
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "body",
    [
        "  literal\n\t汉字🤖 tail  ",
        r"literal\n\tquoted\"bytes",
    ],
)
def test_native_focused_raw_text_preserves_bytes_through_private_receipt_and_resume(
    tmp_path,
    monkeypatch,
    body,
):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    goal = f"微信给张三说“{body}”"
    try:
        result = bridge.send_quick_message(
            goal,
            metadata={
                "source": "launcher",
                "allow_user_foreground_takeover": True,
            },
        )
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        run = service.get_run(run_id)
        assert state["body"] == body, (run["status"], run["result"], state["calls"])
        assert run["status"] == "approval_required", run["result"]
        approved = service.approve_run_approval(run_id, run["pending_approval"]["approval_id"])
        assert state["sent"] == 1
        assert approved["status"] == "completed", approved
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("tamper", ["body", "missing_pre", "different_plan", "extra_action"])
def test_public_or_mutated_plans_cannot_mint_typed_target_authority(tmp_path, monkeypatch, tamper):
    from apps.shell.agent.runtime.tool_execution import RuntimeToolRequestRunner

    original = RuntimeToolRequestRunner.run

    def changed(self, requests, *args, **kwargs):
        for request in requests:
            if request.get("step_id") == "draft-communication-message":
                if tamper == "body":
                    request["input"]["text"] = "NOT AUTHORIZED"
                elif tamper == "different_plan":
                    request["plan_id"] = "foreign-plan"
        if tamper == "missing_pre":
            requests[:] = [
                r
                for r in requests
                if r.get("step_id") != "inspect-typed-draft-draft-communication-message"
            ]
        if tamper == "extra_action":
            requests.append({"tool": "desktop.safe_type_text", "input": {"text": "NOT AUTHORIZED"}})
        return original(self, requests, *args, **kwargs)

    monkeypatch.setattr(RuntimeToolRequestRunner, "run", changed)
    bridge, service, store, state = _fixture(tmp_path, monkeypatch)
    try:
        result = _send(bridge)
        run = service.get_run(service.get_task_run_link(result["task_id"])["run_id"])
        assert run["status"] == "failed" and not run["pending_approval"]
        assert ("type", "NOT AUTHORIZED") not in state["calls"]
        assert ("type", "你好") not in state["calls"]
        assert state["sent"] == 0
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "field", ["run_id", "plan_id", "step_id", "request_id", "tool_call_id", "tool"]
)
def test_typed_raw_observation_tokens_reject_scope_drift_and_are_single_use(field):
    from apps.shell.agent.runtime import typed_draft_target as target

    request = {
        "run_id": "run",
        "plan_id": "plan",
        "step_id": "step",
        "request_id": "request",
        "tool_call_id": "call",
        "tool": "desktop.ui_elements",
        target.OBSERVATION_REQUEST_KEY: {"_authority": target._AUTHORITY},
    }
    token = target.capture_typed_observation(
        request,
        {"ok": True, "data": {"focused_element": {"value": " literal\r\n "}}},
        local_broker_executed=True,
    )
    changed = {**request, field: "foreign"}
    assert target.consume_typed_observation(token, changed, run_id="run") == {}
    assert target.consume_typed_observation(token, request, run_id="run") == {}


@pytest.mark.parametrize(
    "binding,local",
    [
        ({"_authority": "runtime"}, True),
        ({"_authority": None}, True),
        ({}, True),
        (None, True),
        ("metadata", True),
    ],
)
def test_serializable_bindings_cannot_capture_native_exact_typed_observation(binding, local):
    from apps.shell.agent.runtime import typed_draft_target as target

    request = {"tool": "desktop.ui_elements", target.OBSERVATION_REQUEST_KEY: binding}
    assert (
        target.capture_typed_observation(
            request,
            {"ok": True, "data": {"focused_element": {"value": "raw"}}},
            local_broker_executed=local,
        )
        is None
    )


def test_typed_raw_observation_is_private_one_use_and_requires_actual_local_broker():
    from apps.shell.agent.runtime import typed_draft_target as target

    request = {
        "run_id": "run",
        "plan_id": "plan",
        "step_id": "step",
        "request_id": "request",
        "tool_call_id": "call",
        "tool": "desktop.ui_elements",
        target.OBSERVATION_REQUEST_KEY: {"_authority": target._AUTHORITY},
    }
    raw = {"ok": True, "data": {"focused_element": {"value": " raw\r\n\t "}}}
    assert target.capture_typed_observation(request, raw, local_broker_executed=False) is None
    token = target.capture_typed_observation(request, raw, local_broker_executed=True)
    observed = target.consume_typed_observation(token, request, run_id="run")
    assert observed["data"]["focused_element"]["value"] == " raw\r\n\t "
    assert target.consume_typed_observation(token, request, run_id="run") == {}


@pytest.mark.parametrize(
    "body",
    [
        "  literal\r\n\t汉字🤖 tail  ",
        "  api_key=sk-typed-synthetic-secret123456 tail  ",
    ],
)
def test_changed_persisted_original_goal_never_authorizes_literal_body(tmp_path, monkeypatch, body):
    """The existing public-root sanitation must not turn into effect authority."""
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = bridge.send_quick_message(
            f"微信给张三说“{body}”",
            metadata={
                "source": "launcher",
                "allow_user_foreground_takeover": True,
            },
        )
        run = service.get_run(service.get_task_run_link(result["task_id"])["run_id"])
        assert run["status"] == "failed"
        assert "goal_contract_conflict" in run["result"]
        assert not state["calls"] and state["sent"] == 0
        assert not run["pending_approval"]
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("opening,closing", [('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")])
@pytest.mark.parametrize("entrypoint", ["direct", "model_hint"])
def test_compiler_uses_original_quoted_body_for_selected_intent_and_goal_steps(
    opening,
    closing,
    entrypoint,
):
    from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner

    body = "  literal\r\n\t汉字🤖 tail  "
    goal = f"微信给张三说{opening}{body}{closing}"
    planner = RuntimePlanner()
    if entrypoint == "direct":
        decision = planner.decision(goal)
    else:
        decision = planner.decision_from_model_intent_hint(
            goal,
            " ".join(goal.split()),
            "communication",
        )
    assert decision.selected_intent.inputs["direct_message_hint"]["body"] == body
    assert decision.plan.task_core.goal_contract.original_goal == goal
    draft = next(
        step
        for step in decision.plan.tool_plan.steps
        if step.step_id == "draft-communication-message"
    )
    assert draft.input_preview["text"] == body
