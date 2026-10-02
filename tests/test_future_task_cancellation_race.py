"""Deterministic cancellation and exclusive-claim coverage without executing tools."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.yachiyo_agent.future_tasks import future_task_snapshot_from_payload


def make_service(tmp_path):
    return AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
    )


def schedule(service, cron):
    return service.schedule_future_task({
        "title": "Synthetic cancellation test",
        "prompt": "Synthetic task; do not execute tools",
        "runnable_id": "builtin:yachiyo-main",
        "delay_seconds": 0,
        "cron": cron,
    })["future_task"]["future_task_id"]


@pytest.mark.parametrize("cron", ["", "every 1 minutes"])
@pytest.mark.parametrize("boundary", ["before_claim", "before_execution", "executing", "completion"])
@pytest.mark.parametrize("fails", [False, True])
def test_cancellation_wins_at_each_trigger_boundary(tmp_path, monkeypatch, cron, boundary, fails):
    service = make_service(tmp_path)
    task_id = schedule(service, cron)
    scheduler = service.future_task_scheduler
    reached = Event()
    proceed = Event()
    calls = []
    method = {
        "before_claim": "_claim_future_task",
        "before_execution": "_begin_future_task_execution",
        "completion": "_persist_future_task_trigger",
    }.get(boundary)
    if method:
        original = getattr(scheduler, method)

        def paused(*args, **kwargs):
            reached.set()
            assert proceed.wait(10)
            return original(*args, **kwargs)

        monkeypatch.setattr(scheduler, method, paused)

    def create_run(**kwargs):
        calls.append(kwargs)
        if boundary == "executing":
            reached.set()
            assert proceed.wait(10)
        if fails:
            raise RuntimeError("synthetic execution failure")
        return {"run_id": "synthetic-run"}

    monkeypatch.setattr(scheduler, "_create_run_for_runnable", create_run)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(service.trigger_due_future_tasks)
            assert reached.wait(10)
            cancelled = service.cancel_future_task(task_id, reason="user cancelled")
            snapshot = future_task_snapshot_from_payload(cancelled["future_task"])
            assert snapshot.status == "cancelled"
            assert snapshot.trigger_in_flight is (boundary in {"executing", "completion"})
            cancelled_at = snapshot.cancelled_at
            proceed.set()
            result = pending.result(timeout=10)
        item = service.list_future_tasks()["future_tasks"][0]
        assert item["status"] == "cancelled"
        assert item["cancelled_at"] == cancelled_at
        assert item["error"] == "user cancelled"
        assert item["trigger_in_flight"] is False
        assert len(calls) == int(boundary in {"executing", "completion"})
        assert item["run_count"] == int(bool(calls) and not fails)
        assert service.trigger_due_future_tasks(now_epoch=time.time() + 3600)["triggered"] == []
        events = service._conn.execute(
            "SELECT action, payload_json FROM future_task_events WHERE future_task_id=? ORDER BY rowid",
            (task_id,),
        ).fetchall()
        cancellations = [json.loads(row["payload_json"]) for row in events if row["action"] == "future_task.cancel"]
        assert len(cancellations) == 1
        assert cancellations[0]["current_trigger_continues"] is (boundary in {"executing", "completion"})
        if result["triggered"]:
            assert result["triggered"][0]["future_task"]["status"] == "cancelled"
    finally:
        proceed.set()
        service.close()
    restarted = make_service(tmp_path)
    try:
        assert restarted.list_future_tasks()["future_tasks"][0]["status"] == "cancelled"
        assert restarted.trigger_due_future_tasks(now_epoch=time.time() + 3600)["triggered"] == []
    finally:
        restarted.close()


@pytest.mark.parametrize("cron", ["", "every 1 minutes"])
def test_two_trigger_connections_only_create_one_execution(tmp_path, monkeypatch, cron):
    first = make_service(tmp_path)
    second = make_service(tmp_path)
    task_id = schedule(first, cron)
    entered = Event()
    proceed = Event()
    calls = []

    def create_run(**kwargs):
        calls.append(kwargs)
        entered.set()
        assert proceed.wait(10)
        return {"run_id": "synthetic-exclusive-run"}

    for service in (first, second):
        monkeypatch.setattr(service.future_task_scheduler, "_create_run_for_runnable", create_run)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first_pending = pool.submit(first.trigger_due_future_tasks)
            assert entered.wait(10)
            second_result = pool.submit(second.trigger_due_future_tasks).result(timeout=10)
            assert second_result["triggered"] == []
            proceed.set()
            first_result = first_pending.result(timeout=10)
        assert len(calls) == 1
        assert first_result["triggered"][0]["future_task"]["run_count"] == 1
        assert first_result["triggered"][0]["future_task"]["future_task_id"] == task_id
    finally:
        proceed.set()
        second.close()
        first.close()


def test_existing_database_migrates_claim_fields_without_restoring_cancelled_task(tmp_path):
    service = make_service(tmp_path)
    task_id = schedule(service, "every 1 minutes")
    service.cancel_future_task(task_id, reason="user cancelled")
    service.close()
    import sqlite3
    with sqlite3.connect(tmp_path / "agent-runtime.db") as conn:
        conn.execute("ALTER TABLE future_tasks DROP COLUMN trigger_claim_id")
        conn.execute("ALTER TABLE future_tasks DROP COLUMN trigger_in_flight")
    restarted = make_service(tmp_path)
    try:
        item = restarted.list_future_tasks()["future_tasks"][0]
        assert item["status"] == "cancelled"
        assert item["trigger_in_flight"] is False
        assert item["error"] == "user cancelled"
        assert restarted.trigger_due_future_tasks(now_epoch=time.time() + 3600)["triggered"] == []
    finally:
        restarted.close()
