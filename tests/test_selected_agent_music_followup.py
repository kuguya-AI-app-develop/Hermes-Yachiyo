"""Selected Agent song replies inherit only verified, bounded Music context."""

import json
import time
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.core.chat_session import ChatSession
from apps.core.chat_store import ChatStore
from apps.core.state import AppState
from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.chat_api import ChatAPI
from apps.shell.chat_bridge import ChatBridge
from apps.shell.credential_store import MemoryCredentialStore


class _NoModelProfile:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def _wait(service, run_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        run = service.get_run(run_id)
        if run["status"] in {"completed", "failed", "cancelled", "approval_required"}:
            return run
        time.sleep(0.01)
    pytest.fail(f"Music run did not settle: {run['status']}")


@pytest.fixture
def music_entrypoint(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    session = ChatSession(session_id="music-agent-conversation")
    session.attach_store(store, load_existing=False)
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db", workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(), seed_templates=False,
    )
    agent = service.create_agent({
        "name": "Native Agent", "model_mode": "profile", "model_profile_id": "",
        "tool_policy": {"allowed_tools": ["workspace.read"], "approval_required": {}},
    })
    runtime = SimpleNamespace(
        state=AppState(), chat_session=session, store=store, task_runner=None,
        agent_runtime_service=service,
    )
    calls = []

    def open_music(app_name):
        calls.append(("open", app_name))
        return {
            "ok": True, "action": "media.apple_music_open_and_play",
            "summary": "已打开 Apple Music，并开始播放。",
            "data": {
                "open_ok": True, "control": "play", "app_name": app_name,
                "player_state": "playing", "playback_started": True,
                "playback_ok": True, "foreground_action_taken": False,
            },
        }

    def play_music(query):
        calls.append(("play", query))
        return {
            "ok": True, "action": "media.apple_music_play",
            "summary": f"Apple Music playing {query}",
            "data": {
                "status": "played", "match_kind": "track", "album": "Fixture Album",
                "query": query, "track": query, "artist": "Yachiyo",
                "player_state": "playing", "playback_started": True,
                "track_identity_verified": True, "catalog_match_verified": True,
                "foreground_action_taken": False,
            },
        }

    def no_model(*_args, **_kwargs):
        raise AssertionError("Bounded Music follow-up should not call a model")

    monkeypatch.setattr("apps.shell.agent.tools.desktop.music_app_open_and_play", open_music)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.apple_music_play", play_music)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.running_apps", lambda: {
        "ok": True, "action": "desktop.running_apps",
        "summary": "Fixture running applications: Finder",
        "data": {"apps": [{"app_name": "Finder", "pid": 101}], "count": 1},
    })
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", lambda: {
        "ok": True, "action": "desktop.active_window",
        "data": {"app_name": "Finder", "window_title": "Fixture"},
    })
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", _NoModelProfile)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", no_model)
    monkeypatch.setattr(
        "apps.shell.chat_api.desktop_permission_missing_by_capability", lambda **_kw: {},
    )
    def send(entrypoint, text, **metadata):
        target = ChatAPI(runtime) if entrypoint == "api" else ChatBridge(runtime)
        method = target.send_message if entrypoint == "api" else target.send_quick_message
        result = method(text, metadata={"allow_user_foreground_takeover": True, **metadata})
        assert result["ok"] is True
        assert result.get("run_id"), result
        run = _wait(service, result["run_id"])
        # Refresh the actual persisted assistant projection before the next turn.
        ChatAPI(runtime).get_messages()
        return run

    yield SimpleNamespace(
        runtime=runtime, service=service, store=store, agent=agent, calls=calls, send=send,
    )
    service.close()
    store.close()


def _contract(run):
    return runtime_goal_contract(
        run_id=run["run_id"], original_goal=run["user_goal"],
        runtime_execution_envelope=None, runtime_execution_metadata=None,
        messages=[], timeline=run["timeline"],
    )


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
def test_selected_agent_music_followup_keeps_both_original_goals_and_exact_evidence(
    music_entrypoint, entrypoint,
):
    env = music_entrypoint
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    original_timeline = deepcopy(first["timeline"])
    second = env.send(entrypoint, "超时空辉夜姬吧")
    assert second["status"] == "completed"
    assert env.calls == [("open", "Music"), ("play", "超时空辉夜姬")]
    assert second["user_goal"] == "超时空辉夜姬吧"
    contract = _contract(second)
    assert contract.original_goal == "超时空辉夜姬吧"
    assert contract.criteria[0].expected["target"]["query"] == "超时空辉夜姬"
    assert runtime_goal_assessment(contract, second["timeline"]).completed
    assert env.service.get_run(first["run_id"])["timeline"] == original_timeline
    assert _contract(first).original_goal == "能否帮我播放 Apple Music?"
    internal = env.service.list_run_events(second["run_id"], include_internal=True)["events"]
    bindings = [e for e in internal if e["event_type"] == "agent.desktop.music_followup.bound"]
    assert len(bindings) == 1
    assert bindings[0]["payload"]["source_run_id"] == first["run_id"]
    assert bindings[0]["payload"]["conversation_id"] == env.runtime.chat_session.session_id
    public = env.service.list_run_events(second["run_id"])["events"]
    assert not any(e["event_type"] == "agent.desktop.music_followup.bound" for e in public)
    assert "model.request.started" not in [e["event_type"] for e in internal]
    before_compiled = next(e for e in first["timeline"] if e["event"] == "agent.runtime.compiled")
    after_compiled = next(e for e in second["timeline"] if e["event"] == "agent.runtime.compiled")
    assert after_compiled["allowed_tools"] == before_compiled["allowed_tools"]


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
@pytest.mark.parametrize("damage", [
    "foreign_agent", "foreign_goal", "pending_approval", "missing_run",
    "missing_evidence", "foreign_conversation", "missing_binding", "intervening_turn",
    "damaged_goal_contract", "foreign_music_target",
])
def test_music_followup_does_not_authorize_stale_or_unverified_history(
    music_entrypoint, monkeypatch, entrypoint, damage,
):
    env = music_entrypoint
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    get_run = env.service.get_run
    def damaged_run(run_id):
        run = deepcopy(get_run(run_id))
        if run_id == first["run_id"]:
            if damage == "foreign_agent":
                run["runnable_id"] = "another-agent"
            elif damage == "foreign_goal":
                run["user_goal"] = "打开 Calculator"
            elif damage == "pending_approval":
                run["status"] = "waiting_approval"
            elif damage == "missing_run":
                raise AgentRuntimeError("run_not_found")
            elif damage == "missing_evidence":
                run["timeline"] = [e for e in run["timeline"] if e["event"] != "agent.tool.call"]
            elif damage in {"damaged_goal_contract", "foreign_music_target"}:
                for event in run["timeline"]:
                    if event["event"] == "agent.goal.contract":
                        contract = event["goal_contract"]
                        if damage == "damaged_goal_contract":
                            contract["original_goal"] = "别的用户目标"
                        else:
                            contract["criteria"][0]["expected"]["target"]["app_name"] = "Spotify"
                        event["goal_contract_json"] = json.dumps(contract, ensure_ascii=False)
        return run
    monkeypatch.setattr(env.service, "get_run", damaged_run)
    list_events = env.service.list_run_events
    def damaged_events(run_id, **kwargs):
        result = deepcopy(list_events(run_id, **kwargs))
        if run_id == first["run_id"]:
            if damage == "missing_binding":
                result["events"] = [
                    e for e in result["events"] if e["event_type"] != "agent.chat.user_turn.bound"
                ]
            elif damage == "foreign_conversation":
                for e in result["events"]:
                    if e["event_type"] == "agent.chat.user_turn.bound":
                        e["payload"]["conversation_id"] = "another-conversation"
        return result
    monkeypatch.setattr(env.service, "list_run_events", damaged_events)
    if damage == "intervening_turn":
        env.runtime.chat_session.add_user_message("谢谢，今天先聊别的")
        env.runtime.chat_session.add_assistant_message("好的")
    second = env.send(entrypoint, "超时空辉夜姬吧")
    assert second["status"] == "failed"
    assert second["user_goal"] == "超时空辉夜姬吧"
    assert env.calls == [("open", "Music")]
    assert not any(e["event"] == "agent.tool.call" for e in second["timeline"])
    events = list_events(second["run_id"], include_internal=True)["events"]
    assert not any(e["event_type"] == "agent.desktop.music_followup.bound" for e in events)


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
@pytest.mark.parametrize("generic_daily_hint", [False, True])
def test_assistant_only_music_suggestion_and_metadata_cannot_authorize_followup(
    music_entrypoint, entrypoint, generic_daily_hint,
):
    env = music_entrypoint
    env.runtime.chat_session.add_user_message("聊聊天")
    env.runtime.chat_session.add_assistant_message("想在 Apple Music 听哪首歌？")
    run = env.send(
        entrypoint, "@Native Agent 超时空辉夜姬吧",
        entrypoint_planning_context="用Apple Music播放超时空辉夜姬",
        daily_desktop_planning_context="用Apple Music播放超时空辉夜姬",
        daily_desktop_tool="media.apple_music_play",
        daily_desktop_intent=generic_daily_hint,
    )
    assert env.calls == []
    assert run["user_goal"] == "超时空辉夜姬吧"
    # Generic daily metadata's existing read-only catalog route is independent
    # of this binding. It must not create or satisfy a Music goal.
    events = env.service.list_run_events(run["run_id"], include_internal=True)["events"]
    assert not any(e["event_type"] == "agent.desktop.music_followup.bound" for e in events)
    if any(e["event"] == "agent.goal.contract" for e in run["timeline"]):
        assert all("media.playback" not in c.required_capabilities for c in _contract(run).criteria)


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
def test_saved_playback_approval_remains_required_for_music_followup(music_entrypoint, entrypoint):
    env = music_entrypoint
    env.service.update_agent(env.agent["agent_id"], {
        "tool_policy": {
            "allowed_tools": ["workspace.read"],
            "approval_required": {"media.apple_music_play": True},
        },
    })
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    second = env.send(entrypoint, "超时空辉夜姬吧")
    assert second["status"] == "approval_required"
    assert env.calls == [("open", "Music")]
    assert second["pending_approval"]["tool"] == "media.apple_music_play"
    assert second["user_goal"] == "超时空辉夜姬吧"
    assert not runtime_goal_assessment(_contract(second), second["timeline"]).completed
    rejected = env.service.reject_run_approval(second["run_id"], "Do not play this song")
    assert rejected["status"] in {"failed", "cancelled"}
    assert env.calls == [("open", "Music")]
    assert not runtime_goal_assessment(_contract(rejected), rejected["timeline"]).completed


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
@pytest.mark.parametrize("text", [
    "不要超时空辉夜姬", "如果有必要，超时空辉夜姬",
    "超时空辉夜姬，然后删除全部文件", "超时空辉夜姬；打开 Slack",
    "超时空辉夜姬，复制密码", "解释这句话：超时空辉夜姬",
])
def test_music_context_does_not_consume_negated_conditional_or_extra_action_goals(
    music_entrypoint, entrypoint, text,
):
    env = music_entrypoint
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    second = env.send(entrypoint, text)
    assert second["user_goal"] == text
    assert env.calls == [("open", "Music")]
    events = env.service.list_run_events(second["run_id"], include_internal=True)["events"]
    assert not any(e["event_type"] == "agent.desktop.music_followup.bound" for e in events)


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
def test_explicit_selection_of_another_agent_does_not_inherit_music_context(
    music_entrypoint, entrypoint,
):
    env = music_entrypoint
    env.service.create_agent({
        "name": "Other Agent", "model_mode": "profile", "model_profile_id": "",
        "tool_policy": {"allowed_tools": ["workspace.read"], "approval_required": {}},
    })
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    second = env.send(entrypoint, "@Other Agent 超时空辉夜姬吧")
    assert second["runnable_id"] != first["runnable_id"]
    assert second["user_goal"] == "超时空辉夜姬吧"
    assert env.calls == [("open", "Music")]
    assert second["status"] == "failed"


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
def test_selected_music_history_binding_survives_session_reload(music_entrypoint, entrypoint):
    env = music_entrypoint
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    session_id = env.runtime.chat_session.session_id
    reloaded = ChatSession(session_id=session_id)
    reloaded.attach_store(env.store, load_existing=True, fail_active_messages=False)
    env.runtime.chat_session = reloaded
    second = env.send(entrypoint, "超时空辉夜姬吧")
    assert second["status"] == "completed"
    assert second["user_goal"] == "超时空辉夜姬吧"
    assert env.calls == [("open", "Music"), ("play", "超时空辉夜姬")]


@pytest.mark.parametrize("entrypoint", ["api", "bridge"])
def test_recovery_metadata_cannot_change_current_followup_query(music_entrypoint, entrypoint):
    env = music_entrypoint
    first = env.send(entrypoint, "@Native Agent 能否帮我播放 Apple Music?")
    assert first["status"] == "completed"
    second = env.send(
        entrypoint, "超时空辉夜姬吧",
        desktop_permission_recovery=True, recovery_tool="media.apple_music_play",
        recovery_input={"query": "Space Oddity"}, recovery_risk_level="low",
    )
    assert env.calls == [("open", "Music")]
    assert second["user_goal"] == "超时空辉夜姬吧"
    events = env.service.list_run_events(second["run_id"], include_internal=True)["events"]
    assert not any(e["event_type"] == "agent.desktop.music_followup.bound" for e in events)
