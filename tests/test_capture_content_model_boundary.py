"""Content goals need a model; captured context remains usable by that model."""

import base64
import json
from copy import deepcopy
from pathlib import Path

import pytest

from apps.shell.agent.runtime.model_intent_planning import (
    capture_only_content_read_requires_model,
    planner_selection_needs_model_assistance,
)
from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.model_profiles import ModelProfileService
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection
from apps.shell.yachiyo_agent.runtime_execution import runtime_execution_envelope_payload
from tests.test_chat_api import _make_agent_runtime_service, _make_api


@pytest.mark.parametrize(
    "goal",
    [
        "打开微信读一下当前聊天",
        "打开 Slack 看消息",
        "open Discord and read messages",
        "打开活动监视器看看 CPU",
        "打开系统活动监视器看看 CPU",
    ],
)
def test_capture_only_content_goals_require_model_and_preserve_capture_followup(goal):
    selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
    assert planner_selection_needs_model_assistance(selection, goal)
    payload = runtime_execution_envelope_payload(
        selection.decision,
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        full_plan=True,
    )
    captures = [r for r in payload["requests"] if r["tool_name"] == "screen.capture"]
    assert len(captures) == 1
    assert captures[0]["continue_to_model"] is True


@pytest.mark.parametrize(
    "goal",
    [
        "打开微信截图，正文是“读消息”",
        '打开活动监视器截图，标题是"看 CPU"',
        "不要读消息，只打开微信截图",
        "不用看 CPU，只打开活动监视器截图",
        "open Slack and capture it; do not read messages",
        "打开微信然后截图",
        "打开 Slack 截个图",
        "打开活动监视器截图",
    ],
)
def test_quoted_or_negated_reading_does_not_add_a_model_content_action(goal):
    assert not capture_only_content_read_requires_model(
        goal,
        [{"tool": "app.open"}, {"tool": "screen.capture"}],
    )


def test_saved_profile_content_goal_uses_capture_then_actual_ax_text_then_model_reply(
    tmp_path,
    monkeypatch,
):
    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    profiles = ModelProfileService(
        db_path=tmp_path / "profiles.db",
        workspace_dir=tmp_path / "profiles",
        credential_store=MemoryCredentialStore(),
    )
    runtime.agent_runtime_service = service
    monkeypatch.setattr("apps.shell.model_profiles.openai_compatible_chat", lambda *a, **k: "OK")
    profile = profiles.create_profile(
        {
            "name": "Isolated content model",
            "capability": "chat",
            "base_url": "https://model.example.test/v1",
            "model": "fixture-model",
            "api_key": "fixture-test-key",
        }
    )
    assert profiles.test_profile(profile["profile_id"])["ok"] is True
    profiles.set_defaults({"chat": profile["profile_id"]})
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: profiles)
    goal = "打开微信读一下当前聊天"
    actual_content = "明天下午三点在图书馆碰面。"
    calls = []
    model_messages = []
    opened = False
    original_open = desktop.app_open
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop,
        "_run_open_app",
        lambda name: type(
            "R",
            (),
            {
                "returncode": 0,
                "stdout": "",
                "stderr": "",
            },
        )(),
    )
    monkeypatch.setattr(
        desktop,
        "_run_osascript",
        lambda script, args, **k: {
            "ok": True,
            "stdout": "running",
        },
    )

    def open_app(name):
        nonlocal opened
        opened = True
        calls.append("app.open")
        return original_open(name)

    def capture(path):
        assert opened
        calls.append("screen.capture")
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY"
                "42YAAAAASUVORK5CYII="
            )
        )
        return {
            "ok": True,
            "action": "screen.capture",
            "data": {
                "path": str(path),
                "size_bytes": path.stat().st_size,
                "width": 1,
                "height": 1,
                "mime_type": "image/png",
            },
        }

    def observe(**kwargs):
        assert calls[-1] == "screen.capture"
        calls.append("desktop.ui_elements")
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
                "elements": [
                    {
                        "role": "AXStaticText",
                        "name": actual_content,
                        "value": actual_content,
                        "depth": 1,
                    }
                ],
            },
        }

    def model(_url, _model, _key, messages, **kwargs):
        assert _key == "fixture-test-key"
        model_messages.append(deepcopy(messages))
        names = {x.get("function", {}).get("name") for x in kwargs.get("tools", [])}
        if "runtime_propose_task_intent" in names:
            assert calls == []
            name, arguments = (
                "runtime_propose_task_intent",
                {
                    "intent_kind": "desktop_operation",
                    "planning_goal": goal,
                    "action_evidence": "打开",
                },
            )
        elif len(model_messages) == 2:
            assert calls == ["app.open", "screen.capture"]
            assert "screenshots/current-screen.png" in json.dumps(messages)
            name, arguments = "desktop.ui_elements", {"app_name": "WeChat", "role_filter": "text"}
        else:
            assert actual_content in json.dumps(messages, ensure_ascii=False)
            return {"role": "assistant", "content": f"当前聊天约定：{actual_content}"}
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"model-call-{len(model_messages)}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=False),
                    },
                }
            ],
        }

    monkeypatch.setattr(desktop, "app_open", open_app)
    monkeypatch.setattr(
        desktop,
        "app_status",
        lambda name: {
            "ok": True,
            "action": "app.status",
            "data": {"app_name": name, "running": opened},
        },
    )
    monkeypatch.setattr(
        desktop,
        "list_apps",
        lambda query="", limit=20: {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {
                "query": query,
                "apps": [{"name": "WeChat"}],
                "best_match": {"name": "WeChat"},
            },
        },
    )
    monkeypatch.setattr(
        desktop,
        "running_apps",
        lambda **k: {
            "ok": True,
            "action": "desktop.running_apps",
            "data": {"apps": [], "count": 0},
        },
    )
    monkeypatch.setattr(desktop, "screen_capture", capture)
    monkeypatch.setattr(desktop, "ui_elements", observe)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", model)
    try:
        selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
        payload = runtime_execution_envelope_payload(
            selection.decision,
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
            full_plan=True,
            metadata={"allow_user_foreground_takeover": True},
        )
        started = service.start_main_chat_run(
            task_id="content-task",
            session_id="content-session",
            user_goal=goal,
            runtime_execution_envelope=payload,
        )
        run = service.execute_main_chat_model_loop(
            started["run_id"],
            [{"role": "user", "content": goal}],
            runtime_execution_envelope=payload,
            runtime_execution_metadata={"allow_user_foreground_takeover": True},
            tool_policy={"allowed_tools": list(DAILY_DESKTOP_TOOL_NAMES)},
        )
        # The TaskRunner commits the final projection after the model loop returns.
        assert run["status"] == "running", run.get("result")
        run = service.complete_main_chat_run(run["run_id"], run["result"])
        assert run["status"] == "completed", run.get("result")
        assert actual_content in run["result"]
        assert calls == ["app.open", "screen.capture", "desktop.ui_elements"]
        assert len(model_messages) == 3
        assert run["user_goal"] == goal
        assert run["pending_approval"] == {}
    finally:
        service.close()
        store.close()
        profiles.close()
