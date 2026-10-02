from __future__ import annotations

from copy import deepcopy
from subprocess import CompletedProcess

import pytest

from apps.shell.agent.runtime.dispatch_semantics import (
    intrinsic_native_postcondition_state,
    intrinsic_native_postcondition_target_matches,
)
from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.broker import ToolBroker


def _fixture(monkeypatch, *, target="蓝牙", mode="good"):
    pane = desktop._system_settings_target(target)
    title = pane[0] if pane else "System Settings"
    dispatches = []
    reads = []

    def dispatch(args, **kwargs):
        dispatches.append(args)
        return CompletedProcess(args, 1 if mode == "dispatch_failed" else 0, "", "")

    def script(script, args=None, **kwargs):
        is_ui = "jsonString" in script
        reads.append("ui" if is_ui else "window")
        app, pid, window = "System Settings", 100, 200
        observed_title = title
        if mode == "wrong_app":
            app = "Slack"
        if mode == "unknown_window":
            window = 0
        if mode == "sidebar_only":
            observed_title = "System Settings"
        if mode == "wrong_pane":
            observed_title = "Wi-Fi"
        if mode == "pid_drift" and len(reads) == 3:
            pid = 101
        if mode == "window_drift" and is_ui:
            window = 201
        if mode == "permission_error":
            return {"ok": False, "stdout": "", "stderr": "not authorized (-1743)"}
        if is_ui:
            stdout = (
                f"META\t{app}\t{pid}\t{observed_title}\t{window}\n"
                "1\tAXButton\t\tGeneral\t\t\ttrue\t0\t0\t100\t100"
            )
            if mode == "empty_ui":
                stdout = f"META\t{app}\t{pid}\t{observed_title}\t{window}"
        else:
            stdout = f"{app}|{pid}|{observed_title}|{window}"
        return {"ok": True, "stdout": stdout}

    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_resolve_installed_app_name", lambda value: value)
    monkeypatch.setattr(desktop.subprocess, "run", dispatch)
    monkeypatch.setattr(desktop, "_run_osascript", script)
    monkeypatch.setattr(desktop, "_appkit_frontmost_app_name", lambda: {})
    return dispatches, reads


@pytest.mark.parametrize(
    "target", ["蓝牙", "Wi-Fi", "系统设置", "辅助功能权限", "屏幕录制权限", "隐私与安全性"]
)
def test_native_settings_dispatch_requires_stable_real_window_and_pane(
    tmp_path, monkeypatch, target
):
    dispatches, reads = _fixture(monkeypatch, target=target)
    result = ToolBroker(workspace_policy={}, artifact_root=tmp_path).system_settings_open(target)
    assert dispatches and reads == ["window", "ui", "window"]
    assert result["ok"] is True
    assert result["postcondition_verified"] is True
    assert (
        intrinsic_native_postcondition_state("system.settings_open", {"target": target}, result)
        == "open"
    )
    assert intrinsic_native_postcondition_target_matches(
        "system.settings_open",
        {"target": target},
        {"kind": "system", "action": "open_settings", "target": target},
    )


@pytest.mark.parametrize(
    "mode",
    [
        "dispatch_failed",
        "wrong_app",
        "unknown_window",
        "sidebar_only",
        "wrong_pane",
        "pid_drift",
        "window_drift",
        "permission_error",
        "empty_ui",
    ],
)
def test_native_settings_unknown_or_foreign_observations_never_complete(
    tmp_path, monkeypatch, mode
):
    _, reads = _fixture(monkeypatch, mode=mode)
    result = ToolBroker(workspace_policy={}, artifact_root=tmp_path).system_settings_open("蓝牙")
    assert result.get("postcondition_verified") is not True
    assert (
        intrinsic_native_postcondition_state("system.settings_open", {"target": "蓝牙"}, result)
        == ""
    )
    if mode == "dispatch_failed":
        assert reads == []


def test_supplied_settings_flags_and_foreign_receipts_do_not_replace_readback(
    tmp_path, monkeypatch
):
    _fixture(monkeypatch, mode="sidebar_only")
    monkeypatch.setattr(
        desktop,
        "system_settings_open",
        lambda target: {
            "ok": True,
            "action": "system.settings_open",
            "postcondition_verified": True,
            "data": {
                "target": target,
                "settings_readback": {
                    "target": target,
                    "pane": "Bluetooth",
                    "app_name": "System Settings",
                    "pid": 100,
                    "window_id": 200,
                    "window_title": "Bluetooth",
                    "ui_title": "Bluetooth",
                },
            },
        },
    )
    result = ToolBroker(workspace_policy={}, artifact_root=tmp_path).system_settings_open("蓝牙")
    assert "settings_readback" not in result["data"]
    assert result.get("postcondition_verified") is not True


@pytest.mark.parametrize(
    "drift", ["target", "window_title", "ui_title", "pid", "window_id", "app_name", "pane"]
)
def test_settings_intrinsic_receipt_is_bound_to_exact_target_and_identity(
    tmp_path, monkeypatch, drift
):
    _fixture(monkeypatch)
    result = ToolBroker(workspace_policy={}, artifact_root=tmp_path).system_settings_open("蓝牙")
    result = deepcopy(result)
    result["data"]["settings_readback"][drift] = 0 if drift in {"pid", "window_id"} else "foreign"
    assert (
        intrinsic_native_postcondition_state("system.settings_open", {"target": "蓝牙"}, result)
        == ""
    )
    assert not intrinsic_native_postcondition_target_matches(
        "system.settings_open",
        {"target": "蓝牙"},
        {"kind": "system", "action": "open_settings", "target": "Wi-Fi"},
    )


@pytest.mark.parametrize("goal", ["打开蓝牙", "打开系统设置", "打开系统设置看看有哪些选项"])
def test_real_launcher_completes_settings_goal_from_native_pane_readback(
    tmp_path, monkeypatch, goal
):
    from tests.test_chat_bridge import _run_launcher_daily_desktop_quick_message

    target = "蓝牙" if goal == "打开蓝牙" else "系统设置"
    dispatches, reads = _fixture(monkeypatch, target=target)
    result, task, run, event_types = _run_launcher_daily_desktop_quick_message(
        tmp_path, monkeypatch, goal
    )
    assert result["ok"] is True
    assert task["status"] == run["status"] == "completed"
    assert "model.request.started" not in event_types
    assert "model.requested" not in event_types
    assert len(dispatches) == 1
    assert reads[:3] == ["window", "ui", "window"]
    if "选项" in goal:
        assert reads == ["window", "ui", "window", "ui"]
        assert task["tool_calls"][-1]["tool_name"] == "desktop.ui_elements"
        assert "General" in task["summary"]
