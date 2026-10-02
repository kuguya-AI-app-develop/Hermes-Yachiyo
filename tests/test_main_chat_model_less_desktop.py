"""Real ChatAPI partial-result projection after a bounded native dispatch."""

from types import SimpleNamespace

import pytest

from packages.protocol.enums import TaskStatus
from apps.core.chat_session import MessageStatus
from tests.test_chat_api import _make_api, _make_agent_runtime_service, _send_foreground_message


@pytest.mark.parametrize("mode,action,label", [
    ("打开", "copy", "复制选中内容"),
    ("切到", "paste", "粘贴"),
])
def test_main_chat_without_profile_keeps_actual_dispatch_failed_and_unverified(
    tmp_path, monkeypatch, mode, action, label,
):
    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    runtime.agent_runtime_service = service
    calls = []
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: SimpleNamespace(
        get_defaults=lambda: {"chat": ""},
    ))
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *a, **kw: (
        pytest.fail("A dispatched native action must not retry via a missing model")
    ))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", lambda query="", limit=20: {
        "ok": True, "action": "desktop.list_apps", "data": {
            "query": query, "normalized_query": query.casefold(),
            "apps": [{"name": "Google Chrome", "path": "/Applications/Google Chrome.app"}],
            "best_match": {"name": "Google Chrome", "path": "/Applications/Google Chrome.app"},
            "count": 1, "total_count": 1, "truncated": False,
            "resolution": {"requested_app_name": query, "resolved_app_name": "Google Chrome",
                           "resolved_app_path": "/Applications/Google Chrome.app",
                           "app_resolution": "installed_app_bundle", "app_resolution_confidence": "high",
                           "app_resolution_source": "desktop.list_apps"},
        },
    })
    for name in ("app_open", "app_focus"):
        monkeypatch.setattr(f"apps.shell.agent.tools.desktop.{name}", lambda app_name, _name=name: (
            calls.append((_name, app_name)) or {
                "ok": True, "action": "app.open" if _name == "app_open" else "app.focus",
                "data": {"app_name": app_name, "launch_verified": True},
            }
        ))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_shortcut", lambda action: (
        calls.append(("shortcut", action)) or {
            "ok": True, "action": "desktop.safe_shortcut", "data": {"shortcut_action": action},
        }
    ))
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: {
        "ok": True, "action": "desktop.active_window",
        "data": {"app_name": "Google Chrome", "active_app_name": "Google Chrome"},
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", lambda **kwargs: {
        "ok": True, "action": "desktop.ui_elements",
        "data": {"app_name": "Google Chrome", "elements": []},
    })
    try:
        result = _send_foreground_message(api, f"{mode}Google Chrome并{label}")
        run = service.get_run(result["run_id"])
        task = runtime.state.get_task(result["task_id"])
        assistant = runtime.chat_session.get_assistant_message_for_task(result["task_id"])
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert result["status"] == run["status"] == "failed"
        assert task.status == TaskStatus.FAILED
        assert assistant.status == MessageStatus.FAILED
        assert "Google Chrome" in assistant.content
        assert label in assistant.content
        assert "未能确认" in assistant.content
        assert "Profile" not in assistant.content
        assert run["pending_approval"] == {}
        assert calls.count(("shortcut", action)) == 1
        assert calls.count(("app_open" if mode == "打开" else "app_focus", "Google Chrome")) == 1
        event_types = [e["event_type"] for e in events]
        assert "agent.desktop.intent_unverified" in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "model.request.started" not in event_types
        assert "model.request.failed" not in event_types
        assert not any(e["event_type"] == "agent.tool.call" for e in service.list_run_events(run["run_id"])["events"])
    finally:
        service.close()
        store.close()
