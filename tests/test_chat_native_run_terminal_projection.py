"""A durable approval result must settle its linked in-memory chat Task."""

from types import SimpleNamespace

import pytest

from apps.core.chat_session import ChatSession, MessageStatus
from apps.core.state import AppState
from apps.shell.chat_api import ChatAPI
from packages.protocol.enums import TaskStatus


def projection_fixture(monkeypatch, status):
    state = AppState()
    session = ChatSession(session_id="projection-session")
    task = state.create_task("Run printf projection", chat_session_id=session.session_id)
    state.update_task_status(task.task_id, TaskStatus.RUNNING)
    session.upsert_assistant_message(
        task_id=task.task_id, content="Awaiting approval", status=MessageStatus.PROCESSING,
        metadata={"run_id": "projection-run", "run_status": "approval_required",
                  "pending_approval": {"approval_id": "approval-1"},
                  "run_progress_title": "Awaiting approval", "preserved": True},
    )
    api = ChatAPI(SimpleNamespace(state=state, chat_session=session))
    run = {"run_id": "projection-run", "kind": "main_chat_run", "task_id": task.task_id,
           "status": status, "user_goal": task.description, "result": "Native result"}
    monkeypatch.setattr(api, "_linked_main_chat_run_for_task", lambda _task_id: run)
    monkeypatch.setattr(api, "_session_context", lambda: {})
    return api, state, session, task, run


@pytest.mark.parametrize("status,expected", [
    ("completed", TaskStatus.COMPLETED), ("failed", TaskStatus.FAILED),
    ("cancelled", TaskStatus.CANCELLED),
])
def test_terminal_native_run_settles_same_task_and_clears_approval(monkeypatch, status, expected):
    api, state, session, task, run = projection_fixture(monkeypatch, status)
    assert api._sync_main_chat_run_projection_for_running_task(task) is True
    assert state.get_task(task.task_id).status == expected
    assistant = session.get_assistant_message_for_task(task.task_id)
    assert assistant.status == (MessageStatus.COMPLETED if status == "completed" else MessageStatus.FAILED)
    assert assistant.metadata["run_status"] == status
    assert assistant.metadata["pending_approval"] == {}
    assert assistant.metadata["preserved"] is True
    assert "run_progress_title" not in assistant.metadata
    assert state.get_task(task.task_id).result == ("Native result" if status == "completed" else None)
    assert state.get_task(task.task_id).error == (None if status == "completed" else "任务已取消" if status == "cancelled" else "Native result")
    before = assistant.content
    assert api._sync_main_chat_run_projection_for_running_task(task) is False
    assert session.get_assistant_message_for_task(task.task_id).content == before


@pytest.mark.parametrize("field,value", [("task_id", "other-task"), ("user_goal", "Run rm -rf other")])
def test_terminal_projection_rejects_mismatched_run_binding(monkeypatch, field, value):
    api, state, session, task, run = projection_fixture(monkeypatch, "completed")
    run[field] = value
    assert api._sync_main_chat_run_projection_for_running_task(task) is False
    assert state.get_task(task.task_id).status == TaskStatus.RUNNING
    assert session.get_assistant_message_for_task(task.task_id).metadata["pending_approval"]


def test_terminal_run_cannot_overwrite_locally_cancelled_task(monkeypatch):
    api, state, session, task, run = projection_fixture(monkeypatch, "completed")
    state.cancel_task(task.task_id)
    assert api._sync_main_chat_run_projection_for_running_task(task) is False
    assert state.get_task(task.task_id).status == TaskStatus.CANCELLED
