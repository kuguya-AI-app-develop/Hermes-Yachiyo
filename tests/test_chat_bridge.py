"""ChatBridge session overview tests."""

from __future__ import annotations

import json
import base64
from pathlib import Path
import time
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any

from apps.core.chat_session import ChatSession
from apps.core.chat_store import ChatStore
from apps.core.state import AppState
from apps.shell import chat_bridge as chat_bridge_mod
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.agent.tools import desktop as desktop_tools
from apps.shell.chat_bridge import ChatBridge
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.yachiyo_agent import YachiyoAgentService
from apps.shell.yachiyo_agent.contracts import ApprovalDecision
from apps.shell.yachiyo_agent.legacy_tasks import LegacyRuntimePort


_REAL_APP_OPEN = desktop_tools.app_open
_REAL_APP_STATUS = desktop_tools.app_status


class _EmptyActivityStore:
    def list_events(self, **_kwargs):
        return []


class _FakeNoDefaultProfileService:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def _runtime_with_chat_store(store: ChatStore) -> SimpleNamespace:
    session = ChatSession(session_id="session-current")
    session.attach_store(store, load_existing=False)
    return SimpleNamespace(
        state=AppState(),
        chat_session=session,
        task_runner=None,
        agent_runtime_service=_FakeAgentRuntimeService(),
        store=store,
    )


def test_chat_bridge_quick_task_forwards_runtime_execution_envelope(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = object()
    bridge = ChatBridge(runtime)
    captured: dict[str, Any] = {}

    class FakeStarter:
        def __init__(self, app_runtime, service):
            captured["app_runtime"] = app_runtime
            captured["service"] = service

        def execute_existing_main_chat_task(
            self,
            *,
            task_id,
            conversation_id,
            prompt,
            metadata=None,
            runtime_execution_envelope=None,
            direct_tool_requests=None,
        ):
            captured["task_id"] = task_id
            captured["conversation_id"] = conversation_id
            captured["prompt"] = prompt
            captured["metadata"] = metadata
            captured["runtime_execution_envelope"] = runtime_execution_envelope
            captured["direct_tool_requests"] = direct_tool_requests
            return {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "status": "completed",
                "summary": "done",
                "timeline": [],
            }

    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.legacy_ports.LegacyChatTaskStarter",
        FakeStarter,
    )
    envelope = {
        "envelope_id": "envelope-launcher",
        "requests": [{"tool_name": "desktop.list_apps", "input": {"query": "PixelForge"}}],
    }
    direct_requests = [{"tool": "desktop.list_apps", "input": {"query": "PixelForge"}}]

    try:
        result = bridge._execute_yachiyo_desktop_quick_task(
            "task-launcher",
            "打开 PixelForge",
            metadata={"source": "launcher", "launcher_mode": "live2d"},
            runtime_execution_envelope=envelope,
            direct_tool_requests=direct_requests,
        )

        assert result is not None
        assert captured["runtime_execution_envelope"] == envelope
        assert captured["direct_tool_requests"] == direct_requests
        assert captured["metadata"]["launcher_mode"] == "live2d"
        assert captured["conversation_id"] == "session-current"
    finally:
        store.close()


def test_chat_bridge_quick_candidates_use_execution_context() -> None:
    requests = chat_bridge_mod._desktop_candidates_for_quick_message(
        "打开 PixelForge 并点击导出按钮"
    )

    assert [request["tool"] for request in requests] == [
        "desktop.list_apps",
        "desktop.inspect_app",
        "app.focus_and_click_ui_element",
        "desktop.ui_elements",
    ]
    assert requests[0]["runtime_stage"] == "discover"
    assert requests[2]["step_id"] == "operate-foreground-ui"
    assert requests[2]["runtime_stage"] == "operate"
    assert requests[2]["task_todo"]["tool_name"] == "app.focus_and_click_ui_element"
    assert requests[3]["runtime_stage"] == "verify"


def test_chat_bridge_quick_message_executes_polite_app_launch_direct_chain(
    tmp_path,
    monkeypatch,
) -> None:
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = object()
    bridge = ChatBridge(runtime)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(chat_bridge_mod, "_QUICK_DESKTOP_SNAPSHOT_ATTEMPTS", 1)
    monkeypatch.setattr(chat_bridge_mod, "_QUICK_DESKTOP_SNAPSHOT_DELAY_SECONDS", 0)

    class FakeStarter:
        def __init__(self, app_runtime, service):
            captured["app_runtime"] = app_runtime
            captured["service"] = service

        def execute_existing_main_chat_task(
            self,
            *,
            task_id,
            conversation_id,
            prompt,
            metadata=None,
            runtime_execution_envelope=None,
            direct_tool_requests=None,
        ):
            captured["task_id"] = task_id
            captured["conversation_id"] = conversation_id
            captured["prompt"] = prompt
            captured["metadata"] = metadata
            captured["runtime_execution_envelope"] = runtime_execution_envelope
            captured["direct_tool_requests"] = direct_tool_requests
            return {
                "task_id": task_id,
                "conversation_id": conversation_id,
                "status": "completed",
                "summary": "done",
                "timeline": [],
            }

    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.legacy_ports.LegacyChatTaskStarter",
        FakeStarter,
    )
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-politeness-open",
            "task_id": "task-politeness-open",
            "status": "pending",
            "echo": text,
        }
    )

    try:
        result = bridge.send_quick_message(
            "帮我开一下 PixelForge",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        direct_requests = captured["direct_tool_requests"]
        envelope = captured["runtime_execution_envelope"]
        assert result["ok"] is True
        assert result["agent_task"]["status"] == "completed"
        assert captured["prompt"] == "帮我开一下 PixelForge"
        assert captured["metadata"]["desktop_execution_policy"]["mode"] == "preview_input"
        assert "desktop_provider_session_auto_start" not in captured["metadata"]
        assert [request["tool"] for request in direct_requests] == [
            "desktop.list_apps",
            "app.open",
            "desktop.verify",
        ]
        assert direct_requests[0]["input"] == {"query": "PixelForge", "limit": 20}
        assert direct_requests[1]["input"] == {
            "app_name": "PixelForge",
            "selection_source": "desktop.list_apps",
            "query": "PixelForge",
        }
        assert direct_requests[2]["input"] == {
            "app_name": "PixelForge",
            "selection_source": "desktop.list_apps",
            "query": "PixelForge",
            "verification_goal": "app_running",
        }
        assert [request["tool_name"] for request in envelope["requests"]] == [
            "desktop.list_apps",
            "app.open",
            "desktop.verify",
        ]
        assert [request["runtime_stage"] for request in envelope["requests"]] == [
            "discover",
            "operate",
            "verify",
        ]
    finally:
        store.close()


def test_chat_bridge_quick_message_adds_daily_desktop_execution_policy(
    tmp_path,
    monkeypatch,
) -> None:
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    bridge = ChatBridge(runtime)
    captured_metadata: dict[str, Any] = {}
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "true")

    def fake_send_message(text: str, **kwargs: Any) -> dict[str, Any]:
        captured_metadata["metadata"] = kwargs.get("metadata")
        return {
            "ok": True,
            "message_id": "message-hi",
            "task_id": "task-hi",
            "status": "pending",
            "echo": text,
        }

    bridge._chat_api = SimpleNamespace(send_message=fake_send_message)
    try:
        result = bridge.send_quick_message(
            "你好",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        policy = captured_metadata["metadata"]["desktop_execution_policy"]
        assert result["ok"] is True
        assert policy["mode"] == "preview_input"
        assert policy["prefer_background_desktop"] is True
        assert policy["allow_live_foreground"] is False
        assert policy["allow_media_control"] is True
        assert policy["source"] == "daily_bubble"
        assert captured_metadata["metadata"]["desktop_provider_session_auto_start"] is True
    finally:
        store.close()


def test_chat_bridge_quick_message_auto_starts_configured_provider_manifest(
    tmp_path,
    monkeypatch,
) -> None:
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    bridge = ChatBridge(runtime)
    captured_metadata: dict[str, Any] = {}
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", raising=False)
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST",
        str(tmp_path / "provider-manifest.json"),
    )

    def fake_send_message(text: str, **kwargs: Any) -> dict[str, Any]:
        captured_metadata["metadata"] = kwargs.get("metadata")
        return {
            "ok": True,
            "message_id": "message-hi",
            "task_id": "task-hi",
            "status": "pending",
            "echo": text,
        }

    bridge._chat_api = SimpleNamespace(send_message=fake_send_message)
    try:
        result = bridge.send_quick_message(
            "你好",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert captured_metadata["metadata"]["desktop_provider_session_auto_start"] is True
        assert captured_metadata["metadata"]["desktop_provider_session_strict_foreground"] is True
        assert "provider_manifest" not in captured_metadata["metadata"]
    finally:
        store.close()


def test_chat_bridge_quick_message_recommends_isolated_provider_for_input_tasks(
    tmp_path,
    monkeypatch,
) -> None:
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    bridge = ChatBridge(runtime)
    captured: dict[str, Any] = {}

    def fake_send_message(text: str, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "message_id": "message-copy",
            "task_id": "task-copy",
            "status": "pending",
            "echo": text,
        }

    def fake_execute(
        task_id: str,
        text: str,
        *,
        metadata: dict[str, Any] | None = None,
        runtime_execution_envelope: Any | None = None,
        direct_tool_requests: Any | None = None,
    ) -> dict[str, Any]:
        captured["task_id"] = task_id
        captured["text"] = text
        captured["metadata"] = metadata
        captured["runtime_execution_envelope"] = runtime_execution_envelope
        captured["direct_tool_requests"] = direct_tool_requests
        return {"task_id": task_id, "status": "completed", "summary": "done"}

    bridge._chat_api = SimpleNamespace(send_message=fake_send_message)
    monkeypatch.setattr(
        bridge,
        "_agent_task_snapshot_for_quick_message",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(bridge, "_execute_yachiyo_desktop_quick_task", fake_execute)
    try:
        result = bridge.send_quick_message(
            "复制选中文本",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert "desktop_provider_session_auto_start" not in captured["metadata"]
        assert captured["metadata"]["desktop_execution_policy"]["mode"] == "preview_input"
    finally:
        store.close()


def _agent_task_event(
    agent_task: dict[str, Any],
    event_type: str,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    for event in agent_task.get("recent_events") or []:
        if event.get("event_type") != event_type:
            continue
        if detail is not None and event.get("detail") != detail:
            continue
        return event
    raise AssertionError(f"missing agent task event: {event_type}")


def _agent_task_tool_call(agent_task: dict[str, Any], tool_name: str) -> dict[str, Any]:
    for tool_call in reversed(agent_task.get("tool_calls") or []):
        if tool_call.get("tool_name") == tool_name:
            return tool_call
    raise AssertionError(f"missing agent task tool call: {tool_name}")


def _assert_mapping_contains(
    actual: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    assert {key: actual[key] for key in expected} == expected


def _approval_decision_for_run(run: dict[str, Any]) -> ApprovalDecision:
    pending = run.get("pending_approval")
    assert isinstance(pending, dict) and pending.get("approval_id")
    return ApprovalDecision(metadata={"approval_id": pending["approval_id"]})


def _fake_active_window_result(app_name: str, title: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "action": "desktop.active_window",
        "summary": f"Active {app_name}",
        "data": {"app_name": app_name, "title": title or app_name},
    }


def _fake_ui_elements_result(app_name: str, title: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": f"Read visible UI elements for {app_name}",
        "data": {
            "app_name": app_name,
            "title": title or app_name,
            "count": 1,
            "elements": [
                {
                    "role": "AXTextField",
                    "name": "Content",
                    "center": {"x": 320, "y": 240},
                }
            ],
            "visibility_status": "visible",
        },
    }


def _fake_search_ui_result(app_name: str, query: str, submitted: bool) -> dict[str, Any]:
    result = _fake_ui_elements_result(app_name, "Search")
    elements = [{"role": "AXTextField", "name": "Search", "value": query,
                 "focused": True, "editable": True, "depth": 1,
                 "center": {"x": 320, "y": 240}}]
    if submitted:
        elements.extend([
            {"role": "AXTable", "name": "Search Results", "depth": 1},
            {"role": "AXRow", "name": query, "depth": 2},
        ])
    result["data"].update(pid=100, window_id=200, elements=elements, count=len(elements))
    return result


def _fake_inspect_app_result(app_name: str, title: str = "") -> dict[str, Any]:
    return {
        "ok": True,
        "action": "desktop.inspect_app",
        "summary": f"Verified desktop app: {app_name}",
        "data": {
            "app_name": app_name,
            "running": True,
            "focus_verified": True,
            "ui_elements": _fake_ui_elements_result(app_name, title),
        },
    }


_BROWSER_TARGET_HANDOFF_SUMMARY = (
    "桌面操作未完成：browser_owned_target_required。 "
    "你可以这样处理：请先让 Yachiyo 打开目标网页，再继续读取或操作；"
    "它不会接管你当前正在使用的浏览器标签页。"
)
_BACKGROUND_DESKTOP_PROVIDER_HANDOFF_SUMMARY = (
    "后台桌面控制尚未就绪，因此没有打开或操作 Google Chrome，"
    "也没有接管你正在使用的鼠标和键盘。"
    "请先安装或授权后台控制组件后重试。"
)


def _assert_planner_trace_prefix(
    agent_task: dict[str, Any],
    *,
    intent_kind: str,
) -> None:
    event_types = [event["event_type"] for event in agent_task["recent_events"]]
    assert "agent.intent.selected" in event_types
    assert "agent.plan.created" in event_types
    assert "agent.plan.step" in event_types
    assert event_types.index("agent.intent.selected") < event_types.index("agent.plan.created")
    assert event_types.index("agent.plan.created") < event_types.index("agent.plan.step")
    intent_event = _agent_task_event(agent_task, "agent.intent.selected")
    assert intent_event["payload"]["intent"]["kind"] == intent_kind


def _run_launcher_daily_desktop_quick_message(
    tmp_path,
    monkeypatch,
    text: str,
    permission_probe: Any | None = None,
    permission_preflight: Any | None = None,
    seed_messages: list[tuple[str, str]] | None = None,
    launcher_mode: str = "live2d",
) -> tuple[dict, dict, dict, list[str]]:
    # Model the independent read-after-open observation used by the planner.
    # A failed fake launch must not become a verified running application.
    opened_apps: set[str] = set()
    verification_queries: list[str] = []
    fake_open = desktop_tools.app_open
    if fake_open is not _REAL_APP_OPEN and desktop_tools.app_status is _REAL_APP_STATUS:
        def observed_open(app_name: str) -> dict:
            result = fake_open(app_name)
            data = result.get("data") or {}
            if result.get("ok") is True and data.get("launch_verified") is not False:
                opened_apps.add(str(data.get("app_name") or app_name))
            return result

        def observed_status(app_name: str) -> dict:
            verification_queries.append(app_name)
            return {
                "ok": True,
                "action": "app.status",
                "summary": f"Observed running state for {app_name}",
                "data": {"app_name": app_name, "running": app_name in opened_apps},
            }

        monkeypatch.setattr(desktop_tools, "app_open", observed_open)
        monkeypatch.setattr(desktop_tools, "app_status", observed_status)
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    for role, content in seed_messages or []:
        if role == "user":
            runtime.chat_session.add_user_message(content)
        elif role == "assistant":
            runtime.chat_session.add_assistant_message(content)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher daily desktop quick message should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        permission_probe or (lambda use_cache=True: {}),
    )
    if permission_preflight is not None:
        monkeypatch.setattr(
            "apps.shell.agent.tools.desktop.permission_preflight",
            permission_preflight,
        )
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            text,
            metadata={
                "source": "launcher",
                "launcher_mode": launcher_mode,
                "launcher_surface": "quick_message",
                # This legacy integration helper exercises the explicit
                # supervised path. Production launcher requests default to
                # background/preview mode and are covered by safety tests.
                "allow_user_foreground_takeover": True,
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        link = service.get_task_run_link(result["task_id"])
        run = service.get_run(link["run_id"])
        task_timeline = YachiyoAgentService(LegacyRuntimePort(service)).get_task_timeline(
            result["task_id"]
        ).model_dump(mode="json")
        public_events = service.list_run_events(run["run_id"])["events"]
        assert all(event.get("visibility") != "internal" for event in public_events)
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
        policy_decision_events = [
            event for event in events if event["event_type"] == "agent.tool.policy_decision"
        ]
        messages = store.load_messages("session-current", limit=10)
        user = [message for message in messages if message.role == "user"][-1]
        assistant = [message for message in messages if message.role == "assistant"][-1]
        user_metadata = json.loads(user.metadata_json)

        assert result["ok"] is True
        assert agent_task["task_id"] == result["task_id"]
        assert agent_task["conversation_id"] == "session-current"
        assert agent_task["open_in_studio_url"] == f"#/agents?run_id={run['run_id']}"
        assert user_metadata["source"] == "launcher"
        assert user_metadata["launcher_mode"] == launcher_mode
        assert user_metadata["daily_desktop_intent"] is True
        assert user_metadata["daily_desktop_source"] in {
            "runtime_planner",
            "daily_desktop_intent",
        }
        assert user_metadata["entrypoint_plan"] is True
        assert user_metadata["entrypoint_plan_source"] == user_metadata["daily_desktop_source"]
        assert user_metadata["entrypoint_plan_reason"] == user_metadata["daily_desktop_planning_reason"]
        assert user_metadata["entrypoint_plan_tools"] == user_metadata["daily_desktop_tools"]
        if user_metadata["daily_desktop_source"] == "runtime_planner":
            assert user_metadata["daily_desktop_source"] == "runtime_planner"
            if "yachiyo_plan_source" in user_metadata:
                assert user_metadata["yachiyo_plan_source"] == "runtime_planner"
            assert user_metadata["entrypoint_plan_legacy_fallback"] is False
        else:
            assert user_metadata["entrypoint_plan_legacy_fallback"] is True
        assert user_metadata["daily_desktop_tool"]
        assert user_metadata["daily_desktop_tools"]
        assert user_metadata["daily_desktop_tool"] in user_metadata["daily_desktop_tools"]
        if policy_decision_events:
            assert policy_decision_events[0]["payload"]["decision"] == "allow"
            assert policy_decision_events[0]["payload"]["policy_scope"] == "daily_desktop"
        else:
            assert set(user_metadata["daily_desktop_tools"]) <= {
                "calendar.create_event",
                "reminders.create",
            }
        if agent_task["status"] == "waiting_approval":
            assert assistant.content in {
                agent_task["summary"],
                "等待你在 Agent Studio 中审批后继续。",
            }
        else:
            assert assistant.content == agent_task["summary"]
        result["_events"] = events
        result["_verification_queries"] = verification_queries
        result["_task_timeline"] = task_timeline
        return result, agent_task, run, event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_conversation_overview_preserves_session_summary(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    monkeypatch.setattr(chat_bridge_mod, "get_activity_store", lambda: _EmptyActivityStore())
    try:
        runtime.chat_session.add_user_message("请分析 NativeRunEngine 迁移\n保留会话总结")
        runtime.chat_session.add_assistant_message(
            "已经保留 ChatBridge 会话概览。",
            task_id="session-task-1",
        )

        bridge = ChatBridge(runtime)
        sessions = bridge.get_recent_sessions(limit=3)
        overview = bridge.get_conversation_overview(summary_count=2, session_limit=3)

        assert sessions["ok"] is True
        current = sessions["sessions"][0]
        assert current["session_id"] == "session-current"
        assert current["summary"] == (
            "用户：请分析 NativeRunEngine 迁移 保留会话总结；回复：已经保留 ChatBridge 会话概览。"
        )
        assert current["latest_role"] == "assistant"
        assert current["latest_status"] == "completed"
        assert current["latest_task_id"] == "session-task-1"
        assert overview["ok"] is True
        assert overview["latest_reply"] == "已经保留 ChatBridge 会话概览。"
        assert overview["latest_reply_full"] == "已经保留 ChatBridge 会话概览。"
        assert overview["agent_task"]["task_id"] == "session-task-1"
        assert overview["agent_task"]["status"] == "waiting_approval"
        assert overview["agent_task"]["needs_user_action"] is True
        assert overview["agent_task"]["open_in_studio_url"] == "#/agents?run_id=session-task-1"
        assert overview["recent_sessions"][0]["summary"] == current["summary"]
        assert overview["recent_sessions"][0]["latest_task_id"] == "session-task-1"
    finally:
        store.close()


def test_chat_bridge_quick_message_returns_agent_task_snapshot_for_lightweight_entrypoints(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakeDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-browser",
            "task_id": "task-browser",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("打开 GitHub")

        assert result["ok"] is True
        assert result["task_id"] == "task-browser"
        assert result["status"] == "pending"
        assert result["echo"] == "打开 GitHub"
        assert result["agent_task"]["task_id"] == "task-browser"
        assert result["agent_task"]["conversation_id"] == "session-current"
        assert result["agent_task"]["status"] == "running"
        assert result["agent_task"]["current_step"] == "已回退执行 · 打开网页 · 系统浏览器"
        assert result["agent_task"]["open_in_studio_url"] == "#/agents?run_id=run-browser"
        assert result["agent_task"]["recent_events"][0]["event_type"] == "agent.desktop.intent_completed"
        assert result["agent_task"]["recent_events"][0]["payload"] == {
            "status": "completed",
            "tool": "browser.open_url",
        }
        assert result["agent_task"]["tool_calls"][0]["tool_name"] == "browser.open_url"
        assert result["agent_task"]["tool_calls"][0]["status"] == "completed"
    finally:
        store.close()


def _wait_for_agent_run(service: AgentRuntimeService, run_id: str) -> dict:
    for _ in range(50):
        run = service.get_run(run_id)
        if run["status"] != "running":
            return run
        time.sleep(0.05)
    return service.get_run(run_id)


def test_chat_bridge_agent_session_quick_message_uses_daily_desktop_overlay_for_lightweight_entrypoints(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.isolated_provider_session.ensure_isolated_desktop_provider_session_for_envelope",
        lambda _envelope: {
            "ok": False,
            "needed": True,
            "running": False,
            "started": False,
            "reason": "test_provider_unavailable",
        },
    )
    for mode in ("bubble", "live2d"):
        store = ChatStore(db_path=str(tmp_path / f"{mode}-chat.db"))
        runtime = _runtime_with_chat_store(store)
        agent = {
            "id": "agent_native",
            "name": "Native Agent",
            "nickname": "Native Agent",
            "kind": "agent",
            "enabled": True,
            "tool_policy": {
                "allowed_tools": ["workspace.read"],
                "approval_required": {},
            },
        }
        captured: list[dict[str, Any]] = []

        class FakeRunnableService:
            def list_runnables(self):
                return {"runnables": [agent]}

            def parse_known_chat_runnable(self, text):
                if text.startswith("@Native Agent"):
                    return "Native Agent", text.replace("@Native Agent", "", 1).strip()
                return None

            def resolve_runnable(self, *, runnable_id="", name=""):
                if runnable_id == agent["id"] or name == agent["name"] or name == agent["nickname"]:
                    return agent
                return None

            def create_run_for_runnable_async(self, **kwargs):
                captured.append(dict(kwargs))
                run = {
                    "run_id": f"{mode}-agent-run-{len(captured)}",
                    "run_group_id": kwargs.get("run_group_id") or f"{mode}-run-group-{len(captured)}",
                    "status": "processing",
                    "result": "",
                    "runnable": agent,
                }
                on_complete = kwargs.get("on_complete")
                if on_complete:
                    on_complete({
                        **run,
                        "status": "completed",
                        "result": "Agent result",
                    })
                return run

        runtime.agent_runtime_service = FakeRunnableService()
        bridge = ChatBridge(runtime)
        try:
            first = bridge.send_quick_message(
                "@Native Agent 你好",
                metadata={
                    "source": "launcher",
                    "launcher_mode": mode,
                    "launcher_surface": "quick_message",
                },
            )
            assert first["ok"] is True
            assert "daily_desktop_policy_overlay" not in captured[-1]

            second = bridge.send_quick_message(
                "能否帮我播放apple Music?",
                metadata={
                    "source": "launcher",
                    "launcher_mode": mode,
                    "launcher_surface": "quick_message",
                },
            )

            assert second["ok"] is True
            assert captured[-1]["runnable_id"] == agent["id"]
            assert captured[-1]["user_goal"] == "能否帮我播放apple Music?"
            assert captured[-1]["daily_desktop_policy_overlay"] is True
            envelope = captured[-1]["runtime_execution_envelope"]
            assert envelope["decision_id"]
            assert envelope["plan_id"]
            assert (
                captured[-1]["metadata"]["desktop_execution_policy"]["mode"]
                == "preview_input"
            )
            assert "direct_tool_requests" not in captured[-1]
            user = [message for message in runtime.chat_session.get_messages() if message.role == "user"][-1]
            assert user.metadata["launcher_mode"] == mode
            assert user.metadata["daily_desktop_tool"] == "media.music_app_open_and_play"

            third = bridge.send_quick_message(
                "打开系统设置",
                metadata={
                    "source": "launcher",
                    "launcher_mode": mode,
                    "launcher_surface": "quick_message",
                    "desktop_permission_recovery": True,
                    "recovery_tool": "system.settings_open",
                    "recovery_input": {"target": "系统设置"},
                    "recovery_permission_target": "screen_recording",
                    "recovery_risk_level": "low",
                },
            )

            assert third["ok"] is True
            assert "direct_tool_requests" not in captured[-1]
            envelope = captured[-1]["runtime_execution_envelope"]
            assert envelope["decision_id"] and envelope["plan_id"]
            assert captured[-1]["metadata"]["daily_desktop_tool"] == "system.settings_open"
        finally:
            store.close()


def test_chat_bridge_agent_session_executes_daily_desktop_followup_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    service.create_agent(
        {
            "name": "Native Agent",
            "model_mode": "profile",
            "model_profile_id": "",
            "tool_policy": {
                "allowed_tools": ["workspace.read"],
                "approval_required": {},
            },
        }
    )
    runtime.agent_runtime_service = service
    play_calls: list[str] = []
    apple_play_queries: list[str] = []
    list_app_queries: list[str] = []
    shortcut_calls: list[tuple[str, str]] = []
    typed_text: list[str] = []
    submitted_searches: list[bool] = []

    def fake_music_app_open_and_play(app_name: str) -> dict:
        play_calls.append(app_name)
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": f"Opened {app_name} and started playback",
                "data": {
                    'open_ok': True,
                    'control': 'play',
                    "app_name": app_name,
                    "player_state": "playing",
                    "playback_started": True,
                    "playback_ok": True,
                    "foreground_action_taken": False,
                },
        }

    def fake_apple_music_play(query: str) -> dict:
        apple_play_queries.append(query)
        return {
            "ok": True,
            "action": "media.apple_music_play",
            "summary": f"Apple Music playing {query}",
            "data": {
                'status': 'played',
                'match_kind': 'track',
                'album': 'Fixture Album',
                "query": query,
                "track": query,
                "artist": "Yachiyo",
                "player_state": "playing",
                "playback_started": True,
                "track_identity_verified": True,
                "catalog_match_verified": True,
                "foreground_action_taken": False,
            },
        }

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        list_app_queries.append(str(query or ""))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": "Installed apps matching Music: Music",
            "data": {
                "query": str(query or ""),
                "apps": [
                    {
                        "name": "Music",
                        "path": "/Applications/Music.app",
                        "match_score": 100,
                    }
                ],
                "count": 1,
                "total_count": 1,
                "truncated": False,
                "best_match": {
                    "name": "Music",
                    "path": "/Applications/Music.app",
                    "match_score": 100,
                },
                "resolution": {
                    "resolved_app_name": "Music",
                    "matched_name": "Music",
                    "matched_name_source": "desktop.list_apps",
                    "confidence": "high",
                },
            },
            "permission_error": False,
            "fallback_used": False,
        }

    def fake_app_open(app_name: str) -> dict:
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_safe_shortcut(action: str) -> dict:
        shortcut_calls.append(("Music", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Ran {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        typed_text.append(text)
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": f"Typed {len(text)} chars",
            "data": {"text": text},
        }

    def fake_search_submit() -> dict:
        submitted_searches.append(True)
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted search",
            "data": {"submitted": True},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.list_apps",
        fake_list_apps,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_open",
        fake_app_open,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_focus",
        fake_app_focus,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_search_submit",
        fake_search_submit,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.apple_music_play",
        fake_apple_music_play,
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("agent daily desktop follow-up should not call model")
        ),
    )
    bridge = ChatBridge(runtime)
    try:
        first = bridge.send_quick_message(
            "@Native Agent 能否帮我播放 Apple Music?",
            metadata={"allow_user_foreground_takeover": True},
        )
        assert first["ok"] is True
        first_run = _wait_for_agent_run(service, first["run_id"])
        assert first_run["status"] == "completed"

        second = bridge.send_quick_message(
            "超时空辉夜姬吧",
            metadata={"allow_user_foreground_takeover": True},
        )
        assert second["ok"] is True
        second_run = _wait_for_agent_run(service, second["run_id"])
        second_events = service.list_run_events(second["run_id"], include_internal=True)["events"]
        second_event_types = [event["event_type"] for event in second_events]
        second_user = [
            message for message in runtime.chat_session.get_messages() if message.role == "user"
        ][-1]

        assert play_calls == ["Music"]
        assert apple_play_queries == ["超时空辉夜姬"]
        assert list_app_queries == []
        assert shortcut_calls == []
        assert typed_text == []
        assert submitted_searches == []
        assert second_run["status"] == "completed"
        assert "agent.plan.selection" in second_event_types
        assert "agent.desktop.intent_planned" in second_event_types
        assert "agent.tool.call" in second_event_types
        assert "model.request.started" not in second_event_types
        assert second_user.metadata["daily_desktop_intent"] is True
        assert "media.apple_music_play" in second_user.metadata["daily_desktop_tools"]
    finally:
        store.close()


def test_chat_bridge_quick_message_plans_structured_recovery_for_lightweight_entrypoints(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = None
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-recovery",
            "task_id": "task-recovery",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message(
            "修复屏幕录制",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "recovery_tool": "app.open",
                "recovery_input": {"app_name": "屏幕录制权限"},
                "recovery_permission_target": "screen_recording",
                "recovery_risk_level": "low",
            },
        )

        assert result["ok"] is True
        assert result["task_id"] == "task-recovery"
        assert result["status"] == "pending"
        assert result["echo"] == "修复屏幕录制"
        assert result["agent_task"]["task_id"] == "task-recovery"
        assert result["agent_task"]["conversation_id"] == "session-current"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 打开应用"
        assert result["agent_task"]["recent_events"][0]["event_type"] == "agent.desktop.intent_planned"
        assert result["agent_task"]["recent_events"][0]["payload"]["tool"] == "app.open"
        assert result["agent_task"]["recent_events"][0]["payload"]["input_preview"] == {
            "app_name": "屏幕录制权限"
        }
    finally:
        store.close()


def test_chat_bridge_quick_message_plans_multi_step_desktop_request_for_lightweight_entrypoints(
    tmp_path,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = None
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-multi-step",
            "task_id": "task-multi-step",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message(
            "打开 Notes，输入 hello，再复制",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert result["task_id"] == "task-multi-step"
        assert result["agent_task"]["task_id"] == "task-multi-step"
        assert result["agent_task"]["conversation_id"] == "session-current"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 发现已安装应用"
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="desktop_operation",
        )
        selection_event = _agent_task_event(
            result["agent_task"],
            "agent.plan.selection",
        )
        assert selection_event["payload"]["selection_source"] == "runtime_planner"
        assert selection_event["payload"]["selection_reason"] == "runtime_planner_direct"
        assert selection_event["payload"]["legacy_tools"] == []
        assert selection_event["payload"]["plan_tools"] == [
            "desktop.list_apps",
            "app.open_and_safe_type_text",
            "desktop.ui_elements",
            "desktop.safe_shortcut",
            "desktop.ui_elements",
        ]
        assert selection_event["payload"]["plan_step_count"] == 5
        assert selection_event["payload"]["selected_tools"] == selection_event["payload"]["planner_tools"]
        assert selection_event["payload"]["selected_request_count"] == len(
            selection_event["payload"]["selected_tools"]
        )
        planned_event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="desktop.list_apps",
        )
        assert planned_event["payload"] == {
            "input_preview": {"query": "Notes", "limit": 20},
            "status": "planned",
            "tool": "desktop.list_apps",
        }
    finally:
        store.close()


def test_chat_bridge_quick_message_prefers_runtime_planner_before_legacy_candidates(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = None
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-runtime-planner",
            "task_id": "task-runtime-planner",
            "status": "pending",
            "echo": text,
        }
    )
    monkeypatch.setattr(
        chat_bridge_mod,
        "planner_first_daily_desktop_entrypoint_requests",
        lambda _text, **_kwargs: [
            {
                "protocol": "json_fallback",
                "tool": "system.screen_saver_start",
                "input": {},
                "source": "runtime_planner",
                "planning_reason": "planner_fallback_system_control",
            }
        ],
    )
    try:
        result = bridge.send_quick_message(
            "打开屏保",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert result["agent_task"]["task_id"] == "task-runtime-planner"
        event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="system.screen_saver_start",
        )
        assert event["detail"] == "system.screen_saver_start"
        assert event["payload"].get("input_preview", {}) == {}
        assert event["payload"]["status"] == "planned"
        assert event["payload"]["tool"] == "system.screen_saver_start"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(event["payload"])
    finally:
        store.close()


def test_chat_bridge_quick_message_uses_main_chat_tools_for_runtime_planner(
    tmp_path,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = SimpleNamespace(
        _main_chat_tool_policy=lambda: {
            "allowed_tools": ["data.analyze", "workspace.read", "terminal.run", "artifact.write"]
        }
    )
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-data-analysis",
            "task_id": "task-data-analysis",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message(
            "请分析 data/sales.csv 并输出报告",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert result["agent_task"]["task_id"] == "task-data-analysis"
        metadata = result["agent_task"]["metadata"]
        assert metadata["launcher_mode"] == "bubble"
        envelope = metadata["yachiyo_execution_envelope"]
        assert envelope["intent_kind"] == "data_analysis"
        assert [request["tool_name"] for request in envelope["requests"]] == [
            "workspace.read",
            "data.analyze",
        ]
        read_event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="workspace.read",
        )
        assert read_event["detail"] == "workspace.read"
        assert read_event["payload"]["input_preview"] == {
            "path": "data/sales.csv",
            "source_kind": "csv",
        }
        assert read_event["payload"]["status"] == "planned"
        assert read_event["payload"]["tool"] == "workspace.read"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(read_event["payload"])
        event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="data.analyze",
        )
        assert event["detail"] == "data.analyze"
        assert event["payload"]["input_preview"] == {
            "path": "data/sales.csv",
            "source_kind": "csv",
            "artifact_path": "analysis-report.md",
            "requested_outputs": ["report"],
            "artifact_manifest": [
                {"path": "analysis-report.md", "kind": "markdown"},
            ],
        }
        assert event["payload"]["status"] == "planned"
        assert event["payload"]["tool"] == "data.analyze"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(event["payload"])
    finally:
        store.close()


def test_chat_bridge_quick_message_discovers_data_source_with_main_chat_tools(
    tmp_path,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = SimpleNamespace(
        _main_chat_tool_policy=lambda: {
            "allowed_tools": ["workspace.list", "workspace.read", "terminal.run", "artifact.write"]
        }
    )
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-data-discovery",
            "task_id": "task-data-discovery",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message(
            "分析 Downloads 里的销售数据并输出报告",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert result["agent_task"]["task_id"] == "task-data-discovery"
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="data_analysis",
        )
        selection_event = _agent_task_event(
            result["agent_task"],
            "agent.plan.selection",
        )
        assert selection_event["payload"]["selection_source"] == "runtime_planner"
        assert selection_event["payload"]["selected_tools"] == [
            "workspace.list",
            "data.analyze",
        ]
        assert selection_event["payload"]["planner_request_count"] == 2
        assert selection_event["payload"]["selected_request_count"] == 2
        assert selection_event["payload"]["plan_tools"] == [
            "workspace.list",
            "data.analyze",
        ]
        assert selection_event["payload"]["plan_step_count"] == 2
        event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="workspace.list",
        )
        assert event["detail"] == "workspace.list"
        assert event["payload"]["input_preview"] == {
            "path": "Downloads",
            "pattern": "*.{csv,tsv,xlsx,json,jsonl}",
        }
        assert event["payload"]["status"] == "planned"
        assert event["payload"]["tool"] == "workspace.list"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(event["payload"])
        assert "continue_to_model" not in event["payload"]
    finally:
        store.close()


def test_chat_bridge_quick_message_routes_runtime_planner_inside_main_chat_policy(
    tmp_path,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = SimpleNamespace(
        _main_chat_tool_policy=lambda: {"allowed_tools": ["workspace.read"]}
    )
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-policy",
            "task_id": "task-policy",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message(
            "打开 GitHub",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )

        assert result["ok"] is True
        assert result["task_id"] == "task-policy"
        agent_task = result["agent_task"]
        assert agent_task["task_id"] == "task-policy"
        assert agent_task["runtime_execution_envelope"]["intent_kind"] == "web_research"
        assert agent_task["runtime_execution_envelope"]["source"] == "runtime_planner"
        blocked_browser_todo = agent_task["task_core"]["todos"][0]
        assert blocked_browser_todo["capability_id"] == "browser.research"
        assert blocked_browser_todo["tool_name"] is None
        assert blocked_browser_todo["status"] == "blocked"
        assert [
            request["tool_name"]
            for request in agent_task["runtime_execution_envelope"]["requests"]
        ] == []
        assert all(
            event["payload"].get("source") != "daily_desktop_intent"
            for event in agent_task["recent_events"]
        )
    finally:
        store.close()


def test_chat_bridge_quick_message_executes_daily_desktop_task_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    app_dir = tmp_path / "Applications"
    (app_dir / "Passwords.app").mkdir(parents=True)
    (app_dir / "Microsoft Word.app").mkdir()
    open_calls: list[str] = []

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "launch_verified": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop._desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop._application_search_dirs",
        lambda: [app_dir],
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "可以帮我打开 Word 吗",
    )

    assert result["ok"] is True
    assert open_calls == ["Microsoft Word"]
    assert agent_task["summary"] == "已打开 Microsoft Word。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "app.open"
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_opens_system_ui_apps_without_model(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "launch_verified": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    cases = (
        ("打开启动台", "bubble", "Launchpad", "已打开 Launchpad。"),
        ("open control center", "live2d", "Control Center", "已打开 Control Center。"),
        ("open notification center", "bubble", "Notification Center", "已打开 Notification Center。"),
    )
    for text, launcher_mode, app_name, summary in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert open_calls[-1] == app_name
        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.open"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"app_name": app_name}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_opens_system_settings_pane_without_model(
    tmp_path,
    monkeypatch,
):
    settings_calls: list[str] = []

    def fake_system_settings_open(target: str) -> dict:
        settings_calls.append(target)
        return {
            "ok": True,
            "action": "system.settings_open",
            "summary": f"Opened System Settings: {target}",
            "data": {
                "target": target,
                "open_target": "system_settings",
                "settings_label": target,
            },
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.system_settings_open",
        fake_system_settings_open,
    )
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开蓝牙",
    )

    assert result["ok"] is True
    assert settings_calls == ["蓝牙"]
    assert agent_task["summary"] == "已打开系统设置：蓝牙。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "system.settings_open"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"target": "蓝牙"}
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    cases = (
        ("打开 Wi-Fi", "bubble", "Wi-Fi", "已打开系统设置：Wi-Fi。"),
        ("打开无线网络", "live2d", "Wi-Fi", "已打开系统设置：Wi-Fi。"),
        ("打开网络", "bubble", "网络", "已打开系统设置：网络。"),
        ("打开显示设置", "live2d", "显示器", "已打开系统设置：显示器。"),
        ("打开声音设置", "bubble", "声音", "已打开系统设置：声音。"),
        ("open sound settings", "live2d", "声音", "已打开系统设置：声音。"),
        ("打开键盘设置", "bubble", "键盘", "已打开系统设置：键盘。"),
        ("open keyboard settings", "live2d", "键盘", "已打开系统设置：键盘。"),
        ("打开通知设置", "bubble", "通知", "已打开系统设置：通知。"),
        ("open notification settings", "live2d", "通知", "已打开系统设置：通知。"),
        ("打开定位权限", "bubble", "定位服务", "已打开系统设置：定位服务。"),
        ("打开系统设置里的辅助功能", "bubble", "辅助功能权限", "已打开系统设置：辅助功能权限。"),
        ("打开系统设置里的辅助功能", "live2d", "辅助功能权限", "已打开系统设置：辅助功能权限。"),
        ("打开隐私", "bubble", "隐私与安全性", "已打开系统设置：隐私与安全性。"),
        ("open desktop permissions", "live2d", "隐私与安全性", "已打开系统设置：隐私与安全性。"),
        ("打开输入监控权限", "bubble", "输入监控", "已打开系统设置：输入监控。"),
        ("打开完全磁盘访问权限", "live2d", "完全磁盘访问", "已打开系统设置：完全磁盘访问。"),
        ("打开摄像头权限", "bubble", "摄像头", "已打开系统设置：摄像头。"),
        ("修复自动化权限", "bubble", "自动化权限", "已打开系统设置：自动化权限。"),
        ("修一下屏幕录制权限", "live2d", "屏幕录制权限", "已打开系统设置：屏幕录制权限。"),
        ("fix full disk access permissions", "bubble", "完全磁盘访问", "已打开系统设置：完全磁盘访问。"),
        ("fix input monitoring permissions", "live2d", "输入监控", "已打开系统设置：输入监控。"),
    )
    for prompt, launcher_mode, target, summary in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "system.settings_open"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"target": target}
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert settings_calls == [
        "蓝牙",
        "Wi-Fi",
        "Wi-Fi",
        "网络",
        "显示器",
        "声音",
        "声音",
        "键盘",
        "键盘",
        "通知",
        "通知",
        "定位服务",
        "辅助功能权限",
        "辅助功能权限",
        "隐私与安全性",
        "隐私与安全性",
        "输入监控",
        "完全磁盘访问",
        "摄像头",
        "自动化权限",
        "屏幕录制权限",
        "完全磁盘访问",
        "输入监控",
    ]


def test_chat_bridge_quick_message_focuses_app_for_polite_launcher_entrypoint(
    tmp_path,
    monkeypatch,
):
    focus_calls: list[str] = []

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        return _fake_inspect_app_result(app_name)

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    cases = (
        ("能不能切到 Slack", "bubble", "Slack"),
        ("切一下微信", "bubble", "WeChat"),
        ("微信切一下", "live2d", "WeChat"),
        ("你能帮我切到Chrome吗", "bubble", "Google Chrome"),
        ("你可以帮我聚焦Chrome吗", "live2d", "Google Chrome"),
        ("go back to WeChat", "bubble", "WeChat"),
        ("switch back to WeChat", "live2d", "WeChat"),
    )
    for prompt, launcher_mode, app_name in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == f"已切换到 {app_name}。"
        focus_tool_call = next(
            tool_call
            for tool_call in agent_task["tool_calls"]
            if tool_call["tool_name"] == "app.focus"
        )
        assert focus_tool_call["input_preview"] == {"app_name": app_name}
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert focus_calls == [
        "Slack",
        "WeChat",
        "WeChat",
        "Google Chrome",
        "Google Chrome",
        "WeChat",
        "WeChat",
    ]


def test_chat_bridge_quick_message_opens_notes_and_creates_note_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: new note",
            "data": {
                "shortcut_action": action,
                "shortcut_label": "new note",
            },
        }

    def fake_active_window() -> dict:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active Notes",
            "data": {"app_name": "Notes", "title": "Notes"},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        return _fake_ui_elements_result(app_name or "Notes")

    extract_calls: list[str] = []

    def fake_extract_text(selector: str = "") -> dict:
        extract_calls.append(selector)
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 29 characters from browser page",
            "data": {
                "selector": selector,
                "text": "Yachiyo desktop agent runtime",
                "truncated": False,
            },
        }

    windows_calls: list[str] = []

    def fake_windows(app_name: str = "") -> dict:
        windows_calls.append(app_name)
        return {
            "ok": True,
            "action": "desktop.windows",
            "summary": "Read open windows",
            "data": {
                "app_name": app_name,
                "windows": [
                    {"app_name": app_name or "WeChat", "title": "general"},
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.windows", fake_windows)
    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "open Notes and make a new note",
    )

    assert result["ok"] is True
    assert calls == [
        ("open", "Notes"),
        ("focus", "Notes"),
        ("shortcut", "new_note"),
    ]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert "未能确认" in agent_task["summary"]
    assert "Profile" not in agent_task["summary"]
    assert agent_task["tool_calls"][-1]["tool_name"] == "app.open_and_safe_shortcut"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "app_name": "Notes",
        "action": "new_note",
    }
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    second, second_task, second_run, second_event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "显示微信窗口列表",
    )

    assert second["ok"] is True
    assert windows_calls == ["WeChat"]
    assert second_task["status"] == "completed"
    assert second_task["needs_user_action"] is False
    assert second_task["pending_approvals"] == []
    assert second_task["summary"] == "当前窗口：WeChat: general。"
    assert second_task["tool_calls"][-1]["tool_name"] == "desktop.windows"
    assert second_task["tool_calls"][-1]["input_preview"] == {"app_name": "WeChat"}
    assert second_task["tool_calls"][-1]["status"] == "completed"
    assert second_run["status"] == "completed"
    assert second_run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in second_event_types
    assert "agent.tool.call" in second_event_types
    assert "agent.desktop.intent_completed" in second_event_types
    assert "agent.desktop.intent_approval_required" not in second_event_types
    assert "model.request.started" not in second_event_types
    assert "model.requested" not in second_event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "summarize current webpage",
        launcher_mode="live2d",
    )

    assert extract_calls == []
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.extract_text"
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.skipped" in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_opens_notes_creates_note_and_types_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str, str, str]] = []

    real_notes_create = desktop_tools.notes_create

    def fake_run_osascript(script: str, args: list[str] | None = None) -> dict:
        assert "make new note" in script
        assert args == ["hello", "hello", ""]
        return {"ok": True, "stdout": f"x-coredata://test/Note/{len(calls)}"}

    def fake_notes_create(body: str, *, title: str = "", folder_name: str = "") -> dict:
        calls.append(("note", body, title, folder_name))
        # Let the actual native adapter project its returned Notes object id.
        return real_notes_create(body, title=title, folder_name=folder_name)

    monkeypatch.setattr(desktop_tools, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_tools, "_run_osascript", fake_run_osascript)

    monkeypatch.setattr("apps.shell.agent.tools.desktop.notes_create", fake_notes_create)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "新建一个备忘录写 hello",
    )

    assert result["ok"] is True
    assert calls == [("note", "hello", "", "")]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已创建备忘录：hello（5 个字符）。"
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-1:]] == [
        "notes.create",
    ]
    assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == "notes.create"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    cases = (
        ("新建备忘录 hello", "bubble"),
        ("帮我记下 hello", "live2d"),
        ("帮我新建备忘录：hello", "bubble"),
    )
    for prompt, launcher_mode in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert calls[-1] == ("note", "hello", "", "")
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已创建备忘录：hello（5 个字符）。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "notes.create"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_opens_word_and_creates_document_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: new document",
            "data": {
                "shortcut_action": action,
                "shortcut_label": "new document",
            },
        }

    def fake_active_window() -> dict:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active Microsoft Word",
            "data": {"app_name": "Microsoft Word", "title": "Document"},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        return _fake_ui_elements_result(app_name or "Microsoft Word", "Document")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Word 新建文档",
    )

    assert result["ok"] is True
    assert calls == [
        ("open", "Microsoft Word"),
        ("focus", "Microsoft Word"),
        ("shortcut", "new_document"),
    ]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert "未能确认" in agent_task["summary"]
    assert "Profile" not in agent_task["summary"]
    assert agent_task["tool_calls"][-1]["tool_name"] == "app.open_and_safe_shortcut"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "app_name": "Microsoft Word",
        "action": "new_document",
    }
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_opens_calendar_and_creates_event_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: new calendar event",
            "data": {
                "shortcut_action": action,
                "shortcut_label": "new calendar event",
            },
        }

    def fake_active_window() -> dict:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active Calendar",
            "data": {"app_name": "Calendar", "title": "Calendar"},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        return _fake_ui_elements_result(app_name or "Calendar")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    cases = (
        (
            "打开日历新建日程",
            "bubble",
            "app.open_and_safe_shortcut",
            "已打开 Calendar 并发送“新建日程”快捷键。",
        ),
        (
            "日历新建日程",
            "live2d",
            "app.focus_and_safe_shortcut",
            "已切到 Calendar 并发送“新建日程”快捷键。",
        ),
    )
    for prompt, launcher_mode, tool_name, summary in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert "未能确认" in agent_task["summary"]
        assert "Profile" not in agent_task["summary"]
        assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Calendar",
            "action": "new_event",
        }
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert calls == [
        ("open", "Calendar"),
        ("focus", "Calendar"),
        ("shortcut", "new_event"),
        ("focus", "Calendar"),
        ("shortcut", "new_event"),
    ]


def test_chat_bridge_quick_message_opens_named_music_app_without_model(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []
    music_calls: list[str] = []

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "launch_verified": True,
            },
        }

    def fake_music_app_open_and_play(app_name: str) -> dict:
        music_calls.append(app_name)
        return {
            "ok": True,
            "action": "media.music_app_open_and_play",
            "summary": f"Opened {app_name} and attempted playback with media key",
            "data": {
                "app_name": app_name,
                "playback_state_unverified": True,
            },
            "permission_error": False,
            "fallback_used": True,
            "fallback": "system_media_key",
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    app_cases = (
        ("微信帮我打开一下", "live2d", "WeChat"),
        ("open WeChat for me", "bubble", "WeChat"),
        ("你能帮我打开微信吗", "live2d", "WeChat"),
        ("你能启动一下备忘录吗", "bubble", "Notes"),
        ("Could you launch Calendar for me?", "live2d", "Calendar"),
        ("Would you open Notes please?", "bubble", "Notes"),
    )
    for prompt, launcher_mode, app_name in app_cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == f"已打开 {app_name}。"
        open_tool_call = _agent_task_tool_call(agent_task, "app.open")
        assert open_tool_call["input_preview"] == {"app_name": app_name}
        assert open_tool_call["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    music_cases = (
        (
            "打开 Spotify 并播放",
            "bubble",
            "Spotify",
            "已打开 Spotify，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "用 Spotify 播放音乐",
            "live2d",
            "Spotify",
            "已打开 Spotify，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "打开网易云并播放",
            "bubble",
            "网易云音乐",
            "已打开网易云音乐，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "可以帮我打开网易云并播放吗",
            "live2d",
            "网易云音乐",
            "已打开网易云音乐，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "Could you launch Spotify and play music?",
            "bubble",
            "Spotify",
            "已打开 Spotify，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
        ),
    )
    for prompt, launcher_mode, app_name, expected_summary in music_cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == expected_summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "media.music_app_open_and_play"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"app_name": app_name}
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert music_calls == ["Spotify", "Spotify", "网易云音乐", "网易云音乐", "Spotify"]

    assert open_calls == ["WeChat", "WeChat", "WeChat", "Notes", "Calendar", "Notes"]


def test_chat_bridge_quick_message_opens_default_browser_without_model(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Google Chrome for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "Google Chrome",
                        "path": "/Applications/Google Chrome.app",
                        "match_score": 100,
                    }
                ],
                "matches": [
                    {
                        "name": "Google Chrome",
                        "path": "/Applications/Google Chrome.app",
                        "match_score": 100,
                    }
                ],
                "best_match": {
                    "name": "Google Chrome",
                    "path": "/Applications/Google Chrome.app",
                    "match_score": 100,
                },
            },
        }

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "launch_verified": True,
            },
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict:
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Verified desktop app: {app_name}",
            "data": {
                "app_name": app_name,
                "running": True,
                "focus_verified": True,
                "role_filter": role_filter,
                "limit": limit,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    cases = (
        ("打开默认浏览器", "live2d"),
        ("打开网页", "bubble"),
        ("open a browser", "live2d"),
        ("open a webpage", "bubble"),
    )
    for prompt, launcher_mode in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == "已打开 Google Chrome。"
        open_call = _agent_task_tool_call(agent_task, "app.open")
        assert open_call["input_preview"]["app_name"] == "Google Chrome"
        assert open_call["status"] == "completed"
        assert open_call["output_preview"]["data"]["launch_verified"] is True
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert open_calls == ["Google Chrome", "Google Chrome", "Google Chrome", "Google Chrome"]


def test_chat_bridge_quick_message_opens_explicit_desktop_client_without_model(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        app_name = "Finder" if query == "Finder" else "ChatGPT"
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found {app_name} for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": app_name,
                        "path": f"/Applications/{app_name}.app",
                        "match_score": 100,
                    }
                ],
                "best_match": {
                    "name": app_name,
                    "path": f"/Applications/{app_name}.app",
                    "match_score": 100,
                },
            },
        }

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "launch_verified": True,
            },
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict:
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Verified desktop app: {app_name}",
            "data": {
                "app_name": app_name,
                "running": True,
                "focus_verified": True,
                "role_filter": role_filter,
                "limit": limit,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    cases = (
        ("打开 ChatGPT 客户端", "live2d", "ChatGPT"),
        ("打开文件夹", "bubble", "Finder"),
        ("open a folder", "live2d", "Finder"),
    )
    for prompt, launcher_mode, app_name in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == f"已打开 {app_name}。"
        open_call = _agent_task_tool_call(agent_task, "app.open")
        assert open_call["input_preview"]["app_name"] == app_name
        assert open_call["status"] == "completed"
        assert open_call["output_preview"]["data"]["launch_verified"] is True
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert open_calls == ["ChatGPT", "Finder", "Finder"]


def test_chat_bridge_quick_message_executes_running_apps_without_model(
    tmp_path,
    monkeypatch,
):
    running_calls = 0

    def fake_running_apps() -> dict:
        nonlocal running_calls
        running_calls += 1
        return {
            "ok": True,
            "action": "desktop.running_apps",
            "summary": "Running apps: Finder, Google Chrome, Music",
            "data": {
                "apps": [
                    {"name": "Finder", "pid": 101, "frontmost": False},
                    {"name": "Google Chrome", "pid": 202, "frontmost": True},
                    {"name": "Music", "pid": 303, "frontmost": False},
                ],
                "frontmost": "Google Chrome",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", fake_running_apps)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "当前有哪些 App 在运行",
    )

    assert result["ok"] is True
    assert running_calls == 1
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "正在运行的应用：Finder, Google Chrome, Music。前台是 Google Chrome。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.running_apps"
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_active_window_without_model(
    tmp_path,
    monkeypatch,
):
    active_window_calls = 0

    def fake_active_window() -> dict:
        nonlocal active_window_calls
        active_window_calls += 1
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Foreground window: Google Chrome - ChatGPT",
            "data": {
                "app_name": "Google Chrome",
                "title": "ChatGPT",
                "pid": 202,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    cases = (
        ("what app am I using?", "live2d"),
        ("what is the frontmost window", "bubble"),
        ("前台是不是 Chrome", "bubble"),
        ("is Chrome frontmost", "live2d"),
        ("which app is frontmost", "bubble"),
    )
    for prompt, launcher_mode in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "当前前台窗口是 Google Chrome：ChatGPT。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.active_window"
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert active_window_calls == len(cases)


def test_chat_bridge_quick_message_executes_named_windows_list_without_model(
    tmp_path,
    monkeypatch,
):
    windows_calls: list[str] = []

    def fake_windows(app_name: str = "") -> dict:
        windows_calls.append(app_name)
        return {
            "ok": True,
            "action": "desktop.windows",
            "summary": "Read Slack windows",
            "data": {
                "app_name": app_name,
                "windows": [
                    {"app_name": app_name or "Slack", "title": "general"},
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.windows", fake_windows)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "what windows are open in Slack",
    )

    assert result["ok"] is True
    assert windows_calls == ["Slack"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "当前窗口：Slack: general。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.windows"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"app_name": "Slack"}
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_inspects_app_for_visible_ui_elements(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object, object, object, object]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Slack for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "Slack",
                        "path": "/Applications/Slack.app",
                        "match_score": 100,
                    }
                ],
                "best_match": {
                    "name": "Slack",
                    "path": "/Applications/Slack.app",
                    "match_score": 100,
                },
            },
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        calls.append(("inspect", app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": "Slack is ready for foreground actions",
            "data": {
                "app_name": "Slack",
                "focus_verified": True,
                "focus_result": {"ok": True, "data": {"app_name": "Slack", "focus_verified": True}},
                "ui_elements": {
                    "ok": True,
                    "action": "desktop.ui_elements",
                    "data": {
                        "app_name": "Slack",
                        "title": "general",
                        "elements": [
                            {
                                "role": "AXButton",
                                "name": "Send",
                                "center": {"x": 640, "y": 720},
                            },
                        ],
                    },
                },
            },
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name, None, None, None, None))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        calls.append(("ui", app_name or "Slack", None, None, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Slack controls",
            "data": {
                "app_name": app_name or "Slack",
                "title": "general",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "Send",
                        "center": {"x": 640, "y": 720},
                    }
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "what buttons are visible in Slack",
    )

    assert result["ok"] is True
    assert calls == [("inspect", "Slack", True, True, "button", 80)]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == (
        "已切换到 Slack。 当前 Slack 界面控件：Button Send（640, 720）。"
    )
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.inspect_app"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "app_name": "Slack",
        "open_if_needed": True,
        "focus": True,
        "role_filter": "button",
        "limit": 80,
    }
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_generic_english_app_safe_operations(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        calls.append(("list_apps", query, limit))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found PixelForge Studio for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "PixelForge Studio",
                        "bundle_id": "com.example.pixelforge",
                        "path": "/Applications/PixelForge Studio.app",
                        "match_score": 96,
                    }
                ],
            },
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name, None))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        calls.append(("active", "PixelForge Studio", None))
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active PixelForge Studio",
            "data": {"app_name": "PixelForge Studio", "title": "Canvas"},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action, None))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        calls.append(("key", action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": f"Pressed {action}",
            "data": {"key_action": action, "repeat_count": repeat_count},
        }

    def fake_safe_scroll(direction: str, *, pages: int = 1) -> dict:
        calls.append(("scroll", direction, pages))
        return {
            "ok": True,
            "action": "desktop.safe_scroll",
            "summary": f"Scrolled foreground desktop {direction}",
            "data": {"direction": direction, "pages": pages},
        }

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        calls.append(("ui", role_filter, limit))
        return _fake_ui_elements_result("PixelForge Studio", "Canvas")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_scroll", fake_safe_scroll)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)

    cases = (
        (
            "PixelForge press command n",
            [
                ("list_apps", "PixelForge", 20),
                ("focus", "PixelForge Studio", None),
                ("active", "PixelForge Studio", None),
                ("shortcut", "new_window", None),
                ("ui", "", 80),
            ],
            [
                ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
                (
                    "app.focus_and_safe_shortcut",
                    {
                        "app_name": "PixelForge Studio",
                        "app_resolution_source": "desktop.list_apps",
                        "requested_app_name": "PixelForge",
                        "resolved_app_name": "PixelForge Studio",
                        "action": "new_window",
                    },
                ),
                ("desktop.ui_elements", {}),
            ],
            ["app.focus_and_safe_shortcut"],
            {"app_name": "PixelForge Studio", "action": "new_window"},
        ),
        (
            "refresh PixelForge",
            [
                ("list_apps", "PixelForge", 20),
                ("focus", "PixelForge Studio", None),
                ("active", "PixelForge Studio", None),
                ("shortcut", "refresh", None),
                ("ui", "", 80),
            ],
            [
                ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
                (
                    "app.focus_and_safe_shortcut",
                    {
                        "app_name": "PixelForge Studio",
                        "app_resolution_source": "desktop.list_apps",
                        "requested_app_name": "PixelForge",
                        "resolved_app_name": "PixelForge Studio",
                        "action": "refresh",
                    },
                ),
                ("desktop.ui_elements", {}),
            ],
            ["app.focus_and_safe_shortcut"],
            {"app_name": "PixelForge Studio", "action": "refresh"},
        ),
        (
            "press escape in PixelForge",
            [
                ("list_apps", "PixelForge", 20),
                ("focus", "PixelForge Studio", None),
                ("active", "PixelForge Studio", None),
                ("key", "escape", 1),
                ("ui", "", 80),
            ],
            [
                ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
                (
                    "app.focus_and_safe_key",
                    {
                        "app_name": "PixelForge Studio",
                        "app_resolution_source": "desktop.list_apps",
                        "requested_app_name": "PixelForge",
                        "resolved_app_name": "PixelForge Studio",
                        "action": "escape",
                        "repeat_count": 1,
                    },
                ),
                ("desktop.ui_elements", {}),
            ],
            ["app.focus_and_safe_key"],
            {"app_name": "PixelForge Studio", "action": "escape", "repeat_count": 1},
        ),
        (
            "PixelForge scroll down",
            [
                ("list_apps", "PixelForge", 20),
                ("focus", "PixelForge Studio", None),
                ("active", "PixelForge Studio", None),
                ("scroll", "down", 1),
                ("ui", "", 80),
            ],
            [
                ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
                (
                    "app.focus_and_safe_scroll",
                    {
                        "app_name": "PixelForge Studio",
                        "app_resolution_source": "desktop.list_apps",
                        "requested_app_name": "PixelForge",
                        "resolved_app_name": "PixelForge Studio",
                        "direction": "down",
                        "pages": 1,
                    },
                ),
                ("desktop.ui_elements", {}),
            ],
            ["app.focus_and_safe_scroll"],
            {"app_name": "PixelForge Studio", "direction": "down", "pages": 1},
        ),
    )

    for prompt, expected_calls, expected_event_tool_calls, expected_card_tools, final_input_preview in cases:
        calls.clear()
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
        )

        assert result["ok"] is True
        assert [call for call in calls if call[0] != "ui"] == [
            call for call in expected_calls if call[0] != "ui"
        ]
        expected_status = "completed" if expected_card_tools == ["app.focus_and_safe_scroll"] else "failed"
        assert agent_task["status"] == expected_status
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert [call["tool_name"] for call in agent_task["tool_calls"][-len(expected_card_tools) :]] == expected_card_tools
        card_input_preview = agent_task["tool_calls"][-1]["input_preview"]
        assert isinstance(card_input_preview, dict)
        assert final_input_preview.items() <= card_input_preview.items()
        event_tool_calls = [
            (event["payload"]["tool"], event["payload"]["input_preview"])
            for event in result["_events"]
            if event["event_type"] == "agent.tool.call"
        ]
        primary_events = [(tool, inputs) for tool, inputs in event_tool_calls if tool != "desktop.ui_elements" and tool != "desktop.verify"]
        expected_primary = [(tool, inputs) for tool, inputs in expected_event_tool_calls if tool != "desktop.ui_elements"]
        assert [tool for tool, _input in primary_events] == [tool for tool, _input in expected_primary]
        for (_tool, actual_input), (_expected_tool, expected_input) in zip(primary_events, expected_primary):
            assert expected_input.items() <= actual_input.items()
        if expected_status == "failed":
            assert "未能确认" in agent_task["summary"]
            assert "PixelForge Studio" in agent_task["summary"]
        assert run["status"] == expected_status
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.input_resolved" in event_types
        assert "agent.tool.call" in event_types
        if expected_status == "failed":
            assert "agent.desktop.intent_completed" not in event_types
            assert "agent.desktop.intent_unverified" in event_types
        else:
            assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_discovered_app_followup_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        calls.append(("list_apps", query, limit))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Typora for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "Typora",
                        "bundle_id": "abnerworks.Typora",
                        "path": "/Applications/Typora.app",
                        "match_score": 97,
                    }
                ],
            },
        }

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name, None))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name, None))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        calls.append(("active", "Typora", None))
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active Typora",
            "data": {"app_name": "Typora", "title": "Untitled"},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action, None))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text, None))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": f"Typed {len(text)} chars",
            "data": {"text_length": len(text)},
        }

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        calls.append(("ui", role_filter, limit))
        result = _fake_ui_elements_result("Typora", "Untitled")
        result["data"]["elements"][0].update({"name": "周报", "value": "周报"})
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开一个能写 markdown 的应用，新建文档标题为周报",
    )

    assert result["ok"] is True
    assert calls == [
        ("list_apps", "markdown", 20),
        ("open", "Typora", None),
        ("focus", "Typora", None),
        ("active", "Typora", None),
        ("shortcut", "new_document", None),
        ("focus", "Typora", None),
        ("active", "Typora", None),
        ("type", "周报", None),
        ("ui", "", 80),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.tool.input_resolved" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    plan_events = [
        event["payload"]
        for event in result["_events"]
        if event["event_type"] == "agent.plan.selection"
    ]
    assert plan_events
    followup_target = next(
        payload["followup_target"]
        for payload in reversed(plan_events)
        if payload.get("followup_target")
    )
    assert followup_target == {
        "kind": "desktop_discovered_app_action",
        "app_query": "markdown",
        "app_name_source": "desktop.list_apps",
        "capability_description": "markdown",
        "target_action": "safe_shortcut",
        "safe_shortcut_action": "new_document",
        "compose_text": "周报",
        "body_source": "explicit_user_text",
        "post_action_observation": {
            "tool": "desktop.ui_elements",
            "input": {},
        },
    }
    tool_calls = [
        (event["payload"]["tool"], event["payload"]["input_preview"])
        for event in result["_events"]
        if event["event_type"] == "agent.tool.call"
    ]
    assert [tool for tool, _preview in tool_calls] == [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_type_text",
        "desktop.ui_elements",
        "desktop.ui_elements",
    ]
    expected_input_previews = [
        {"query": "markdown", "limit": 20},
        {
            "app_name": "Typora",
            "action": "new_document",
            "app_resolution_source": "desktop.list_apps",
            "requested_app_name": "markdown",
            "resolved_app_name": "Typora",
            "resolved_app_path": "/Applications/Typora.app",
            "app_resolution_score": "97",
        },
        {"app_name": "Typora", "text": "周报"},
        {},
        {},
    ]
    for (_tool, input_preview), expected in zip(tool_calls, expected_input_previews):
        assert expected.items() <= input_preview.items()


def test_chat_bridge_quick_message_opens_generic_browser_followup_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        calls.append(("list_apps", query, limit))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Safari for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "Safari",
                        "bundle_id": "com.apple.Safari",
                        "path": "/Applications/Safari.app",
                        "match_score": 93,
                    }
                ],
            },
        }

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name, None))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        calls.append(("inspect", app_name, limit))
        return _fake_inspect_app_result(app_name, "Start Page")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开一个浏览器",
    )

    assert result["ok"] is True
    assert calls == [
        ("list_apps", "browser", 20),
        ("open", "Safari", None),
    ]
    assert result["_verification_queries"] == ["Safari"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.plan.selection" in event_types
    assert "agent.tool.input_resolved" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    plan_events = [
        event["payload"]
        for event in result["_events"]
        if event["event_type"] == "agent.plan.selection"
    ]
    assert plan_events
    followup_plan = next(
        payload for payload in reversed(plan_events) if payload.get("followup_target")
    )
    assert followup_plan["followup_target"] == {
        "kind": "desktop_discovered_app_action",
        "app_query": "browser",
        "app_name_source": "desktop.list_apps",
        "capability_description": "browser",
        "target_action": "open_app",
    }
    tool_calls = [
        (event["payload"]["tool"], event["payload"]["input_preview"])
        for event in result["_events"]
        if event["event_type"] == "agent.tool.call"
    ]
    assert tool_calls == [
        ("desktop.list_apps", {"query": "browser", "limit": 20}),
        (
            "app.open",
            {
                "app_name": "Safari",
                "app_resolution_source": "desktop.list_apps",
                "requested_app_name": "browser",
                "resolved_app_name": "Safari",
                "resolved_app_path": "/Applications/Safari.app",
                "app_resolution_score": "93",
            },
        ),
        (
            "desktop.verify",
            {
                "app_name": "Safari",
                "app_resolution_source": "desktop.list_apps",
                "requested_app_name": "browser",
                "resolved_app_name": "Safari",
                "resolved_app_path": "/Applications/Safari.app",
                "app_resolution_score": "93",
                "verification_goal": "app_running",
            },
        ),
        ("desktop.verify", {"app_name": "Safari", "verification_goal": "app_running"}),
    ]


def test_chat_bridge_quick_message_reads_current_ui_elements_without_fake_app_focus(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object]] = []

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        calls.append(("ui", role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read current buttons",
            "data": {
                "app_name": "Google Chrome",
                "title": "ChatGPT",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "Send",
                        "center": {"x": 640, "y": 720},
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_focus",
        lambda app_name: (_ for _ in ()).throw(
            AssertionError(f"current UI query should not focus fake app: {app_name}")
        ),
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "你能看看现在有哪些按钮吗",
    )

    assert calls == [("ui", "button", 80)]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["summary"] == "当前 Google Chrome 界面控件：Button Send（640, 720）。"
    assert [call["tool_name"] for call in agent_task["tool_calls"][-1:]] == [
        "desktop.ui_elements",
    ]
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "role_filter": "button",
        "limit": 80,
    }
    assert run["status"] == "completed"
    assert "agent.desktop.intent_unverified" not in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_unverified" not in event_types
    assert "model.request.started" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "where is the login button",
    )

    assert calls[-1] == ("ui", "button", 80)
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["summary"] == "当前 Google Chrome 界面控件：Button Send（640, 720）。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.ui_elements"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "role_filter": "button",
        "limit": 80,
    }
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "读一下当前界面文字",
    )

    assert calls[-1] == ("ui", "text", 80)
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.ui_elements"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "role_filter": "text",
        "limit": 80,
    }
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_opens_app_then_reads_ui_elements_for_chinese_followup(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object, object, object, object]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found WeChat for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "WeChat",
                        "path": "/Applications/WeChat.app",
                        "match_score": 100,
                    }
                ],
                "best_match": {
                    "name": "WeChat",
                    "path": "/Applications/WeChat.app",
                    "match_score": 100,
                },
            },
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        calls.append(("inspect", app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": "WeChat is ready for foreground actions",
            "data": {
                "app_name": "WeChat",
                "open_result": {
                    "ok": True,
                    "data": {"app_name": "WeChat", "launch_verified": True},
                },
                "focus_verified": True,
                "ui_elements": {
                    "ok": True,
                    "action": "desktop.ui_elements",
                    "data": {
                        "app_name": "WeChat",
                        "title": "Chats",
                        "elements": [
                            {
                                "role": "AXButton",
                                "name": "搜索",
                                "center": {"x": 120, "y": 88},
                            },
                        ],
                    },
                },
            },
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name, None, None, None, None))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        calls.append(("ui", app_name or "WeChat", None, None, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read WeChat controls",
            "data": {
                "app_name": app_name or "WeChat",
                "title": "Chats",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "搜索",
                        "center": {"x": 120, "y": 88},
                    }
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开微信看看有什么按钮",
    )

    assert result["ok"] is True
    assert calls == [("inspect", "WeChat", True, True, "button", 80)]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == (
        "已打开 WeChat。 当前 WeChat 界面控件：Button 搜索（120, 88）。"
    )
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.inspect_app"
    assert agent_task["tool_calls"][-1]["input_preview"] == {
        "app_name": "WeChat",
        "open_if_needed": True,
        "focus": True,
        "role_filter": "button",
        "limit": 80,
    }
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert event_types.count("agent.desktop.intent_planned") == 1
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_opens_system_settings_then_reads_options_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, object, object]] = []

    def fake_system_settings_open(target: str) -> dict:
        calls.append(("settings", target, None))
        return {
            "ok": True,
            "action": "system.settings_open",
            "summary": f"Opened System Settings: {target}",
            "data": {
                "target": target,
                "open_target": "system_settings",
                "settings_label": "System Settings",
            },
        }

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        calls.append(("ui", role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read System Settings options",
            "data": {
                "app_name": "System Settings",
                "title": "Settings",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "General",
                        "center": {"x": 120, "y": 88},
                    },
                ],
            },
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.system_settings_open",
        fake_system_settings_open,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    for launcher_mode in ("bubble", "live2d"):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "打开系统设置看看有哪些选项",
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已打开系统设置。 当前 System Settings 界面控件："
            "Button General（120, 88）。"
        )
        assert [call["tool_name"] for call in agent_task["tool_calls"][-2:]] == [
            "system.settings_open",
            "desktop.ui_elements",
        ]
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "role_filter": "",
            "limit": 80,
        }
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert event_types.count("agent.desktop.intent_planned") == 2
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert calls == [
        ("settings", "系统设置", None),
        ("ui", "", 80),
        ("settings", "系统设置", None),
        ("ui", "", 80),
    ]


def test_chat_bridge_quick_message_executes_app_status_without_model(
    tmp_path,
    monkeypatch,
):
    status_calls: list[str] = []

    def fake_app_status(app_name: str) -> dict:
        status_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.status",
            "summary": f"{app_name} is running",
            "data": {"app_name": app_name, "running": True},
        }

    def fake_windows(app_name: str = "") -> dict:
        return {
            "ok": True,
            "action": "desktop.windows",
            "summary": f"Read windows for {app_name}",
            "data": {
                "app_name": app_name,
                "windows": [{"app_name": app_name, "title": app_name}],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_status", fake_app_status)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.windows", fake_windows)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "Chrome 开着吗",
    )

    assert result["ok"] is True
    assert status_calls == ["Google Chrome"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "Google Chrome 当前正在运行。"
    status_call = _agent_task_tool_call(agent_task, "app.status")
    assert status_call["status"] == "completed"
    assert status_call["input_preview"]["app_name"] == "Google Chrome"
    assert not any(
        call["tool_name"] == "desktop.windows"
        for call in agent_task["tool_calls"]
    )
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    cases = (
        ("Google Chrome 在运行吗", "bubble", "Google Chrome"),
        ("检查一下 Slack 是否运行", "live2d", "Slack"),
        ("Finder 是否运行", "bubble", "Finder"),
    )
    for prompt, launcher_mode, app_name in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert status_calls[-1] == app_name
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == f"{app_name} 当前正在运行。"
        status_call = _agent_task_tool_call(agent_task, "app.status")
        assert status_call["status"] == "completed"
        assert status_call["input_preview"]["app_name"] == app_name
        assert not any(
            call["tool_name"] == "desktop.windows"
            for call in agent_task["tool_calls"]
        )
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_minimize_window_without_approval(
    tmp_path,
    monkeypatch,
):
    minimize_calls = 0

    def fake_minimize_window() -> dict:
        nonlocal minimize_calls
        minimize_calls += 1
        return {
            "ok": True,
            "action": "desktop.minimize_window",
            "summary": "Minimized the foreground window",
            "data": {"key": "m", "modifiers": ["command"]},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_minimize_window",
        fake_minimize_window,
    )
    cases = (
        ("最小化当前窗口", "bubble"),
        ("隐藏当前窗口", "bubble"),
        ("隐藏前台窗口", "live2d"),
        ("Can you minimize the current app?", "live2d"),
        ("Could you minimize the foreground application please?", "bubble"),
    )
    for index, (text, launcher_mode) in enumerate(cases, start=1):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert minimize_calls == index
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已发送最小化当前窗口指令。"
        assert _agent_task_tool_call(agent_task, "desktop.minimize_window")["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_hide_app_without_approval(
    tmp_path,
    monkeypatch,
):
    hide_calls = 0

    def fake_hide_app() -> dict:
        nonlocal hide_calls
        hide_calls += 1
        return {
            "ok": True,
            "action": "desktop.hide_app",
            "summary": "Hid the foreground app",
            "data": {"key": "h", "modifiers": ["command"]},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_hide_app",
        fake_hide_app,
    )
    cases = (
        ("隐藏当前应用", "bubble"),
        ("你可以帮我隐藏一下前台应用吗", "live2d"),
        ("Can you hide the current app?", "bubble"),
        ("Could you hide the foreground app please?", "live2d"),
    )
    for index, (text, launcher_mode) in enumerate(cases, start=1):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert hide_calls == index
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已发送隐藏当前应用指令。"
        assert _agent_task_tool_call(agent_task, "desktop.hide_app")["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_named_app_hide_without_approval(
    tmp_path,
    monkeypatch,
):
    hide_calls: list[str] = []

    def fake_app_hide(app_name: str) -> dict:
        hide_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.hide",
            "summary": f"Hid {app_name}",
            "data": {"app_name": app_name, "hide_status": "hidden"},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_hide",
        fake_app_hide,
    )
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "隐藏 Slack",
    )

    assert result["ok"] is True
    assert hide_calls == ["Slack"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已隐藏 Slack。"
    assert _agent_task_tool_call(agent_task, "app.hide")["status"] == "completed"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_app_prefix_hide_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    hide_calls: list[str] = []

    def fake_app_hide(app_name: str) -> dict:
        hide_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.hide",
            "summary": f"Hid {app_name}",
            "data": {"app_name": app_name, "hide_status": "hidden"},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_hide",
        fake_app_hide,
    )

    for launcher_mode in ("bubble", "live2d"):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 收起来",
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已隐藏 Google Chrome。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.hide"
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert hide_calls == ["Google Chrome", "Google Chrome"]


def test_chat_bridge_quick_message_executes_named_app_show_without_approval(
    tmp_path,
    monkeypatch,
):
    show_calls: list[str] = []

    def fake_app_show(app_name: str) -> dict:
        show_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.show",
            "summary": f"Showed {app_name}",
            "data": {"app_name": app_name, "show_status": "shown", "restored_window_count": 1},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_show",
        fake_app_show,
    )
    cases = (
        ("打开 Slack 并切到前台", "live2d", "Slack"),
        ("把微信调出来", "bubble", "WeChat"),
        ("你能帮我显示Finder吗", "live2d", "Finder"),
        ("你能帮我还原微信吗", "bubble", "WeChat"),
    )
    for prompt, launcher_mode, app_name in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == f"已显示 {app_name}。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.show"
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert show_calls == ["Slack", "WeChat", "Finder", "WeChat"]


def test_chat_bridge_quick_message_executes_named_app_window_focus_without_approval(
    tmp_path,
    monkeypatch,
):
    focus_calls: list[tuple[str, str]] = []

    def fake_app_focus_window(app_name: str, title_contains: str) -> dict:
        focus_calls.append((app_name, title_contains))
        return {
            "ok": True,
            "action": "app.focus_window",
            "summary": f"Focused {app_name} window: {title_contains}",
            "data": {
                "app_name": app_name,
                "title_contains": title_contains,
                "focus_status": "focused",
                "window_index": 2,
                "window_title": title_contains,
            },
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_focus_window",
        fake_app_focus_window,
    )
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "切到 Slack 的 general 窗口",
    )

    assert result["ok"] is True
    assert focus_calls == [("Slack", "general")]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已切换到 Slack 的 general 窗口。"
    assert _agent_task_tool_call(agent_task, "app.focus_window")["status"] == "completed"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_named_app_minimize_without_approval(
    tmp_path,
    monkeypatch,
):
    minimize_calls: list[str] = []

    def fake_app_minimize(app_name: str) -> dict:
        minimize_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.minimize",
            "summary": f"Minimized {app_name}",
            "data": {"app_name": app_name, "minimize_status": "minimized", "window_count": 2},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_minimize",
        fake_app_minimize,
    )
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "最小化 Slack",
    )

    assert result["ok"] is True
    assert minimize_calls == ["Slack"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已最小化 Slack。"
    assert _agent_task_tool_call(agent_task, "app.minimize")["status"] == "completed"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_app_prefix_minimize_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    minimize_calls: list[str] = []

    def fake_app_minimize(app_name: str) -> dict:
        minimize_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.minimize",
            "summary": f"Minimized {app_name}",
            "data": {"app_name": app_name, "minimize_status": "minimized", "window_count": 2},
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_minimize",
        fake_app_minimize,
    )

    for launcher_mode in ("bubble", "live2d"):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 最小化一下",
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已最小化 Google Chrome。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.minimize"
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert minimize_calls == ["Google Chrome", "Google Chrome"]


def test_chat_bridge_quick_message_executes_natural_music_request_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    open_and_play_calls = 0

    def fake_music_app_open_and_play(app_name: str) -> dict:
        nonlocal open_and_play_calls
        open_and_play_calls += 1
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": f"Opened {app_name} and started playback",
            "data": {
                "app_name": app_name,
                "open_ok": True,
                "playback_ok": True,
                "control": "play",
                "player_state": "playing",
                "track": "超时空辉夜姬",
                "artist": "Yachiyo",
            },
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    expected_summary = "已打开 Apple Music，并开始播放。当前：超时空辉夜姬 - Yachiyo。"
    expected_tool = "media.music_app_open_and_play"
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "Can you play Apple Music?",
    )

    assert result["ok"] is True
    assert open_and_play_calls == 1
    assert agent_task["summary"] == expected_summary
    assert agent_task["tool_calls"][-1]["tool_name"] == expected_tool
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert result["_task_timeline"]["run_id"] == run["run_id"]
    assert result["_task_timeline"]["task_id"] == result["task_id"]
    assert result["_task_timeline"]["status"] == "completed"
    assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == expected_tool
    assert result["_task_timeline"]["tool_calls"][-1]["status"] == "completed"
    assert result["_task_timeline"]["tool_calls"][-1]["output_preview"]["data"]["track"] == "超时空辉夜姬"
    timeline_event_types = [
        event["event_type"] for event in result["_events"]
    ]
    assert timeline_event_types.index("agent.desktop.intent_planned") < timeline_event_types.index(
        "agent.tool.call"
    ) < timeline_event_types.index("agent.desktop.intent_completed")
    assert run["status"] == "completed"
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    launcher_prompts = [
        ("能帮我播放 Apple Music 吗", "bubble"),
        ("给我来点音乐", "bubble"),
        ("帮我用 Apple Music 放一首歌", "live2d"),
        ("用 Apple Music 随便放点歌", "bubble"),
        ("打开 Apple Music 播放音乐", "live2d"),
        ("打开 Apple Music 随便放点音乐", "bubble"),
        ("播放一下 Apple Music 里的歌", "live2d"),
        ("Apple Music 随便放点", "bubble"),
        ("Music app play something", "live2d"),
        ("start playing in Music", "bubble"),
        ("put on some music", "bubble"),
        ("能不能直接播个 Apple Music", "live2d"),
        ("放音乐听听", "live2d"),
        ("听点音乐", "bubble"),
        ("想听音乐", "live2d"),
        ("我想听歌", "bubble"),
        ("听一首歌", "live2d"),
        ("播点东西", "bubble"),
        ("play something", "live2d"),
        ("I want to listen to music", "bubble"),
        ("用 Apple Music 听点音乐", "bubble"),
        ("播放苹果音乐", "live2d"),
    ]
    for prompt, launcher_mode in launcher_prompts:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == expected_summary
        assert agent_task["tool_calls"][-1]["tool_name"] == expected_tool
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert result["_task_timeline"]["run_id"] == run["run_id"]
        assert result["_task_timeline"]["status"] == "completed"
        assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == expected_tool
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert open_and_play_calls == 1 + len(launcher_prompts)


def test_chat_bridge_quick_message_executes_natural_schedule_creation_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    tomorrow = date.today() + timedelta(days=1)
    tomorrow_1000 = f"{tomorrow.isoformat()}T10:00"
    tomorrow_1100 = f"{tomorrow.isoformat()}T11:00"
    calls: list[tuple[str, str, str, str]] = []

    def fake_calendar_create_event(
        title: str,
        *,
        start_at: str,
        end_at: str | None = None,
        calendar_name: str = "",
    ) -> dict:
        calls.append(("calendar", title, start_at, str(end_at or "")))
        return {
            "ok": True,
            "action": "calendar.create_event",
            "summary": "Created calendar event",
            "data": {
                "title": title,
                "start_at": start_at,
                "end_at": str(end_at or ""),
                "calendar_name": calendar_name,
            },
        }

    def fake_reminders_create(title: str, *, due_at: str | None = None, list_name: str = "") -> dict:
        calls.append(("reminder", title, str(due_at or ""), list_name))
        return {
            "ok": True,
            "action": "reminders.create",
            "summary": "Created reminder",
            "data": {
                "title": title,
                "due_at": str(due_at or ""),
                "list_name": list_name,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.calendar_create_event", fake_calendar_create_event)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.reminders_create", fake_reminders_create)

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "创建明天上午10点开会的日程",
        launcher_mode="bubble",
    )

    assert result["ok"] is True
    assert calls == []
    assert agent_task["status"] == "waiting_approval"
    assert agent_task["pending_approvals"][0]["tool_name"] == "calendar.create_event"
    assert agent_task["pending_approvals"][0]["input_preview"] == {
        "title": "开会",
        "start_at": tomorrow_1000,
        "end_at": tomorrow_1100,
    }
    assert run["status"] == "approval_required"
    assert "agent.desktop.intent_approval_required" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "创建明天上午10点开会的提醒",
        launcher_mode="live2d",
    )

    assert result["ok"] is True
    assert calls == []
    assert agent_task["status"] == "waiting_approval"
    assert agent_task["pending_approvals"][0]["tool_name"] == "reminders.create"
    assert agent_task["pending_approvals"][0]["input_preview"] == {
        "title": "开会",
        "due_at": tomorrow_1000,
    }
    assert run["status"] == "approval_required"
    assert "agent.desktop.intent_approval_required" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "明天上午10点提醒我开会",
        launcher_mode="bubble",
    )

    assert result["ok"] is True
    assert calls == []
    assert agent_task["status"] == "waiting_approval"
    assert agent_task["pending_approvals"][0]["tool_name"] == "reminders.create"
    assert agent_task["pending_approvals"][0]["input_preview"] == {
        "title": "开会",
        "due_at": tomorrow_1000,
    }
    assert run["status"] == "approval_required"
    assert "agent.desktop.intent_approval_required" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_music_followup_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    play_calls: list[str] = []
    list_app_queries: list[str] = []
    shortcut_calls: list[tuple[str, str]] = []
    typed_texts: list[str] = []
    submit_calls: list[bool] = []
    music_app_calls: list[str] = []

    def fake_apple_music_play(query: str) -> dict:
        play_calls.append(query)
        return {
            "ok": True,
            "action": "media.apple_music_play",
            "summary": f"Apple Music playing {query}",
            "data": {
                'status': 'played',
                'match_kind': 'track',
                'album': 'Fixture Album',
                "query": query,
                "track": query,
                "artist": "Yachiyo",
                "player_state": "playing",
                "playback_started": True,
                "track_identity_verified": True,
                "catalog_match_verified": True,
                "foreground_action_taken": False,
            },
        }

    def fake_list_apps(query: str = "", limit: int = 200) -> dict:
        list_app_queries.append(query)
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Installed apps matching {query}: Music",
            "data": {
                "query": query,
                "count": 1,
                "apps": [
                    {
                        "name": "Music",
                        "path": "/System/Applications/Music.app",
                        "match_confidence": "high",
                        "match_score": 100,
                    }
                ],
                "best_match": {
                    "name": "Music",
                    "path": "/System/Applications/Music.app",
                    "match_confidence": "high",
                    "match_score": 100,
                },
            },
        }

    def fake_app_open(app_name: str) -> dict:
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_safe_shortcut(action: str) -> dict:
        shortcut_calls.append(("Music", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        typed_texts.append(text)
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": f"Typed {len(text)} characters",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        submit_calls.append(True)
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted foreground search query",
            "data": {"key": "return"},
        }

    def fake_music_app_open_and_play(app_name: str) -> dict:
        music_app_calls.append(app_name)
        query = typed_texts[-1] if typed_texts else ""
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": f"Opened {app_name} and started playback",
            "data": {
                'open_ok': True,
                'playback_ok': True,
                'control': 'play',
                "app_name": app_name,
                "playback_started": True,
                "player_state": "playing",
                "track": query,
                "artist": "Yachiyo",
                "playback_state_unverified": False,
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result("Music", "Search Results")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Music", "Search Results")
        result["data"]["elements"][0].update({"role": "AXButton", "name": "Play"})
        return result

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.apple_music_play",
        fake_apple_music_play,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_search_submit",
        fake_search_submit,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "超时空辉夜姬吧",
        seed_messages=[
            ("user", "能否帮我播放 Apple Music?"),
            ("assistant", "想听哪首歌？"),
        ],
    )

    assert result["ok"] is True
    assert play_calls == ["超时空辉夜姬"]
    assert typed_texts == []
    assert music_app_calls == []
    assert agent_task["tool_calls"][-1]["tool_name"] == "media.apple_music_play"
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == "media.apple_music_play"
    assert result["_task_timeline"]["tool_calls"][-1]["status"] == "completed"
    assert result["_task_timeline"]["tool_calls"][-1]["input_preview"]["query"] == "超时空辉夜姬"
    run_event_types = [event["event_type"] for event in result["_events"]]
    assert run_event_types.index("agent.desktop.intent_planned") < run_event_types.index(
        "agent.tool.call"
    ) < run_event_types.index("agent.desktop.intent_completed")
    assert run["status"] == "completed"
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    direct_prompts = (
        ("我想听超时空辉夜姬吧", "bubble", "超时空辉夜姬"),
        ("播放超时空辉夜姬 Apple Music", "live2d", "超时空辉夜姬"),
        ("放点周杰伦", "bubble", "周杰伦"),
        ("播点轻音乐", "live2d", "轻音乐"),
        ("play some jazz", "bubble", "jazz"),
        ("play Some Nights", "live2d", "Some Nights"),
        ("播个超时空辉夜姬", "live2d", "超时空辉夜姬"),
        ("put some jazz on Apple Music", "bubble", "jazz"),
        ("帮我在 Apple Music 搜一下超时空辉夜姬并播放", "bubble", "超时空辉夜姬"),
        ("Apple Music 搜索超时空辉夜姬并播放", "live2d", "超时空辉夜姬"),
        ("search Space Oddity in Apple Music and play it", "bubble", "Space Oddity"),
        ("Apple Music search Space Oddity and play it", "live2d", "Space Oddity"),
        ("search Apple Music for Taylor Swift and play it", "bubble", "Taylor Swift"),
        ("超时空辉夜姬播放", "bubble", "超时空辉夜姬"),
        ("周杰伦播放一下", "live2d", "周杰伦"),
    )
    for prompt, launcher_mode, query in direct_prompts:
        before_play = len(play_calls)
        before_music_app = len(music_app_calls)
        before_type = len(typed_texts)
        before_submit = len(submit_calls)
        before_list = len(list_app_queries)
        before_shortcut = len(shortcut_calls)
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        dedicated_apple_music = "apple music" in prompt.lower()
        if dedicated_apple_music:
            assert len(play_calls) == before_play + 1
            assert play_calls[-1] == query
            assert len(music_app_calls) == before_music_app
            assert len(typed_texts) == before_type
            expected_tool = "media.apple_music_play"
            expected_input = {"query": query}
        else:
            assert len(play_calls) == before_play
            assert len(music_app_calls) == before_music_app + 1
            assert music_app_calls[-1] == "Music"
            assert len(typed_texts) == before_type + 1
            assert typed_texts[-1] == query
            assert len(submit_calls) == before_submit + 1
            assert len(list_app_queries) == before_list + 1
            assert len(shortcut_calls) == before_shortcut + 1
            expected_tool = "media.music_app_open_and_play"
            expected_input = {"app_name": "Music"}
        action_call = _agent_task_tool_call(agent_task, expected_tool)
        assert expected_input.items() <= action_call["input_preview"].items()
        assert action_call["status"] == "completed"
        timeline_action_call = next(
            call
            for call in reversed(result["_task_timeline"]["tool_calls"])
            if call["tool_name"] == expected_tool
        )
        assert timeline_action_call["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    dedicated_prompt_count = sum(
        1 for prompt, _launcher_mode, _query in direct_prompts if "apple music" in prompt.lower()
    )
    generic_prompt_count = len(direct_prompts) - dedicated_prompt_count
    assert len(play_calls) == 1 + dedicated_prompt_count
    assert len(music_app_calls) == generic_prompt_count
    assert len(submit_calls) == generic_prompt_count
    assert len(list_app_queries) == generic_prompt_count
    assert len(shortcut_calls) == generic_prompt_count


def test_chat_bridge_quick_message_executes_music_control_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    control_calls: list[str] = []

    def fake_system_media_control(action: str) -> dict:
        control_calls.append(action)
        return {
            "ok": True,
            "action": "media.system_control",
            "summary": f"Current media {action} attempted",
            "data": {
                "control": action,
                "media_key_control": "toggle" if action in {"play", "pause"} else action,
                "player_state": "unknown",
                "playback_state_unverified": True,
            },
            "permission_error": False,
            "fallback_used": True,
            "fallback": "system_media_key",
        }

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.system_media_control",
        fake_system_media_control,
    )

    cases = (
        (
            "换首歌",
            "bubble",
            "next",
            "已发送媒体键尝试切到下一首当前媒体，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "换首歌",
            "live2d",
            "next",
            "已发送媒体键尝试切到下一首当前媒体，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "继续放歌",
            "bubble",
            "play",
            "已发送媒体键尝试开始播放当前媒体，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "恢复音乐",
            "live2d",
            "play",
            "已发送媒体键尝试开始播放当前媒体，但无法确认播放状态；请在播放器中确认后重试。",
        ),
        (
            "pause the music",
            "bubble",
            "pause",
            "已发送媒体键尝试暂停当前媒体，但无法确认播放状态；请在播放器中确认后重试。",
        ),
    )
    for prompt, launcher_mode, expected_action, expected_summary in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["summary"] == expected_summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "media.system_control"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"action": expected_action}
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert result["_task_timeline"]["run_id"] == run["run_id"]
        assert result["_task_timeline"]["status"] == "failed"
        assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == "media.system_control"
        assert result["_task_timeline"]["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert control_calls == ["next", "next", "play", "play", "pause"]


def test_chat_bridge_quick_message_executes_app_search_followup_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {"shortcut_action": action, "key": "f", "modifiers": ["command"]},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_active_window() -> dict:
        result = _fake_active_window_result("WeChat", "Search")
        result["data"].update(pid=100, window_id=200)
        return result

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted foreground search query",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_ui_elements(role_filter="", limit=80, app_name="") -> dict:
        current_app = app_name or next((value for action, value in reversed(calls) if action in {"open", "focus"}), 'WeChat')
        query = next((value for action, value in reversed(calls) if action == "type"), "")
        last_type = max((i for i, (action, _) in enumerate(calls) if action == "type"), default=-1)
        submitted = any(action == "search_submit" for action, _ in calls[last_type+1:])
        return _fake_search_ui_result(current_app, query, submitted)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "搜索张三",
        seed_messages=[
            ("user", "打开微信"),
            ("assistant", "已打开 WeChat。"),
        ],
    )

    assert result["ok"] is True
    assert calls == [("focus", "WeChat"), ("shortcut", "find"), ("type", "张三"), ("search_submit", "")]
    assert agent_task["summary"] == (
        "已切到 WeChat 并发送“打开查找”快捷键。 "
        "已向前台输入文字（2 个字符）。 已提交前台搜索。"
    )
    tool_names = [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]]
    for tool_name in (
        "app.focus_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
    ):
        assert tool_name in tool_names
    assert tool_names.index("app.focus_and_safe_shortcut") < tool_names.index(
        "desktop.safe_type_text"
    )
    assert tool_names.index("desktop.safe_type_text") < tool_names.index(
        "desktop.search_submit"
    )
    timeline_tool_names = [
        tool_call["tool_name"] for tool_call in result["_task_timeline"]["tool_calls"]
    ]
    for tool_name in (
        "app.focus_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
    ):
        assert tool_name in timeline_tool_names
    run_event_types = [event["event_type"] for event in result["_events"]]
    assert run_event_types.count("agent.desktop.intent_planned") == 5
    assert run_event_types.index("agent.desktop.intent_planned") < run_event_types.index(
        "agent.tool.call"
    ) < run_event_types.index("agent.desktop.intent_completed")
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_app_search_field_type_without_approval(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {"shortcut_action": action, "key": "f", "modifiers": ["command"]},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                result = _fake_active_window_result(app_name)
                result["data"].update(pid=100, window_id=200)
                return result
        return _fake_active_window_result("Slack")

    def fake_ui_elements(role_filter="", limit=80, app_name="") -> dict:
        current_app = app_name or next((value for action, value in reversed(calls) if action in {"open", "focus"}), 'Slack')
        query = next((value for action, value in reversed(calls) if action == "type"), "")
        last_type = max((i for i, (action, _) in enumerate(calls) if action == "type"), default=-1)
        submitted = any(action == "search_submit" for action, _ in calls[last_type+1:])
        result = _fake_search_ui_result(current_app, query, submitted)
        if current_app == "Slack":
            result["data"]["elements"][0]["name"] = "搜索框"
        return result
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    # Explicit semantic field clicking keeps its approval, unlike Cmd-F.
    click_calls = []
    def fake_click_ui_element(target, *, role_filter="", limit=80, click_count=1, expected_app_name=""):
        click_calls.append((target, role_filter, limit, click_count))
        calls.append(("click", target))
        return {"ok": True, "action": "desktop.click_ui_element", "data": {"target": target, "role_filter": role_filter, "click_count": click_count}}
    def fake_inspect_app(app_name, *, open_if_needed=True, focus=True, role_filter="", limit=80):
        return {"ok": True, "action": "desktop.inspect_app", "data": {
            "app_name": app_name, "app_found": True, "running": True,
            "focus_verified": True, "ready_for_foreground_action": True,
            "ui_elements": fake_ui_elements(app_name=app_name),
        }}
    monkeypatch.setattr(desktop_tools, "inspect_app", fake_inspect_app)
    monkeypatch.setattr(desktop_tools, "click_ui_element", fake_click_ui_element)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeNoDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no main model for explicit search")))
    monkeypatch.setattr("apps.shell.chat_api.desktop_permission_missing_by_capability", lambda use_cache=True: {})
    store = ChatStore(db_path=str(tmp_path / "slack-search-chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(db_path=tmp_path / "slack-search-runtime.db", workspace_dir=tmp_path / "slack-search-runtime", credential_store=MemoryCredentialStore(), seed_templates=False)
    runtime.agent_runtime_service = service
    try:
        initial = ChatBridge(runtime).send_quick_message("打开 Slack 点击搜索框输入 yachiyo 并搜索", metadata={"source": "launcher", "launcher_mode": "live2d", "launcher_surface": "quick_message", "allow_user_foreground_takeover": True})
        initial_task = initial["agent_task"]
        initial_run = service.get_run(initial["run_id"])
        assert initial_task["status"] == "waiting_approval"
        assert initial_run["pending_approval"]["tool"] == "app.open_and_click_ui_element"
        assert click_calls == []
        assert not any(action in {"type", "search_submit", "shortcut"} for action, _ in calls)
        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(initial["task_id"], _approval_decision_for_run(initial_run))
        agent_task = approved.model_dump(mode="json")
        run = service.get_run(initial["run_id"])
        result = {**initial, "agent_task": agent_task}
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
    finally:
        service.close()
        store.close()

    assert result["ok"] is True
    assert calls == [
        ("open", "Slack"),
        ("focus", "Slack"),
        ("click", "搜索框"),
        ("type", "yachiyo"),
        ("search_submit", ""),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert "yachiyo" in str([call for call in calls if call[0] == "type"])
    assert click_calls == [("搜索框", "text", 80, 1)]
    tool_names = [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]]
    assert {"app.open_and_click_ui_element", "desktop.safe_type_text", "desktop.search_submit"} <= set(tool_names)
    successful = [event["payload"]["tool"] for event in events if event["event_type"] == "agent.tool.call" and event["payload"].get("result", {}).get("ok") is True]
    assert successful.index("app.open_and_click_ui_element") < successful.index("desktop.safe_type_text") < successful.index("desktop.search_submit")
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Finder 找下载文件",
        launcher_mode="live2d",
    )

    assert result["ok"] is True
    assert calls[-5:] == [
        ("open", "Finder"),
        ("focus", "Finder"),
        ("shortcut", "find"),
        ("type", "下载文件"),
        ("search_submit", ""),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已打开 Finder 并发送“打开查找”快捷键。 已向前台输入文字（4 个字符）。 已提交前台搜索。"
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"] if tool_call["tool_name"] in {"app.open_and_safe_shortcut", "desktop.safe_type_text", "desktop.search_submit"}] == [
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
    ]
    timeline_tool_names = [
        call["tool_name"] for call in result["_task_timeline"]["tool_calls"]
    ]
    assert "app.open_and_safe_shortcut" in timeline_tool_names
    assert timeline_tool_names.index("app.open_and_safe_shortcut") < timeline_tool_names.index(
        "desktop.safe_type_text"
    )
    assert any(event["payload"].get("tool") == "desktop.ui_elements" for event in result["_events"] if event["event_type"] == "agent.tool.call")
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.call" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    launcher_cases = [
        ("在微信搜索文件传输助手", "bubble", "focus", "WeChat", "文件传输助手"),
        ("Apple Music 搜索超时空辉夜姬", "live2d", "focus", "Music", "超时空辉夜姬"),
        ("Finder 找下载文件", "bubble", "focus", "Finder", "下载文件"),
    ]
    for prompt, launcher_mode, app_action, app_name, typed_text in launcher_cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert calls[-4:] == [
            (app_action, app_name),
            ("shortcut", "find"),
            ("type", typed_text),
            ("search_submit", ""),
        ]
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        open_summary = (
            f"已切到 {app_name} 并发送“打开查找”快捷键"
            if app_action == "focus"
            else f"已打开 {app_name} 并发送“打开查找”快捷键"
        )
        assert agent_task["summary"] == (
            f"{open_summary}。 "
            f"已向前台输入文字（{len(typed_text)} 个字符）。 已提交前台搜索。"
        )
        tool_names = [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]]
        assert f"app.{app_action}_and_safe_shortcut" in tool_names
        assert "desktop.safe_type_text" in tool_names
        assert "desktop.search_submit" in tool_names
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types


def test_chat_bridge_quick_message_prepares_comm_message_then_waits_for_send_approval(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_active_window() -> dict:
        app_name = next(
            (
                value
                for action, value in reversed(calls)
                if action in {"open", "focus"}
            ),
            "WeChat",
        )
        return {**_fake_active_window_result(app_name), "data": {"app_name": app_name, "title": app_name, "pid": 100, "window_id": 200}}

    def fake_ui_elements(**kwargs) -> dict:
        app = kwargs.get("app_name") or next((value for action, value in reversed(calls) if action in {"open", "focus"}), "WeChat")
        typed = [(i, value) for i, (action, value) in enumerate(calls) if action == "type"]
        submits = [i for i, (action, _) in enumerate(calls) if action == "search_submit"]
        submitted = submits[-1] if submits else -1
        query = next((value for i, value in reversed(typed) if submitted < 0 or i < submitted), "")
        message = next((value for i, value in reversed(typed) if i > submitted and submitted >= 0), "")
        pasted = any(action == "shortcut" and value == "paste" for action, value in calls)
        if pasted:
            message = "clipboard content"
        search_active = any(action == "shortcut" and value == "find" for action, value in calls)
        elements = [{"role": "AXTextField", "name": "Search", "value": query, "depth": 1, "editable": True, "focused": submitted < 0 and search_active, "center": {"x": 320, "y": 240}}]
        if submitted >= 0:
            elements += [{"role": "AXTable", "name": "Search Results", "depth": 1}, {"role": "AXRow", "name": query, "depth": 2}]
        if not search_active:
            elements = []
        # A simple body-only goal types straight into the message composer.
        if not submits and typed and not any(action == "shortcut" and value == "find" for action, value in calls):
            message = typed[-1][1]
        elements.append({"role": "AXTextArea", "name": "Message", "value": message,
                         "focused": not search_active or submitted >= 0, "editable": True, "depth": 1, "center": {"x": 320, "y": 480}})
        return _with_native_focused_ui_element({"ok": True, "action": "desktop.ui_elements", "data": {"app_name": app, "pid": 100, "window_id": 200, "elements": elements}})
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-comm-approval.db",
        workspace_dir=tmp_path / "runtime-comm-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher communication compose should not call model")
        ),
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_submit_foreground",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("submit_foreground should wait for approval")
        ),
    )
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "微信给张三说你好",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        agent_task = result["agent_task"]
        link = service.get_task_run_link(result["task_id"])
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        second = bridge.send_quick_message(
            "打开 Slack 给 Alice 说 hello",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        second_task = second["agent_task"]
        second_link = service.get_task_run_link(second["task_id"])
        second_run = service.get_run(second_link["run_id"])
        second_event_types = [
            event["event_type"]
            for event in service.list_run_events(second_run["run_id"], include_internal=True)["events"]
        ]
    finally:
        service.close()
        store.close()

    assert result["ok"] is True
    assert calls[:5] == [
        ("focus", "WeChat"),
        ("shortcut", "find"),
        ("type", "张三"),
        ("search_submit", ""),
        ("type", "你好"),
    ]
    assert agent_task["status"] == "waiting_approval"
    assert agent_task["needs_user_action"] is True
    assert agent_task["pending_approvals"][0]["tool_name"] == "desktop.submit_foreground"
    assert run["status"] == "approval_required"
    assert run["pending_approval"]["tool"] == "desktop.submit_foreground"
    assert run["pending_approval"]["input_preview"] == {"action": "send"}
    assert "agent.desktop.intent_approval_required" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    assert second["ok"] is True
    assert calls[-5:] == [
        ("open", "Slack"),
        ("shortcut", "find"),
        ("type", "Alice"),
        ("search_submit", ""),
        ("type", "hello"),
    ]
    assert second_task["status"] == "waiting_approval"
    assert second_task["needs_user_action"] is True
    assert second_task["pending_approvals"][0]["tool_name"] == "desktop.submit_foreground"
    assert second_run["status"] == "approval_required"
    assert second_run["pending_approval"]["tool"] == "desktop.submit_foreground"
    assert second_run["pending_approval"]["input_preview"] == {"action": "send"}
    assert "agent.desktop.intent_approval_required" in second_event_types
    assert "agent.desktop.intent_completed" not in second_event_types
    assert "model.request.started" not in second_event_types
    assert "model.requested" not in second_event_types


def test_chat_bridge_quick_message_executes_foreground_search_type_submit_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {"shortcut_action": action, "key": "f", "modifiers": ["command"]},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Google Chrome", "Search Results")
        result["data"]["elements"][0]["value"] = "yachiyo"
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "点搜索框输入 yachiyo 然后搜索",
    )

    assert result["ok"] is True
    assert calls == [("shortcut", "find"), ("type", "yachiyo"), ("search_submit", "")]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已发送“打开查找”快捷键。 已向前台输入文字（7 个字符）。 已提交前台搜索。"
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-3:]] == [
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
    ]
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert event_types.count("agent.desktop.intent_planned") == 3
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_browser_prefix_search_field_type_without_click(
    tmp_path, monkeypatch,
):
    # Explicitly named field clicks preserve their semantic approval.
    calls = []
    state = {"app": "Google Chrome", "query": "", "focused": False, "submitted": False}

    def app_action(action, app_name):
        state["app"] = app_name
        calls.append((action, app_name))
        return {"ok": True, "action": f"app.{action}", "data": {"app_name": app_name}}

    def ui(**kwargs):
        result = _fake_search_ui_result(state["app"], state["query"], state["submitted"])
        result["data"]["elements"][0].update(name="搜索", focused=state["focused"])
        if state["focused"]:
            result["data"]["focused_element"] = dict(result["data"]["elements"][0])
        return result

    def inspect(app_name, **kwargs):
        state["app"] = app_name
        result = ui()
        return {"ok": True, "action": "desktop.inspect_app", "data": {"app_name": app_name, "pid": 100, "window_id": 200, "ui_elements": result}}

    def click(target, *, role_filter="", limit=80, click_count=1, expected_app_name=""):
        assert target == "搜索" and expected_app_name == state["app"]
        calls.append(("click", target))
        state["focused"] = True
        return {"ok": True, "action": "desktop.click_ui_element", "data": {
            "app_name": state["app"], "pid": 100, "window_id": 200,
            "target": target, "role_filter": role_filter, "click_count": click_count,
            "matched_element": {"role": "AXTextField", "name": "搜索", "center": {"x": 320, "y": 240}},
        }}

    def type_text(text):
        assert state["focused"]
        calls.append(("type", text))
        state["query"] = text
        return {"ok": True, "action": "desktop.safe_type_text", "data": {"character_count": len(text), "explicit_user_text": True}}

    def submit():
        assert state["focused"] and state["query"]
        calls.append(("search_submit", ""))
        state["submitted"] = True
        return {"ok": True, "action": "desktop.search_submit", "data": {"key": "return", "modifiers": []}}

    def catalog(query="", limit=20):
        name = {"Chrome": "Google Chrome"}.get(query, query)
        match = {"name": name, "path": f"/Applications/{name}.app"}
        return {"ok": True, "action": "desktop.list_apps", "data": {"query": query, "apps": [match], "best_match": match}}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", catalog)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", lambda: {"ok": True, "action": "desktop.running_apps", "data": {"apps": [{"name": state["app"], "pid": 100}]}})
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: {"ok": True, "action": "desktop.active_window", "data": {"app_name": state["app"], "pid": 100, "window_id": 200}})
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", lambda name: app_action("open", name))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", lambda name: app_action("focus", name))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", inspect)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", ui)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.click_ui_element", click)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", lambda *_a, **_kw: pytest.fail("An explicit field click must not invent Cmd-F"))
    monkeypatch.setattr("apps.shell.agent.tools.browser.click", lambda *_a, **_kw: pytest.fail("Native app field clicks must not take a browser tab"))
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeNoDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_a, **_kw: pytest.fail("Explicit app field search must not call a model"))
    monkeypatch.setattr("apps.shell.chat_api.desktop_permission_missing_by_capability", lambda use_cache=True: {})

    for index, (goal, app, mode, expected, click_tool) in enumerate((
        ("Chrome 点击搜索框输入 yachiyo", "Google Chrome", "bubble", [("focus", "Google Chrome"), ("click", "搜索"), ("type", "yachiyo")], "app.focus_and_click_ui_element"),
        ("打开 Safari 点击搜索栏输入 yachiyo 并搜索", "Safari", "live2d", [("open", "Safari"), ("focus", "Safari"), ("click", "搜索"), ("type", "yachiyo"), ("search_submit", "")], "app.open_and_click_ui_element"),
    )):
        calls.clear()
        state.update(app=app, query="", focused=False, submitted=False)
        store = ChatStore(db_path=str(tmp_path / f"browser-prefix-{index}.db"))
        runtime = _runtime_with_chat_store(store)
        service = AgentRuntimeService(db_path=tmp_path / f"browser-prefix-runtime-{index}.db", workspace_dir=tmp_path / f"browser-prefix-work-{index}", credential_store=MemoryCredentialStore(), seed_templates=False)
        runtime.agent_runtime_service = service
        try:
            result = ChatBridge(runtime).send_quick_message(goal, metadata={"source": "launcher", "launcher_mode": mode, "launcher_surface": "quick_message", "allow_user_foreground_takeover": True})
            run = service.get_run(result["run_id"])
            assert result["agent_task"]["status"] == "waiting_approval"
            assert run["pending_approval"]["tool"] == click_tool
            assert calls == []
            assert not state["focused"] and not state["query"]
            approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(result["task_id"], _approval_decision_for_run(run))
            task = approved.model_dump(mode="json")
            assert calls == expected
            assert task["status"] == "completed", task["summary"]
            assert task["pending_approvals"] == []
            assert service.get_run(result["run_id"])["status"] == "completed"
            events = service.list_run_events(result["run_id"], include_internal=True)["events"]
            assert not any(e["event_type"] in {"model.request.started", "model.requested"} for e in events)
            assert any(e["event_type"] == "agent.tool.call" and e["payload"].get("tool") == "desktop.ui_elements" for e in events)
            assert all(e.get("visibility") != "internal" for e in service.list_run_events(result["run_id"])["events"])
        finally:
            service.close()
            store.close()


def test_chat_bridge_quick_message_executes_browser_read_followup_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    extract_calls: list[str] = []

    def fake_extract_text(selector: str = "") -> dict:
        extract_calls.append(selector)
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 29 characters from browser page",
            "data": {
                "selector": selector,
                "text": "Yachiyo desktop agent runtime",
                "truncated": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "读取内容",
        seed_messages=[
            ("user", "打开 GitHub"),
            ("assistant", "已打开 GitHub。"),
        ],
    )

    assert result["ok"] is True
    assert extract_calls == []
    assert agent_task["status"] == "failed"
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.extract_text"
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == "browser.extract_text"
    assert run["status"] == "failed"
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    for prompt, launcher_mode in (
        ("总结当前网页", "bubble"),
        ("what is this page about", "live2d"),
    ):
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert result["ok"] is True
        assert agent_task["status"] == "failed"
        assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.extract_text"
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert result["_task_timeline"]["tool_calls"][-1]["tool_name"] == "browser.extract_text"
        assert not any(
            event["event_type"] == "agent.desktop.intent_completed"
            for event in result["_task_timeline"]["events"]
        )
        assert run["status"] == "failed"
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert extract_calls == []


def test_chat_bridge_quick_message_requires_approval_for_browser_click_followup(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-click-followup.db",
        workspace_dir=tmp_path / "runtime-browser-click-followup",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    runtime.chat_session.add_user_message("打开 GitHub")
    runtime.chat_session.add_assistant_message("已打开 GitHub。")
    focus_calls: list[str] = []
    click_calls: list[tuple[str, int]] = []
    ui_click_calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser click follow-up should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_browser_click(selector: str, **kwargs: Any) -> dict:
        click_calls.append((selector, int(kwargs.get("click_count") or 1)))
        return {
            "ok": True,
            "action": "browser.click",
            "summary": "Clicked browser selector",
            "data": {
                "selector": selector,
                "label": "登录",
                "tag": "BUTTON",
            },
        }

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        click_count: int = 1,
        expected_app_name: str = "",
    ) -> dict:
        ui_click_calls.append((target, role_filter, limit, click_count))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked UI element: {target}",
            "data": {"target": target, "label": target, "role": role_filter or "button"},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {"app_name": app_name, "focus_verified": focus},
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "Google Chrome")

    def fake_ui_elements(**_kwargs: Any) -> dict:
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "Google Chrome",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "登录",
                        "enabled": True,
                    }
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.click_ui_element", fake_click_ui_element)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.browser.click", fake_browser_click)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "点登录",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert click_calls == []
        assert inspect_calls == []
        assert focus_calls == []
        assert ui_click_calls == []
        assert waiting_task["status"] == "failed"
        assert waiting_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
        assert waiting_task["needs_user_action"] is False
        assert waiting_task["pending_approvals"] == []
        assert waiting_run["status"] == "failed"
        assert waiting_run["pending_approval"] == {}
        event_types = [
            event["event_type"]
            for event in service.list_run_events(waiting_run["run_id"], include_internal=True)["events"]
        ]
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_opens_browser_then_requires_approval_for_page_click(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-open-click.db",
        workspace_dir=tmp_path / "runtime-browser-open-click",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    open_calls: list[str] = []
    focus_calls: list[str] = []
    click_calls: list[tuple[str, int]] = []
    ui_click_calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser open+click should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_browser_click(selector: str, **kwargs: Any) -> dict:
        click_calls.append((selector, int(kwargs.get("click_count") or 1)))
        return {
            "ok": True,
            "action": "browser.click",
            "summary": "Clicked browser selector",
            "data": {
                "selector": selector,
                "label": "登录",
                "tag": "BUTTON",
            },
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        click_count: int = 1,
        expected_app_name: str = "",
    ) -> dict:
        ui_click_calls.append((target, role_filter, limit, click_count))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked UI element: {target}",
            "data": {"target": target, "label": target, "role": role_filter or "button"},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {"app_name": app_name, "focus_verified": focus},
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "Google Chrome")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read visible login button",
            "data": {
                "app_name": app_name or "Google Chrome",
                "title": "Login",
                "count": 1,
                "elements": [
                    {"role": "AXButton", "name": "登录", "value": "登录"},
                ],
                "visibility_status": "visible",
                "role_filter": role_filter,
                "limit": limit,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.click_ui_element", fake_click_ui_element)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.browser.click", fake_browser_click)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 Chrome 点击登录按钮",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert inspect_calls == []
        assert open_calls == []
        assert focus_calls == []
        assert click_calls == []
        assert ui_click_calls == []
        assert waiting_task["status"] == "failed"
        assert waiting_task["summary"].startswith(
            _BACKGROUND_DESKTOP_PROVIDER_HANDOFF_SUMMARY
        )
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"] == []
        assert waiting_run["status"] == "failed"
        assert waiting_run["pending_approval"] == {}
        event_types = [
            event["event_type"]
            for event in service.list_run_events(waiting_run["run_id"], include_internal=True)["events"]
        ]

        assert open_calls == []
        assert focus_calls == []
        assert click_calls == []
        assert ui_click_calls == []
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "agent.desktop.intent_unavailable" in event_types
        assert "agent.desktop.permission_recovery" not in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_searches_then_requires_approval_for_first_result_click(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-search-click.db",
        workspace_dir=tmp_path / "runtime-browser-search-click",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    app_open_calls: list[str] = []
    open_calls: list[str] = []
    click_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser search+click should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_open(app_name: str) -> dict:
        app_open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_open_url(url: str) -> dict:
        open_calls.append(url)
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened browser page: {url}",
            "data": {
                "url": url,
                "title": "Search",
                "target_id": "target-search-owned",
                "target_websocket_available": True,
            },
        }

    def fake_browser_click(selector: str, **kwargs: Any) -> dict:
        click_calls.append((selector, int(kwargs.get("click_count") or 1)))
        return {
            "ok": True,
            "action": "browser.click",
            "summary": "Clicked browser selector",
            "data": {
                "selector": selector,
                "label": "Yachiyo result",
                "tag": "A",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    monkeypatch.setattr("apps.shell.agent.tools.browser.click", fake_browser_click)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 Chrome 搜索 yachiyo 然后打开第一个结果",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert app_open_calls == []
        assert open_calls == ["https://www.google.com/search?q=yachiyo"]
        assert click_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert any(
            call["tool_name"] == "browser.open_url" and call["status"] == "completed"
            for call in waiting_task["tool_calls"]
        )
        assert waiting_task["pending_approvals"][0]["tool_name"] == "browser.click"
        pending_input = waiting_task["pending_approvals"][0]["input_preview"]
        assert pending_input == {"selector": "search-result=1", "click_count": 1}
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "browser.click"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert app_open_calls == []
        assert open_calls == ["https://www.google.com/search?q=yachiyo"]
        assert click_calls == [("search-result=1", 1)]
        assert approved.status == "completed", approved.summary
        assert approved.summary == (
            "已打开网页：https://www.google.com/search?q=yachiyo。 "
            "已点击网页元素：Yachiyo result。"
        )
        assert approved.needs_user_action is False
        assert approved.pending_approvals == []
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_app_scoped_ui_click(
    tmp_path,
    monkeypatch,
):
    # Static control presence does not prove the semantic effect of a click.
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-ui-click.db",
        workspace_dir=tmp_path / "runtime-app-ui-click",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    focus_calls: list[str] = []
    click_calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app-scoped UI click should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {
                "app_name": app_name,
                "app_found": True,
                "running": True,
                "focus_verified": focus,
                "ready_for_foreground_action": True,
                "ui_elements": fake_ui_elements(role_filter=role_filter, limit=limit, app_name=app_name),
            },
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        click_count: int = 1,
        expected_app_name: str = "",
    ) -> dict:
        click_calls.append((target, role_filter, limit, click_count))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked {target}",
            "data": {"target": target, "role_filter": role_filter},
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "Slack")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        observed_app = app_name or (focus_calls[-1] if focus_calls else "Slack")
        observed_target = "搜索框" if observed_app == "WeChat" else (click_calls[-1][0] if click_calls else "Send")
        observed_role = "text" if observed_app == "WeChat" else (click_calls[-1][1] if click_calls else "button")
        result = _fake_ui_elements_result(observed_app, observed_app)
        result["data"]["elements"][0].update(
            {
                "role": "AXTextField" if observed_role == "text" else "AXButton",
                "name": observed_target,
                "value": observed_target,
            }
        )
        result["data"]["role_filter"] = role_filter
        result["data"]["limit"] = limit
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.click_ui_element",
        fake_click_ui_element,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "click the Send button in Slack",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert inspect_calls == [("Slack", True, True, "button", 80)]
        assert focus_calls == []
        assert click_calls == []
        assert waiting_task["status"] == "waiting_approval", waiting_task["summary"]
        assert waiting_task["needs_user_action"] is True
        assert len(waiting_task["pending_approvals"]) == 1
        assert waiting_task["pending_approvals"][0]["tool_name"] == (
            "app.focus_and_click_ui_element"
        )
        pending_input = waiting_task["pending_approvals"][0]["input_preview"]
        assert pending_input == {
            "app_name": "Slack",
            "target": "Send",
            "role_filter": "button",
            "limit": 80,
            "click_count": 1,
        }
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "app.focus_and_click_ui_element"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert focus_calls == ["Slack"]
        assert click_calls == [("Send", "button", 80, 1)]
        assert approved.status == "failed"
        assert approved.pending_approvals == []
        assert run["status"] == "failed"
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

        second = bridge.send_quick_message(
            "微信点击搜索框",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        second_task_id = second["task_id"]
        second_waiting_task = second["agent_task"]
        second_link = service.get_task_run_link(second_task_id)
        second_waiting_run = service.get_run(second_link["run_id"])

        assert second["ok"] is True
        assert inspect_calls[-1] == ("WeChat", True, True, "text", 80)
        assert ("Slack", True, True, "button", 80) in inspect_calls
        assert focus_calls == ["Slack"]
        assert click_calls == [("Send", "button", 80, 1)]
        assert second_waiting_task["status"] == "waiting_approval"
        assert second_waiting_task["needs_user_action"] is True
        assert len(second_waiting_task["pending_approvals"]) == 1
        assert second_waiting_task["pending_approvals"][0]["tool_name"] == (
            "app.focus_and_click_ui_element"
        )
        second_pending_input = second_waiting_task["pending_approvals"][0]["input_preview"]
        assert second_pending_input == {
            "app_name": "WeChat",
            "target": "搜索框",
            "role_filter": "text",
            "limit": 80,
            "click_count": 1,
        }
        assert second_waiting_run["status"] == "approval_required"
        assert second_waiting_run["pending_approval"]["tool"] == "app.focus_and_click_ui_element"

        second_approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            second_task_id,
            _approval_decision_for_run(second_waiting_run),
        )
        second_run = service.get_run(second_link["run_id"])
        second_event_types = [
            event["event_type"]
            for event in service.list_run_events(second_run["run_id"], include_internal=True)["events"]
        ]

        assert focus_calls == ["Slack", "WeChat"]
        assert click_calls == [("Send", "button", 80, 1), ("搜索框", "text", 80, 1)]
        assert second_approved.status == "failed"
        assert second_approved.pending_approvals == []
        verification_events = [
            event
            for event in second_run["timeline"]
            if event.get("event") == "agent.tool.call"
            and event.get("detail") == "desktop.ui_elements"
            and event.get("result", {}).get("ok") is True
        ]
        verification_input = verification_events[-1]["input_preview"]
        assert {
            key: verification_input[key]
            for key in ("app_name", "role_filter", "limit")
        } == {"app_name": "WeChat", "role_filter": "text", "limit": 80}
        assert "selection_source" not in verification_input
        assert "query" not in verification_input
        assert second_run["status"] == "failed"
        assert "agent.desktop.intent_approval_required" in second_event_types
        assert "agent.desktop.intent_completed" not in second_event_types
        assert "agent.replan.requested" not in second_event_types
        assert "desktop.provider_session.required" not in second_event_types
        assert "model.request.started" not in second_event_types
        assert "model.requested" not in second_event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_app_open_ui_click(
    tmp_path,
    monkeypatch,
):
    # Static control presence does not prove the semantic effect of a click.
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-open-ui-click.db",
        workspace_dir=tmp_path / "runtime-app-open-ui-click",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    open_calls: list[str] = []
    focus_calls: list[str] = []
    click_calls: list[tuple[str, str, int, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app open UI click should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        click_count: int = 1,
        expected_app_name: str = "",
    ) -> dict:
        click_calls.append((target, role_filter, limit, click_count))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked {target}",
            "data": {"target": target, "role_filter": role_filter},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {
                "app_name": app_name,
                "app_found": True,
                "running": True,
                "focus_verified": focus,
                "ready_for_foreground_action": True,
                "ui_elements": fake_ui_elements(role_filter=role_filter, limit=limit, app_name=app_name),
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "Slack")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Slack", "Search")
        result["data"]["elements"][0].update(
            {"role": "AXButton", "name": "搜索"}
        )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.click_ui_element",
        fake_click_ui_element,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 Slack 点搜索",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert inspect_calls == [("Slack", True, True, "button", 80)]
        assert open_calls == []
        assert focus_calls == []
        assert click_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == (
            "app.focus_and_click_ui_element"
        )
        assert waiting_task["pending_approvals"][0]["input_preview"] == {
            "app_name": "Slack",
            "target": "搜索",
            "role_filter": "button",
            "limit": 80,
            "click_count": 1,
        }
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "app.focus_and_click_ui_element"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert open_calls == []
        assert focus_calls == ["Slack"]
        assert click_calls == [("搜索", "button", 80, 1)]
        assert approved.status == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_continues_after_app_open_non_search_ui_click_approval(
    tmp_path,
    monkeypatch,
):
    # Static control presence does not prove the semantic effect of a click.
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-open-ui-click-then-type.db",
        workspace_dir=tmp_path / "runtime-app-open-ui-click-then-type",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    open_calls: list[str] = []
    focus_calls: list[str] = []
    click_calls: list[tuple[str, str, int, int]] = []
    typed_text: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app open UI click then type should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        click_count: int = 1,
        expected_app_name: str = "",
    ) -> dict:
        click_calls.append((target, role_filter, limit, click_count))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked {target}",
            "data": {"target": target, "role_filter": role_filter},
        }

    def fake_safe_type_text(text: str) -> dict:
        typed_text.append(text)
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {
                "app_name": app_name,
                "app_found": True,
                "running": True,
                "focus_verified": focus,
                "ready_for_foreground_action": True,
                "ui_elements": fake_ui_elements(role_filter=role_filter, limit=limit, app_name=app_name),
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "Slack")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Slack", "General")
        result["data"]["elements"] = [
            {
                "role": "AXButton",
                "name": "频道",
                "center": {"x": 320, "y": 240},
            },
            {
                "role": "AXTextField",
                "name": "Message",
                "value": typed_text[-1] if typed_text else "",
                "center": {"x": 560, "y": 720},
            },
        ]
        result["data"]["count"] = 2
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.click_ui_element",
        fake_click_ui_element,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 Slack 点频道输入 yachiyo",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert inspect_calls == [("Slack", True, True, "", 80)]
        assert open_calls == []
        assert focus_calls == []
        assert click_calls == []
        assert typed_text == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == (
            "app.focus_and_click_ui_element"
        )
        assert waiting_task["pending_approvals"][0]["input_preview"] == {
            "app_name": "Slack",
            "target": "频道",
            "role_filter": "",
            "limit": 80,
            "click_count": 1,
        }
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "app.focus_and_click_ui_element"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert open_calls == []
        assert focus_calls == ["Slack"]
        assert click_calls == [("频道", "", 80, 1)]
        assert typed_text == ["yachiyo"]
        assert approved.status == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_app_open_type_into_ui_element(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-open-type-into-ui.db",
        workspace_dir=tmp_path / "runtime-app-open-type-into-ui",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []
    open_calls: list[str] = []
    focus_calls: list[str] = []
    type_calls: list[tuple[str, str, str, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app open UI type should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )

    def fake_app_open(app_name: str) -> dict:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_type_into_ui_element(
        target: str,
        text: str,
        *,
        role_filter: str = "",
        limit: int = 80,
        expected_app_name: str = "",
    ) -> dict:
        type_calls.append((target, text, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.type_into_ui_element",
            "summary": f"Typed into {target}",
            "data": {
                "app_name": "WeChat",
                "target": target,
                "text": text,
                "role_filter": role_filter,
                "character_count": len(text),
            },
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {
                "app_name": app_name,
                "app_found": True,
                "running": True,
                "focus_verified": focus,
                "ready_for_foreground_action": True,
                "ui_elements": fake_ui_elements(role_filter=role_filter, limit=limit, app_name=app_name),
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(focus_calls[-1] if focus_calls else "WeChat")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "WeChat", "Chat")
        result["data"]["elements"][0].update(
            {
                "role": "AXTextField",
                "name": "消息框",
                "value": "文件传输助手" if type_calls else "",
            }
        )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.type_into_ui_element",
        fake_type_into_ui_element,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开微信在消息框输入文件传输助手",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert inspect_calls == [("WeChat", True, True, "text", 80)]
        assert open_calls == []
        assert focus_calls == []
        assert type_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == (
            "app.open_and_type_into_ui_element"
        )
        assert waiting_task["pending_approvals"][0]["input_preview"] == {
            "app_name": "WeChat",
            "target": "消息框",
            "text": "文件传输助手",
            "role_filter": "text",
            "limit": 80,
        }
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "app.open_and_type_into_ui_element"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert open_calls == ["WeChat"]
        assert focus_calls == ["WeChat"]
        assert type_calls == [("消息框", "文件传输助手", "text", 80)]
        assert approved.status == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_app_scoped_search_field_type(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    inspect_calls: list[tuple[str, bool, bool, str, int]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {"shortcut_action": action, "key": "f", "modifiers": ["command"]},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        result = _fake_ui_elements_result(app_name or "WeChat")
        result["data"]["elements"][0].update({"name": "搜索框", "value": ""})
        return result

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: bool = True,
        focus: bool = True,
        role_filter: str = "",
        limit: int = 80,
    ) -> dict:
        inspect_calls.append((app_name, open_if_needed, focus, role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Inspected {app_name}",
            "data": {
                "app_name": app_name,
                "app_found": True,
                "running": True,
                "focus_verified": focus,
                "ready_for_foreground_action": True,
                "ui_elements": fake_ui_elements(role_filter=role_filter, limit=limit, app_name=app_name),
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开微信搜索框输入文件传输助手",
    )

    assert result["ok"] is True
    assert inspect_calls == [("WeChat", True, True, "text", 80)]
    assert calls == []
    assert agent_task["status"] == "waiting_approval"
    assert agent_task["needs_user_action"] is True
    assert agent_task["pending_approvals"][0]["tool_name"] == "app.open_and_type_into_ui_element"
    assert agent_task["pending_approvals"][0]["input_preview"] == {
        "app_name": "WeChat",
        "target": "搜索框",
        "text": "文件传输助手",
        "role_filter": "text",
        "limit": 80,
    }
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-1:]] == [
        "app.open_and_type_into_ui_element",
    ]
    assert run["status"] == "approval_required"
    assert run["pending_approval"]["tool"] == "app.open_and_type_into_ui_element"
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.desktop.intent_approval_required" in event_types
    assert "agent.tool.approval_required" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_browser_open_url_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    opened_urls: list[str] = []

    def fake_open_url(url: str) -> dict:
        opened_urls.append(url)
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-test-owned",
                "target_websocket_available": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开浏览器并访问 GitHub",
    )

    assert opened_urls == ["https://github.com"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开网页：https://github.com。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    local_url_cases = (
        ("打开 127.0.0.1:5173", "bubble", "http://127.0.0.1:5173"),
        ("打开本地 127.0.0.1:5173", "bubble", "http://127.0.0.1:5173"),
        ("打开网页 github.com", "live2d", "https://github.com"),
        ("open 192.168.1.10:8000/status", "live2d", "http://192.168.1.10:8000/status"),
        ("Can you search Chrome for weather?", "bubble", "https://www.google.com/search?q=weather"),
    )
    for prompt, launcher_mode, url in local_url_cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == f"已打开网页：{url}。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"url": url}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "百度 open hanako",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "已打开网页：https://www.baidu.com/s?wd=open+hanako。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url"
        assert agent_task["tool_calls"][-1]["input_preview"]["url"] == "https://www.baidu.com/s?wd=open+hanako"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert opened_urls == [
        "https://github.com",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5173",
        "https://github.com",
        "http://192.168.1.10:8000/status",
        "https://www.google.com/search?q=weather",
        "https://www.baidu.com/s?wd=open+hanako",
        "https://www.baidu.com/s?wd=open+hanako",
    ]


def test_chat_bridge_quick_message_executes_address_bar_url_without_approval(
    tmp_path,
    monkeypatch,
):
    opened_urls: list[str] = []

    def fake_open_url(url: str) -> dict:
        opened_urls.append(url)
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-test-owned",
                "target_websocket_available": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "在地址栏输入 github.com 并回车",
    )

    assert opened_urls == ["https://github.com"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已打开网页：https://github.com。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url"
    assert run["status"] == "completed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_completed" in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_browser_open_url_and_extract_text(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_open_url(url: str) -> dict:
        calls.append(("open", url))
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-test-owned",
                "target_websocket_available": True,
            },
        }

    def fake_extract_text(selector: str = "") -> dict:
        calls.append(("extract", selector))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 31 characters from browser page",
            "data": {
                "selector": selector,
                "text": "GitHub page text for Yachiyo",
                "truncated": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    cases = [
        ("打开 GitHub 并读一下页面", "live2d", "https://github.com", "GitHub page text for Yachiyo", ""),
        ("打开 GitHub 看看内容", "bubble", "https://github.com", "GitHub page text for Yachiyo", ""),
        ("打开 github.com 读一下内容", "live2d", "https://github.com", "GitHub page text for Yachiyo", ""),
        ("浏览器打开 GitHub 然后读一下", "bubble", "https://github.com", "GitHub page text for Yachiyo", ""),
        ("打开网页并读一下 github.com", "live2d", "https://github.com", "GitHub page text for Yachiyo", ""),
        (
            "打开 GitHub 并概括内容",
            "bubble",
            "https://github.com",
            "网页内容摘要：\n- GitHub page text for Yachiyo",
            "summary",
        ),
        (
            "open github.com and summarize",
            "live2d",
            "https://github.com",
            "网页内容摘要：\n- GitHub page text for Yachiyo",
            "summary",
        ),
        (
            "summarize github.com after opening it",
            "bubble",
            "https://github.com",
            "网页内容摘要：\n- GitHub page text for Yachiyo",
            "summary",
        ),
        (
            "搜索 oha yachiyo 并读一下结果",
            "bubble",
            "https://www.google.com/search?q=oha+yachiyo",
            "GitHub page text for Yachiyo",
            "",
        ),
    ]
    for prompt, launcher_mode, expected_url, expected_summary, expected_presentation in cases:
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == expected_summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url_and_extract_text"
        assert agent_task["tool_calls"][-1]["input_preview"]["url"] == expected_url
        completed_event = next(
            event
            for event in result["_task_timeline"]["events"]
            if event["event_type"] == "agent.desktop.intent_completed"
        )
        if expected_presentation:
            assert completed_event["payload"]["presentation"] == expected_presentation
        else:
            assert "presentation" not in completed_event["payload"]
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert calls == [
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://github.com"),
        ("extract", ""),
        ("open", "https://www.google.com/search?q=oha+yachiyo"),
        ("extract", ""),
    ]


def test_chat_bridge_quick_message_executes_browser_open_url_and_screenshot(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_open_url(url: str) -> dict:
        calls.append(("open", url))
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-test-owned",
                "target_websocket_available": True,
            },
        }

    def fake_screenshot(target_path) -> dict:
        calls.append(("screenshot", str(target_path)))
        return {
            "ok": True,
            "action": "browser.screenshot",
            "summary": "Captured current browser page",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "format": "png",
                "size": 10,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    monkeypatch.setattr("apps.shell.agent.tools.browser.screenshot", fake_screenshot)
    cases = (
        (
            "打开 Chrome 访问 github.com 并截图",
            "bubble",
            "https://github.com",
            "已打开网页并截取当前网页。",
        ),
        (
            "打开网页并截图 github.com",
            "live2d",
            "https://github.com",
            "已打开网页并截取当前网页。",
        ),
        (
            "用浏览器搜索 oha yachiyo 并截图",
            "bubble",
            "https://www.google.com/search?q=oha+yachiyo",
            "已打开网页并截取当前网页。",
        ),
        (
            "google oha yachiyo and screenshot results",
            "live2d",
            "https://www.google.com/search?q=oha+yachiyo",
            "已打开网页并截取当前网页。",
        ),
    )
    for text, launcher_mode, expected_url, expected_summary in cases:
        calls.clear()
        result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert calls[0] == ("open", expected_url)
        assert calls[1][0] == "screenshot"
        assert calls[1][1].endswith("browser/current-page.png")
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == expected_summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url_and_screenshot"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "url": expected_url,
            "reason": "user asked to capture the browser page after opening a URL",
        }
        assert agent_task["artifacts"][-1]["path"] == "browser/current-page.png"
        completed_event = next(
            event
            for event in result["_task_timeline"]["events"]
            if event["event_type"] == "agent.desktop.intent_completed"
        )
        assert completed_event["detail"] == "browser.open_url_and_screenshot"
        assert run["status"] == "completed"
        assert "artifact.created" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_system_volume_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    volume_calls: list[tuple[str, object, object]] = []
    pending_status_level: list[int | None] = [None]

    def fake_system_volume(action: str, *, level=None, step=None) -> dict:
        volume_calls.append((action, level, step))
        if action == "down":
            old_level = 50
            new_level = 40
            pending_status_level[0] = new_level
            summary = "System volume decreased from 50% to 40%"
        elif action == "status":
            new_level = pending_status_level[0] or 50
            old_level = new_level
            pending_status_level[0] = None
            summary = f"System volume is {new_level}%"
        else:
            old_level = 40
            new_level = 50
            pending_status_level[0] = new_level
            summary = "System volume increased from 40% to 50%"
        return {
            "ok": True,
            "action": "system.volume",
            "summary": summary,
            "data": {
                "requested_action": action,
                "old_level": old_level,
                "old_muted": False,
                "level": new_level,
                "muted": False,
                "changed": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.system_volume", fake_system_volume)
    cases = (
        ("调大音量", "bubble", "up", "已把系统音量从 40% 调高到 50%。"),
        ("turn it up", "live2d", "up", "已把系统音量从 40% 调高到 50%。"),
        ("大点声", "bubble", "up", "已把系统音量从 40% 调高到 50%。"),
        ("放大音量", "bubble", "up", "已把系统音量从 40% 调高到 50%。"),
        ("Apple Music 放大音量", "live2d", "up", "已把系统音量从 40% 调高到 50%。"),
        ("sound up", "live2d", "up", "已把系统音量从 40% 调高到 50%。"),
        ("make it quieter", "bubble", "down", "已把系统音量从 50% 调低到 40%。"),
        ("缩小音量", "bubble", "down", "已把系统音量从 50% 调低到 40%。"),
        ("Apple Music 缩小音量", "live2d", "down", "已把系统音量从 50% 调低到 40%。"),
        ("sound down", "live2d", "down", "已把系统音量从 50% 调低到 40%。"),
        ("查看当前音量", "bubble", "status", "当前系统音量是 50%。"),
        ("show current volume", "live2d", "status", "当前系统音量是 50%。"),
    )
    for prompt, launcher_mode, action, summary in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == summary
        assert agent_task["tool_calls"][-1]["tool_name"] == "system.volume"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"action": action}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert volume_calls == [
        call
        for action in (
            "up",
            "up",
            "up",
            "up",
            "up",
            "up",
            "down",
            "down",
            "down",
            "down",
            "status",
            "status",
        )
        for call in (
            [(action, None, None)]
            if action == "status"
            else [(action, None, None), ("status", None, None)]
        )
    ]


def test_chat_bridge_quick_message_executes_clipboard_write_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    clipboard_calls: list[str] = []
    clipboard_read_calls: list[int] = []

    def fake_clipboard_write(text: str) -> dict:
        clipboard_calls.append(text)
        return {
            "ok": True,
            "action": "clipboard.write",
            "summary": "Copied 11 characters to clipboard",
            "data": {
                "text_length": len(text),
                "platform": "macos",
            },
        }

    def fake_clipboard_read(*, max_chars=2000) -> dict:
        clipboard_read_calls.append(max_chars)
        text = clipboard_calls[-1] if clipboard_calls else ""
        return {
            "ok": True,
            "action": "clipboard.read",
            "summary": f"Read {len(text)} characters from clipboard",
            "data": {
                "text": text,
                "text_length": len(text),
                "truncated": False,
                "max_chars": max_chars,
                "platform": "macos",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_write", fake_clipboard_write)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", fake_clipboard_read)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "复制以下内容：hello world",
    )

    assert clipboard_calls == ["hello world"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已复制 11 个字符到剪贴板。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "clipboard.write"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_unverified" not in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    cases = (
        ("设置剪贴板为 hello", "bubble"),
        ("set clipboard to hello", "live2d"),
    )
    for text, launcher_mode in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert clipboard_calls[-1] == "hello"
        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "已复制 5 个字符到剪贴板。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "clipboard.write"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_unverified" not in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert clipboard_read_calls == [2000, 2000, 2000]


def test_chat_bridge_quick_message_executes_clipboard_read_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    read_calls: list[int] = []

    def fake_clipboard_read(*, max_chars=2000) -> dict:
        read_calls.append(max_chars)
        return {
            "ok": True,
            "action": "clipboard.read",
            "summary": "Read 11 characters from clipboard",
            "data": {
                "text": "hello world",
                "text_length": 11,
                "truncated": False,
                "max_chars": max_chars,
                "platform": "macos",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", fake_clipboard_read)
    cases = (
        ("剪贴板里是什么", "bubble"),
        ("读一下剪贴板", "live2d"),
        ("把剪贴板读给我", "bubble"),
        ("what is on my clipboard", "live2d"),
    )
    for text, launcher_mode in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "剪贴板内容：hello world"
        assert agent_task["tool_calls"][-1]["tool_name"] == "clipboard.read"
        assert agent_task["tool_calls"][-1]["input_preview"] == {}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_unverified" not in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert read_calls == [2000, 2000, 2000, 2000]


def test_chat_bridge_quick_message_copies_and_reads_selected_text_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str | int]] = []

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: copy",
            "data": {"shortcut_action": action},
        }

    def fake_clipboard_read(*, max_chars=2000) -> dict:
        calls.append(("read", max_chars))
        return {
            "ok": True,
            "action": "clipboard.read",
            "summary": "Read 13 characters from clipboard",
            "data": {
                "text": "selected text",
                "text_length": 13,
                "truncated": False,
                "max_chars": max_chars,
                "platform": "macos",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", fake_clipboard_read)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "读一下选中的内容",
    )

    partial_summary = "已执行复制，但无法确认剪贴板内容来自当前选区；任务已停止。"
    assert calls == [("shortcut", "copy"), ("read", 2000)]
    assert agent_task["status"] == "failed"
    assert agent_task["summary"] == partial_summary
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]] == [
        "desktop.safe_shortcut",
    ]
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert event_types.count("agent.desktop.intent_planned") == 2
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.post_action_verification.enqueued" in event_types
    assert "agent.replan.requested" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "我选中了什么",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["summary"] == partial_summary
        assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]] == [
            "desktop.safe_shortcut",
        ]
        assert run["status"] == "failed"
        assert event_types.count("agent.desktop.intent_planned") == 2
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.post_action_verification.enqueued" in event_types
        assert "agent.replan.requested" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    for prompt, launcher_mode in (
        ("复制选中文字并读取剪贴板", "bubble"),
        ("copy selected text and read clipboard", "live2d"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["summary"] == partial_summary
        assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]] == [
            "desktop.safe_shortcut",
        ]
        assert run["status"] == "failed"
        assert event_types.count("agent.desktop.intent_planned") == 2
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.post_action_verification.enqueued" in event_types
        assert "agent.replan.requested" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert calls == [
        ("shortcut", "copy"),
        ("read", 2000),
        ("shortcut", "copy"),
        ("read", 2000),
        ("shortcut", "copy"),
        ("read", 2000),
        ("shortcut", "copy"),
        ("read", 2000),
        ("shortcut", "copy"),
        ("read", 2000),
    ]


def test_chat_bridge_quick_message_executes_open_path_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_open_path(path: str) -> dict:
        open_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.open_path",
            "summary": f"Opened {path}",
            "data": {
                "path": path,
                "open_target": "system_open",
                "exists": True,
                "is_dir": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.open_path", fake_open_path)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "open Finder and open Downloads folder",
    )

    assert open_calls == ["~/Downloads"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件夹：~/Downloads。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    cases = (
        ("打开 Finder 看看下载文件夹", "bubble", "~/Downloads"),
        ("打开访达看看下载文件夹", "live2d", "~/Downloads"),
        ("打开图片文件夹", "bubble", "~/Pictures"),
        ("在 Finder 打开照片目录", "live2d", "~/Pictures"),
        ("打开公共文件夹", "bubble", "~/Public"),
        ("打开 Public 文件夹", "live2d", "~/Public"),
        ("打开影片文件夹", "live2d", "~/Movies"),
        ("打开音乐目录", "bubble", "~/Music"),
        ("打开 Music 文件夹", "live2d", "~/Music"),
        ("打开用户目录", "bubble", "~"),
        ("open user directory", "live2d", "~"),
        ("打开 iCloud Drive", "bubble", "~/Library/Mobile Documents/com~apple~CloudDocs"),
        ("打开 iCloud 云盘", "live2d", "~/Library/Mobile Documents/com~apple~CloudDocs"),
        ("打开共享文件夹", "bubble", "/Users/Shared"),
        ("打开当前工作区", "bubble", "."),
        ("打开项目目录", "live2d", "."),
        ("打开垃圾桶", "bubble", "~/.Trash"),
        ("open trash folder", "live2d", "~/.Trash"),
    )
    for prompt, launcher_mode, expected_path in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert open_calls[-1] == expected_path
        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == f"已打开文件夹：{expected_path}。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"path": expected_path}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_latest_download_open_path_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_open_path(path: str) -> dict:
        open_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.open_path",
            "summary": "Opened new.pdf",
            "data": {
                "path": path,
                "display_path": "~/Downloads/new.pdf",
                "desktop_object": "latest_download",
                "open_target": "system_open",
                "exists": True,
                "is_dir": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.open_path", fake_open_path)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开最近下载的文件",
    )

    assert open_calls == ["latest_download"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件：~/Downloads/new.pdf。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "latest_download"}
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开下载目录里的最新文件",
        launcher_mode="live2d",
    )

    assert open_calls[-1] == "latest_download"
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件：~/Downloads/new.pdf。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "latest_download"}
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_latest_screenshot_open_path_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_open_path(path: str) -> dict:
        open_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.open_path",
            "summary": "Opened Screenshot.png",
            "data": {
                "path": path,
                "display_path": "~/Desktop/Screenshot.png",
                "desktop_object": "latest_screenshot",
                "source_folder": "~/Desktop",
                "open_target": "system_open",
                "exists": True,
                "is_dir": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.open_path", fake_open_path)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开刚才的截图",
    )

    assert open_calls == ["latest_screenshot"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件：~/Desktop/Screenshot.png。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "latest_screenshot"}
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_finder_selection_open_path_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    open_calls: list[str] = []

    def fake_open_path(path: str) -> dict:
        open_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.open_path",
            "summary": "Opened selected.pdf",
            "data": {
                "path": path,
                "display_path": "~/Desktop/selected.pdf",
                "desktop_object": "finder_selection",
                "source_app": "Finder",
                "open_target": "system_open",
                "exists": True,
                "is_dir": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.open_path", fake_open_path)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开选中的文件",
    )

    assert open_calls == ["finder_selection"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件：~/Desktop/selected.pdf。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "finder_selection"}
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开当前选中的 Finder 文件",
        launcher_mode="bubble",
    )

    assert open_calls[-1] == "finder_selection"
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已打开文件：~/Desktop/selected.pdf。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
    assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "finder_selection"}
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_reveal_path_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    reveal_calls: list[str] = []

    def fake_reveal_path(path: str) -> dict:
        reveal_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.reveal_path",
            "summary": f"Revealed {path}",
            "data": {
                "path": path,
                "open_target": "finder_reveal",
                "exists": True,
                "is_dir": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.reveal_path", fake_reveal_path)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "open Finder and show Downloads folder",
    )

    assert reveal_calls == ["~/Downloads"]
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已在 Finder 中显示：~/Downloads。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.reveal_path"
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    for prompt, launcher_mode in (
        ("显示当前选中的 Finder 文件", "bubble"),
        ("显示当前选中文件", "live2d"),
        ("在 Finder 中显示当前工作区", "bubble"),
        ("在 Finder 中显示项目目录", "live2d"),
        ("在 Finder 中显示 iCloud 云盘", "bubble"),
        ("在 Finder 中显示共享文件夹", "live2d"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        if "选中" in prompt:
            expected_path = "finder_selection"
        elif "iCloud" in prompt:
            expected_path = "~/Library/Mobile Documents/com~apple~CloudDocs"
        elif "共享" in prompt:
            expected_path = "/Users/Shared"
        else:
            expected_path = "."
        assert reveal_calls[-1] == expected_path
        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == f"已在 Finder 中显示：{expected_path}。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.reveal_path"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"path": expected_path}
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_screen_capture_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    capture_targets: list[str] = []

    def fake_screen_capture(target_path) -> dict:
        capture_targets.append(str(target_path))
        return {
            "ok": True,
            "action": "screen.capture",
            "summary": "已截取当前屏幕。",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "size_bytes": 10,
                "width": 100,
                "height": 80,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.screen_capture", fake_screen_capture)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "look at my screen",
    )

    assert capture_targets
    assert capture_targets[0].endswith("screenshots/current-screen.png")
    assert agent_task["status"] == "completed"
    assert agent_task["summary"] == "已截取当前屏幕。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "screen.capture"
    assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
    assert run["status"] == "completed"
    assert "artifact.created" in event_types
    assert "agent.desktop.intent_completed" in event_types

    for launcher_mode, prompt in (
        ("bubble", "帮我截个屏"),
        ("bubble", "看一下我现在的界面"),
        ("live2d", "show me the screen"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "已截取当前屏幕。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "screen.capture"
        assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert run["status"] == "completed"
        assert "artifact.created" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert len(capture_targets) == 4


def test_chat_bridge_quick_message_executes_app_then_screen_capture_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return _REAL_APP_OPEN(app_name)

    monkeypatch.setattr(desktop_tools, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_tools, "_run_open_app", lambda name: SimpleNamespace(returncode=0, stdout="", stderr=""))
    monkeypatch.setattr(desktop_tools, "_run_osascript", lambda script, args, **kwargs: {"ok": True, "stdout": "running"})

    def fake_screen_capture(target_path) -> dict:
        calls.append(("capture", str(target_path)))
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="))
        return {
            "ok": True,
            "action": "screen.capture",
            "summary": "已截取当前屏幕。",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "size_bytes": target_path.stat().st_size,
                "width": 1,
                "height": 1,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_status", lambda name: {
        "ok": True, "action": "app.status", "data": {"app_name": name, "running": any(action == "open" and app == name for action, app in calls)}
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", lambda query="", limit=20: {
        "ok": True, "action": "desktop.list_apps", "data": {"query": query, "apps": [{"name": {"微信": "WeChat", "活动监视器": "Activity Monitor", "系统活动监视器": "Activity Monitor"}.get(query, query)}], "best_match": {"name": {"微信": "WeChat", "活动监视器": "Activity Monitor", "系统活动监视器": "Activity Monitor"}.get(query, query)}}
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.screen_capture", fake_screen_capture)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开微信然后截图",
    )

    assert calls[0] == ("open", "WeChat")
    assert calls[1][0] == "capture"
    assert calls[1][1].endswith("screenshots/current-screen.png")
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已打开 WeChat。 已截取当前屏幕。"
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
        "app.open",
        "screen.capture",
    ]
    assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
    assert run["status"] == "completed"
    assert event_types.count("agent.desktop.intent_planned") == 3
    assert "artifact.created" in event_types
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    # Content understanding needs a model; a screenshot alone cannot fulfill it.
    cases = (
        ("打开微信看看有没有新消息", "bubble"),
        ("把微信打开然后看看有没有未读", "live2d"),
        ("打开微信读一下当前聊天", "bubble"),
        ("打开 Slack 看消息", "bubble"),
        ("open Discord and read messages", "live2d"),
        ("打开活动监视器看看 CPU", "live2d"),
        ("打开系统活动监视器看看 CPU", "bubble"),
    )
    for index, (prompt, launcher_mode) in enumerate(cases):
        before = list(calls)
        store = ChatStore(db_path=str(tmp_path / f"capture-understanding-{index}.db"))
        runtime = _runtime_with_chat_store(store)
        service = AgentRuntimeService(db_path=tmp_path / f"capture-understanding-runtime-{index}.db", workspace_dir=tmp_path / f"capture-understanding-work-{index}", credential_store=MemoryCredentialStore(), seed_templates=False)
        runtime.agent_runtime_service = service
        try:
            result = ChatBridge(runtime).send_quick_message(prompt, metadata={"source": "launcher", "launcher_mode": launcher_mode, "launcher_surface": "quick_message", "allow_user_foreground_takeover": True})
            assert result["ok"] is False, prompt
            assert result["committed"] is False
            assert "对话模型" in result["error"]
            assert calls == before
        finally:
            service.close()
            store.close()

    for goal, app in (("打开 Slack 并截个图", "Slack"), ("打开活动监视器然后截图", "Activity Monitor")):
        before_count = len(calls)
        _result, task, run, event_types = _run_launcher_daily_desktop_quick_message(tmp_path, monkeypatch, goal)
        assert calls[before_count] == ("open", app)
        assert calls[before_count + 1][0] == "capture"
        assert task["status"] == run["status"] == "completed"
        assert task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_app_prefix_screen_capture_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_screen_capture(target_path) -> dict:
        calls.append(("capture", str(target_path)))
        return {
            "ok": True,
            "action": "screen.capture",
            "summary": "已截取当前屏幕。",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "size_bytes": 10,
                "width": 100,
                "height": 80,
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.screen_capture", fake_screen_capture)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 看看界面",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已切换到 Google Chrome。 已截取当前屏幕。"
        assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
            "app.focus",
            "screen.capture",
        ]
        assert agent_task["tool_calls"][-2]["input_preview"] == {
            "app_name": "Google Chrome",
        }
        assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert run["status"] == "completed"
        assert event_types.count("agent.desktop.intent_planned") == 3
        assert "artifact.created" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 观察一下",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已切换到 Google Chrome。 已截取当前屏幕。"
        assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
            "app.focus",
            "screen.capture",
        ]
        assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert run["status"] == "completed"
        assert event_types.count("agent.desktop.intent_planned") == 3
        assert "artifact.created" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "看一下 Chrome 当前界面",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已切换到 Google Chrome。 已截取当前屏幕。"
        assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
            "app.focus",
            "screen.capture",
        ]
        assert agent_task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert run["status"] == "completed"
        assert event_types.count("agent.desktop.intent_planned") == 3
        assert "artifact.created" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert len(calls) == 12
    for index in range(0, len(calls), 2):
        assert calls[index] == ("focus", "Google Chrome")
        assert calls[index + 1][0] == "capture"
        assert calls[index + 1][1].endswith("screenshots/current-screen.png")


def test_chat_bridge_quick_message_executes_app_safe_shortcut_sequence_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    editor_text = "selected draft 文本"
    selected_text = ""
    clipboard_text = ""
    clipboard_revision = 0

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_shortcut(action: str) -> dict:
        nonlocal selected_text, clipboard_text, clipboard_revision
        calls.append(("shortcut", action))
        if action == "select_all":
            selected_text = editor_text
        elif action == "copy":
            clipboard_text = selected_text
            clipboard_revision += 1
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut",
            "data": {"shortcut_action": action},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                return _fake_active_window_result(app_name)
        return _fake_active_window_result("Finder")

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        current_app = next(
            (
                value
                for action, value in reversed(calls)
                if action in {"open", "focus"}
            ),
            app_name or "Finder",
        )
        result = _fake_ui_elements_result(current_app, "Editor")
        result["data"].update(pid=100, window_id=200)
        result["data"]["focused_element"] = {"role": "AXTextArea", "name": "Editor", "value": editor_text, "focused": True, "editable": True}
        result["data"]["elements"][0].update(role="AXTextArea", value=editor_text, focused=True, editable=True)
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", lambda max_chars=2000: {
        "ok": True, "action": "clipboard.read", "data": {"text": clipboard_text, "text_length": len(clipboard_text), "truncated": False, "max_chars": max_chars,
            "pasteboard_revision": clipboard_revision, "pasteboard_revision_stable": True}
    })
    def fake_catalog(query="", limit=20):
        name = {"微信": "WeChat", "Chrome": "Google Chrome"}.get(query, query)
        match = {"name": name, "path": f"/Applications/{name}.app"}
        return {"ok": True, "action": "desktop.list_apps",
                "data": {"query": query, "apps": [match], "best_match": match}}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_catalog)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开微信然后全选复制",
    )

    assert calls == [
        ("open", "WeChat"),
        ("focus", "WeChat"),
        ("shortcut", "select_all"),
        ("shortcut", "copy"),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "剪贴板是空的。 已打开 WeChat 并发送“全选”快捷键。 已发送“复制选中内容”快捷键。"
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"] if tool_call["tool_name"] in {"app.open_and_safe_shortcut", "desktop.safe_shortcut"}] == [
        "app.open_and_safe_shortcut",
        "desktop.safe_shortcut",
    ]
    assert run["status"] == "completed"
    assert event_types.count("agent.desktop.intent_planned") == 7
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types

    calls.clear()
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Finder 并按 Command N",
    )

    assert calls == [
        ("open", "Finder"),
        ("focus", "Finder"),
        ("shortcut", "new_window"),
    ]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert "Finder" in agent_task["summary"] and "new_window" in agent_task["summary"]
    assert "未能确认" in agent_task["summary"]
    open_shortcut_call = _agent_task_tool_call(agent_task, "app.open_and_safe_shortcut")
    assert open_shortcut_call["input_preview"] == {
        "app_name": "Finder",
        "action": "new_window",
    }
    assert run["status"] == "failed"
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "model.request.started" not in event_types

    calls.clear()
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "把Chrome打开然后新建标签页",
    )

    assert calls == [
        ("open", "Google Chrome"),
        ("focus", "Google Chrome"),
        ("shortcut", "new_tab"),
    ]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert "Google Chrome" in agent_task["summary"] and "new_tab" in agent_task["summary"]
    assert "未能确认" in agent_task["summary"]
    open_shortcut_call = _agent_task_tool_call(agent_task, "app.open_and_safe_shortcut")
    assert open_shortcut_call["input_preview"] == {
        "app_name": "Google Chrome",
        "action": "new_tab",
    }
    assert run["status"] == "failed"
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_browser_extract_text_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    extract_calls: list[str] = []

    def fake_extract_text(selector: str = "") -> dict:
        extract_calls.append(selector)
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 29 characters from browser page",
            "data": {
                "selector": selector,
                "text": "Yachiyo desktop agent runtime",
                "truncated": False,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "读当前网页",
    )

    assert extract_calls == []
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.extract_text"
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert "agent.desktop.intent_planned" in event_types
    assert "agent.tool.skipped" in event_types
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    for prompt, launcher_mode in (
        ("read current webpage", "bubble"),
        ("extract current page text", "live2d"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.extract_text"
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.skipped" in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert extract_calls == []


def test_chat_bridge_quick_message_opens_browser_then_extracts_current_page_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_extract_text(selector: str = "") -> dict:
        calls.append(("extract", selector))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 29 characters from browser page",
            "data": {
                "selector": selector,
                "text": "Yachiyo desktop agent runtime",
                "truncated": False,
            },
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                return _fake_active_window_result(app_name)
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Chrome 读取当前页",
    )

    assert calls == [("open", "Google Chrome")]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
        "app.open",
        "browser.extract_text",
    ]
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert event_types.count("agent.desktop.intent_planned") == 3
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "focus Chrome and extract page text",
        launcher_mode="live2d",
    )

    assert calls[-1:] == [("focus", "Google Chrome")]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert [tool_call["tool_name"] for tool_call in agent_task["tool_calls"][-2:]] == [
        "app.focus",
        "browser.extract_text",
    ]
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert run["status"] == "failed"
    assert run["pending_approval"] == {}
    assert event_types.count("agent.desktop.intent_planned") == 3
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.intent_approval_required" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_browser_screenshot_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    screenshot_targets: list[str] = []

    def fake_browser_screenshot(target_path) -> dict:
        screenshot_targets.append(str(target_path))
        return {
            "ok": True,
            "action": "browser.screenshot",
            "summary": "Captured current browser page",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "format": "png",
                "size": 10,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.screenshot", fake_browser_screenshot)
    for prompt, launcher_mode in (
        ("screenshot this page", "bubble"),
        ("screenshot current webpage", "live2d"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert screenshot_targets == []
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.screenshot"
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert agent_task["artifacts"] == []
        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.skipped" in event_types
        assert "artifact.created" not in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types

    assert screenshot_targets == []
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_permission_diagnosis_for_launcher_entrypoints(
    tmp_path,
    monkeypatch,
):
    permission_calls: list[bool] = []

    def fake_permissions() -> dict:
        permission_calls.append(True)
        return {
            "ok": True,
            "action": "desktop.permissions",
            "summary": "Desktop permissions ready",
            "data": {
                "permission_targets": [],
                "affected_tools": [],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.permissions", fake_permissions)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_open",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("permission diagnosis should not open an app")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.screen_capture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("permission diagnosis should not capture the screen")
        ),
    )
    for prompt, launcher_mode in (
        ("需要什么权限", "live2d"),
        ("为什么不能打开应用？", "bubble"),
        ("为什么不能读取屏幕？", "live2d"),
    ):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "桌面执行权限已就绪。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.permissions"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert permission_calls == [True, True, True]


def test_chat_bridge_quick_message_executes_safe_shortcut_without_approval(
    tmp_path,
    monkeypatch,
):
    # Dispatch-only fixture: semantic shortcut effects require independent observation.
    shortcut_calls: list[str] = []

    def fake_safe_shortcut(action: str) -> dict:
        shortcut_calls.append(action)
        key, modifiers, label = desktop_tools._SAFE_SHORTCUTS[action]
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {label}",
            "data": {
                "shortcut_action": action,
                "key": key,
                "modifiers": list(modifiers),
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    cases = (
        ("复制一下选中的内容", "live2d", "copy", "已复制选中内容。"),
        ("复制选中文字", "bubble", "copy", "已复制选中内容。"),
        ("copy current selection", "live2d", "copy", "已复制选中内容。"),
        ("你可以帮我复制一下吗", "bubble", "copy", "已复制选中内容。"),
        ("你能帮我粘贴吗", "live2d", "paste", "已粘贴。"),
        ("你能帮我全选吗", "bubble", "select_all", "已全选。"),
        ("你可以帮我撤销吗", "live2d", "undo", "已撤销。"),
        ("切到下一个窗口", "bubble", "next_window", "已切到下一个窗口。"),
        ("切到上一个窗口", "live2d", "previous_window", "已切到上一个窗口。"),
        ("切换到上一个应用", "bubble", "switch_previous_app", "已切到上一个应用。"),
        ("switch to previous app", "live2d", "switch_previous_app", "已切到上一个应用。"),
        ("切到下一个应用", "bubble", "switch_next_app", "已切到下一个应用。"),
        ("switch to next app", "live2d", "switch_next_app", "已切到下一个应用。"),
        ("隐藏其他应用", "bubble", "hide_other_apps", "已隐藏其他应用。"),
        ("hide other apps", "live2d", "hide_other_apps", "已隐藏其他应用。"),
        ("最大化当前窗口", "bubble", "toggle_full_screen", "已切换当前窗口全屏。"),
        ("maximize the current window", "live2d", "toggle_full_screen", "已切换当前窗口全屏。"),
        ("打开任务控制中心", "bubble", "mission_control", "已打开任务控制中心。"),
        ("显示当前应用窗口", "bubble", "application_windows", "已显示当前应用窗口。"),
        ("打开聚焦搜索", "bubble", "spotlight_search", "已打开 Spotlight。"),
        ("打开 emoji 面板", "live2d", "emoji_picker", "已打开 Emoji 面板。"),
        ("锁屏", "bubble", "lock_screen", "已锁屏。"),
        ("打开强制退出窗口", "live2d", "force_quit_dialog", "已打开强制退出窗口。"),
        ("Can you copy?", "bubble", "copy", "已复制选中内容。"),
        ("copy current page link", "bubble", "copy_current_page_link", "已复制当前网页链接。"),
        ("复制当前网页链接", "live2d", "copy_current_page_link", "已复制当前网页链接。"),
        ("Could you paste?", "live2d", "paste", "已粘贴。"),
        ("Would you select all please?", "bubble", "select_all", "已全选。"),
        ("switch to next window", "bubble", "next_window", "已切到下一个窗口。"),
        ("switch to previous window", "live2d", "previous_window", "已切到上一个窗口。"),
        ("show mission control", "live2d", "mission_control", "已打开任务控制中心。"),
        ("show app windows", "live2d", "application_windows", "已显示当前应用窗口。"),
        ("spotlight search", "bubble", "spotlight_search", "已打开 Spotlight。"),
        ("show emoji picker", "live2d", "emoji_picker", "已打开 Emoji 面板。"),
        ("lock screen", "bubble", "lock_screen", "已锁屏。"),
        ("show force quit applications", "bubble", "force_quit_dialog", "已打开强制退出窗口。"),
        ("浏览器刷新", "live2d", "refresh", "已刷新。"),
        ("refresh page", "bubble", "refresh", "已刷新。"),
        ("refresh the current page", "bubble", "refresh", "已刷新。"),
        ("刷新当前页面", "live2d", "refresh", "已刷新。"),
        ("reload page", "live2d", "refresh", "已刷新。"),
        ("open new tab", "bubble", "new_tab", "已新建标签页。"),
        ("open a new tab", "live2d", "new_tab", "已新建标签页。"),
        ("新开一个标签页", "bubble", "new_tab", "已新建标签页。"),
        ("go back one page", "live2d", "browser_back", "已返回上一页。"),
        ("forward page", "bubble", "browser_forward", "已前进一页。"),
        ("把剪贴板内容粘贴到当前输入框", "bubble", "paste", "已粘贴。"),
        ("关闭当前标签页", "bubble", "close_tab", "已关闭标签页。"),
        ("切到下一个标签页", "live2d", "next_tab", "已切到下一个标签页。"),
        ("切到上一个标签页", "bubble", "previous_tab", "已切到上一个标签页。"),
        (
            "重新打开关闭的标签页",
            "live2d",
            "reopen_closed_tab",
            "已重新打开关闭的标签页。",
        ),
        (
            "重新打开刚关闭的标签页",
            "bubble",
            "reopen_closed_tab",
            "已重新打开关闭的标签页。",
        ),
    )
    for text, launcher_mode, action, summary in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        assert shortcut_calls[-1] == action
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert ("未能确认" in agent_task["summary"] or "无法确认" in agent_task["summary"]
                or "缺少可运行的 Chat Profile" in agent_task["summary"])
        shortcut_call = _agent_task_tool_call(agent_task, "desktop.safe_shortcut")
        assert shortcut_call["input_preview"] == {"action": action}
        assert shortcut_call["status"] == ("failed" if action == "copy" else "completed")
        assert shortcut_call["output_preview"]["ok"] is True
        assert shortcut_call["output_preview"]["data"]["shortcut_action"] == action
        assert run["status"] == "failed"
        assert "agent.desktop.intent_completed" not in event_types
        assert "run.failed" in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_app_scoped_safe_shortcut_without_approval(
    tmp_path,
    monkeypatch,
):
    # Dispatch-only fixture: semantic shortcut effects require independent observation.
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: new tab",
            "data": {"shortcut_action": action, "key": "t", "modifiers": ["command"]},
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    cases = (
        ("Chrome 新建标签页", "new_tab", "已切到 Google Chrome 并发送“新建标签页”快捷键。"),
        ("Chrome 关闭当前标签页", "close_tab", "已切到 Google Chrome 并发送“关闭标签页”快捷键。"),
        ("Chrome 切到下一个标签页", "next_tab", "已切到 Google Chrome 并发送“切到下一个标签页”快捷键。"),
        ("Chrome 切到上一个标签页", "previous_tab", "已切到 Google Chrome 并发送“切到上一个标签页”快捷键。"),
        ("Chrome 最大化", "toggle_full_screen", "已切到 Google Chrome 并发送“切换当前窗口全屏”快捷键。"),
        ("Chrome 全屏", "toggle_full_screen", "已切到 Google Chrome 并发送“切换当前窗口全屏”快捷键。"),
    )
    for text, action, summary in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
        )

        assert calls[-2:] == [("focus", "Google Chrome"), ("shortcut", action)]
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert ("未能确认" in agent_task["summary"] or "无法确认" in agent_task["summary"]
                or "缺少可运行的 Chat Profile" in agent_task["summary"])
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_shortcut"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "action": action,
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert agent_task["tool_calls"][-1]["output_preview"]["ok"] is True
        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_completed" not in event_types
        assert "run.failed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_app_scoped_browser_back_without_fake_app_name(
    tmp_path,
    monkeypatch,
):
    # Dispatch-only fixture: semantic shortcut effects require independent observation.
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: browser back",
            "data": {
                "shortcut_action": action,
                "shortcut_label": "browser back",
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "切到 Chrome 后退一下",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert ("未能确认" in agent_task["summary"] or "无法确认" in agent_task["summary"]
                or "缺少可运行的 Chat Profile" in agent_task["summary"])
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_shortcut"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "action": "browser_back",
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert agent_task["tool_calls"][-1]["output_preview"]["ok"] is True
        assert run["status"] == "failed"
        assert "agent.desktop.intent_completed" not in event_types
        assert "run.failed" in event_types
        assert "model.request.started" not in event_types

    assert calls == [
        ("focus", "Google Chrome"),
        ("shortcut", "browser_back"),
        ("focus", "Google Chrome"),
        ("shortcut", "browser_back"),
    ]


def test_chat_bridge_quick_message_executes_app_prefix_find_shortcut_without_model(
    tmp_path,
    monkeypatch,
):
    # Dispatch-only fixture: semantic shortcut effects require independent observation.
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: find",
            "data": {
                "shortcut_action": action,
                "shortcut_label": "find",
            },
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action == "focus":
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 打开搜索",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert ("未能确认" in agent_task["summary"] or "无法确认" in agent_task["summary"]
                or "缺少可运行的 Chat Profile" in agent_task["summary"])
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_shortcut"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "action": "find",
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert agent_task["tool_calls"][-1]["output_preview"]["ok"] is True
        assert run["status"] == "failed"
        assert "agent.desktop.intent_completed" not in event_types
        assert "run.failed" in event_types
        assert "model.request.started" not in event_types

    assert calls == [
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("shortcut", "find"),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("shortcut", "find"),
    ]


def test_chat_bridge_quick_message_executes_safe_type_text_without_approval(
    tmp_path,
    monkeypatch,
):
    typed_text: list[str] = []

    def fake_safe_type_text(text: str) -> dict:
        typed_text.append(text)
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"app_name": "Foreground App", "character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Foreground App", "Editor")
        if typed_text:
            result["data"]["elements"][0].update(
                {"name": typed_text[-1], "value": typed_text[-1]}
            )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: _fake_active_window_result("Foreground App"))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "输入 你好八千代 到前台",
    )

    assert typed_text == ["你好八千代"]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已向前台输入文字（5 个字符）。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.safe_type_text"
    assert agent_task["tool_calls"][-1]["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert "agent.replan.requested" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "在当前输入框输入 hello",
        launcher_mode="bubble",
    )

    assert typed_text[-1] == "hello"
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已向前台输入文字（5 个字符）。"
    assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.safe_type_text"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert "agent.replan.requested" not in event_types


def test_chat_bridge_quick_message_executes_spotlight_search_sequence_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    typed_text = ""

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {
                "shortcut_action": action,
                "key": "space",
                "modifiers": ["command"],
            },
        }

    def fake_safe_type_text(text: str) -> dict:
        nonlocal typed_text
        typed_text = text
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"app_name": "Spotlight", "character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Spotlight", "Spotlight Search")
        result["data"]["elements"][0].update(
            {"name": "Search", "value": typed_text}
        )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: _fake_active_window_result("Spotlight", "Spotlight Search"))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    cases = (
        ("Spotlight 搜索 yachiyo", "bubble"),
        ("open Spotlight and search yachiyo", "live2d"),
    )
    for prompt, launcher_mode in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert calls[-2:] == [("shortcut", "spotlight_search"), ("type", "yachiyo")]
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已发送“打开 Spotlight”快捷键。 已向前台输入文字（7 个字符）。"
        )
        assert [call["tool_name"] for call in agent_task["tool_calls"][-2:]] == [
            "desktop.safe_shortcut",
            "desktop.safe_type_text",
        ]
        assert [call["status"] for call in agent_task["tool_calls"][-2:]] == [
            "completed",
            "completed",
        ]
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert "agent.replan.requested" not in event_types


def test_chat_bridge_quick_message_executes_search_submit_without_approval(
    tmp_path,
    monkeypatch,
):
    submit_calls: list[str] = []

    def fake_search_submit() -> dict:
        submit_calls.append("search_submit")
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted foreground search query",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        return _fake_ui_elements_result(
            app_name or "Google Chrome",
            "Search Results",
        )

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    cases = (
        ("提交当前搜索", "bubble"),
        ("press enter to search", "live2d"),
    )
    for prompt, launcher_mode in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已提交前台搜索。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.search_submit"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert submit_calls == ["search_submit", "search_submit"]


def test_chat_bridge_quick_message_executes_app_open_and_safe_type_text_without_approval(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Notes"))
        return _fake_active_window_result("Notes")

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        current_app = next(
            (
                value
                for action, value in reversed(calls)
                if action in {"open", "focus"}
            ),
            app_name or "Notes",
        )
        typed_value = next(
            (value for action, value in reversed(calls) if action == "type"),
            "",
        )
        result = _fake_ui_elements_result(current_app, "Editor")
        if typed_value:
            result["data"]["elements"][0].update(
                {"name": typed_value, "value": typed_value}
            )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Notes 输入 hello yachiyo",
    )

    assert calls == [
        ("open", "Notes"),
        ("focus", "Notes"),
        ("active", "Notes"),
        ("type", "hello yachiyo"),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已打开 Notes 并输入文字（13 个字符）。"
    assert _agent_task_tool_call(agent_task, "app.open_and_safe_type_text")["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert "agent.replan.requested" not in event_types

    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开微信输入你好",
    )

    assert calls[-4:] == [
        ("open", "WeChat"),
        ("focus", "WeChat"),
        ("active", "WeChat"),
        ("type", "你好"),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已打开 WeChat 并输入文字（2 个字符）。"
    type_call = _agent_task_tool_call(agent_task, "app.open_and_safe_type_text")
    assert type_call["input_preview"] == {
        "app_name": "WeChat",
        "text": "你好",
    }
    assert type_call["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert "agent.replan.requested" not in event_types


def test_chat_bridge_permission_preflight_blocks_multi_step_desktop_intent_before_mutation(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []
    permission_probe_calls: list[bool] = []
    permission_cache_warmed = False

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Notes"))
        return _fake_active_window_result("Notes")

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut: copy",
            "data": {"shortcut_action": action, "key": "c", "modifiers": ["command"]},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Notes", "Editor")
        result["data"]["elements"][0].update(
            {"name": "hello", "value": "hello"}
        )
        return result

    def fake_permission_probe(use_cache: bool = True) -> dict[str, list[str]]:
        nonlocal permission_cache_warmed
        permission_probe_calls.append(use_cache)
        permission_cache_warmed = True
        return {"foreground_input": ["accessibility"]}

    def fake_permission_preflight() -> dict:
        if not permission_cache_warmed:
            return {
                "ok": True,
                "action": "desktop.permission_preflight",
                "permission_error": False,
                "permission_targets": [],
                "affected_tools": [],
                "recovery_actions": [],
                "data": {"ready": True},
            }
        return {
            "ok": True,
            "action": "desktop.permission_preflight",
            "permission_error": True,
            "permission_targets": ["accessibility"],
            "affected_tools": ["app.open_and_safe_type_text", "desktop.safe_shortcut"],
            "recovery_actions": [
                {
                    "label": "打开辅助功能权限",
                    "tool": "app.open",
                    "input": {"app_name": "辅助功能权限"},
                    "permission_target": "accessibility",
                    "risk_level": "low",
                }
            ],
            "diagnostic_route": "/yachiyo/readiness",
            "data": {
                "ready": False,
                "permission_targets": ["accessibility"],
                "affected_tools": ["app.open_and_safe_type_text", "desktop.safe_shortcut"],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Notes，输入 hello，再复制",
        permission_probe=fake_permission_probe,
        permission_preflight=fake_permission_preflight,
    )

    assert calls == []
    assert permission_probe_calls == [True]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is True
    assert agent_task["pending_approvals"] == []
    assert "desktop_permission_required" in agent_task["summary"]
    assert "accessibility" in agent_task["summary"]
    assert [
        tool_call["tool_name"]
        for tool_call in agent_task["tool_calls"]
        if tool_call["tool_name"] in {
            "app.open_and_safe_type_text",
            "desktop.safe_shortcut",
        }
    ] == ["app.open_and_safe_type_text"]
    assert agent_task["tool_calls"][0]["status"] == "failed"
    assert run["status"] == "failed"
    assert event_types.count("agent.desktop.intent_planned") == 3
    assert "agent.desktop.permission_preflight" in event_types
    assert "tool.requested" not in event_types
    preflight_event = next(
        event
        for event in _result["_events"]
        if event["event_type"] == "agent.desktop.permission_preflight"
    )
    assert preflight_event["payload"]["permission_targets"] == ["accessibility"]
    assert preflight_event["payload"]["affected_tools"] == [
        "app.open_and_safe_type_text",
    ]
    assert "agent.desktop.permission_recovery" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert "agent.replan.requested" not in event_types


def test_chat_bridge_quick_message_executes_app_find_sequence_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Executed safe shortcut",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"text": text, "character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted foreground search query",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                calls.append(("active", app_name))
                result = _fake_active_window_result(app_name)
                result["data"].update(pid=100, window_id=200)
                return result
        calls.append(("active", "Finder"))
        return _fake_active_window_result("Finder")

    def fake_ui_elements(role_filter="", limit=80, app_name="") -> dict:
        calls.append(("ui", app_name or "Finder"))
        current_app = app_name or next((value for action, value in reversed(calls) if action in {"open", "focus"}), 'Finder')
        query = next((value for action, value in reversed(calls) if action == "type"), "")
        last_type = max((i for i, (action, _) in enumerate(calls) if action == "type"), default=-1)
        submitted = any(action == "search_submit" for action, _ in calls[last_type+1:])
        return _fake_search_ui_result(current_app, query, submitted)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Finder，然后搜索下载",
    )

    assert [call for call in calls if call[0] not in {"active", "ui"}] == [
        ("open", "Finder"),
        ("focus", "Finder"),
        ("shortcut", "find"),
        ("type", "下载"),
        ("search_submit", ""),
    ]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == (
        "已打开 Finder 并发送“打开查找”快捷键。 "
        "已向前台输入文字（2 个字符）。 已提交前台搜索。"
    )
    tool_names = [tool_call["tool_name"] for tool_call in agent_task["tool_calls"]]
    assert all(
        tool_name in tool_names
        for tool_name in (
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
        )
    )
    assert tool_names.index("app.open_and_safe_shortcut") < tool_names.index(
        "desktop.safe_type_text"
    ) < tool_names.index("desktop.search_submit")
    assert run["status"] == "completed"
    assert event_types.count("agent.desktop.intent_planned") == 5
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types


def test_chat_bridge_quick_message_executes_safe_click_without_approval(
    tmp_path,
    monkeypatch,
):
    clicked: list[tuple[int, int]] = []

    def fake_safe_click(x: int, y: int) -> dict:
        clicked.append((x, y))
        return {
            "ok": True,
            "action": "desktop.safe_click",
            "summary": "Clicked explicit foreground coordinate at (120, 240)",
            "data": {
                "x": x,
                "y": y,
                "click_count": 1,
                "explicit_user_coordinates": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_click", fake_safe_click)
    _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "点击 120, 240",
    )

    assert clicked == [(120, 240)]
    assert agent_task["status"] == "completed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == "已点击前台位置：120, 240。"
    assert _agent_task_tool_call(agent_task, "desktop.safe_click")["status"] == "completed"
    assert run["status"] == "completed"
    assert "agent.desktop.intent_completed" in event_types
    assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_executes_safe_arrow_key_without_approval(
    tmp_path,
    monkeypatch,
):
    pressed: list[tuple[str, int]] = []

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        pressed.append((action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": "Pressed Down Arrow",
            "data": {
                "key_action": action,
                "key_label": "Down Arrow",
                "repeat_count": repeat_count,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    cases = (
        ("按向下箭头三次", "bubble", "arrow_down", 3, "已按下箭头（3 次）。"),
        ("按向下箭头三次", "live2d", "arrow_down", 3, "已按下箭头（3 次）。"),
        ("你能帮我按一下Escape吗", "bubble", "escape", 1, "已按Escape。"),
        ("你可以帮我按Tab吗", "live2d", "tab", 1, "已按Tab。"),
        ("显示桌面", "bubble", "show_desktop", 1, "已显示桌面。"),
        ("Could you press Escape?", "bubble", "escape", 1, "已按Escape。"),
        ("Can you hit Tab?", "live2d", "tab", 1, "已按Tab。"),
        ("show desktop", "live2d", "show_desktop", 1, "已显示桌面。"),
    )
    for text, launcher_mode, action, repeat_count, summary in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            text,
            launcher_mode=launcher_mode,
        )

        expected_unverified = (
            f"{summary.rstrip('。')}，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == expected_unverified
        safe_key_call = _agent_task_tool_call(agent_task, "desktop.safe_key")
        assert safe_key_call["input_preview"] == {
            "action": action,
            "repeat_count": repeat_count,
        }
        assert safe_key_call["status"] == "failed"
        assert run["status"] == "failed"
        assert run["result"] == expected_unverified
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert pressed == [
        ("arrow_down", 3),
        ("arrow_down", 3),
        ("escape", 1),
        ("tab", 1),
        ("show_desktop", 1),
        ("escape", 1),
        ("tab", 1),
        ("show_desktop", 1),
    ]


def test_chat_bridge_quick_message_executes_next_input_focus_as_safe_tab_key(
    tmp_path,
    monkeypatch,
):
    pressed: list[tuple[str, int]] = []
    typed_texts: list[str] = []

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        pressed.append((action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": "Pressed Tab",
            "data": {
                "key_action": action,
                "key_label": "Tab",
                "repeat_count": repeat_count,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        lambda text: typed_texts.append(text) or {"ok": True, "action": "desktop.safe_type_text"},
    )
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "切到下一个输入框",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已按Tab，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.safe_key"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "action": "tab",
            "repeat_count": 1,
        }
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert pressed == [("tab", 1), ("tab", 1)]
    assert typed_texts == []


def test_chat_bridge_quick_message_executes_app_prefix_safe_tab_key_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str] | tuple[str, str, int]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        calls.append(("key", action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": "Pressed Tab",
            "data": {
                "key_action": action,
                "key_label": "Tab",
                "repeat_count": repeat_count,
            },
        }

    def fake_active_window() -> dict:
        for call in reversed(calls):
            if call[0] == "focus":
                app_name = str(call[1])
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 按 Tab",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已切到 Google Chrome 并按Tab，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_key"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "action": "tab",
            "repeat_count": 1,
        }
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert calls == [
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("key", "tab", 1),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("key", "tab", 1),
    ]


def test_chat_bridge_quick_message_executes_previous_input_focus_as_safe_shift_tab_key(
    tmp_path,
    monkeypatch,
):
    pressed: list[tuple[str, int]] = []
    typed_texts: list[str] = []

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        pressed.append((action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": "Pressed Shift+Tab",
            "data": {
                "key_action": action,
                "key_label": "Shift+Tab",
                "repeat_count": repeat_count,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        lambda text: typed_texts.append(text) or {"ok": True, "action": "desktop.safe_type_text"},
    )
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "切到上一个输入框",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已按Shift+Tab，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.safe_key"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "action": "shift_tab",
            "repeat_count": 1,
        }
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert run["status"] == "failed"
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types

    assert pressed == [("shift_tab", 1), ("shift_tab", 1)]
    assert typed_texts == []


def test_chat_bridge_quick_message_executes_safe_scroll_page_without_approval(
    tmp_path,
    monkeypatch,
):
    scrolled: list[tuple[str, int]] = []

    def fake_safe_scroll(direction: str, *, pages: int = 1) -> dict:
        scrolled.append((direction, pages))
        return {
            "ok": True,
            "action": "desktop.safe_scroll",
            "summary": "Scrolled foreground desktop down 1 page",
            "data": {
                "direction": direction,
                "pages": pages,
                "explicit_user_scroll": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_scroll", fake_safe_scroll)
    cases = (
        ("翻到下一页", "bubble"),
        ("翻到下一页", "live2d"),
        ("滚动一下", "bubble"),
        ("scroll a little", "live2d"),
    )
    for prompt, launcher_mode in cases:
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已向下滚动前台界面（1 页）。"
        safe_scroll_call = _agent_task_tool_call(agent_task, "desktop.safe_scroll")
        assert safe_scroll_call["input_preview"] == {
            "direction": "down",
            "pages": 1,
        }
        assert safe_scroll_call["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert scrolled == [("down", 1), ("down", 1), ("down", 1), ("down", 1)]


def test_chat_bridge_quick_message_executes_app_prefix_safe_scroll_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str] | tuple[str, str, int]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_safe_scroll(direction: str, *, pages: int = 1) -> dict:
        calls.append(("scroll", direction, pages))
        return {
            "ok": True,
            "action": "desktop.safe_scroll",
            "summary": "Scrolled foreground desktop down 1 page",
            "data": {
                "direction": direction,
                "pages": pages,
                "explicit_user_scroll": True,
            },
        }

    def fake_active_window() -> dict:
        for call in reversed(calls):
            if call[0] == "focus":
                app_name = str(call[1])
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_scroll", fake_safe_scroll)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 向下滚动一下",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已切到 Google Chrome 并向下滚动前台界面（1 页）。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_scroll"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "direction": "down",
            "pages": 1,
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert calls == [
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("scroll", "down", 1),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("scroll", "down", 1),
    ]


def test_chat_bridge_quick_message_executes_app_prefix_safe_click_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str] | tuple[str, int, int]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action == "focus":
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    def fake_safe_click(x: int, y: int) -> dict:
        calls.append(("click", x, y))
        return {
            "ok": True,
            "action": "desktop.safe_click",
            "summary": "Clicked explicit foreground coordinate at (120, 240)",
            "data": {
                "x": x,
                "y": y,
                "click_count": 1,
                "explicit_user_coordinates": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_click", fake_safe_click)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 点击 120, 240",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已打开 Google Chrome 并点击前台位置：120, 240。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.open_and_safe_click"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "x": 120,
            "y": 240,
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types

    assert calls == [
        ("open", "Google Chrome"),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("click", 120, 240),
        ("open", "Google Chrome"),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("click", 120, 240),
    ]


def test_chat_bridge_quick_message_executes_app_prefix_safe_type_text_without_model(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action == "focus":
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(app_name or "Google Chrome", "Page")
        result["data"]["elements"][0].update(
            {"name": "hello", "value": "hello"}
        )
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    for launcher_mode in ("bubble", "live2d"):
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            "Chrome 输入 hello",
            launcher_mode=launcher_mode,
        )

        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已切到 Google Chrome 并输入文字（5 个字符）。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.focus_and_safe_type_text"
        assert agent_task["tool_calls"][-1]["input_preview"] == {
            "app_name": "Google Chrome",
            "text": "hello",
        }
        assert agent_task["tool_calls"][-1]["status"] == "completed"
        assert run["status"] == "completed"
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert "agent.replan.requested" not in event_types

    assert calls == [
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("type", "hello"),
        ("focus", "Google Chrome"),
        ("active", "Google Chrome"),
        ("type", "hello"),
    ]


def test_chat_bridge_quick_message_prepares_app_safe_type_text_then_waits_for_send_approval(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                calls.append(("active", app_name))
                return {**_fake_active_window_result(app_name), "data": {"app_name": app_name, "title": app_name, "pid": 100, "window_id": 200}}
        calls.append(("active", "WeChat"))
        return {**_fake_active_window_result("WeChat"), "data": {"app_name": app_name, "title": app_name, "pid": 100, "window_id": 200}}

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_ui_elements(**kwargs) -> dict:
        app = kwargs.get("app_name") or next((value for action, value in reversed(calls) if action in {"open", "focus"}), "WeChat")
        typed = [(i, value) for i, (action, value) in enumerate(calls) if action == "type"]
        submits = [i for i, (action, _) in enumerate(calls) if action == "search_submit"]
        submitted = submits[-1] if submits else -1
        query = next((value for i, value in reversed(typed) if submitted < 0 or i < submitted), "")
        message = next((value for i, value in reversed(typed) if i > submitted and submitted >= 0), "")
        pasted = any(action == "shortcut" and value == "paste" for action, value in calls)
        if pasted:
            message = "clipboard content"
        search_active = any(action == "shortcut" and value == "find" for action, value in calls)
        elements = [{"role": "AXTextField", "name": "Search", "value": query, "depth": 1, "editable": True, "focused": submitted < 0 and search_active, "center": {"x": 320, "y": 240}}]
        if submitted >= 0:
            elements += [{"role": "AXTable", "name": "Search Results", "depth": 1}, {"role": "AXRow", "name": query, "depth": 2}]
        if not search_active:
            elements = []
        # A simple body-only goal types straight into the message composer.
        if not submits and typed and not any(action == "shortcut" and value == "find" for action, value in calls):
            message = typed[-1][1]
        elements.append({"role": "AXTextArea", "name": "Message", "value": message,
                         "focused": not search_active or submitted >= 0, "editable": True, "depth": 1, "center": {"x": 320, "y": 480}})
        return _with_native_focused_ui_element({"ok": True, "action": "desktop.ui_elements", "data": {"app_name": app, "pid": 100, "window_id": 200, "elements": elements}})
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_submit_foreground",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("submit_foreground should wait for approval")
        ),
    )
    cases = (
        (
            "微信输入 hello 并发送",
            "bubble",
            [
                ("focus", "WeChat"),
                ("active", "WeChat"),
                ("type", "hello"),
            ],
            "app.focus_and_safe_type_text",
            "desktop.submit_foreground",
            {"action": "send"},
        ),
        (
            "打开微信发送 hello",
            "live2d",
            [],
            None,
            "app.open_and_safe_type_text",
            {"app_name": "WeChat", "text": "hello"},
        ),
        (
            "打开微信发你好",
            "bubble",
            [],
            None,
            "app.open_and_safe_type_text",
            {"app_name": "WeChat", "text": "你好"},
        ),
    )
    for (
        prompt,
        launcher_mode,
        expected_calls,
        completed_tool,
        approval_tool,
        approval_input,
    ) in cases:
        calls.clear()
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert calls == expected_calls
        assert agent_task["status"] == "waiting_approval"
        assert agent_task["needs_user_action"] is True
        assert agent_task["pending_approvals"][0]["tool_name"] == approval_tool
        assert agent_task["pending_approvals"][0]["input_preview"] == approval_input
        if completed_tool is not None:
            assert any(
                tool_call["tool_name"] == completed_tool
                and tool_call["status"] == "completed"
                for tool_call in agent_task["tool_calls"]
            )
        assert agent_task["tool_calls"][-1]["tool_name"] == approval_tool
        assert agent_task["tool_calls"][-1]["status"] == "waiting_approval"
        assert agent_task["tool_calls"][-1]["input_preview"] == approval_input
        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == approval_tool
        assert run["pending_approval"]["input_preview"] == approval_input
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_prepares_paste_then_waits_for_send_approval(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_active_window() -> dict:
        for action, app_name in reversed(calls):
            if action in {"open", "focus"}:
                calls.append(("active", app_name))
                return {**_fake_active_window_result(app_name), "data": {"app_name": app_name, "title": app_name, "pid": 100, "window_id": 200}}
        return {**_fake_active_window_result("WeChat"), "data": {"app_name": "WeChat", "title": "WeChat", "pid": 100, "window_id": 200}}

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict:
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True},
        }

    def fake_search_submit() -> dict:
        calls.append(("search_submit", ""))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "summary": "Submitted foreground search query",
            "data": {"key": "return", "modifiers": []},
        }

    def fake_hotkey(key: str, *, modifiers: list[str] | None = None) -> dict:
        calls.append(("hotkey", "+".join([*list(modifiers or []), key])))
        return {
            "ok": True,
            "action": "desktop.hotkey",
            "summary": "Sent hotkey",
            "data": {"key": key, "modifiers": list(modifiers or [])},
        }

    def fake_ui_elements(**kwargs) -> dict:
        app = kwargs.get("app_name") or next((value for action, value in reversed(calls) if action in {"open", "focus"}), "WeChat")
        typed = [(i, value) for i, (action, value) in enumerate(calls) if action == "type"]
        submits = [i for i, (action, _) in enumerate(calls) if action == "search_submit"]
        submitted = submits[-1] if submits else -1
        query = next((value for i, value in reversed(typed) if submitted < 0 or i < submitted), "")
        message = next((value for i, value in reversed(typed) if i > submitted and submitted >= 0), "")
        pasted = any(action == "shortcut" and value == "paste" for action, value in calls)
        if pasted:
            message = "clipboard content"
        search_active = any(action == "shortcut" and value == "find" for action, value in calls)
        elements = [{"role": "AXTextField", "name": "Search", "value": query, "depth": 1, "editable": True, "focused": submitted < 0 and search_active, "center": {"x": 320, "y": 240}}]
        if submitted >= 0:
            elements += [{"role": "AXTable", "name": "Search Results", "depth": 1}, {"role": "AXRow", "name": query, "depth": 2}]
            elements.append({"role": "AXStaticText", "name": query, "value": query, "description": "Conversation header", "depth": 1})
        if not search_active:
            elements = []
        # A simple body-only goal types straight into the message composer.
        if not submits and typed and not any(action == "shortcut" and value == "find" for action, value in calls):
            message = typed[-1][1]
        elements.append({"role": "AXTextArea", "name": "Message", "value": message,
                         "focused": not search_active or submitted >= 0, "editable": True, "depth": 1, "center": {"x": 320, "y": 480}})
        return _with_native_focused_ui_element({"ok": True, "action": "desktop.ui_elements", "data": {"app_name": app, "pid": 100, "window_id": 200, "elements": elements}})
    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", lambda **_kw: {
        "ok": True, "action": "desktop.running_apps", "data": {"apps": [{"name": "WeChat", "app_name": "WeChat", "running": True}]}
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", lambda query="", limit=20: {
        "ok": True, "action": "desktop.list_apps", "data": {"query": query, "apps": [{"name": "WeChat", "app_name": "WeChat", "path": "/Applications/WeChat.app", "match_score": 1.0}], "best_match": {"name": "WeChat", "app_name": "WeChat", "path": "/Applications/WeChat.app", "match_score": 1.0}}
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", fake_search_submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_hotkey", fake_hotkey)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", lambda max_chars=2000: {
        "ok": True, "action": "clipboard.read", "data": {"text": "clipboard content", "text_length": 17, "truncated": False, "max_chars": max_chars}
    })
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_submit_foreground",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("submit_foreground should wait for approval")
        ),
    )
    cases = (
        (
            "当前输入框粘贴并发送",
            "bubble",
            [("shortcut", "paste")],
            "desktop.safe_shortcut",
        ),
        (
            "打开微信粘贴后发送",
            "live2d",
            [
                ("open", "WeChat"),
                ("active", "WeChat"),
                ("focus", "WeChat"),
                ("active", "WeChat"),
                ("shortcut", "paste"),
            ],
            "app.open",
        ),
        (
            "微信给文件传输助手粘贴并发送",
            "bubble",
            [
                ("focus", "WeChat"),
                ("active", "WeChat"),
                ("shortcut", "find"),
                ("type", "文件传输助手"),
                ("search_submit", ""),
                ("shortcut", "paste"),
            ],
            "app.focus",
        ),
    )
    for prompt, launcher_mode, expected_calls, first_tool in cases:
        calls.clear()
        _result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
            tmp_path,
            monkeypatch,
            prompt,
            launcher_mode=launcher_mode,
        )

        assert calls == expected_calls, prompt
        assert agent_task["status"] == "waiting_approval"
        assert agent_task["needs_user_action"] is True
        assert agent_task["pending_approvals"][0]["tool_name"] == "desktop.submit_foreground"
        assert agent_task["pending_approvals"][0]["input_preview"] == {"action": "send"}
        assert any(
            tool_call["tool_name"] == first_tool and tool_call["status"] == "completed"
            for tool_call in agent_task["tool_calls"]
        )
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.submit_foreground"
        assert agent_task["tool_calls"][-1]["status"] == "waiting_approval"
        assert [
            tool_call["input_preview"]
            for tool_call in agent_task["tool_calls"]
            if tool_call["tool_name"] == "desktop.submit_foreground"
            and tool_call["status"] == "waiting_approval"
        ] == [{"action": "send"}]
        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == "desktop.submit_foreground"
        assert run["pending_approval"]["input_preview"] == {"action": "send"}
        assert not any(
            call["tool_name"] == "desktop.submit_foreground" and call["status"] == "completed"
            for call in agent_task["tool_calls"]
        )
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types


def test_chat_bridge_quick_message_surfaces_safe_click_accessibility_recovery(
    tmp_path,
    monkeypatch,
):
    osascript_calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr("apps.shell.agent.tools.desktop._desktop_platform", lambda: "macos")

    def fake_run_osascript(script: str, args: list[str] | None = None) -> dict:
        osascript_calls.append((script, args))
        return {
            "ok": False,
            "summary": "osascript failed",
            "error": "Not authorized to send Apple events to System Events. Accessibility permission denied.",
            "permission_error": True,
            "fallback_used": False,
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop._run_osascript", fake_run_osascript)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "点击 120, 240",
    )
    timeline = result["_task_timeline"]
    assert any(
        event["event_type"] == "agent.desktop.permission_recovery"
        for event in timeline["events"]
    ), {"event_types": event_types, "agent_task": agent_task, "timeline": timeline}
    recovery_event = next(
        event
        for event in timeline["events"]
        if event["event_type"] == "agent.desktop.permission_recovery"
        and "desktop.safe_click" in event["payload"].get("affected_tools", [])
    )

    assert osascript_calls
    assert ["120", "240", "1"] in [args for _script, args in osascript_calls]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is True
    assert agent_task["pending_approvals"] == []
    assert "桌面操作未完成：Not authorized to send Apple events to System Events." in agent_task["summary"]
    assert "缺少权限：accessibility" in agent_task["summary"]
    assert "可直接打开：打开辅助功能权限。" in agent_task["summary"]
    safe_click_tool_call = next(
        tool_call
        for tool_call in agent_task["tool_calls"]
        if tool_call["tool_name"] == "desktop.safe_click"
    )
    safe_click_output = safe_click_tool_call["output_preview"]
    assert safe_click_output["permission_error"] in {True, "True"}
    assert "accessibility" in str(safe_click_output["permission_targets"])
    assert "打开辅助功能权限" in str(safe_click_output["recovery_actions"])
    assert recovery_event["payload"]["recovery_actions"] == [
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "recovery_retry_input": {"x": 120, "y": 240},
            "recovery_retry_prompt": "点击 120, 240",
            "recovery_retry_tool": "desktop.safe_click",
            "retry_input": {"x": 120, "y": 240},
            "retry_prompt": "点击 120, 240",
            "retry_tool": "desktop.safe_click",
            "risk_level": "low",
        }
    ]
    assert run["status"] == "failed"
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.permission_recovery" in event_types
    assert "model.request.started" not in event_types
    assert recovery_event["payload"]["permission_targets"] == ["accessibility"]
    assert recovery_event["payload"]["affected_tools"] == ["desktop.safe_click"]


def test_chat_bridge_quick_message_surfaces_browser_owned_target_handoff(
    tmp_path,
    monkeypatch,
):
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "当前网页是什么",
    )
    timeline = result["_task_timeline"]

    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is False
    assert agent_task["pending_approvals"] == []
    assert agent_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
    assert agent_task["tool_calls"][-1]["tool_name"] == "browser.current_page"
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert agent_task["tool_calls"][-1]["output_preview"]["blocking_conditions"] == [
        "browser_owned_target_required"
    ]
    assert agent_task["tool_calls"][-1]["output_preview"]["user_handoff_required"] is True
    assert run["status"] == "failed"
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.permission_recovery" not in event_types
    assert "model.request.started" not in event_types
    assert not any(
        event["event_type"] == "agent.desktop.permission_recovery"
        for event in timeline["events"]
    )


def test_chat_bridge_quick_message_surfaces_app_open_recovery(
    tmp_path,
    monkeypatch,
):
    def fake_app_open(app_name: str) -> dict:
        return {
            "ok": False,
            "action": "app.open",
            "summary": "app.open failed",
            "error": "Application not found.",
            "error_code": "app_not_found",
            "data": {"app_name": app_name},
            "permission_error": False,
            "fallback_used": False,
            "recovery_hints": ["确认应用已安装，或换用精确应用名。"],
            "recovery_actions": [
                {
                    "label": "打开应用程序文件夹",
                    "tool": "desktop.open_path",
                    "input": {"path": "/Applications"},
                    "permission_target": "app_not_found",
                    "risk_level": "low",
                },
                {
                    "label": "打开 App Store",
                    "tool": "app.open",
                    "input": {"app_name": "App Store"},
                    "permission_target": "app_not_found",
                    "risk_level": "low",
                },
            ],
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 MissingTool",
    )
    timeline = result["_task_timeline"]
    recovery_event = next(
        (
            event
            for event in timeline["events"]
            if event["event_type"] == "agent.desktop.permission_recovery"
        ),
        None,
    )
    assert recovery_event is not None, {
        "summary": agent_task["summary"],
        "tool_calls": agent_task["tool_calls"],
        "event_types": [event["event_type"] for event in timeline["events"]],
    }

    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is True
    assert "已尝试启动 MissingTool，但 macOS 没找到这个应用" in agent_task["summary"]
    assert [a["label"] for a in recovery_event["payload"]["recovery_actions"]] == ["打开应用程序文件夹", "打开 App Store"]
    assert agent_task["tool_calls"][-1]["tool_name"] == "app.open"
    assert agent_task["tool_calls"][-1]["status"] == "failed"
    assert agent_task["tool_calls"][-1]["output_preview"]["error_code"] == "app_not_found"
    assert agent_task["tool_calls"][-1]["output_preview"]["recovery_actions"][0] == {
        "label": "打开应用程序文件夹",
        "tool": "desktop.open_path",
        "input": {"path": "/Applications"},
        "permission_target": "app_not_found",
        "risk_level": "low",
    }
    assert run["status"] == "failed"
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.permission_recovery" in event_types
    assert "model.request.started" not in event_types
    assert "model.request.failed" not in event_types
    assert recovery_event["payload"]["permission_targets"] == []
    assert recovery_event["payload"]["affected_tools"] == ["app.open"]
    assert recovery_event["payload"]["recovery_actions"] == agent_task["tool_calls"][-1]["output_preview"]["recovery_actions"]


def test_chat_bridge_quick_message_surfaces_app_foreground_action_recovery(
    tmp_path,
    monkeypatch,
):
    calls: list[tuple[str, str]] = []

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        calls.append(("key", action))
        return {
            "ok": False,
            "action": "desktop.safe_key",
            "summary": "desktop.safe_key failed",
            "error": "Not authorized to send events to System Events.",
            "permission_error": True,
            "permission_targets": ["accessibility"],
            "recovery_actions": [
                {
                    "label": "打开辅助功能权限",
                    "tool": "app.open",
                    "input": {"app_name": "辅助功能权限"},
                    "permission_target": "accessibility",
                    "risk_level": "low",
                }
            ],
            "data": {
                "key_action": action,
                "key_label": "Tab",
                "repeat_count": repeat_count,
            },
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result("Google Chrome")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    result, agent_task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path,
        monkeypatch,
        "打开 Chrome，然后按 Tab",
    )
    timeline = result["_task_timeline"]
    recovery_event = next(
        event
        for event in timeline["events"]
        if event["event_type"] == "agent.desktop.permission_recovery"
    )
    tool_call = agent_task["tool_calls"][-1]

    assert calls == [("open", "Google Chrome"), ("focus", "Google Chrome"), ("key", "tab")]
    assert agent_task["status"] == "failed"
    assert agent_task["needs_user_action"] is True
    assert agent_task["summary"].startswith(
        "已打开 Google Chrome，但没能按Tab。 缺少权限：accessibility。"
    )
    assert "可直接打开：打开辅助功能权限。" in agent_task["summary"]
    assert tool_call["tool_name"] == "app.open_and_safe_key"
    assert tool_call["status"] == "failed"
    assert tool_call["output_preview"]["permission_targets"] == ["accessibility"]
    recovery_actions = tool_call["output_preview"]["recovery_actions"]
    assert len(recovery_actions) == 1
    recovery_action = recovery_actions[0]
    assert {
        "label": "打开辅助功能权限",
        "tool": "app.open",
        "input": {"app_name": "辅助功能权限"},
        "permission_target": "accessibility",
        "recovery_retry_prompt": "打开Google Chrome并按Tab",
        "recovery_retry_tool": "app.open_and_safe_key",
        "retry_prompt": "打开Google Chrome并按Tab",
        "retry_tool": "app.open_and_safe_key",
        "risk_level": "low",
    }.items() <= recovery_action.items()
    expected_retry_input = {
        "app_name": "Google Chrome",
        "action": "tab",
        "repeat_count": 1,
    }
    assert expected_retry_input.items() <= recovery_action["recovery_retry_input"].items()
    assert expected_retry_input.items() <= recovery_action["retry_input"].items()
    assert run["status"] == "failed"
    assert "agent.desktop.intent_unverified" in event_types
    assert "agent.desktop.intent_completed" not in event_types
    assert "agent.desktop.permission_recovery" in event_types
    assert "model.request.started" not in event_types
    assert recovery_event["payload"]["permission_targets"] == ["accessibility"]
    assert recovery_event["payload"]["affected_tools"] == ["app.open_and_safe_key"]
    assert recovery_event["payload"]["recovery_actions"] == tool_call["output_preview"]["recovery_actions"]


def test_chat_bridge_quick_message_executes_structured_recovery_action_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-recovery-action.db",
        workspace_dir=tmp_path / "runtime-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    open_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher structured recovery action should not call model")
        ),
    )

    def fake_system_settings_open(target: str) -> dict:
        open_calls.append(target)
        return {
            "ok": True,
            "action": "system.settings_open",
            "summary": f"Opened {target}",
            "data": {
                "target": target,
                "open_target": "system_settings",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.system_settings_open", fake_system_settings_open)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开屏幕录制权限",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "recovery_tool": "system.settings_open",
                "recovery_input": {"target": "屏幕录制权限"},
                "recovery_permission_target": "screen_recording",
                "recovery_risk_level": "low",
                "allow_user_foreground_takeover": True,
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        run = service.get_run(result["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]
        messages = store.load_messages("session-current", limit=10)
        user = next(message for message in messages if message.role == "user")
        user_metadata = json.loads(user.metadata_json)

        assert result["ok"] is True
        assert open_calls == ["屏幕录制权限"]
        assert agent_task["status"] == "failed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == (
            "已打开系统设置：屏幕录制权限，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert agent_task["tool_calls"][-1]["tool_name"] == "system.settings_open"
        assert agent_task["tool_calls"][-1]["input_preview"]["target"] == "屏幕录制权限"
        assert agent_task["tool_calls"][-1]["status"] == "failed"
        assert (
            agent_task["tool_calls"][-1]["output_preview"]["reason"]
            == "desktop_verification_missing"
        )
        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert user_metadata["desktop_permission_recovery"] is True
        assert user_metadata["recovery_tool"] == "system.settings_open"
        assert user_metadata["recovery_input"] == {"target": "屏幕录制权限"}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_open_path_recovery_action_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-open-path-recovery-action.db",
        workspace_dir=tmp_path / "runtime-open-path-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    open_path_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher open-path recovery action should not call model")
        ),
    )

    def fake_open_path(path: str) -> dict:
        open_path_calls.append(path)
        return {
            "ok": True,
            "action": "desktop.open_path",
            "summary": f"Opened {path}",
            "data": {
                "path": path,
                "display_path": path,
                "open_target": "system_open",
                "exists": True,
                "is_dir": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.open_path", fake_open_path)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 ~/Downloads",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "recovery_tool": "desktop.open_path",
                "recovery_input": {"path": "~/Downloads"},
                "recovery_permission_target": "file_access",
                "recovery_risk_level": "low",
                "allow_user_foreground_takeover": True,
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        run = service.get_run(result["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]
        messages = store.load_messages("session-current", limit=10)
        user = next(message for message in messages if message.role == "user")
        user_metadata = json.loads(user.metadata_json)

        assert result["ok"] is True
        assert open_path_calls == ["~/Downloads"]
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已打开文件夹：~/Downloads。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "desktop.open_path"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"path": "~/Downloads"}
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert user_metadata["desktop_permission_recovery"] is True
        assert user_metadata["recovery_tool"] == "desktop.open_path"
        assert user_metadata["recovery_input"] == {"path": "~/Downloads"}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_browser_open_recovery_action_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-open-recovery-action.db",
        workspace_dir=tmp_path / "runtime-browser-open-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    opened_urls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser recovery action should not call model")
        ),
    )

    def fake_open_url(url: str) -> dict:
        opened_urls.append(url)
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "browser": "Google Chrome",
                "target_id": "target-recovery-owned",
                "target_websocket_available": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开 https://github.com",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "recovery_tool": "browser.open_url",
                "recovery_input": {"url": "https://github.com"},
                "recovery_permission_target": "browser",
                "recovery_risk_level": "low",
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        run = service.get_run(result["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]
        messages = store.load_messages("session-current", limit=10)
        user = next(message for message in messages if message.role == "user")
        user_metadata = json.loads(user.metadata_json)

        assert result["ok"] is True
        assert opened_urls == ["https://github.com"]
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已打开网页：https://github.com。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "browser.open_url"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"url": "https://github.com"}
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert user_metadata["desktop_permission_recovery"] is True
        assert user_metadata["recovery_tool"] == "browser.open_url"
        assert user_metadata["recovery_input"] == {"url": "https://github.com"}
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_control_recovery_actions_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-control-recovery-action.db",
        workspace_dir=tmp_path / "runtime-control-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    music_control_calls: list[str] = []
    music_play_calls: list[str] = []
    music_open_calls: list[str] = []
    music_app_calls: list[str] = []
    volume_calls: list[tuple[str, object, object]] = []
    current_volume_level = 20
    brightness_calls: list[tuple[str, object]] = []
    display_sleep_calls: list[str] = []
    screen_saver_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher control recovery action should not call model")
        ),
    )

    from apps.shell.agent.tools import desktop as native_desktop
    native_play = native_desktop.apple_music_play
    native_control = native_desktop.apple_music_control
    native_music_open = native_desktop.music_app_open_and_play

    def fake_music_script(script, args, **kwargs):
        if args and args[0] in {"play", "pause"}:
            state = "paused" if args[0] == "pause" else "playing"
            stdout = (f"controlled|{args[0]}|{state}||" if args[0] == "pause" else f"controlled|{args[0]}|{state}|超时空辉夜姬|Yachiyo")
        else:
            stdout = f"played|{args[0]}|Yachiyo|playing|track|Album|identity_verified"
        return {"ok": True, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(native_desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(native_desktop, "_run_osascript", fake_music_script)

    def fake_apple_music_control(action: str) -> dict:
        music_control_calls.append(action)
        return native_control(action)

    def fake_apple_music_play(query: str) -> dict:
        music_play_calls.append(query)
        return native_play(query)

    def fake_apple_music_open_and_play() -> dict:
        music_open_calls.append("open")
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": "Opened Music and started playback",
            "data": {
                "app_name": "Music",
                "playback_ok": True,
                "track": "超时空辉夜姬",
                "artist": "Yachiyo",
            },
        }

    def fake_music_app_open_and_play(app_name: str) -> dict:
        music_app_calls.append(app_name)
        if app_name == "Music":
            return native_music_open(app_name)
        return {
            "ok": True, "action": "media.music_app_open_and_play",
            "summary": f"Opened {app_name} and attempted playback with media key",
            "data": {"app_name": app_name, "playback_state_unverified": True},
        }

    def fake_system_volume(action: str, *, level=None, step=None) -> dict:
        nonlocal current_volume_level
        volume_calls.append((action, level, step))
        old_level = current_volume_level
        if action == "set":
            current_volume_level = int(level)
        return {
            "ok": True,
            "action": "system.volume",
            "summary": "System volume set to 35%",
            "data": {
                "requested_action": action,
                "old_level": old_level,
                "old_muted": False,
                "level": current_volume_level,
                "muted": False,
                "changed": action != "status",
            },
        }

    def fake_system_brightness(action: str, *, step=None) -> dict:
        brightness_calls.append((action, step))
        return {
            "ok": True,
            "action": "system.brightness",
            "summary": "Display brightness decreased",
            "data": {
                "requested_action": action,
                "step": step,
                "key_code": 144,
            },
        }

    def fake_system_display_sleep() -> dict:
        display_sleep_calls.append("called")
        return {
            "ok": True,
            "action": "system.display_sleep",
            "summary": "Display sleep requested",
            "data": {"requested_action": "sleep"},
        }

    def fake_system_screen_saver_start() -> dict:
        screen_saver_calls.append("called")
        return {
            "ok": True,
            "action": "system.screen_saver_start",
            "summary": "Screen saver start requested",
            "data": {"requested_action": "start"},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.apple_music_control", fake_apple_music_control)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.apple_music_play", fake_apple_music_play)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.system_volume", fake_system_volume)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.system_brightness", fake_system_brightness)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.system_display_sleep",
        fake_system_display_sleep,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.system_screen_saver_start",
        fake_system_screen_saver_start,
    )
    monkeypatch.setattr(native_desktop, "app_open", lambda name: {"ok": True, "action": "app.open", "data": {"app_name": name}})
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "暂停 Apple Music",
                "media.apple_music_control",
                {"action": "pause"},
                "已暂停 Apple Music。",
                "completed",
            ),
            (
                "在 Apple Music 中播放 超时空辉夜姬",
                "media.apple_music_play",
                {"query": "超时空辉夜姬"},
                "已在 Apple Music 播放：超时空辉夜姬 - Yachiyo。",
                "completed",
            ),
            (
                "打开Apple Music并播放",
                "media.music_app_open_and_play",
                {"app_name": "Music"},
                "已打开 Apple Music，并开始播放。当前：超时空辉夜姬 - Yachiyo。",
                "completed",
            ),
            (
                "打开Spotify并播放",
                "media.music_app_open_and_play",
                {"app_name": "Spotify"},
                "已打开 Spotify，并用媒体键尝试开始播放，但无法确认播放状态；请在播放器中确认后重试。",
                "failed",
            ),
            (
                "把音量调到 35%",
                "system.volume",
                {"action": "set", "level": 35},
                "已把系统音量调到 35%。",
                "completed",
            ),
            (
                "屏幕暗一点",
                "system.brightness",
                {"action": "down"},
                "已调低屏幕亮度（2 格）。",
                "failed",
            ),
            (
                "关闭屏幕",
                "system.display_sleep",
                {},
                "已让显示器睡眠。",
                "failed",
            ),
            (
                "启动屏幕保护程序",
                "system.screen_saver_start",
                {},
                "已启动屏幕保护程序。",
                "failed",
            ),
        )
        for prompt, tool_name, tool_input, expected_summary, expected_status in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "live2d",
                    "launcher_surface": "quick_message",
                    "runnable_kind": "main",
                    "daily_desktop_intent": True,
                    "desktop_permission_recovery": True,
                    "allow_user_foreground_takeover": True,
                    "recovery_tool": tool_name,
                    "recovery_input": tool_input,
                    "recovery_permission_target": "desktop_control",
                    "recovery_risk_level": "low",
                },
            )
            assert "agent_task" in result, result
            agent_task = result["agent_task"]
            run = service.get_run(result["run_id"])
            event_types = [
                event["event_type"]
                for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
            ]

            assert result["ok"] is True
            assert agent_task["status"] == expected_status
            assert agent_task["pending_approvals"] == []
            if tool_name in {"system.brightness", "system.display_sleep", "system.screen_saver_start"}:
                assert agent_task["summary"].startswith(expected_summary.rstrip("。"))
                assert "未能确认" in agent_task["summary"]
            else:
                assert agent_task["summary"] == expected_summary
            assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
            assert tool_input.items() <= agent_task["tool_calls"][-1]["input_preview"].items()
            assert agent_task["tool_calls"][-1]["status"] == expected_status
            assert run["status"] == expected_status
            assert run["pending_approval"] == {}
            assert "agent.desktop.intent_planned" in event_types
            assert "agent.tool.call" in event_types
            if expected_status == "completed":
                assert agent_task["needs_user_action"] is False
                assert "agent.desktop.intent_completed" in event_types
                assert "agent.desktop.intent_unverified" not in event_types
            else:
                assert "agent.desktop.intent_unverified" in event_types
                assert "agent.desktop.intent_completed" not in event_types
            assert "agent.desktop.intent_approval_required" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types

        assert music_control_calls == ["pause", "play"]
        assert music_play_calls == ["超时空辉夜姬"]
        assert music_open_calls == []
        assert music_app_calls == ["Music", "Spotify"]
        assert volume_calls == [("set", 35, None), ("status", None, None)]
        assert brightness_calls == [("down", 2)]
        assert display_sleep_calls == ["called"]
        assert screen_saver_calls == ["called"]
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_diagnostic_recovery_actions_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-diagnostic-recovery-action.db",
        workspace_dir=tmp_path / "runtime-diagnostic-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    clipboard_writes: list[str] = []
    clipboard_value = "hello world"
    clipboard_reads: list[int] = []
    screen_targets: list[str] = []
    permission_calls: list[bool] = []
    active_window_calls = 0
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher diagnostic recovery action should not call model")
        ),
    )

    def fake_clipboard_write(text: str) -> dict:
        nonlocal clipboard_value
        clipboard_value = text
        clipboard_writes.append(text)
        return {
            "ok": True,
            "action": "clipboard.write",
            "summary": "Copied 5 characters to clipboard",
            "data": {"text_length": len(text), "platform": "macos"},
        }

    def fake_clipboard_read(*, max_chars=2000) -> dict:
        clipboard_reads.append(max_chars)
        return {
            "ok": True,
            "action": "clipboard.read",
            "summary": "Read 11 characters from clipboard",
            "data": {
                "text": clipboard_value,
                "text_length": len(clipboard_value),
                "truncated": False,
                "max_chars": max_chars,
                "platform": "macos",
            },
        }

    def fake_screen_capture(target_path) -> dict:
        screen_targets.append(str(target_path))
        return {
            "ok": True,
            "action": "screen.capture",
            "summary": "已截取当前屏幕。",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "size_bytes": 10,
                "width": 100,
                "height": 80,
            },
        }

    def fake_permissions() -> dict:
        permission_calls.append(True)
        return {
            "ok": True,
            "action": "desktop.permissions",
            "summary": "Desktop permissions ready",
            "data": {"permission_targets": [], "affected_tools": []},
        }

    def fake_active_window() -> dict:
        nonlocal active_window_calls
        active_window_calls += 1
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Foreground window: Google Chrome - ChatGPT",
            "data": {"app_name": "Google Chrome", "title": "ChatGPT", "pid": 202},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_write", fake_clipboard_write)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.clipboard_read", fake_clipboard_read)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.screen_capture", fake_screen_capture)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.permissions", fake_permissions)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "把 hello 复制到剪贴板",
                "clipboard.write",
                {"text": "hello"},
                "已复制 5 个字符到剪贴板。",
            ),
            (
                "读取剪贴板",
                "clipboard.read",
                {},
                "剪贴板内容：hello",
            ),
            (
                "截图当前屏幕",
                "screen.capture",
                {"reason": "user asked to capture the screen"},
                "已截取当前屏幕。",
            ),
            (
                "检查桌面权限",
                "desktop.permissions",
                {},
                "桌面执行权限已就绪。",
            ),
            (
                "查看当前窗口",
                "desktop.active_window",
                {},
                "当前前台窗口是 Google Chrome：ChatGPT。",
            ),
        )
        for prompt, tool_name, tool_input, expected_summary in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "live2d",
                    "launcher_surface": "quick_message",
                    "runnable_kind": "main",
                    "daily_desktop_intent": True,
                    "desktop_permission_recovery": True,
                    "recovery_tool": tool_name,
                    "recovery_input": tool_input,
                    "recovery_permission_target": "desktop_diagnostic",
                    "recovery_risk_level": "low",
                    "allow_user_foreground_takeover": True,
                },
            )
            assert "agent_task" in result, result
            agent_task = result["agent_task"]
            run = service.get_run(result["run_id"])
            event_types = [
                event["event_type"]
                for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
            ]

            assert result["ok"] is True
            assert agent_task["status"] == "completed"
            assert agent_task["needs_user_action"] is False
            assert agent_task["pending_approvals"] == []
            assert agent_task["summary"] == expected_summary
            assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
            assert agent_task["tool_calls"][-1]["input_preview"] == tool_input
            assert run["status"] == "completed"
            assert run["pending_approval"] == {}
            assert "agent.desktop.intent_planned" in event_types
            assert "agent.tool.call" in event_types
            assert "agent.desktop.intent_completed" in event_types
            assert "agent.desktop.intent_approval_required" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types

        assert clipboard_writes == ["hello"]
        assert clipboard_reads == [2000, 2000]
        assert screen_targets and screen_targets[0].endswith("screenshots/current-screen.png")
        assert permission_calls == [True]
        assert active_window_calls == 1
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_observation_recovery_actions_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-observation-recovery-action.db",
        workspace_dir=tmp_path / "runtime-observation-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    current_page_calls = 0
    opened_urls: list[str] = []
    extract_calls: list[str] = []
    screenshot_calls: list[str] = []
    running_calls = 0
    windows_calls: list[str] = []
    ui_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher observation recovery action should not call model")
        ),
    )

    def fake_current_page() -> dict:
        nonlocal current_page_calls
        current_page_calls += 1
        return {
            "ok": True,
            "action": "browser.current_page",
            "summary": "Current browser page: ChatGPT",
            "data": {"title": "ChatGPT", "url": "https://chatgpt.com/"},
        }

    def fake_extract_text(selector: str = "") -> dict:
        extract_calls.append(selector)
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted 29 characters from browser page",
            "data": {
                "selector": selector,
                "text": "Yachiyo desktop agent runtime",
                "truncated": False,
            },
        }

    def fake_open_url(url: str) -> dict:
        opened_urls.append(url)
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "browser": "Google Chrome",
                "target_id": "target-observation-owned",
                "target_websocket_available": True,
            },
        }

    def fake_screenshot(target_path) -> dict:
        screenshot_calls.append(str(target_path))
        return {
            "ok": True,
            "action": "browser.screenshot",
            "summary": "Captured current browser page",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "format": "png",
                "size": 10,
            },
        }

    def fake_running_apps() -> dict:
        nonlocal running_calls
        running_calls += 1
        return {
            "ok": True,
            "action": "desktop.running_apps",
            "summary": "Running apps: Finder, Google Chrome, Music",
            "data": {
                "apps": [
                    {"name": "Finder", "pid": 101, "frontmost": False},
                    {"name": "Google Chrome", "pid": 202, "frontmost": True},
                    {"name": "Music", "pid": 303, "frontmost": False},
                ],
                "frontmost": "Google Chrome",
            },
        }

    def fake_windows(app_name: str = "") -> dict:
        windows_calls.append(app_name)
        return {
            "ok": True,
            "action": "desktop.windows",
            "summary": "Read open windows",
            "data": {
                "app_name": app_name,
                "windows": [
                    {"app_name": "Google Chrome", "title": "ChatGPT"},
                ],
            },
        }

    def fake_ui_elements(role_filter: str = "", limit: int = 80, app_name: str = "") -> dict:
        ui_calls.append((role_filter, limit))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read current UI elements",
            "data": {
                "app_name": "Google Chrome",
                "title": "ChatGPT",
                "elements": [
                    {
                        "role": "AXButton",
                        "name": "Send",
                        "enabled": True,
                        "center": {"x": 640, "y": 720},
                    },
                ],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.current_page", fake_current_page)
    monkeypatch.setattr("apps.shell.agent.tools.browser.open_url", fake_open_url)
    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    monkeypatch.setattr("apps.shell.agent.tools.browser.screenshot", fake_screenshot)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", fake_running_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.windows", fake_windows)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "读取当前网页标题和地址",
                "browser.current_page",
                {},
                _BROWSER_TARGET_HANDOFF_SUMMARY,
                "failed",
            ),
            (
                "读取当前网页正文",
                "browser.extract_text",
                {},
                _BROWSER_TARGET_HANDOFF_SUMMARY,
                "failed",
            ),
            (
                "打开并读取 https://github.com",
                "browser.open_url_and_extract_text",
                {"url": "https://github.com"},
                "Yachiyo desktop agent runtime",
                "completed",
            ),
            (
                "打开 https://github.com 并截图",
                "browser.open_url_and_screenshot",
                {"url": "https://github.com", "reason": "user asked to capture the browser page after opening a URL"},
                "已打开网页并截取当前网页。",
                "completed",
            ),
            (
                "截取当前网页",
                "browser.screenshot",
                {"reason": "user asked to capture the browser page"},
                _BROWSER_TARGET_HANDOFF_SUMMARY,
                "failed",
            ),
            (
                "列出当前运行的应用",
                "desktop.running_apps",
                {},
                "正在运行的应用：Finder, Google Chrome, Music。前台是 Google Chrome。",
                "completed",
            ),
            (
                "查看Google Chrome窗口",
                "desktop.windows",
                {"app_name": "Google Chrome"},
                "当前窗口：Google Chrome: ChatGPT。",
                "completed",
            ),
            (
                "查看当前界面按钮",
                "desktop.ui_elements",
                {"role_filter": "button", "limit": 80},
                "当前 Google Chrome 界面控件：Button Send（640, 720）。",
                "completed",
            ),
        )
        for prompt, tool_name, tool_input, expected_summary, expected_status in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "live2d",
                    "launcher_surface": "quick_message",
                    "runnable_kind": "main",
                    "daily_desktop_intent": True,
                    "desktop_permission_recovery": True,
                    "recovery_tool": tool_name,
                    "recovery_input": tool_input,
                    "recovery_permission_target": "desktop_observation",
                    "recovery_risk_level": "low",
                    "allow_user_foreground_takeover": True,
                },
            )
            assert "agent_task" in result, result
            agent_task = result["agent_task"]
            run = service.get_run(result["run_id"])
            event_types = [
                event["event_type"]
                for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
            ]

            assert result["ok"] is True
            assert agent_task["status"] == expected_status
            assert agent_task["needs_user_action"] is False
            assert agent_task["pending_approvals"] == []
            assert agent_task["summary"] == expected_summary
            assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
            assert agent_task["tool_calls"][-1]["input_preview"] == tool_input
            assert agent_task["tool_calls"][-1]["status"] == expected_status
            assert run["status"] == expected_status
            assert run["pending_approval"] == {}
            assert "agent.desktop.intent_planned" in event_types
            if expected_status == "completed":
                assert "agent.tool.call" in event_types
                assert "agent.desktop.intent_completed" in event_types
                assert "agent.desktop.intent_unverified" not in event_types
            else:
                assert "agent.tool.skipped" in event_types
                assert "agent.desktop.intent_unverified" in event_types
                assert "agent.desktop.intent_completed" not in event_types
            assert "agent.desktop.intent_approval_required" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types

        assert current_page_calls == 0
        assert opened_urls == ["https://github.com", "https://github.com"]
        assert extract_calls == [""]
        assert len(screenshot_calls) == 1
        assert all(call.endswith("browser/current-page.png") for call in screenshot_calls)
        assert running_calls == 1
        assert windows_calls == ["Google Chrome"]
        assert ui_calls == [("button", 80)]
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_safe_foreground_recovery_actions_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-safe-foreground-recovery-action.db",
        workspace_dir=tmp_path / "runtime-safe-foreground-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    calls: list[tuple] = []
    typed_text = ""
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher safe foreground recovery action should not call model")
        ),
    )

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        calls.append(("key", action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": f"Pressed {action}",
            "data": {"key_action": action, "repeat_count": repeat_count},
        }

    def fake_safe_scroll(direction: str, *, pages: int = 1) -> dict:
        calls.append(("scroll", direction, pages))
        return {
            "ok": True,
            "action": "desktop.safe_scroll",
            "summary": f"Scrolled foreground desktop {direction}",
            "data": {"direction": direction, "pages": pages, "explicit_user_scroll": True},
        }

    def fake_safe_click(x: int, y: int) -> dict:
        calls.append(("click", x, y))
        return {
            "ok": True,
            "action": "desktop.safe_click",
            "summary": f"Clicked explicit foreground coordinate at ({x}, {y})",
            "data": {"x": x, "y": y, "click_count": 1, "explicit_user_coordinates": True},
        }

    def fake_safe_type_text(text: str) -> dict:
        nonlocal typed_text
        typed_text = text
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True, "app_name": "ForegroundApp"},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_scroll", fake_safe_scroll)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_click", fake_safe_click)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    def fake_ui_elements(**_kwargs) -> dict:
        app_name = "ForegroundApp"
        return {"ok": True, "action": "desktop.ui_elements", "data": {
            "app_name": app_name,
            "elements": [{"role": "AXTextField", "name": "Input", "value": typed_text, "focused": True}],
        }}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: _fake_active_window_result("ForegroundApp"))
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "复制选中内容",
                "desktop.safe_shortcut",
                {"action": "copy"},
                "已发送“复制选中内容”快捷键。",
            ),
            (
                "按Tab",
                "desktop.safe_key",
                {"action": "tab", "repeat_count": 1},
                "已按Tab。",
            ),
            (
                "向下滚动2页",
                "desktop.safe_scroll",
                {"direction": "down", "pages": 2},
                "已向下滚动前台界面（2 页）。",
            ),
            (
                "点击 120, 240",
                "desktop.safe_click",
                {"x": 120, "y": 240},
                "已点击前台位置：120, 240。",
            ),
            (
                "输入hello",
                "desktop.safe_type_text",
                {"text": "hello"},
                "已向前台输入文字（5 个字符）。",
            ),
        )
        for prompt, tool_name, tool_input, expected_summary in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "live2d",
                    "launcher_surface": "quick_message",
                    "runnable_kind": "main",
                    "daily_desktop_intent": True,
                    "desktop_permission_recovery": True,
                    "recovery_tool": tool_name,
                    "recovery_input": tool_input,
                    "recovery_permission_target": "foreground_input",
                    "recovery_risk_level": "low",
                    "allow_user_foreground_takeover": True,
                },
            )
            assert "agent_task" in result, result
            agent_task = result["agent_task"]
            run = service.get_run(result["run_id"])
            event_types = [
                event["event_type"]
                for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
            ]

            assert result["ok"] is True
            expect_unverified = tool_name.endswith(("safe_key", "safe_shortcut"))
            expected_status = "failed" if expect_unverified else "completed"
            assert agent_task["status"] == expected_status, agent_task
            assert agent_task["needs_user_action"] is False
            assert agent_task["pending_approvals"] == []
            if expect_unverified:
                assert "无法确认" in agent_task["summary"] or "未能确认" in agent_task["summary"]
            else:
                assert agent_task["summary"] == expected_summary
            assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
            assert tool_input.items() <= agent_task["tool_calls"][-1]["input_preview"].items()
            assert agent_task["tool_calls"][-1]["output_preview"]["ok"] is True
            assert run["status"] == expected_status
            assert run["pending_approval"] == {}
            assert "agent.desktop.intent_planned" in event_types
            assert "agent.tool.call" in event_types
            if expect_unverified:
                assert "agent.desktop.intent_unverified" in event_types
                assert "agent.desktop.intent_completed" not in event_types
            else:
                assert "agent.desktop.intent_completed" in event_types
            assert "agent.desktop.intent_approval_required" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types

        assert calls == [
            ("shortcut", "copy"),
            ("key", "tab", 1),
            ("scroll", "down", 2),
            ("click", 120, 240),
            ("type", "hello"),
        ]
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_app_foreground_recovery_actions_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-foreground-recovery-action.db",
        workspace_dir=tmp_path / "runtime-app-foreground-recovery-action",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    calls: list[tuple] = []
    typed_text = ""
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app foreground recovery action should not call model")
        ),
    )

    def fake_app_open(app_name: str) -> dict:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict:
        for call in reversed(calls):
            if len(call) >= 2 and call[0] in {"open", "focus"}:
                app_name = str(call[1])
                calls.append(("active", app_name))
                return _fake_active_window_result(app_name)
        calls.append(("active", "Google Chrome"))
        return _fake_active_window_result("Google Chrome")

    def fake_safe_type_text(text: str) -> dict:
        nonlocal typed_text
        typed_text = text
        calls.append(("type", text))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": "Typed user-provided text into the foreground app",
            "data": {"character_count": len(text), "explicit_user_text": True, "app_name": next((str(c[1]) for c in reversed(calls) if c[0] in {"open", "focus"}), "Google Chrome")},
        }

    def fake_safe_shortcut(action: str) -> dict:
        calls.append(("shortcut", action))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_key(action: str, *, repeat_count: int = 1) -> dict:
        calls.append(("key", action, repeat_count))
        return {
            "ok": True,
            "action": "desktop.safe_key",
            "summary": f"Pressed {action}",
            "data": {"key_action": action, "repeat_count": repeat_count},
        }

    def fake_safe_scroll(direction: str, *, pages: int = 1) -> dict:
        calls.append(("scroll", direction, pages))
        return {
            "ok": True,
            "action": "desktop.safe_scroll",
            "summary": f"Scrolled foreground desktop {direction}",
            "data": {"direction": direction, "pages": pages, "explicit_user_scroll": True},
        }

    def fake_safe_click(x: int, y: int) -> dict:
        calls.append(("click", x, y))
        return {
            "ok": True,
            "action": "desktop.safe_click",
            "summary": f"Clicked explicit foreground coordinate at ({x}, {y})",
            "data": {"x": x, "y": y, "click_count": 1, "explicit_user_coordinates": True},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", fake_safe_type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_key", fake_safe_key)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_scroll", fake_safe_scroll)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_click", fake_safe_click)
    def fake_ui_elements(**_kwargs) -> dict:
        app_name = next((str(c[1]) for c in reversed(calls) if c[0] in {"open", "focus"}), "Google Chrome")
        return {"ok": True, "action": "desktop.ui_elements", "data": {
            "app_name": app_name,
            "elements": [{"role": "AXTextField", "name": "Input", "value": typed_text, "focused": True}],
        }}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "打开Notes并输入hello",
                "app.open_and_safe_type_text",
                {"app_name": "Notes", "text": "hello"},
                "已打开 Notes 并输入文字（5 个字符）。",
            ),
            (
                "切到Google Chrome并粘贴",
                "app.focus_and_safe_shortcut",
                {"app_name": "Google Chrome", "action": "paste"},
                "已切到 Google Chrome 并发送“粘贴”快捷键。",
            ),
            (
                "打开Google Chrome并按Tab",
                "app.open_and_safe_key",
                {"app_name": "Google Chrome", "action": "tab", "repeat_count": 1},
                "已打开 Google Chrome 并按Tab。",
            ),
            (
                "切到Google Chrome并向下滚动2页",
                "app.focus_and_safe_scroll",
                {"app_name": "Google Chrome", "direction": "down", "pages": 2},
                "已切到 Google Chrome 并向下滚动前台界面（2 页）。",
            ),
            (
                "打开Google Chrome并点击 120, 240",
                "app.open_and_safe_click",
                {"app_name": "Google Chrome", "x": 120, "y": 240},
                "已打开 Google Chrome 并点击前台位置：120, 240。",
            ),
        )
        for prompt, tool_name, tool_input, expected_summary in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "live2d",
                    "launcher_surface": "quick_message",
                    "runnable_kind": "main",
                    "daily_desktop_intent": True,
                    "desktop_permission_recovery": True,
                    "recovery_tool": tool_name,
                    "recovery_input": tool_input,
                    "recovery_permission_target": "foreground_input",
                    "recovery_risk_level": "low",
                    "allow_user_foreground_takeover": True,
                },
            )
            assert "agent_task" in result, result
            agent_task = result["agent_task"]
            run = service.get_run(result["run_id"])
            events = service.list_run_events(run["run_id"], include_internal=True)["events"]
            event_types = [event["event_type"] for event in events]

            assert result["ok"] is True
            expect_unverified = tool_name.endswith(("safe_key", "safe_shortcut"))
            expected_status = "failed" if expect_unverified else "completed"
            assert agent_task["status"] == expected_status, agent_task
            assert agent_task["needs_user_action"] is False
            assert agent_task["pending_approvals"] == []
            if expect_unverified:
                assert "无法确认" in agent_task["summary"] or "未能确认" in agent_task["summary"]
            else:
                assert agent_task["summary"] == expected_summary
            assert agent_task["tool_calls"][-1]["tool_name"] == tool_name
            assert tool_input.items() <= agent_task["tool_calls"][-1]["input_preview"].items()
            if expect_unverified:
                native_tool = tool_name
                assert any(
                    event["event_type"] == "agent.tool.call"
                    and event.get("payload", {}).get("tool") == native_tool
                    and event["payload"].get("result", {}).get("ok") is True
                    for event in events
                )
            else:
                assert agent_task["tool_calls"][-1]["output_preview"]["ok"] is True
            assert run["status"] == expected_status
            assert run["pending_approval"] == {}
            assert "agent.desktop.intent_planned" in event_types
            assert "agent.tool.call" in event_types
            if expect_unverified:
                assert "agent.desktop.intent_unverified" in event_types
                assert "agent.desktop.intent_completed" not in event_types
            else:
                assert "agent.desktop.intent_completed" in event_types
            assert "agent.desktop.intent_approval_required" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types

        assert ("type", "hello") in calls
        assert ("shortcut", "paste") in calls
        assert ("key", "tab", 1) in calls
        assert ("scroll", "down", 2) in calls
        assert ("click", 120, 240) in calls
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_ui_element_recovery_retry_keeps_approval_gate(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-ui-element-recovery-approval.db",
        workspace_dir=tmp_path / "runtime-ui-element-recovery-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher UI element recovery retry should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.type_into_ui_element",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("type_into_ui_element should wait for approval")
        ),
    )
    def fake_ui_elements(**_kwargs) -> dict:
        return {"ok": True, "action": "desktop.ui_elements", "data": {
            "app_name": "WeChat", "elements": [{"role": "AXTextField", "name": "消息", "value": "", "focused": True}],
        }}

    def fake_inspect_app(app_name, **_kwargs) -> dict:
        return {"ok": True, "action": "desktop.inspect_app", "data": {
            "app_name": app_name, "app_found": True, "running": True, "ready": True,
            "ui_elements": fake_ui_elements(),
        }}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    bridge = ChatBridge(runtime)
    try:
        tool_input = {
            "app_name": "WeChat",
            "target": "消息",
            "text": "文件传输助手",
            "role_filter": "text",
            "limit": 80,
        }
        result = bridge.send_quick_message(
            '打开WeChat并在前台控件"消息"输入"文件传输助手"',
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "desktop_permission_retry": True,
                "recovery_action_kind": "retry_original",
                "recovery_tool": "app.open_and_type_into_ui_element",
                "recovery_input": tool_input,
                "recovery_permission_target": "foreground_input",
                "recovery_retry_tool": "app.open_and_type_into_ui_element",
                "recovery_retry_input": tool_input,
                "source_task_id": "task-source-ui-type",
                "allow_user_foreground_takeover": True,
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        run = service.get_run(result["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert result["ok"] is True
        assert agent_task["status"] == "waiting_approval"
        assert agent_task["needs_user_action"] is True
        assert agent_task["pending_approvals"][0]["tool_name"] == "app.open_and_type_into_ui_element"
        assert agent_task["pending_approvals"][0]["input_preview"] == tool_input
        assert agent_task["tool_calls"][-1]["tool_name"] == "app.open_and_type_into_ui_element"
        assert agent_task["tool_calls"][-1]["status"] == "waiting_approval"
        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == "app.open_and_type_into_ui_element"
        assert run["pending_approval"]["input_preview"] == tool_input
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.tool.approval_required" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_executes_recovery_retry_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-recovery-retry.db",
        workspace_dir=tmp_path / "runtime-recovery-retry",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    play_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher recovery retry should not call model")
        ),
    )

    from apps.shell.agent.tools import desktop as native_desktop
    native_play = native_desktop.apple_music_play
    native_control = native_desktop.apple_music_control
    native_music_open = native_desktop.music_app_open_and_play

    def fake_music_script(script, args, **kwargs):
        if args and args[0] in {"play", "pause"}:
            state = "paused" if args[0] == "pause" else "playing"
            stdout = (f"controlled|{args[0]}|{state}||" if args[0] == "pause" else f"controlled|{args[0]}|{state}|超时空辉夜姬|Yachiyo")
        else:
            stdout = f"played|{args[0]}|Yachiyo|playing|track|Album|identity_verified"
        return {"ok": True, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(native_desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(native_desktop, "_run_osascript", fake_music_script)

    def fake_apple_music_play(query: str) -> dict:
        play_calls.append(query)
        return native_play(query)

    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.apple_music_play",
        fake_apple_music_play,
    )
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "在 Apple Music 中播放 超时空辉夜姬",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
                "daily_desktop_intent": True,
                "desktop_permission_recovery": True,
                "desktop_permission_retry": True,
                "recovery_action_kind": "retry_original",
                "recovery_tool": "media.apple_music_play",
                "recovery_input": {"query": "超时空辉夜姬"},
                "recovery_permission_target": "music_app",
                "recovery_retry_tool": "media.apple_music_play",
                "recovery_retry_input": {"query": "超时空辉夜姬"},
                "recovery_retry_prompt": "在 Apple Music 中播放 超时空辉夜姬",
                "source_task_id": "task-source-music",
            },
        )
        assert "agent_task" in result, result
        agent_task = result["agent_task"]
        run = service.get_run(result["run_id"])
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
        planned_event = next(
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        )
        retry_context_event = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.recovery_retry_context"
        )
        messages = store.load_messages("session-current", limit=10)
        user = next(message for message in messages if message.role == "user")
        user_metadata = json.loads(user.metadata_json)

        assert result["ok"] is True
        assert play_calls == ["超时空辉夜姬"]
        assert agent_task["status"] == "completed"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert agent_task["summary"] == "已在 Apple Music 播放：超时空辉夜姬 - Yachiyo。"
        assert agent_task["tool_calls"][-1]["tool_name"] == "media.apple_music_play"
        assert agent_task["tool_calls"][-1]["input_preview"] == {"query": "超时空辉夜姬"}
        assert planned_event["payload"]["source"] == "daily_desktop_metadata"
        assert planned_event["payload"]["planning_reason"] == "structured_recovery_metadata"
        assert planned_event["payload"]["tool"] == "media.apple_music_play"
        assert retry_context_event["payload"]["retry_tool"] == "media.apple_music_play"
        assert retry_context_event["payload"]["retry_input"] == {"query": "超时空辉夜姬"}
        assert retry_context_event["payload"]["retry_prompt"] == "在 Apple Music 中播放 超时空辉夜姬"
        assert retry_context_event["payload"]["source_task_id"] == "task-source-music"
        assert user_metadata["desktop_permission_retry"] is True
        assert user_metadata["recovery_action_kind"] == "retry_original"
        assert user_metadata["recovery_retry_tool"] == "media.apple_music_play"
        assert "agent.desktop.recovery_retry_context" in event_types
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_approval_executes_and_completes_launcher_task(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-approval.db",
        workspace_dir=tmp_path / "runtime-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    hotkey_calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher hotkey approval should not call model")
        ),
    )

    def fake_hotkey(key: str, *, modifiers: list[str] | None = None) -> dict:
        hotkey_calls.append((key, modifiers))
        return {
            "ok": True,
            "action": "desktop.hotkey",
            "summary": "Sent hotkey",
            "data": {
                "key": key,
                "modifiers": list(modifiers or []),
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_hotkey", fake_hotkey)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "你能帮我按Command L吗",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert hotkey_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == "desktop.hotkey"
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "desktop.hotkey"

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert hotkey_calls == [("l", ["command"])]
        assert approved.status == "completed"
        assert approved.summary == "已发送快捷键：Command+L。"
        assert approved.needs_user_action is False
        assert approved.pending_approvals == []
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.output.completed" in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_copies_current_page_link_without_model(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-current-page-link-copy.db",
        workspace_dir=tmp_path / "runtime-current-page-link-copy",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    hotkey_calls: list[tuple[str, list[str] | None]] = []
    shortcut_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("current page link copy should not call model")
        ),
    )

    def fake_hotkey(key: str, *, modifiers: list[str] | None = None) -> dict:
        hotkey_calls.append((key, modifiers))
        return {
            "ok": True,
            "action": "desktop.hotkey",
            "summary": "Sent hotkey",
            "data": {
                "key": key,
                "modifiers": list(modifiers or []),
            },
        }

    def fake_safe_shortcut(action: str) -> dict:
        shortcut_calls.append(action)
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": "Copied",
            "data": {
                "shortcut_action": action,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_hotkey", fake_hotkey)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", fake_safe_shortcut)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "copy current page link",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        agent_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert result["ok"] is True
        assert hotkey_calls == []
        assert shortcut_calls == ["copy_current_page_link"]
        assert agent_task["status"] == "completed"
        assert agent_task["summary"] == "已发送“复制当前网页链接”快捷键。"
        assert agent_task["needs_user_action"] is False
        assert agent_task["pending_approvals"] == []
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_routes_system_hotkeys_to_approval_and_completes(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-system-hotkey-approval.db",
        workspace_dir=tmp_path / "runtime-system-hotkey-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    hotkey_calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher system hotkey approval should not call model")
        ),
    )

    def fake_hotkey(key: str, *, modifiers: list[str] | None = None) -> dict:
        hotkey_calls.append((key, modifiers))
        return {
            "ok": True,
            "action": "desktop.hotkey",
            "summary": "Sent hotkey",
            "data": {
                "key": key,
                "modifiers": list(modifiers or []),
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_hotkey", fake_hotkey)
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "Can you press Command L?",
                {"key": "l", "modifiers": ["command"]},
                ("l", ["command"]),
                "已发送快捷键：Command+L。",
            ),
            (
                "当前窗口按回车",
                {"key": "return", "modifiers": []},
                ("return", []),
                "已发送快捷键：return。",
            ),
        )
        for text, input_preview, expected_call, summary in cases:
            result = bridge.send_quick_message(
                text,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "bubble",
                    "launcher_surface": "quick_message",
                    "allow_user_foreground_takeover": True,
                },
            )
            task_id = result["task_id"]
            waiting_task = result["agent_task"]
            link = service.get_task_run_link(task_id)
            waiting_run = service.get_run(link["run_id"])

            assert result["ok"] is True
            assert waiting_task["status"] == "waiting_approval"
            assert waiting_task["needs_user_action"] is True
            assert waiting_task["pending_approvals"][0]["tool_name"] == "desktop.hotkey"
            pending_input = waiting_task["pending_approvals"][0]["input_preview"]
            assert pending_input == input_preview
            assert waiting_run["status"] == "approval_required"
            assert waiting_run["pending_approval"]["tool"] == "desktop.hotkey"

            approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
                task_id,
                _approval_decision_for_run(waiting_run),
            )
            run = service.get_run(link["run_id"])
            events = service.list_run_events(run["run_id"], include_internal=True)["events"]
            event_types = [event["event_type"] for event in events]
            executed_hotkeys = [
                event
                for event in events
                if event["event_type"] == "agent.tool.call"
                and isinstance(event.get("payload"), dict)
                and event["payload"].get("tool") == "desktop.hotkey"
                and isinstance(event["payload"].get("result"), dict)
                and event["payload"]["result"].get("ok") is True
                and event["payload"].get("approval_resume_result_canonical") is not True
            ]

            assert hotkey_calls[-1] == expected_call
            assert approved.status == "completed"
            assert approved.summary == summary
            assert approved.needs_user_action is False
            assert approved.pending_approvals == []
            assert run["status"] == "completed"
            assert run["pending_approval"] == {}
            assert len(executed_hotkeys) == 1
            assert event_types.count("agent.desktop.intent_approval_required") == 1
            assert event_types.count("agent.tool.approval_required") == 1
            assert "agent.desktop.intent_completed" in event_types
            assert "model.output.completed" in event_types
            assert "agent.replan.requested" not in event_types
            assert "model.request.started" not in event_types
            assert "model.requested" not in event_types
            assert not any(
                event_type.startswith("agent.post_action_verification.")
                for event_type in event_types
            )

        assert hotkey_calls == [
            ("l", ["command"]),
            ("return", []),
        ]
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_browser_click_approval_executes_and_completes(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-click-approval.db",
        workspace_dir=tmp_path / "runtime-browser-click-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    click_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser click approval should not call model")
        ),
    )

    def fake_browser_click(selector: str, **kwargs: Any) -> dict:
        click_calls.append((selector, int(kwargs.get("click_count") or 1)))
        return {
            "ok": True,
            "action": "browser.click",
            "summary": "Clicked browser selector",
            "data": {
                "selector": selector,
                "label": "登录",
                "tag": "BUTTON",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.click", fake_browser_click)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "点击当前网页上的登录按钮",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert click_calls == []
        assert waiting_task["status"] == "failed"
        assert waiting_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
        assert waiting_task["needs_user_action"] is False
        assert waiting_task["pending_approvals"] == []
        assert waiting_run["status"] == "failed"
        assert waiting_run["pending_approval"] == {}
        event_types = [
            event["event_type"]
            for event in service.list_run_events(waiting_run["run_id"], include_internal=True)["events"]
        ]

        assert click_calls == []
        assert "agent.desktop.intent_approval_required" not in event_types
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.output.completed" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_browser_mutations_require_owned_target_before_approval(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-browser-search-result-type.db",
        workspace_dir=tmp_path / "runtime-browser-search-result-type",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    click_calls: list[tuple[str, int]] = []
    type_calls: list[tuple[str, str]] = []
    search_selector = (
        'input[type="search"], input[name="q"], textarea[name="q"], '
        'input[aria-label*="搜索" i], input[placeholder*="搜索" i], '
        'input[aria-label*="search" i], input[placeholder*="search" i]'
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher browser approval should not call model")
        ),
    )

    def fake_browser_click(selector: str, **kwargs: Any) -> dict:
        click_calls.append((selector, int(kwargs.get("click_count") or 1)))
        return {
            "ok": True,
            "action": "browser.click",
            "summary": "Clicked browser selector",
            "data": {
                "selector": selector,
                "label": "Yachiyo result",
                "tag": "A",
            },
        }

    def fake_browser_type_text(selector: str, text: str, **_kwargs: Any) -> dict:
        type_calls.append((selector, text))
        return {
            "ok": True,
            "action": "browser.type_text",
            "summary": "Typed browser text",
            "data": {
                "selector": selector,
                "tag": "INPUT",
                "length": len(text),
                "content_verified": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.browser.click", fake_browser_click)
    monkeypatch.setattr("apps.shell.agent.tools.browser.type_text", fake_browser_type_text)
    bridge = ChatBridge(runtime)
    try:
        cases = (
            (
                "click the first search result",
                "browser.click",
                {"selector": "search-result=1", "click_count": 1},
            ),
            (
                "type hello in current webpage search field",
                "browser.type_text",
                {"selector": search_selector, "text": "hello"},
            ),
        )
        for prompt, tool_name, input_preview in cases:
            result = bridge.send_quick_message(
                prompt,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "bubble",
                    "launcher_surface": "quick_message",
                },
            )
            task_id = result["task_id"]
            waiting_task = result["agent_task"]
            link = service.get_task_run_link(task_id)
            waiting_run = service.get_run(link["run_id"])

            assert result["ok"] is True
            assert waiting_task["status"] == "failed"
            assert waiting_task["summary"] == _BROWSER_TARGET_HANDOFF_SUMMARY
            assert waiting_task["needs_user_action"] is False
            assert waiting_task["pending_approvals"] == []
            assert waiting_task["tool_calls"][-1]["tool_name"] == tool_name
            assert waiting_task["tool_calls"][-1]["input_preview"] == input_preview
            assert waiting_run["status"] == "failed"
            assert waiting_run["pending_approval"] == {}
            event_types = [
                event["event_type"]
                for event in service.list_run_events(waiting_run["run_id"], include_internal=True)["events"]
            ]

            assert "agent.desktop.intent_approval_required" not in event_types
            assert "agent.desktop.intent_unverified" in event_types
            assert "agent.desktop.intent_completed" not in event_types
            assert "model.output.completed" not in event_types

        assert click_calls == []
        assert type_calls == []
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_app_quit(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-app-quit-approval.db",
        workspace_dir=tmp_path / "runtime-app-quit-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    quit_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher app quit approval should not call model")
        ),
    )

    native_quit = desktop_tools.app_quit
    script_calls = []

    def fake_script(script, args, **kwargs):
        script_calls.append((script, args))
        return {"ok": True, "stdout": "quit|Slack" if len(script_calls) == 1 else "not_running"}

    def fake_app_quit(app_name: str) -> dict:
        quit_calls.append(app_name)
        return native_quit(app_name)

    monkeypatch.setattr(desktop_tools, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_tools, "_run_osascript", fake_script)
    monkeypatch.setattr(desktop_tools, "list_apps", lambda query="", limit=20: {
        "ok": True, "action": "desktop.list_apps", "data": {
            "query": query, "apps": [{"name": "Slack", "path": "/Applications/Slack.app"}],
        },
    })

    def fake_running_apps() -> dict:
        return {
            "ok": True,
            "action": "desktop.running_apps",
            "summary": "Running apps: Finder",
            "data": {
                "apps": [{"name": "Finder", "pid": 101, "frontmost": True}],
                "frontmost": "Finder",
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_quit", fake_app_quit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", fake_running_apps)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "退出 Slack",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert quit_calls == []
        assert script_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == "app.quit"
        assert "退出应用 Slack" in waiting_task["pending_approvals"][0]["policy_reason"]
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "app.quit"
        assert waiting_run["pending_approval"]["input_preview"] == {"app_name": "Slack"}

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert quit_calls == ["Slack"]
        assert len(script_calls) == 2
        assert all(args == ["Slack"] for script, args in script_calls)
        assert approved.status == "completed", approved.summary
        assert approved.summary == "已退出 Slack。"
        assert approved.needs_user_action is False
        assert approved.pending_approvals == []
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.output.completed" in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_terminal_run_intent(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-terminal-run-approval.db",
        workspace_dir=tmp_path / "runtime-terminal-run-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    terminal_calls: list[tuple[str, bool]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher terminal run intent should not call model")
        ),
    )

    def fake_run_terminal_command(command: str, **kwargs: Any) -> dict:
        terminal_calls.append((command, bool(kwargs.get("shell", False))))
        return {
            "ok": True,
            "returncode": 0,
            "timed_out": False,
            "shell": bool(kwargs.get("shell", False)),
            "stdout": "Desktop\n",
            "stderr": "",
        }

    monkeypatch.setattr("apps.shell.agent.tools.broker.run_terminal_command", fake_run_terminal_command)
    bridge = ChatBridge(runtime)
    try:
        result = bridge.send_quick_message(
            "打开终端运行 ls",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
            },
        )
        task_id = result["task_id"]
        waiting_task = result["agent_task"]
        link = service.get_task_run_link(task_id)
        waiting_run = service.get_run(link["run_id"])

        assert result["ok"] is True
        assert terminal_calls == []
        assert waiting_task["status"] == "waiting_approval"
        assert waiting_task["needs_user_action"] is True
        assert waiting_task["pending_approvals"][0]["tool_name"] == "terminal.run"
        assert waiting_task["pending_approvals"][0]["input_preview"] == {
            "command": "ls",
        }
        assert waiting_run["status"] == "approval_required"
        assert waiting_run["pending_approval"]["tool"] == "terminal.run"
        assert waiting_run["pending_approval"]["input_preview"] == {
            "command": "ls",
        }

        approved = YachiyoAgentService(LegacyRuntimePort(service)).approve(
            task_id,
            _approval_decision_for_run(waiting_run),
        )
        run = service.get_run(link["run_id"])
        event_types = [
            event["event_type"]
            for event in service.list_run_events(run["run_id"], include_internal=True)["events"]
        ]

        assert terminal_calls == [("ls", False)]
        assert approved.status == "completed"
        assert approved.summary == "已运行命令：ls。\n输出：Desktop"
        assert approved.needs_user_action is False
        assert approved.pending_approvals == []
        assert run["status"] == "completed"
        assert run["pending_approval"] == {}
        assert "agent.desktop.intent_approval_required" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_requires_approval_for_foreground_input_tools(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-foreground-approval.db",
        workspace_dir=tmp_path / "runtime-foreground-approval",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    runtime.agent_runtime_service = service
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("launcher foreground approval should not call model")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_type_text",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("type_text should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_click",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("click should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.click_ui_element",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("click_ui_element should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.type_into_ui_element",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("type_into_ui_element should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_submit_foreground",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("submit_foreground should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_close_window",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("close_window should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_quit_app",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("quit_app should wait for approval")
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_hotkey",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("hotkey should wait for approval")
        ),
    )
    focused_apps: list[str] = []

    def fake_app_focus(app_name: str) -> dict:
        focused_apps.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name, "focus_verified": True},
        }

    def fake_active_window() -> dict:
        return _fake_active_window_result(
            focused_apps[-1] if focused_apps else "Google Chrome"
        )

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.active_window",
        fake_active_window,
    )

    def fake_ui_elements(
        role_filter: str = "",
        limit: int = 80,
        app_name: str = "",
    ) -> dict:
        result = _fake_ui_elements_result(
            app_name or "Google Chrome",
            "Current Page",
        )
        result["data"]["elements"] = [
            {
                "role": "AXTextField",
                "name": "Search",
                "enabled": True,
                "center": {"x": 320, "y": 120},
            },
            {
                "role": "AXButton",
                "name": "Login",
                "enabled": True,
                "center": {"x": 640, "y": 120},
            },
            {
                "role": "AXButton",
                "name": "登录",
                "enabled": True,
                "center": {"x": 640, "y": 180},
            },
        ]
        result["data"]["count"] = 3
        return result

    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    bridge = ChatBridge(runtime)
    try:
        cases = [
            ("关闭当前窗口", "desktop.close_window", {}),
            ("把当前窗口关了", "desktop.close_window", {}),
            ("当前窗口关一下", "desktop.close_window", {}),
            ("退出当前应用", "desktop.quit_app", {}),
            ("关掉这个应用", "desktop.quit_app", {}),
            ("close the current app", "desktop.quit_app", {}),
            (
                "click the search field",
                "desktop.click_ui_element",
                {"target": "search", "role_filter": "text", "limit": 80, "click_count": 1},
            ),
            (
                "点击可见的登录按钮",
                "desktop.click_ui_element",
                {"target": "登录", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "点一下登录",
                "desktop.click_ui_element",
                {"target": "登录", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "当前界面点击登录",
                "desktop.click_ui_element",
                {"target": "登录", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "前台点登录",
                "desktop.click_ui_element",
                {"target": "登录", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "current window click Login",
                "desktop.click_ui_element",
                {"target": "Login", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "Can you click the login button?",
                "desktop.click_ui_element",
                {"target": "login", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "click login",
                "desktop.click_ui_element",
                {"target": "login", "role_filter": "button", "limit": 80, "click_count": 1},
            ),
            (
                "Can you type hello into the search field?",
                "desktop.type_into_ui_element",
                {"target": "search", "text": "hello", "role_filter": "text", "limit": 80},
            ),
            (
                "open Chrome and press command l",
                "app.open_and_hotkey",
                {"app_name": "Google Chrome", "key": "l", "modifiers": ["command"]},
            ),
            (
                "Could you open Chrome and press Command L?",
                "app.open_and_hotkey",
                {"app_name": "Google Chrome", "key": "l", "modifiers": ["command"]},
            ),
            ("send current message", "desktop.submit_foreground", {"action": "send"}),
            ("发送当前内容", "desktop.submit_foreground", {"action": "send"}),
            ("当前输入框发送", "desktop.submit_foreground", {"action": "send"}),
            ("前台发送", "desktop.submit_foreground", {"action": "send"}),
            ("发送前台内容", "desktop.submit_foreground", {"action": "send"}),
            ("按回车提交", "desktop.submit_foreground", {"action": "submit"}),
            ("当前输入框提交", "desktop.submit_foreground", {"action": "submit"}),
            ("前台提交", "desktop.submit_foreground", {"action": "submit"}),
            ("微信按回车发送", "desktop.submit_foreground", {"action": "send"}),
            ("Chrome press return to send", "desktop.submit_foreground", {"action": "send"}),
            ("按 Command+L", "desktop.hotkey", {"key": "l", "modifiers": ["command"]}),
            ("Can you press Command L?", "desktop.hotkey", {"key": "l", "modifiers": ["command"]}),
            ("敲一下回车", "desktop.hotkey", {"key": "return", "modifiers": []}),
            ("hit enter", "desktop.hotkey", {"key": "return", "modifiers": []}),
            ("tap the return key", "desktop.hotkey", {"key": "return", "modifiers": []}),
        ]
        for text, tool_name, input_preview in cases:
            result = bridge.send_quick_message(
                text,
                metadata={
                    "source": "launcher",
                    "launcher_mode": "bubble",
                    "launcher_surface": "quick_message",
                    "allow_user_foreground_takeover": True,
                },
            )
            task_id = result["task_id"]
            waiting_task = result["agent_task"]
            link = service.get_task_run_link(task_id)
            waiting_run = service.get_run(link["run_id"])

            assert result["ok"] is True
            assert waiting_task["status"] == "waiting_approval"
            assert waiting_task["needs_user_action"] is True
            assert waiting_task["pending_approvals"][0]["tool_name"] == tool_name
            assert waiting_run["status"] == "approval_required"
            assert waiting_run["pending_approval"]["tool"] == tool_name
            assert waiting_run["pending_approval"]["input_preview"] == input_preview

        app_scoped_result = bridge.send_quick_message(
            "微信关闭窗口",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "allow_user_foreground_takeover": True,
            },
        )
        app_scoped_task = app_scoped_result["agent_task"]
        app_scoped_link = service.get_task_run_link(app_scoped_result["task_id"])
        app_scoped_run = service.get_run(app_scoped_link["run_id"])

        assert app_scoped_result["ok"] is True
        assert app_scoped_task["status"] == "waiting_approval"
        assert app_scoped_task["needs_user_action"] is True
        focus_call = _agent_task_tool_call(app_scoped_task, "app.focus")
        assert focus_call["input_preview"] == {"app_name": "WeChat"}
        assert focus_call["status"] == "completed"
        assert app_scoped_task["tool_calls"][-1]["tool_name"] == "desktop.close_window"
        assert app_scoped_task["tool_calls"][-1]["status"] == "waiting_approval"
        assert app_scoped_task["pending_approvals"][0]["tool_name"] == "desktop.close_window"
        assert app_scoped_run["status"] == "approval_required"
        assert app_scoped_run["pending_approval"]["tool"] == "desktop.close_window"
        assert app_scoped_run["pending_approval"]["input_preview"] == {}
    finally:
        service.close()
        store.close()


def test_chat_bridge_quick_message_waits_briefly_for_daily_desktop_snapshot(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakeDelayedDesktopIntentRuntimeService()
    monkeypatch.setattr(chat_bridge_mod, "_QUICK_DESKTOP_SNAPSHOT_ATTEMPTS", 2)
    monkeypatch.setattr(chat_bridge_mod, "_QUICK_DESKTOP_SNAPSHOT_DELAY_SECONDS", 0)
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-delayed-browser",
            "task_id": "task-delayed-browser",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("打开 GitHub")

        assert result["ok"] is True
        assert result["task_id"] == "task-delayed-browser"
        assert result["agent_task"]["task_id"] == "task-delayed-browser"
        assert result["agent_task"]["status"] == "running"
        assert result["agent_task"]["current_step"] == "已回退执行 · 打开网页 · 系统浏览器"
        assert result["agent_task"]["recent_events"][0]["event_type"] == "agent.desktop.intent_completed"
        assert result["agent_task"]["tool_calls"][0]["tool_name"] == "browser.open_url"
        assert runtime.agent_runtime_service.calls == [
            ("get_task_run_link", "task-delayed-browser"),
            ("get_task_run_link", "task-delayed-browser"),
            ("get_task_run_link", "task-delayed-browser"),
        ]
    finally:
        store.close()


def test_chat_bridge_quick_message_returns_planned_web_task_before_run_link(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-pending-browser",
            "task_id": "task-pending-browser",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("打开 GitHub")

        assert result["ok"] is True
        assert result["task_id"] == "task-pending-browser"
        assert result["status"] == "pending"
        assert result["echo"] == "打开 GitHub"
        assert result["agent_task"]["task_id"] == "task-pending-browser"
        assert result["agent_task"]["conversation_id"] == "session-current"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 打开网页"
        assert result["agent_task"]["progress_text"] == "准备执行 · 打开网页"
        assert result["agent_task"]["open_in_studio_url"] is None
        assert result["agent_task"]["runtime_execution_envelope"]["intent_kind"] == "web_research"
        assert [
            request["tool_name"]
            for request in result["agent_task"]["runtime_execution_envelope"]["requests"]
        ] == ["browser.open_url"]
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="web_research",
        )
        planned_event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="browser.open_url",
        )
        assert planned_event["detail"] == "browser.open_url"
        assert planned_event["payload"]["input_preview"] == {"url": "https://github.com"}
        assert planned_event["payload"]["status"] == "planned"
        assert planned_event["payload"]["tool"] == "browser.open_url"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(planned_event["payload"])
        assert set(runtime.agent_runtime_service.calls) == {
            ("get_task_run_link", "task-pending-browser")
        }
        assert len(runtime.agent_runtime_service.calls) >= chat_bridge_mod._QUICK_DESKTOP_SNAPSHOT_ATTEMPTS
    finally:
        store.close()


def test_chat_bridge_quick_message_plans_screen_capture_for_lightweight_entrypoints(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-screen",
            "task_id": "task-screen",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("帮我看看现在屏幕")

        assert result["ok"] is True
        assert result["task_id"] == "task-screen"
        assert result["agent_task"]["task_id"] == "task-screen"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 截取屏幕"
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="desktop_operation",
        )
        planned_event = next(
            event
            for event in reversed(result["agent_task"]["recent_events"])
            if event["event_type"] == "agent.desktop.intent_planned"
            and event["detail"] == "screen.capture"
        )
        assert planned_event["payload"] == {
            "input_preview": {"reason": "user asked to capture the screen"},
            "status": "planned",
            "tool": "screen.capture",
        }
    finally:
        store.close()


def test_chat_bridge_quick_message_uses_runtime_planner_for_launcher_modes(tmp_path):
    for mode in ("bubble", "live2d"):
        store = ChatStore(db_path=str(tmp_path / f"chat-{mode}.db"))
        runtime = _runtime_with_chat_store(store)
        runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
        bridge = ChatBridge(runtime)
        bridge._chat_api = SimpleNamespace(
            send_message=lambda text, **_kwargs: {
                "ok": True,
                "message_id": f"message-screen-{mode}",
                "task_id": f"task-screen-{mode}",
                "status": "pending",
                "echo": text,
            }
        )
        try:
            result = bridge.send_quick_message(
                "帮我看看现在屏幕",
                metadata={
                    "source": "launcher",
                    "launcher_mode": mode,
                    "launcher_surface": "quick_message",
                },
            )

            assert result["ok"] is True
            assert result["agent_task"]["task_id"] == f"task-screen-{mode}"
            _assert_planner_trace_prefix(
                result["agent_task"],
                intent_kind="desktop_operation",
            )
            selection_event = _agent_task_event(
                result["agent_task"],
                "agent.plan.selection",
            )
            assert selection_event["payload"]["selection_source"] == "runtime_planner"
            planned_event = _agent_task_event(
                result["agent_task"],
                "agent.desktop.intent_planned",
                detail="screen.capture",
            )
            assert planned_event["detail"] == "screen.capture"
            assert planned_event["payload"]["input_preview"] == {
                "reason": "user asked to capture the screen"
            }
            assert planned_event["payload"]["status"] == "planned"
            assert planned_event["payload"]["tool"] == "screen.capture"
            assert {
                "source",
                "planning_reason",
                "capability_id",
                "runtime_stage",
                "task_todo",
            }.isdisjoint(planned_event["payload"])
        finally:
            store.close()


def test_chat_bridge_quick_message_plans_app_observe_for_lightweight_entrypoints(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-app-observe",
            "task_id": "task-app-observe",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("看一下 Chrome 当前界面")

        assert result["ok"] is True
        assert result["task_id"] == "task-app-observe"
        assert result["agent_task"]["task_id"] == "task-app-observe"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 发现已安装应用"
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="desktop_operation",
        )
        planned_event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="desktop.list_apps",
        )
        assert planned_event["detail"] == "desktop.list_apps"
        assert planned_event["payload"]["input_preview"] == {
            "query": "Chrome",
            "limit": 20,
        }
        assert planned_event["payload"]["status"] == "planned"
        assert planned_event["payload"]["tool"] == "desktop.list_apps"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(planned_event["payload"])
    finally:
        store.close()


def test_chat_bridge_quick_message_plans_app_open_visual_followup_for_lightweight_entrypoints(
    tmp_path,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-app-open-observe",
            "task_id": "task-app-open-observe",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("打开微信看看有没有新消息")

        assert result["ok"] is True
        assert result["task_id"] == "task-app-open-observe"
        assert result["agent_task"]["task_id"] == "task-app-open-observe"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 发现已安装应用"
        _assert_planner_trace_prefix(
            result["agent_task"],
            intent_kind="desktop_operation",
        )
        planned_event = _agent_task_event(
            result["agent_task"],
            "agent.desktop.intent_planned",
            detail="desktop.list_apps",
        )
        assert planned_event["detail"] == "desktop.list_apps"
        assert planned_event["payload"]["input_preview"] == {
            "query": "WeChat",
            "limit": 20,
        }
        assert planned_event["payload"]["status"] == "planned"
        assert planned_event["payload"]["tool"] == "desktop.list_apps"
        assert {
            "source",
            "planning_reason",
            "capability_id",
            "runtime_stage",
            "task_todo",
        }.isdisjoint(planned_event["payload"])
    finally:
        store.close()


def test_chat_bridge_quick_message_plans_note_creation_for_lightweight_entrypoints(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-note",
            "task_id": "task-note",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("帮我记下 hello")

        assert result["ok"] is True
        assert result["task_id"] == "task-note"
        assert result["agent_task"]["task_id"] == "task-note"
        assert result["agent_task"]["status"] == "queued"
        assert result["agent_task"]["current_step"] == "准备执行 · 创建备忘录"
        planned_event = next(
            event
            for event in result["agent_task"]["recent_events"]
            if event["event_type"] == "agent.desktop.intent_planned"
        )
        assert planned_event["detail"] == "notes.create"
        assert planned_event["payload"] == {
            "input_preview": {"body": "hello"},
            "status": "planned",
            "tool": "notes.create",
        }
    finally:
        store.close()


def test_chat_bridge_quick_message_keeps_plain_chat_without_planned_agent_task(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    runtime.agent_runtime_service = _FakePendingDesktopIntentRuntimeService()
    bridge = ChatBridge(runtime)
    bridge._chat_api = SimpleNamespace(
        send_message=lambda text, **_kwargs: {
            "ok": True,
            "message_id": "message-plain",
            "task_id": "task-plain",
            "status": "pending",
            "echo": text,
        }
    )
    try:
        result = bridge.send_quick_message("今天状态怎么样？")

        assert result == {
            "ok": True,
            "message_id": "message-plain",
            "task_id": "task-plain",
            "status": "pending",
            "echo": "今天状态怎么样？",
        }
        assert runtime.agent_runtime_service.calls == [("get_task_run_link", "task-plain")]
    finally:
        store.close()


def test_chat_bridge_quick_message_forwards_entrypoint_metadata(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    bridge = ChatBridge(runtime)
    received: dict[str, object] = {}

    def send_message(text, **kwargs):
        received["text"] = text
        received["metadata"] = kwargs.get("metadata")
        return {
            "ok": True,
            "message_id": "message-launcher",
            "task_id": "",
            "status": "pending",
        }

    bridge._chat_api = SimpleNamespace(send_message=send_message)
    try:
        result = bridge.send_quick_message(
            "打开 Cursor",
            metadata={
                "source": "launcher",
                "launcher_mode": "bubble",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
            },
        )

        assert result["ok"] is True
        assert received["text"] == "打开 Cursor"
        received_metadata = received["metadata"]
        assert isinstance(received_metadata, dict)
        assert {
            key: received_metadata[key]
            for key in ("source", "launcher_mode", "launcher_surface", "runnable_kind")
        } == {
            "source": "launcher",
            "launcher_mode": "bubble",
            "launcher_surface": "quick_message",
            "runnable_kind": "main",
        }
        assert received_metadata["desktop_execution_policy"]["mode"] == "preview_input"
    finally:
        store.close()


def test_chat_bridge_quick_message_forwards_browser_followup_planning_context(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    runtime = _runtime_with_chat_store(store)
    bridge = ChatBridge(runtime)
    received: dict[str, object] = {}

    def get_messages(**_kwargs):
        return {
            "ok": True,
            "messages": [
                {"role": "user", "content": "打开 https://github.com"},
                {"role": "assistant", "content": "已打开 https://github.com。"},
            ],
        }

    def send_message(text, **kwargs):
        received["text"] = text
        received["metadata"] = kwargs.get("metadata")
        return {
            "ok": True,
            "message_id": "message-launcher",
            "task_id": "",
            "status": "pending",
        }

    bridge._chat_api = SimpleNamespace(
        get_messages=get_messages,
        send_message=send_message,
    )
    try:
        result = bridge.send_quick_message(
            "点击登录",
            metadata={
                "source": "launcher",
                "launcher_mode": "live2d",
                "launcher_surface": "quick_message",
                "runnable_kind": "main",
            },
        )

        assert result["ok"] is True
        assert received["text"] == "点击登录"
        received_metadata = received["metadata"]
        assert isinstance(received_metadata, dict)
        assert {
            key: received_metadata[key]
            for key in (
                "source",
                "launcher_mode",
                "launcher_surface",
                "runnable_kind",
                "entrypoint_planning_context",
            )
        } == {
            "source": "launcher",
            "launcher_mode": "live2d",
            "launcher_surface": "quick_message",
            "runnable_kind": "main",
            "entrypoint_planning_context": "当前浏览器页面 点击登录",
        }
        assert received_metadata["desktop_execution_policy"]["mode"] == "preview_input"
    finally:
        store.close()


def test_session_summary_uses_processing_and_failed_statuses():
    assert chat_bridge_mod._session_summary([
        {
            "role": "user",
            "content": "生成最终验收清单",
            "status": "processing",
            "created_at": "2026-06-10T00:00:00+00:00",
        }
    ]) == "处理中：生成最终验收清单"
    assert chat_bridge_mod._session_summary([
        {
            "role": "user",
            "content": "运行发布验证",
            "status": "completed",
            "created_at": "2026-06-10T00:00:00+00:00",
        },
        {
            "role": "assistant",
            "content": "provider error: token redacted",
            "status": "failed",
            "created_at": "2026-06-10T00:01:00+00:00",
        },
    ]) == "失败：provider error: token redacted"


class _FakeAgentRuntimeService:
    def get_run(self, run_id: str):
        return {
            "run_id": run_id,
            "user_goal": "Launcher Agent Task",
            "status": "approval_required",
            "pending_approval": {
                "approval_id": "approval-1",
                "tool": "terminal.run",
                "input_preview": {"command": "pytest"},
            },
            "timeline": [
                {
                    "event": "agent.tool.approval_required",
                    "detail": "terminal.run",
                }
            ],
        }


class _FakeDesktopIntentRuntimeService:
    def get_task_run_link(self, task_id: str):
        assert task_id == "task-browser"
        return {
            "task_id": task_id,
            "run_id": "run-browser",
            "session_id": "session-current",
        }

    def get_run(self, run_id: str):
        assert run_id == "run-browser"
        return {
            "run_id": run_id,
            "kind": "main_chat_run",
            "user_goal": "打开 GitHub",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.desktop.intent_completed",
                    "detail": "browser.open_url",
                    "payload": {
                        "tool": "browser.open_url",
                        "source": "daily_desktop_intent",
                        "result": {
                            "ok": True,
                            "fallback_used": True,
                            "fallback": "system_browser",
                            "data": {"url": "https://github.com"},
                        },
                    },
                }
            ],
        }


class _FakeDelayedDesktopIntentRuntimeService:
    def __init__(self) -> None:
        self.calls = []

    def get_task_run_link(self, task_id: str):
        self.calls.append(("get_task_run_link", task_id))
        if len(self.calls) == 1:
            raise KeyError(task_id)
        return {
            "task_id": task_id,
            "run_id": "run-delayed-browser",
            "session_id": "session-current",
        }

    def get_run(self, run_id: str):
        assert run_id == "run-delayed-browser"
        return {
            "run_id": run_id,
            "kind": "main_chat_run",
            "user_goal": "打开 GitHub",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.desktop.intent_completed",
                    "detail": "browser.open_url",
                    "payload": {
                        "tool": "browser.open_url",
                        "source": "daily_desktop_intent",
                        "result": {
                            "ok": True,
                            "fallback_used": True,
                            "fallback": "system_browser",
                            "data": {"url": "https://github.com"},
                        },
                    },
                }
            ],
        }


class _FakePendingDesktopIntentRuntimeService:
    def __init__(self) -> None:
        self.calls = []

    def get_task_run_link(self, task_id: str):
        self.calls.append(("get_task_run_link", task_id))
        raise KeyError(task_id)


def _with_native_focused_ui_element(result: dict) -> dict:
    data = result.get("data") or {}
    focused = [element for element in data.get("elements", [])
               if element.get("focused") is True
               and element.get("role") in {"AXTextField", "AXTextArea", "AXComboBox"}]
    if len(focused) == 1:
        data["focused_element"] = {key: value for key, value in focused[0].items()
            if key in {"role", "name", "identifier", "description", "value", "focused", "editable", "enabled"}}
    return result
