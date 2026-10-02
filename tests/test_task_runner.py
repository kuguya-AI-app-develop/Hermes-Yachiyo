import asyncio
import base64
import json
import time
from types import SimpleNamespace
from typing import Any

import pytest

import apps.core.activity_store as activity_store_mod
import apps.core.chat_store as chat_store_mod
import apps.core.task_runner as task_runner_mod
import apps.shell.chat_api as chat_api_mod
import apps.shell.proactive as proactive_mod
from apps.core.activity_store import ActivityStore
from apps.core.chat_session import ChatSession, MessageStatus
from apps.core.chat_store import ChatStore
from apps.core.executor import NativeAgentExecutor, NativeAgentUnavailableExecutor
from apps.core.special_sessions import PROACTIVE_CHAT_SESSION_ID
from apps.core.state import AppState
from apps.core.task_runner import TaskRunner
from apps.shell.agent.runtime.model_intent_planning import MODEL_INTENT_PLANNING_TOOL_NAME
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.chat_api import ChatAPI
from apps.shell.config import Live2DModeConfig
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.proactive import ProactiveDesktopService
from packages.protocol.enums import TaskStatus, TaskType


class _InstantExecutor:
    @property
    def name(self) -> str:
        return "InstantExecutor"

    async def run(self, task):
        return f"done:{task.description}"


class _FakeDefaultProfileService:
    def get_defaults(self):
        return {"chat": "profile_default"}

    def get_profile_private(self, profile_id):
        assert profile_id in {"profile_default", "profile_vision"}
        return {
            "profile_id": profile_id,
            "provider": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
            "capability": "vision" if profile_id == "profile_vision" else "chat",
            "status": "available",
            "enabled": True,
        }


class _FakeNoDefaultProfileService:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def test_task_runner_group_summary_parent_sync_clears_pending_metadata(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    session = ChatSession(session_id="task-runner-group-summary-parent-sync")
    session.attach_store(store, load_existing=False)
    try:
        parent_task_id = "parent_group_dispatch_task"
        summary_task_id = "summary_group_dispatch_task"
        session.upsert_assistant_message(
            task_id=parent_task_id,
            content="群组派发已完成，等待主模型汇总。",
            status=MessageStatus.COMPLETED,
            metadata={
                "group_agent_summary_task_id": summary_task_id,
                "group_agent_summary_pending": True,
            },
        )

        task_runner_mod._sync_group_summary_parent_status(
            session,
            summary_task_id=summary_task_id,
            summary_metadata={"group_agent_summary_for_task_id": parent_task_id},
            summary_status="completed",
        )

        parent = session.get_assistant_message_for_task(parent_task_id)
        assert parent is not None
        assert "group_agent_summary_pending" not in parent.metadata
        assert parent.metadata["group_agent_summary_status"] == "completed"
    finally:
        store.close()


def test_task_runner_direct_group_summary_parent_sync_marks_pending_false(tmp_path):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    session = ChatSession(session_id="task-runner-direct-group-summary-parent-sync")
    session.attach_store(store, load_existing=False)
    try:
        direct_task_id = "direct_agent_message_task"
        summary_task_id = "direct_summary_task"
        direct_message_id = session.upsert_assistant_message(
            task_id=direct_task_id,
            content="Design 已完成，等待主模型整理。",
            status=MessageStatus.COMPLETED,
            metadata={
                "group_agent_summary_task_id": summary_task_id,
                "group_agent_summary_pending": True,
            },
        )

        task_runner_mod._sync_group_summary_parent_status(
            session,
            summary_task_id=summary_task_id,
            summary_metadata={"group_direct_agent_summary_for_message_id": direct_message_id},
            summary_status="completed",
        )

        parent = session.get_assistant_message_for_task(direct_task_id)
        assert parent is not None
        assert parent.metadata["group_agent_summary_pending"] is False
        assert parent.metadata["group_agent_summary_status"] == "completed"
    finally:
        store.close()


async def _wait_for(condition, *, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = condition()
        except Exception as exc:
            last = exc
            await asyncio.sleep(0.02)
            continue
        if last:
            return last
        await asyncio.sleep(0.02)
    raise TimeoutError(f"condition not met; last={last!r}")


def _task_run_with_status(
    service: AgentRuntimeService,
    task_id: str,
    status: str,
) -> dict | None:
    run = service.get_run(service.get_task_run_link(task_id)["run_id"])
    return run if run["status"] == status else None


@pytest.mark.asyncio
async def test_stop_awaits_in_progress_task_cancellation():
    runner = TaskRunner(AppState())
    child = asyncio.create_task(asyncio.sleep(60))
    runner._in_progress["task1"] = child

    await runner.stop()

    assert child.done()
    assert runner._in_progress == {}


@pytest.mark.asyncio
async def test_task_runner_writes_final_reply_to_original_background_session(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    state = AppState()
    background = ChatSession(session_id="background-session")
    background.attach_store(store, load_existing=False)
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="后台任务",
        chat_session_id="background-session",
    )
    runner = TaskRunner(state, executor=_InstantExecutor())  # type: ignore[arg-type]
    try:
        await runner._execute_with_state(task.task_id)

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        messages = store.load_messages("background-session", limit=10)
        assistant = next(message for message in messages if message.role == "assistant")
        assert assistant.task_id == task.task_id
        assert assistant.status == "completed"
        assert assistant.content == "done:后台任务"
        activity_titles = [event.title for event in activity_store.latest_for_task(task.task_id, limit=5)]
        assert "Yachiyo 回复完成" in activity_titles
    finally:
        activity_store.close()
        store.close()


def test_task_runner_does_not_recreate_deleted_background_session(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    state = AppState()
    target_session_id = "deleted-background-session"
    target = ChatSession(session_id=target_session_id)
    target.attach_store(store, load_existing=False)
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="late background result",
        chat_session_id=target_session_id,
    )
    state.update_task_status(
        task.task_id,
        TaskStatus.COMPLETED,
        result="must not resurrect deleted chat",
    )
    store.delete_session(target_session_id)

    try:
        TaskRunner(state)._sync_task_message(task.task_id)

        assert store.get_session(target_session_id) is None
        assert store.load_messages(target_session_id, limit=0) == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_task_runner_activity_events_use_injected_activity_store(tmp_path, monkeypatch):
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))

    def fail_global_activity_store():
        raise AssertionError("TaskRunner should use the injected ActivityStore")

    monkeypatch.setattr(activity_store_mod, "get_activity_store", fail_global_activity_store)
    state = AppState()
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="注入 ActivityStore 的任务",
    )
    runner = TaskRunner(
        state,
        executor=_InstantExecutor(),  # type: ignore[arg-type]
        activity_store=activity_store,
    )
    try:
        await runner._execute_with_state(task.task_id)

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        events = [event.to_dict() for event in activity_store.latest_for_task(task.task_id, limit=5)]
        titles = [event["title"] for event in events]
        assert "Yachiyo 开始处理" in titles
        assert "Yachiyo 回复完成" in titles
        assert all(event["tool_name"] == "native_agent" for event in events)
    finally:
        activity_store.close()


@pytest.mark.asyncio
async def test_task_runner_executes_daily_desktop_intent_when_model_profile_missing(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime-daily-desktop.db",
        workspace_dir=tmp_path / "agent-runtime-daily-desktop",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    state = AppState()
    session = ChatSession(session_id="task-runner-daily-desktop")
    session.attach_store(store, load_existing=False)
    open_calls: list[str] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict:
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Music for {query}",
            "data": {
                "query": query,
                "apps": [{"name": "Music", "path": "/Applications/Music.app"}],
                "best_match": {
                    "name": "Music",
                    "path": "/Applications/Music.app",
                    "match_score": 100,
                    "match_confidence": "high",
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
        open_if_needed: Any = True,
        focus: Any = True,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict:
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": f"Verified {app_name}",
            "data": {
                "app_name": app_name,
                "running": True,
                "open_if_needed": open_if_needed,
                "focus": focus,
                "role_filter": role_filter,
                "limit": limit,
            },
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("daily desktop task should not call model"),
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)

    daily_executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
    )
    runner = TaskRunner(
        state,
        executor=NativeAgentUnavailableExecutor(
            "Native Agent 当前未就绪，请先配置并选择默认对话模型。",
            reason="model_profile_required",
            daily_desktop_executor=daily_executor,
        ),
        activity_store=activity_store,
    )
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        task_runner=runner,
        store=store,
        agent_runtime_service=service,
    )
    api = ChatAPI(runtime)
    try:
        sent = api.send_message(
            "打开 Apple Music",
            metadata={"allow_user_foreground_takeover": True},
        )
        assert sent["ok"] is True

        await runner._execute_with_state(sent["task_id"])

        task = state.get_task(sent["task_id"])
        assert task is not None
        assert task.status == TaskStatus.COMPLETED
        assert task.result == "已打开 Music。"
        assert open_calls == ["Music"]
        assistant = next(
            message
            for message in store.load_messages(session.session_id, limit=10)
            if message.role == "assistant" and message.task_id == sent["task_id"]
        )
        assert assistant is not None
        assert assistant.status == "completed"
        assert assistant.content == "已打开 Music。"
        link = service.get_task_run_link(sent["task_id"])
        run = service.get_run(link["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        assert run["status"] == "completed"
        assert "agent.desktop.intent_planned" in event_types
        assert "agent.tool.call" in event_types
        assert "agent.desktop.intent_completed" in event_types
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_main_chat_native_tool_approval_roundtrip(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    workdir = tmp_path / "workspace"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "app.txt").write_text("before\n", encoding="utf-8")
    session = ChatSession(session_id="main-chat-approval-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="请把 src/app.txt 改成 after",
        chat_session_id=session.session_id,
    )
    user_message_id = session.add_user_message(task.description)
    session.link_message_to_task(user_message_id, task.task_id)
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_write_patch" for tool in tools or [])
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "src/app.txt",
                                    "patch": "--- src/app.txt\n+++ src/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "已完成修改。"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {
            "allowed_tools": ["workspace.read", "workspace.write_patch"],
            "approval_required": {"workspace.write_patch": True},
        },
        workspace_policy_getter=lambda: {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
    )
    runner = TaskRunner(state, executor=executor)
    api = ChatAPI(SimpleNamespace(state=state, chat_session=session, store=store))
    runner_task: asyncio.Task | None = None
    try:
        runner_task = asyncio.create_task(runner._execute_with_state(task.task_id))
        run = await _wait_for(
            lambda: _task_run_with_status(service, task.task_id, "approval_required")
        )
        assert run["status"] == "approval_required"
        assert state.get_task(task.task_id).status == TaskStatus.RUNNING

        waiting_payload = api.get_messages()
        assistant = next(message for message in waiting_payload["messages"] if message["role"] == "assistant")
        assert waiting_payload["approval_count"] == 1
        assert assistant["status"] == "processing"
        assert assistant["metadata"]["run_status"] == "approval_required"
        assert assistant["metadata"]["run_id"] == run["run_id"]
        assert assistant["metadata"]["pending_approval"]["tool"] == "workspace.write_patch"
        assert assistant["activity_events"][0]["status"] == "approval_required"

        approved = await asyncio.to_thread(service.approve_run_approval, run["run_id"])
        assert approved["status"] == "running"
        await runner_task

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "已完成修改。"
        assert (workdir / "src" / "app.txt").read_text(encoding="utf-8") == "after\n"
        final_messages = store.load_messages(session.session_id, limit=10)
        final_assistant = next(message for message in final_messages if message.role == "assistant")
        assert final_assistant.status == "completed"
        assert final_assistant.content == "已完成修改。"
        event_types = [event["event_type"] for event in service.list_run_events(run["run_id"])["events"]]
        assert "agent.tool.approval_required" in event_types
        assert "agent.tool.approval_approved" in event_types
        assert "model.output.completed" in event_types
        assert "run.completed" in event_types
    finally:
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_main_chat_approval_timeout_clears_chat_and_activity_projection(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    workdir = tmp_path / "workspace"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "app.txt").write_text("before\n", encoding="utf-8")
    session = ChatSession(session_id="main-chat-approval-timeout-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="请把 src/app.txt 改成 after，但审批会超时",
        chat_session_id=session.session_id,
    )
    user_message_id = session.add_user_message(task.description)
    session.link_message_to_task(user_message_id, task.task_id)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        assert messages[-1]["role"] == "user"
        assert any((tool.get("function") or {}).get("name") == "workspace_write_patch" for tool in tools or [])
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_patch_timeout",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_patch",
                        "arguments": json.dumps(
                            {
                                "path": "src/app.txt",
                                "patch": "--- src/app.txt\n+++ src/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    monkeypatch.setattr(
        NativeAgentExecutor,
        "_approval_wait_timeout_seconds",
        staticmethod(lambda: 0.05),
    )

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {
            "allowed_tools": ["workspace.read", "workspace.write_patch"],
            "approval_required": {"workspace.write_patch": True},
        },
        workspace_policy_getter=lambda: {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    api = ChatAPI(SimpleNamespace(state=state, chat_session=session, store=store, agent_runtime_service=service))
    runner_task: asyncio.Task | None = None
    try:
        runner_task = asyncio.create_task(runner._execute_with_state(task.task_id))
        run = await _wait_for(
            lambda: _task_run_with_status(service, task.task_id, "approval_required")
        )
        assert run["status"] == "approval_required"

        waiting_payload = api.get_messages()
        waiting_assistant = next(message for message in waiting_payload["messages"] if message["role"] == "assistant")
        assert waiting_payload["approval_count"] == 1
        assert waiting_assistant["metadata"]["run_status"] == "approval_required"
        assert waiting_assistant["metadata"]["pending_approval"]["tool"] == "workspace.write_patch"

        await runner_task

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert "工具审批已超时：approval_wait_timeout" in str(updated.error)
        assert (workdir / "src" / "app.txt").read_text(encoding="utf-8") == "before\n"

        timed_out_run = service.get_run(run["run_id"])
        assert timed_out_run["status"] == "cancelled"
        assert timed_out_run["pending_approval"] == {}
        event_types = [event["event_type"] for event in service.list_run_events(run["run_id"])["events"]]
        assert event_types.count("approval.timeout") == 1
        assert "run.completed" not in event_types

        final_payload = api.get_messages()
        final_assistant = next(message for message in final_payload["messages"] if message["role"] == "assistant")
        assert final_payload["approval_count"] == 0
        assert final_assistant["status"] == "failed"
        assert "工具审批已超时：approval_wait_timeout" in final_assistant["content"]
        assert final_assistant["metadata"]["run_status"] == "failed"
        assert final_assistant["metadata"]["pending_approval"] == {}
        assert final_assistant["activity_events"][0]["status"] == "failed"
        assert any(
            event.title == "Yachiyo 回复失败" and event.status == "failed"
            for event in activity_store.latest_for_task(task.task_id, limit=8)
        )
    finally:
        if runner_task is not None and not runner_task.done():
            runner_task.cancel()
            await asyncio.gather(runner_task, return_exceptions=True)
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_main_chat_auto_delegation_uses_native_runtime(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="main-chat-delegation-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="请让 Research Agent Respond with exactly 'Research Agent native delegation result'，然后给我结论",
        chat_session_id=session.session_id,
    )
    user_message_id = session.add_user_message(task.description)
    session.link_message_to_task(user_message_id, task.task_id)
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "# Agent\nName: Research Agent" in last_content:
            assert "# User Goal\nRespond with exactly 'Research Agent native delegation result'" in last_content
            return {"role": "assistant", "content": "Research Agent native delegation result"}
        if "[Oha-Yachiyo 自动委派 Run 汇总]" in last_content:
            assert "Research Agent：已完成" in last_content
            assert "Research Agent native delegation result" in last_content
            return {"role": "assistant", "content": "总结：Research Agent 的委派结果已整合。"}
        if "[OHA 委派结果]" in last_content:
            assert "Research Agent native delegation result" in last_content
            return {"role": "assistant", "content": "最终结论：Native 委派链路已闭环。"}
        return {
            "role": "assistant",
            "content": json.dumps(
                {
                    "action": "run_oha_agent",
                    "agent": "Research Agent",
                    "goal": "Respond with exactly 'Research Agent native delegation result'",
                },
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    agent = service.create_agent(
        {
            "name": "Research Agent",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
    )
    runner = TaskRunner(state, executor=executor)
    try:
        await runner._execute_with_state(task.task_id)

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "最终结论：Native 委派链路已闭环。"
        assert len(model_calls) == 3

        assistant = next(message for message in store.load_messages(session.session_id, limit=10) if message.role == "assistant")
        assert assistant.task_id == task.task_id
        assert assistant.status == "completed"
        assert assistant.content == "最终结论：Native 委派链路已闭环。"

        link = service.get_task_run_link(task.task_id)
        main_run = service.get_run(link["run_id"])
        assert main_run["kind"] == "main_chat_run"
        assert main_run["status"] == "completed"
        assert main_run["result"] == "最终结论：Native 委派链路已闭环。"
        main_event_types = [event["event_type"] for event in service.list_run_events(main_run["run_id"])["events"]]
        assert main_event_types.count("model.output.completed") == 2
        assert "run.completed" in main_event_types

        delegated_runs = [
            run for run in service.list_runs(limit=20)["runs"]
            if run["kind"] == "agent_run" and run["runnable_id"] == agent["agent_id"]
        ]
        assert len(delegated_runs) == 1
        delegated = delegated_runs[0]
        assert delegated["status"] == "completed"
        assert delegated["result"] == "Research Agent native delegation result"
        assert service.get_run_group(delegated["run_group_id"])["source"] == "delegation"

        activity_events = [event.to_dict() for event in activity_store.latest_for_task(task.task_id, limit=10)]
        activity_summary = [
            (event["title"], event["status"], event["tool_name"], event["phase"])
            for event in activity_events
        ]
        assert any(
            event["title"] == "正在委派给 Research Agent"
            and event["tool_name"] == "oha.delegation"
            and event["phase"] == "subagent"
            for event in activity_events
        ), activity_summary
        completed_activity = next(event for event in activity_events if event["title"] == "Research Agent 委派完成")
        assert completed_activity["status"] == "completed"
        assert completed_activity["metadata"]["run_id"] == delegated["run_id"]
        assert completed_activity["metadata"]["run_group_id"] == delegated["run_group_id"]

        summary_api = ChatAPI(SimpleNamespace(state=state, chat_session=session, agent_runtime_service=service))
        summary_created = summary_api.summarize_delegated_run(delegated["run_id"])
        assert summary_created["ok"] is True
        assert summary_created["summary_created"] is True
        summary_task = state.get_task(summary_created["task_id"])
        assert summary_task is not None
        assert summary_task.chat_session_id == session.session_id
        assert "[Oha-Yachiyo 自动委派 Run 汇总]" in summary_task.response_context
        assert "Research Agent：已完成" in summary_task.response_context
        assert "汇报：Research Agent native delegation result" in summary_task.response_context

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "总结：Research Agent 的委派结果已整合。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["result"] == "总结：Research Agent 的委派结果已整合。"
        assert summary_run["run_id"] != main_run["run_id"]
        summary_event_types = [event["event_type"] for event in service.list_run_events(summary_run["run_id"])["events"]]
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types
        summary_assistant = next(
            message
            for message in store.load_messages(session.session_id, limit=20)
            if message.task_id == summary_task.task_id
        )
        assert summary_assistant.status == "completed"
        assert summary_assistant.content == "总结：Research Agent 的委派结果已整合。"
        assert "run_oha_agent" not in summary_assistant.content
        assert "<oha_delegation>" not in summary_assistant.content
        assert len(model_calls) == 4
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_group_dispatch_summary_uses_native_runtime(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="main-chat-group-dispatch-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        agent_runtime_service=service,
    )
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组 Agent 汇总]" in last_content:
            assert "不要再派发新的 Agent 任务" in last_content
            assert "Coding：已完成" in last_content
            assert "汇报：Coding native dispatch result" in last_content
            return {"role": "assistant", "content": "群组总结：Coding 已完成 Native 群聊派发验证。"}
        if "# Agent\nName: Coding Agent" in last_content:
            assert "# User Goal\nRespond with exactly 'Coding native dispatch result'" in last_content
            assert "[Oha-Yachiyo 群组执行约定]" in last_content
            assert "你在群内身份是：Coding" in last_content
            return {"role": "assistant", "content": "Coding native dispatch result"}
        assert "请安排 Coding Respond with exactly 'Coding native dispatch result'" in last_content
        assert "oha.group_dispatch" in str(messages[0]["content"])
        return {
            "role": "assistant",
            "content": (
                "我会让 Coding 处理这件事。\n"
                '{"tool":"oha.group_dispatch","input":{"tasks":[{"kind":"agent","target":"Coding",'
                '"goal":"Respond with exactly \'Coding native dispatch result\'"}]}}'
            ),
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    coding = service.create_agent(
        {
            "name": "Coding Agent",
            "nickname": "Coding",
            "description": "runs native group dispatch tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
    )
    runner = TaskRunner(state, executor=executor)
    api = ChatAPI(runtime)
    try:
        created = api.create_group_session(name="Native Dispatch Group", participant_ids=[coding["agent_id"]])
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"
        assert created["session_context"]["participants"][1]["id"] == coding["agent_id"]

        sent = api.send_message("@主模型 请安排 Coding Respond with exactly 'Coding native dispatch result'")
        assert sent["ok"] is True
        await runner._execute_with_state(sent["task_id"])

        main_task = state.get_task(sent["task_id"])
        assert main_task is not None
        assert main_task.status == TaskStatus.COMPLETED
        main_link = service.get_task_run_link(sent["task_id"])
        main_run = service.get_run(main_link["run_id"])
        assert main_run["kind"] == "main_chat_run"
        assert main_run["status"] == "completed"

        dispatch_payload = api.get_messages()
        parent = next(
            message
            for message in dispatch_payload["messages"]
            if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
        )
        agent_message = next(
            message
            for message in dispatch_payload["messages"]
            if message["role"] == "assistant"
            and message["metadata"].get("sender", {}).get("nickname") == "Coding"
        )
        assert parent["metadata"]["group_dispatch_count"] == 1
        assert parent["metadata"]["group_dispatch_run_group_id"] == agent_message["metadata"]["run_group_id"]
        assert agent_message["metadata"]["runnable_id"] == coding["agent_id"]
        assert agent_message["metadata"]["delegated_by_task_id"] == sent["task_id"]
        assert agent_message["metadata"]["delegated_goal"] == "Respond with exactly 'Coding native dispatch result'"

        run_id = agent_message["metadata"]["run_id"]
        run = await _wait_for(
            lambda: (
                service.get_run(run_id)
                if service.get_run(run_id)["status"] in {"completed", "failed", "cancelled", "approval_required"}
                else None
            )
        )
        assert run["status"] == "completed"
        assert run["runnable_id"] == coding["agent_id"]
        assert run["result"] == "Coding native dispatch result"

        completed_agent = await _wait_for(
            lambda: next(
                (
                    message
                    for message in api.get_messages()["messages"]
                    if message["role"] == "assistant"
                    and "Coding native dispatch result" in str(message["content"] or "")
                ),
                None,
            )
        )
        assert completed_agent["metadata"]["run_id"] == run_id
        assert completed_agent["metadata"]["run_status"] == "completed"
        assert completed_agent["metadata"]["agent_report"] == "Coding native dispatch result"

        final_payload = api.get_messages()
        summary_message = next(
            message
            for message in final_payload["messages"]
            if message["metadata"].get("group_agent_summary_for_task_id") == sent["task_id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_message["status"] == "processing"
        assert summary_task is not None
        assert summary_task.chat_session_id == session.session_id
        assert "[Oha-Yachiyo 群组 Agent 汇总]" in summary_task.response_context
        assert "Coding：已完成" in summary_task.response_context
        assert "汇报：Coding native dispatch result" in summary_task.response_context

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "群组总结：Coding 已完成 Native 群聊派发验证。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["result"] == "群组总结：Coding 已完成 Native 群聊派发验证。"
        assert summary_run["run_id"] != main_run["run_id"]
        summary_event_types = [event["event_type"] for event in service.list_run_events(summary_run["run_id"])["events"]]
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types
        summary_assistant = next(
            message
            for message in store.load_messages(session.session_id, limit=20)
            if message.task_id == summary_task.task_id
        )
        assert summary_assistant.status == "completed"
        assert summary_assistant.content == "群组总结：Coding 已完成 Native 群聊派发验证。"
        assert "oha.group_dispatch" not in summary_assistant.content
        assert "<oha_group_dispatch>" not in summary_assistant.content

        settled_payload = api.get_messages()
        settled_parent = next(
            message
            for message in settled_payload["messages"]
            if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
        )
        assert "group_agent_summary_pending" not in settled_parent["metadata"]
        assert settled_parent["metadata"]["group_agent_summary_status"] == "completed"
        assert len(model_calls) == 3
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_direct_group_agent_summary_uses_native_runtime(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="direct-group-agent-summary-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        agent_runtime_service=service,
        activity_store=activity_store,
    )
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组直接 Agent 汇总]" in last_content:
            assert "Design：已完成" in last_content
            assert "汇报：Design native direct result" in last_content
            assert "不要再派发新的 Agent 任务" in last_content
            return {"role": "assistant", "content": "主模型整理：Design 的直接执行结果已归档。"}
        assert "# Agent\nName: Design Agent" in last_content
        assert "# User Goal\nRespond with exactly 'Design native direct result'." in last_content
        assert "[Oha-Yachiyo 群组执行约定]" in last_content
        assert "你在群内身份是：Design" in last_content
        return {"role": "assistant", "content": "Design native direct result"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    design = service.create_agent(
        {
            "name": "Design Agent",
            "nickname": "Design",
            "description": "runs native direct group summary tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    api = ChatAPI(runtime)
    try:
        created = api.create_group_session(name="Native Direct Group", participant_ids=[design["agent_id"]])
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"

        sent = api.send_message("Respond with exactly 'Design native direct result'.", runnable_id=design["agent_id"])
        assert sent["ok"] is True
        assert sent["agent_run_id"]

        run = await _wait_for(
            lambda: (
                service.get_run(sent["agent_run_id"])
                if service.get_run(sent["agent_run_id"])["status"] in {"completed", "failed", "cancelled", "approval_required"}
                else None
            )
        )
        assert run["kind"] == "agent_run"
        assert run["status"] == "completed"
        assert run["result"] == "Design native direct result"

        completed_agent = await _wait_for(
            lambda: next(
                (
                    message
                    for message in api.get_messages()["messages"]
                    if message["role"] == "assistant"
                    and message["metadata"].get("sender", {}).get("nickname") == "Design"
                    and message["metadata"].get("run_status") == "completed"
                ),
                None,
            )
        )
        assert completed_agent["metadata"]["agent_report"] == "Design native direct result"
        assert completed_agent["metadata"]["group_agent_summary_pending"] is True

        summary_message = next(
            message
            for message in api.get_messages()["messages"]
            if message["metadata"].get("group_direct_agent_summary_for_message_id") == completed_agent["id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_task is not None
        assert summary_task.chat_session_id == session.session_id
        assert "[Oha-Yachiyo 群组直接 Agent 汇总]" in summary_task.response_context
        assert "Design：已完成" in summary_task.response_context
        assert "汇报：Design native direct result" in summary_task.response_context

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "主模型整理：Design 的直接执行结果已归档。"
        summary_run = service.get_run(service.get_task_run_link(summary_task.task_id)["run_id"])
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["result"] == "主模型整理：Design 的直接执行结果已归档。"
        summary_event_types = [event["event_type"] for event in service.list_run_events(summary_run["run_id"])["events"]]
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        final_payload = api.get_messages()
        final_summary = next(
            message
            for message in final_payload["messages"]
            if message["task_id"] == summary_task.task_id
        )
        final_agent = next(
            message
            for message in final_payload["messages"]
            if message["id"] == completed_agent["id"]
        )
        assert final_summary["status"] == "completed"
        assert final_summary["content"] == "主模型整理：Design 的直接执行结果已归档。"
        assert final_agent["metadata"]["group_agent_summary_pending"] is False
        assert final_agent["metadata"]["group_agent_summary_status"] == "completed"
        assert len(model_calls) == 2
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_direct_group_agent_rejected_summary_uses_native_runtime(tmp_path, monkeypatch):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="direct-group-agent-rejected-summary-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        agent_runtime_service=service,
        activity_store=activity_store,
    )
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if any(
            (tool.get("function") or {}).get("name") == MODEL_INTENT_PLANNING_TOOL_NAME
            for tool in tools or []
        ):
            proposal = {
                "intent_kind": "code_task",
                "planning_goal": "请执行终端命令 `printf should-not-run`",
                "action_evidence": "执行终端命令 `printf should-not-run`",
            }
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_intent",
                    "type": "function",
                    "function": {
                        "name": MODEL_INTENT_PLANNING_TOOL_NAME,
                        "arguments": json.dumps(proposal, ensure_ascii=False),
                    },
                }],
            }
        if "[Oha-Yachiyo 群组直接 Agent 汇总]" in last_content:
            assert "Design：已取消" in last_content
            assert "汇报：工具审批已拒绝：Rejected by user" in last_content
            assert "不要再派发新的 Agent 任务" in last_content
            return {"role": "assistant", "content": "主模型整理：Design 的审批拒绝已告知用户。"}
        context = "\n".join(str(message.get("content") or "") for message in messages)
        assert "# Agent\nName: Design Agent" in context
        assert "# User Goal\n请执行终端命令 `printf should-not-run`" in context
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps(
                            {"command": "printf should-not-run", "timeout_seconds": 30},
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: _FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    design = service.create_agent(
        {
            "name": "Design Agent",
            "nickname": "Design",
            "description": "runs native rejected direct group summary tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
            "tool_policy": {
                "allowed_tools": ["terminal.run"],
                "approval_required": {"terminal.run": True},
            },
            "workspace_policy": {
                "default_workdir": str(tmp_path),
                "readable_scopes": ["."],
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    api = ChatAPI(runtime)
    try:
        created = api.create_group_session(name="Native Direct Reject Group", participant_ids=[design["agent_id"]])
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"

        sent = api.send_message("请执行终端命令 `printf should-not-run`", runnable_id=design["agent_id"])
        assert sent["ok"] is True
        assert sent["agent_run_id"]

        waiting = await _wait_for(
            lambda: (
                service.get_run(sent["agent_run_id"])
                if service.get_run(sent["agent_run_id"])["status"] in {"approval_required", "failed", "cancelled"}
                else None
            )
        )
        assert waiting["status"] == "approval_required", waiting.get("result")
        assert waiting["kind"] == "agent_run"
        assert waiting["pending_approval"]["tool"] == "terminal.run"

        rejected = await asyncio.to_thread(service.reject_run_approval, waiting["run_id"], "Rejected by user")
        assert rejected["status"] == "cancelled"
        assert rejected["result"] == "工具审批已拒绝：Rejected by user"
        rejected_event_types = [event["event_type"] for event in service.list_run_events(waiting["run_id"])["events"]]
        assert "agent.tool.approval_required" in rejected_event_types
        assert "agent.tool.approval_rejected" in rejected_event_types

        rejected_agent = await _wait_for(
            lambda: next(
                (
                    message
                    for message in api.get_messages()["messages"]
                    if message["role"] == "assistant"
                    and message["metadata"].get("sender", {}).get("nickname") == "Design"
                    and message["metadata"].get("run_status") == "cancelled"
                ),
                None,
            )
        )
        assert rejected_agent["status"] == "failed"
        assert rejected_agent["metadata"]["agent_report"] == "工具审批已拒绝：Rejected by user"
        assert rejected_agent["metadata"]["group_agent_summary_pending"] is True

        summary_message = next(
            message
            for message in api.get_messages()["messages"]
            if message["metadata"].get("group_direct_agent_summary_for_message_id") == rejected_agent["id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_task is not None
        assert summary_task.chat_session_id == session.session_id
        assert "[Oha-Yachiyo 群组直接 Agent 汇总]" in summary_task.response_context
        assert "Design：已取消" in summary_task.response_context
        assert "汇报：工具审批已拒绝：Rejected by user" in summary_task.response_context

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "主模型整理：Design 的审批拒绝已告知用户。"
        summary_run = service.get_run(service.get_task_run_link(summary_task.task_id)["run_id"])
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["result"] == "主模型整理：Design 的审批拒绝已告知用户。"
        summary_event_types = [event["event_type"] for event in service.list_run_events(summary_run["run_id"])["events"]]
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        final_payload = api.get_messages()
        final_summary = next(
            message
            for message in final_payload["messages"]
            if message["task_id"] == summary_task.task_id
        )
        final_agent = next(
            message
            for message in final_payload["messages"]
            if message["id"] == rejected_agent["id"]
        )
        assert final_summary["status"] == "completed"
        assert final_summary["content"] == "主模型整理：Design 的审批拒绝已告知用户。"
        assert final_agent["metadata"]["group_agent_summary_pending"] is False
        assert final_agent["metadata"]["group_agent_summary_status"] == "completed"
        assert len(model_calls) == 2
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("image_route", ["chat", "vision_text"])
async def test_task_runner_main_chat_image_attachment_reaches_native_model(tmp_path, monkeypatch, image_route):
    from apps.bridge.routes import agents as agent_routes
    from apps.bridge.routes import runs as run_routes

    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:9999")
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="main-chat-image-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    captured_messages: list[list[dict]] = []
    image_bytes = b"fake-native-image"
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        captured_messages.append(messages)
        content = messages[-1]["content"]
        if image_route == "vision_text" and len(captured_messages) == 2:
            assert content == "看一下这张图\n\n[图片识别结果]\n这是一张测试图片。"
            assert tools in (None, [])
            return {"role": "assistant", "content": "这是一张测试图片。"}
        assert isinstance(content, list)
        expected_text = (
            "看一下这张图" if image_route == "chat"
            else "请准确分析这些图片，并输出供另一个对话模型继续回答用户请求的详细文字描述。"
        )
        assert content[0] == {"type": "text", "text": expected_text}
        image_parts = [part for part in content if part.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"] == data_url
        return {"role": "assistant", "content": "这是一张测试图片。"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.native_capabilities.get_native_image_input_capability",
        lambda: {"can_attach_images": True, "route": image_route, "profile_id": "profile_vision"},
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
    )
    runner = TaskRunner(state, executor=executor)
    api = ChatAPI(SimpleNamespace(state=state, chat_session=session, store=store))
    try:
        sent = api.send_message(
            "看一下这张图",
            attachments=[{"name": "screen.png", "data_url": data_url}],
            client_message_id="image-client-1",
        )

        assert sent["ok"] is True
        assert len(sent["attachments"]) == 1
        task = state.get_task(sent["task_id"])
        assert task is not None
        assert task.attachments[0]["kind"] == "image"
        await runner._execute_with_state(task.task_id)

        assert len(captured_messages) == (1 if image_route == "chat" else 2)
        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "这是一张测试图片。"
        messages = api.get_messages()["messages"]
        user_message = next(message for message in messages if message["role"] == "user")
        assistant_message = next(message for message in messages if message["role"] == "assistant")
        assert user_message["attachments"][0]["url"].startswith("http://127.0.0.1:9999/ui/chat/attachments/")
        assert "path" not in user_message["attachments"][0]
        assert assistant_message["content"] == "这是一张测试图片。"
        assert assistant_message["status"] == "completed"
        run = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
        event_types = [event["event_type"] for event in service.list_run_events(run["run_id"])["events"]]
        assert run["status"] == "completed"
        assert "task.linked" in event_types
        assert "model.output.completed" in event_types
        assert "run.completed" in event_types

        listed_runs = await agent_routes.list_runs(limit=20)
        detail = await agent_routes.get_any_run(run["run_id"])
        replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
        page = await run_routes.list_run_events(run["run_id"], after_sequence=1, limit=1)

        assert any(item["run_id"] == run["run_id"] for item in listed_runs["runs"])
        assert detail["run_id"] == run["run_id"]
        assert detail["kind"] == "main_chat_run"
        assert detail["status"] == "completed"
        assert detail["result"] == "这是一张测试图片。"
        assert any(event.get("event") == "run.completed" for event in detail["timeline"])
        public_sequences = [event["sequence"] for event in replay["events"]]
        assert public_sequences == sorted(set(public_sequences))
        internal_events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert [event["sequence"] for event in internal_events] == list(range(1, len(internal_events) + 1))
        assert any(event["event_type"] == "agent.input.image.bound" for event in internal_events)
        assert not any(event["event_type"] == "agent.input.image.bound" for event in replay["events"])
        assert [event["event_type"] for event in replay["events"]] == event_types
        assert page["after_sequence"] == 1
        assert page["limit"] == 1
        assert len(page["events"]) == 1
        assert page["events"][0]["sequence"] == 2
    finally:
        service.close()
        activity_store.close()
        store.close()


@pytest.mark.asyncio
async def test_task_runner_proactive_screenshot_task_uses_native_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:9999")
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    current_session = ChatSession(session_id="visible-chat-session")
    current_session.attach_store(store, load_existing=False)
    state = AppState()
    captured_messages: list[list[dict]] = []
    image_bytes = b"fake-proactive-screen"

    def fake_capture(target_path):
        target_path.write_bytes(image_bytes)
        return {
            "path": str(target_path),
            "mime_type": "image/png",
            "format": "png",
            "width": 120,
            "height": 80,
            "size": len(image_bytes),
        }

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        captured_messages.append(messages)
        content = messages[-1]["content"]
        assert isinstance(content, list)
        assert "主动桌面观察" in content[0]["text"]
        assert "详细对话框阅读" in content[0]["text"]
        image_parts = [part for part in content if part.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"] == (
            "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        )
        return {"role": "assistant", "content": "主动观察：建议先保存当前工作。"}

    image_capability = {"can_attach_images": True, "route": "chat"}
    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(proactive_mod, "capture_screenshot_to_file", fake_capture)
    monkeypatch.setattr(proactive_mod, "get_native_image_input_capability", lambda: image_capability)
    monkeypatch.setattr(
        "apps.shell.native_capabilities.get_native_image_input_capability",
        lambda: image_capability,
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=current_session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    mode_config = Live2DModeConfig(
        proactive_enabled=True,
        proactive_desktop_watch_enabled=True,
        proactive_interval_seconds=300,
    )
    runtime = SimpleNamespace(
        state=state,
        config=SimpleNamespace(live2d_mode=mode_config),
        chat_session=current_session,
        store=store,
        task_runner=runner,
        is_native_agent_ready=lambda: True,
        get_status=lambda: {"native_agent": {"limited_tools": []}},
    )
    proactive_service = ProactiveDesktopService(runtime, mode_config)
    try:
        scheduled = proactive_service.trigger_now()

        assert scheduled["ok"] is True
        assert scheduled["status"] == "scheduled"
        assert scheduled["session_id"] == PROACTIVE_CHAT_SESSION_ID
        task = state.get_task(scheduled["task_id"])
        assert task is not None
        assert task.task_type == TaskType.SCREENSHOT
        assert task.chat_session_id == PROACTIVE_CHAT_SESSION_ID
        assert task.attachments[0]["source"] == "proactive_desktop_watch"
        assert current_session.get_messages(limit=0) == []

        await runner._execute_with_state(task.task_id)

        assert len(captured_messages) == 1
        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "主动观察：建议先保存当前工作。"

        proactive_session = ChatSession(session_id=PROACTIVE_CHAT_SESSION_ID)
        proactive_session.attach_store(store, load_existing=True)
        assistant = proactive_session.get_assistant_message_for_task(task.task_id)
        assert assistant is not None
        assert assistant.content == "主动观察：建议先保存当前工作。"
        assert getattr(assistant.status, "value", assistant.status) == "completed"
        assert current_session.get_messages(limit=0) == []

        run = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
        assert run["kind"] == "main_chat_run"
        assert run["session_id"] == PROACTIVE_CHAT_SESSION_ID
        assert run["status"] == "completed"
        assert run["result"] == "主动观察：建议先保存当前工作。"
        event_types = [event["event_type"] for event in service.list_run_events(run["run_id"])["events"]]
        assert "task.linked" in event_types
        assert event_types.count("model.output.completed") == 1
        assert "run.completed" in event_types
    finally:
        service.close()
        activity_store.close()
        store.close()
