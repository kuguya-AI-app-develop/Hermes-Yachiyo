"""Native Music receipts require observed effects, not dispatch acknowledgements."""

from __future__ import annotations

import pytest

from apps.shell.agent.tools import desktop
from apps.shell.agent.tools.broker import ToolBroker


def _broker(tmp_path):
    return ToolBroker(
        workspace_policy={"default_workdir": str(tmp_path)},
        artifact_root=tmp_path / "artifacts",
    )


def _native_script(monkeypatch, stdout):
    calls = []

    def execute(script, args, **kwargs):
        calls.append((script, args))
        return {"ok": True, "stdout": stdout, "stderr": ""}

    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_run_osascript", execute)
    return calls


@pytest.mark.parametrize("action,state,verified", [
    ("play", "playing", True), ("pause", "paused", True),
    ("play", "paused", False), ("pause", "playing", False),
    ("pause", "unknown", False), ("toggle", "playing", False),
    ("next", "playing", False), ("previous", "playing", False),
])
def test_control_receipt_requires_actual_matching_player_state(
    tmp_path, monkeypatch, action, state, verified
):
    calls = _native_script(monkeypatch, f"controlled|{action}|{state}|Track|Artist")
    result = _broker(tmp_path).media_apple_music_control(action)
    assert len(calls) == 1
    assert calls[0][1] == [action]
    assert result["ok"] is True
    assert result["data"]["player_state"] == state
    assert (result.get("postcondition_verified") is True) is verified


@pytest.mark.parametrize("action,before,after,state,persistent_before,persistent_after,verified", [
    ("next", "101", "102", "playing", "", "", True),
    ("previous", "102", "101", "paused", "", "", True),
    ("next", "101", "101", "playing", "", "", False),
    ("next", "", "102", "playing", "", "", False),
    ("next", "101", "102", "unknown", "", "", False),
    ("next", "101", "102", "playing", "AAAAAAAAAAAAAAAA", "AAAAAAAAAAAAAAAA", False),
    ("next", "", "", "playing", "AAAAAAAAAAAAAAAA", "BBBBBBBBBBBBBBBB", True),
    ("next", "", "", "playing", "AAAAAAAAAAAAAAAA", "aaaaaaaaaaaaaaaa", False),
    ("next", "", "", "playing", "not-an-id", "another-value", False),
])
def test_track_navigation_requires_before_after_identity_change(
    tmp_path, monkeypatch, action, before, after, state,
    persistent_before, persistent_after, verified,
):
    stdout = "\x1f".join([
        "controlled-v2", action, state, before, after,
        persistent_before, persistent_after, "Track", "Artist",
    ])
    calls = _native_script(monkeypatch, stdout)
    result = _broker(tmp_path).media_apple_music_control(action)
    assert len(calls) == 1
    assert "currentMusicTrackIdentity" in calls[0][0]
    assert result["ok"] is True
    assert (result.get("postcondition_verified") is True) is verified


@pytest.mark.parametrize("state,track,identity,verified", [
    ("playing", "Requested", "identity_verified", True),
    ("paused", "Requested", "identity_verified", False),
    ("playing", "Different", "identity_verified", False),
    ("playing", "Requested", "identity_unverified", False),
])
def test_play_receipt_binds_requested_track_and_actual_playback(
    tmp_path, monkeypatch, state, track, identity, verified,
):
    calls = _native_script(
        monkeypatch, f"played|{track}|Artist|{state}|track|Album|{identity}"
    )
    result = _broker(tmp_path).media_apple_music_play("Requested")
    assert len(calls) == 1
    assert calls[0][1][0] == "Requested"
    assert result["ok"] is True
    assert (result.get("postcondition_verified") is True) is verified


@pytest.mark.parametrize("method,argument,tool,data", [
    ("media_apple_music_control", "pause", "media.apple_music_control",
     {"control": "pause", "player_state": "unknown", "fallback": "system_media_key"}),
    ("media_apple_music_control", "next", "media.apple_music_control",
     {"control": "next", "player_state": "playing", "track_change_verified": True}),
    ("media_apple_music_play", "Requested", "media.apple_music_play",
     {"query": "Requested", "player_state": "playing", "track": "Different"}),
])
def test_supplied_completion_claims_cannot_replace_media_observations(
    tmp_path, monkeypatch, method, argument, tool, data,
):
    claims = {"postcondition_verified": True, "verification_passed": True, "verified": True}
    monkeypatch.setattr(desktop, method.removeprefix("media_"), lambda _: {
        "ok": True, "action": tool, **claims, "data": {**data, **claims},
    })
    result = getattr(_broker(tmp_path), method)(argument)
    assert result["ok"] is True
    for key in claims:
        assert key not in result
        assert key not in result["data"]


@pytest.mark.parametrize("state,open_ok,verified", [
    ("playing", True, True), ("paused", True, False),
    ("unknown", True, False), ("playing", False, False),
])
def test_music_app_alias_uses_real_native_playback_observation(
    tmp_path, monkeypatch, state, open_ok, verified,
):
    calls = _native_script(monkeypatch, f"controlled|play|{state}|Track|Artist")
    monkeypatch.setattr(desktop, "app_open", lambda name: {
        "ok": open_ok, "action": "app.open", "data": {"app_name": name},
    })
    result = _broker(tmp_path).media_music_app_open_and_play("Music")
    assert len(calls) == 1
    assert calls[0][1] == ["play"]
    assert (result.get("postcondition_verified") is True) is verified


def test_other_music_apps_media_key_fallback_cannot_claim_completion(tmp_path, monkeypatch):
    monkeypatch.setattr(desktop, "music_app_open_and_play", lambda _: {
        "ok": True, "action": "media.music_app_open_and_play",
        "fallback_used": True, "postcondition_verified": True,
        "data": {"app_name": "Spotify", "player_state": "unknown", "verified": True},
    })
    result = _broker(tmp_path).media_music_app_open_and_play("Spotify")
    assert result["ok"] is True
    assert "postcondition_verified" not in result
    assert "verified" not in result["data"]
