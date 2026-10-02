"""Opening an app must not finish a chat request that also asks for a capture."""

import base64
from pathlib import Path

import pytest

from apps.shell.agent.runtime.goal_runtime import runtime_goal_contract
from tests.test_chat_bridge import _run_launcher_daily_desktop_quick_message

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR4nGP4DwQACfsD/fteaysAAAAASUVORK5CYII="
)


@pytest.mark.parametrize("goal", ["打开微信然后截个图", "打开微信然后截图"])
@pytest.mark.parametrize("capture_ok", [True, False])
def test_chat_waits_for_the_requested_capture_and_keeps_its_original_goal(
    tmp_path, monkeypatch, goal, capture_ok,
):
    calls = []
    captures = []

    def opened(app_name):
        calls.append(("open", app_name))
        return {
            "ok": True, "action": "app.open", "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_status": "running", "launch_verified": True},
        }

    def captured(target_path):
        calls.append(("capture", str(target_path)))
        if not capture_ok:
            return {"ok": False, "action": "screen.capture", "error": "capture adapter failed"}
        path = Path(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(PNG)
        captures.append(path)
        return {
            "ok": True, "action": "screen.capture", "summary": "Captured current screen",
            "data": {"path": str(path), "mime_type": "image/png", "size_bytes": len(PNG),
                     "width": 1, "height": 1},
        }

    def catalog(query="", limit=20):
        app = {"name": "WeChat", "path": "/Applications/WeChat.app", "match_score": 100}
        return {
            "ok": True, "action": "desktop.list_apps",
            "data": {"query": query, "apps": [app], "best_match": app,
                     "count": 1, "total_count": 1, "truncated": False},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", catalog)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", opened)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.screen_capture", captured)
    _result, task, run, events = _run_launcher_daily_desktop_quick_message(
        tmp_path, monkeypatch, goal,
    )
    assert calls[0] == ("open", "WeChat")
    assert [call[0] for call in calls] == ["open", "capture"]
    assert "model.request.started" not in events
    assert task["status"] == run["status"] == ("completed" if capture_ok else "failed")
    contract = runtime_goal_contract(
        run_id=run["run_id"], original_goal=goal, runtime_execution_envelope=None,
        runtime_execution_metadata=None, messages=[], timeline=run["timeline"],
    )
    assert contract.original_goal == goal
    if capture_ok:
        assert captures[0].read_bytes() == PNG
        assert task["artifacts"][-1]["path"] == "screenshots/current-screen.png"
        assert "artifact.created" in events
    else:
        assert not task["artifacts"]
        assert "agent.desktop.intent_completed" not in events
