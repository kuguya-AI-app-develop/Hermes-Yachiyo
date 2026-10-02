"""Passive host permissions must remain observable without a desktop backend."""

from types import SimpleNamespace

import pytest

from apps.core.chat_session import ChatSession
from apps.core.chat_store import ChatStore
from apps.core.state import AppState
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.chat_api import ChatAPI
from apps.shell.chat_bridge import ChatBridge
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.yachiyo_agent import desktop_execution_policy as policy
from apps.shell.yachiyo_agent.policy import desktop_tool_execution_mode_for_input


class _NoModelProfile:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def _metadata():
    return {
        "source": "launcher",
        "launcher_mode": "live2d",
        "launcher_surface": "quick_message",
        "runnable_kind": "main",
        "daily_desktop_intent": True,
        "desktop_permission_recovery": True,
        "recovery_tool": "desktop.permissions",
        "recovery_input": {},
        "recovery_permission_target": "desktop_diagnostic",
        "recovery_risk_level": "low",
    }


def test_passive_permission_route_does_not_probe_or_require_a_desktop_backend(monkeypatch):
    def forbidden_probe(*_args, **_kwargs):
        raise AssertionError("Passive permission diagnosis must not probe a desktop backend")

    monkeypatch.setattr(policy, "sandbox_desktop_provider_status", forbidden_probe)
    metadata = policy.with_daily_entrypoint_desktop_execution_policy(_metadata())
    metadata["desktop_provider_health_probe"] = True
    route = policy.desktop_execution_route_decision(
        "desktop.permissions",
        policy=metadata["desktop_execution_policy"],
        execution_mode=desktop_tool_execution_mode_for_input("desktop.permissions", {}),
        metadata=metadata,
    )
    assert route["can_execute"] is True
    assert route["provider_execution_required"] is False
    assert route["selected_provider_kind"] == "none"
    assert route["selected_provider_id"] == ""
    assert route["requires_user_foreground_session"] is False
    assert route["user_foreground_takeover_risk"] is False


@pytest.mark.parametrize(
    "tool,payload",
    [
        ("app.open", {"app_name": "PixelForge"}),
        ("desktop.safe_click", {"x": 10, "y": 10}),
        ("desktop.safe_key", {"key": "tab"}),
        ("desktop.safe_type_text", {"text": "hello"}),
        ("desktop.active_window", {}),
        ("desktop.read_ui", {}),
    ],
)
def test_permission_diagnosis_exception_does_not_allow_other_desktop_tools(
    monkeypatch, tool, payload
):
    monkeypatch.setattr(
        policy,
        "sandbox_desktop_provider_status",
        lambda *_args, **_kwargs: {
            "available": False,
            "provider_kind": "background_desktop",
            "status": "provider_required",
            "blocking_conditions": ["background_desktop_provider_required"],
        },
    )
    metadata = policy.with_daily_entrypoint_desktop_execution_policy(_metadata())
    route = policy.desktop_execution_route_decision(
        tool,
        policy=metadata["desktop_execution_policy"],
        execution_mode=desktop_tool_execution_mode_for_input(tool, payload),
        metadata=metadata,
    )
    assert route["can_execute"] is False


@pytest.mark.parametrize("entrypoint", ["send_message", "send_quick_message"])
@pytest.mark.parametrize("diagnostic", ["ready", "missing", "error"])
def test_actual_bridge_passive_permission_diagnosis_calls_adapter_and_preserves_failures(
    tmp_path,
    monkeypatch,
    entrypoint,
    diagnostic,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    session = ChatSession(session_id="passive-permission-session")
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
        store=store,
        task_runner=None,
        agent_runtime_service=service,
    )
    calls = []

    def no_model(*_args, **_kwargs):
        raise AssertionError("Passive diagnosis should not call a model")

    def fake_permissions():
        calls.append({"active_verification": False})
        if diagnostic == "error":
            return {
                "ok": False,
                "action": "desktop.permissions",
                "error": "diagnostic_adapter_failed",
                "summary": "Permission diagnostic failed",
                "permission_error": False,
            }
        missing = ["accessibility"] if diagnostic == "missing" else []
        return {
            "ok": True,
            "action": "desktop.permissions",
            "active_verification": False,
            "checked": True,
            "diagnostic_status": "verified",
            "permission_error": bool(missing),
            "permission_targets": missing,
            "missing_permissions": missing,
            "summary": "Desktop permissions ready"
            if not missing
            else "Accessibility permission missing",
            "data": {
                "platform": "macos",
                "ready": not missing,
                "active_verification": False,
                "checked": True,
                "diagnostic_status": "verified",
                "permission_targets": missing,
                "checks": {
                    "accessibility": {"ready": not missing, "checked": True},
                    "screen_recording": {"ready": True, "checked": True},
                    "screen_capture": {"ready": True, "checked": True},
                },
            },
        }

    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service", lambda: _NoModelProfile()
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", no_model)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.permissions", fake_permissions)
    try:
        frontend = ChatAPI(runtime) if entrypoint == "send_message" else ChatBridge(runtime)
        result = getattr(frontend, entrypoint)("检查桌面权限", metadata=_metadata())
        assert calls == [{"active_verification": False}], result
        task = result["agent_task"]
        assert result["run_id"]
        run = service.get_run(result["run_id"])
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        if diagnostic != "error":
            assert not any(event["event_type"].startswith("model.request") for event in events)
        assert [call["tool_name"] for call in task["tool_calls"]] == ["desktop.permissions"]
        assert not run["pending_approval"]
        output = task["tool_calls"][-1]["output_preview"]
        assert output.get("blocked_by") != "provider_required"
        if diagnostic == "ready":
            assert task["status"] == run["status"] == "completed", result
            assert task["needs_user_action"] is False
        elif diagnostic == "missing":
            # Completing a diagnosis is separate from granting permission.
            assert task["status"] == run["status"] == "completed", result
            assert output["permission_error"] is True
            assert "accessibility" in output["missing_permissions"]
            assert output["data"]["ready"] is False
            assert "已就绪" not in task["summary"], task["summary"]
        else:
            assert task["status"] != "completed" and run["status"] == "failed", result
            assert output["error"] == "diagnostic_adapter_failed"
    finally:
        service.close()
        store.close()
