"""Prepared submits recover only exact native bytes under the winning approval."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime import tool_execution as te
from apps.shell.agent.runtime.prepared_submit_resume_observation import (
    consume_actual_prepared_submit_observation,
    observe_actual_prepared_submit_target,
)
from apps.shell.agent.runtime.tool_approvals import (
    APPROVAL_REQUEST_FINGERPRINT_KEY,
    ensure_pending_approval_request_fingerprint,
)
from apps.shell.agent.tools import desktop
from tests.test_native_typed_draft_target import _fixture, _send


@pytest.mark.parametrize("phase", ["recover", "atomic_pre"])
@pytest.mark.parametrize(
    "mutation",
    [
        "bytes",
        "window",
        "pid",
        "app",
        "identity",
        "permission",
        "approval",
        "truncated",
        "missing_focus",
        "composer",
        "recipient",
    ],
)
def test_approved_send_refuses_changed_or_unobservable_native_target(
    tmp_path,
    monkeypatch,
    phase,
    mutation,
):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        pending = service.get_run(run_id)["pending_approval"]
        original_ui = desktop.ui_elements
        calls = []

        def changed_ui(**kwargs):
            snapshot = deepcopy(original_ui(**kwargs))
            calls.append("observe")
            if phase == "atomic_pre" and len(calls) == 1:
                return snapshot
            data = snapshot["data"]
            focused = data["focused_element"]
            if mutation == "bytes":
                focused["value"] += " "
            elif mutation == "window":
                data["window_id"] += 1
            elif mutation == "pid":
                data["pid"] += 1
            elif mutation == "app":
                data["app_name"] = "Slack"
            elif mutation == "identity":
                focused["name"] = "Other Message"
            elif mutation == "permission":
                snapshot["permission_error"] = True
            elif mutation == "approval":
                snapshot["approval_required"] = True
            elif mutation == "truncated":
                data["truncated"] = True
            elif mutation == "missing_focus":
                data.pop("focused_element")
            elif mutation == "composer":
                focused["name"] = "Search"
            elif mutation == "recipient":
                data["elements"][0]["value"] = "李四"
                data["elements"][0]["name"] = "李四"
            return snapshot

        monkeypatch.setattr(desktop, "ui_elements", changed_ui)
        approved = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 0
        assert approved["status"] == "failed"
        assert calls
    finally:
        service.close()
        store.close()


def test_version_one_receipt_keeps_strict_legacy_approved_send(tmp_path, monkeypatch):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        pending = service.runs.pending_approval_private(run_id)
        receipt = pending["tool_request"][te.RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY]
        assert receipt["version"] == 2 and "content" not in receipt
        receipt["version"] = 1
        receipt["content"] = state["body"]
        receipt.pop("content_length")
        pending.pop(APPROVAL_REQUEST_FINGERPRINT_KEY)
        ensure_pending_approval_request_fingerprint(pending)
        service._update_run(run_id, pending_approval=pending)
        approved = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 1
        assert approved["status"] == "completed"
    finally:
        service.close()
        store.close()


def test_hash_only_private_receipt_contains_no_raw_secret_or_public_capability(
    tmp_path, monkeypatch
):
    from apps.shell.agent.runtime.tool_approvals import approval_request_fingerprint
    from packages.security import sanitize_sensitive_value

    context = {"_authority": te._RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY}
    for key in (
        "run_id",
        "plan_id",
        "source_step_id",
        "source_request_id",
        "source_tool_call_id",
        "source_verifier_step_id",
        "source_verifier_request_id",
        "source_verifier_tool_call_id",
        "provider_kind",
        "provider_id",
        "target_app_name",
        "submit_step_id",
        "submit_request_id",
        "submit_tool_call_id",
    ):
        context[key] = "identity"
    import hashlib

    content = "api_key=sk-synthetic-private-content123456\r\n  literal  "
    context.update(content=content, content_sha256=hashlib.sha256(content.encode()).hexdigest())
    receipt = te.persisted_prepared_submit_receipt_from_private_context(context)
    assert receipt["version"] == 2 and "content" not in receipt
    request = {
        "tool": "desktop.submit_foreground",
        "input": {"action": "send"},
        te.RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY: receipt,
    }
    redacted = sanitize_sensitive_value(request, max_depth=12, text_limit=0, max_items=100)
    assert approval_request_fingerprint(request) == approval_request_fingerprint(redacted)
    assert content not in repr(redacted)


def test_public_observation_or_foreign_broker_cannot_restore_literal_content():
    request = {"tool": "desktop.submit_foreground"}
    assert (
        consume_actual_prepared_submit_observation(
            {"ok": True, "data": {}}, request=request, run_id="run"
        )
        == {}
    )
    assert (
        observe_actual_prepared_submit_target(
            object(),
            request=request,
            run_id="run",
            allowed_tools=["desktop.ui_elements"],
            budget=None,
            assert_active=lambda: pytest.fail("No authority to claim"),
        )
        is None
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "version",
        "length_type",
        "length",
        "hash",
        "secret_content",
        "provider",
        "recipient",
        "composer",
        "source_call",
        "source_step",
        "verifier_call",
        "duplicate_receipt",
        "receipt_window_shape",
        "receipt_target_shape",
        "goal_run",
    ],
)
def test_corrupt_or_foreign_hash_only_lineage_is_rejected_before_any_live_read(
    tmp_path,
    monkeypatch,
    mutation,
):
    from dataclasses import replace

    from apps.shell.agent.runtime.goal_runtime import runtime_goal_contract

    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        run = service.get_run(run_id)
        pending = service.runs.pending_approval_private(run_id)
        request = deepcopy(pending["tool_request"])
        receipt = request[te.RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY]
        timeline = deepcopy(run["timeline"])
        contract = runtime_goal_contract(
            run_id=run_id,
            original_goal=run["user_goal"],
            goal_contract_template=None,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=[],
            timeline=timeline,
        )
        if mutation == "version":
            receipt["version"] = True
        elif mutation == "length_type":
            receipt["content_length"] = True
        elif mutation == "length":
            receipt["content_length"] += 1
        elif mutation == "hash":
            receipt["content_sha256"] = "0" * 64
        elif mutation == "secret_content":
            receipt["content"] = "api_key=sk-synthetic-injected-secret123456"
        elif mutation == "provider":
            receipt["provider_id"] = "foreign"
        elif mutation == "recipient":
            receipt["target_recipient"] = "李四"
        elif mutation == "composer":
            receipt["composer_required"] = False
        elif mutation == "source_call":
            receipt["source_tool_call_id"] = "foreign"
        elif mutation == "source_step":
            receipt["source_step_id"] = "foreign"
        elif mutation == "verifier_call":
            receipt["source_verifier_tool_call_id"] = "foreign"
        elif mutation in {"duplicate_receipt", "receipt_window_shape", "receipt_target_shape"}:
            event = next(
                e
                for e in timeline
                if e.get("tool_call_id") == receipt["source_verifier_tool_call_id"]
                and e.get("source") == "runtime_native_postcondition_receipt"
                and e.get("event") == "agent.tool.call"
                and e.get("execution_mode") == "trusted_observation_receipt_projection"
            )
            if mutation == "duplicate_receipt":
                timeline.append(deepcopy(event))
            elif mutation == "receipt_window_shape":
                event["result"]["target_window"] = "malformed"
            else:
                event["result"]["target_ui_identity"] = ["malformed"]
        elif mutation == "goal_run":
            contract = replace(contract, run_id="foreign")
        restored = te.rehydrate_private_prepared_submit_context(
            request,
            timeline,
            run_id=run_id,
            goal_contract=contract,
            observe_private_target=lambda: pytest.fail("Unbound lineage must not read UI"),
        )
        assert restored == {}
        assert state["sent"] == 0
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize(
    "field",
    [
        "tool",
        "run_id",
        "plan_id",
        "decision_id",
        "tool_plan_id",
        "step_id",
        "request_id",
        "tool_call_id",
    ],
)
def test_local_resume_observation_tokens_wipe_raw_bytes_on_success_or_scope_failure(field):
    from apps.shell.agent.runtime.prepared_submit_resume_observation import (
        _LocalPreparedSubmitObservation,
    )

    request = {
        key: "identity"
        for key in (
            "tool",
            "run_id",
            "plan_id",
            "decision_id",
            "tool_plan_id",
            "step_id",
            "request_id",
            "tool_call_id",
        )
    }
    raw = {"ok": True, "data": {"focused_element": {"value": " synthetic\r\n "}}}
    token = _LocalPreparedSubmitObservation(request, "identity", raw)
    changed = {**request, field: "foreign"}
    assert (
        consume_actual_prepared_submit_observation(token, request=changed, run_id="identity") == {}
    )
    assert token.result == {}
    assert (
        consume_actual_prepared_submit_observation(token, request=request, run_id="identity") == {}
    )
    valid = _LocalPreparedSubmitObservation(request, "identity", raw)
    observed = consume_actual_prepared_submit_observation(valid, request=request, run_id="identity")
    assert observed["data"]["focused_element"]["value"] == " synthetic\r\n "
    assert valid.result == {}
    assert (
        consume_actual_prepared_submit_observation(valid, request=request, run_id="identity") == {}
    )


def test_cancellation_while_reading_the_approved_target_prevents_send(tmp_path, monkeypatch):
    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        pending = service.get_run(run_id)["pending_approval"]
        original_ui = desktop.ui_elements

        def cancelled_ui(**kwargs):
            snapshot = original_ui(**kwargs)
            service.cancel_run(run_id)
            return snapshot

        monkeypatch.setattr(desktop, "ui_elements", cancelled_ui)
        updated = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == 0
        assert updated["status"] == "cancelled"
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("changed_body", [False, True])
def test_hash_only_resume_reacquires_native_bytes_in_a_fresh_runtime_instance(
    tmp_path, monkeypatch, changed_body
):
    from apps.shell.agent_runtime import AgentRuntimeService
    from apps.shell.credential_store import MemoryCredentialStore

    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    original_service = service
    try:
        result = _send(bridge)
        run_id = service.get_task_run_link(result["task_id"])["run_id"]
        pending = service.get_run(run_id)["pending_approval"]
        service = AgentRuntimeService(
            db_path=tmp_path / "runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        if changed_body:
            state["body"] += " "
        updated = service.approve_run_approval(run_id, pending["approval_id"])
        assert state["sent"] == (0 if changed_body else 1)
        assert updated["status"] == ("failed" if changed_body else "completed")
    finally:
        service.close()
        original_service.close()
        store.close()


def test_secret_clipboard_approval_persists_only_hash_facts_and_sends_exact_native_content(
    tmp_path,
    monkeypatch,
):
    import json

    from tests import test_chat_api
    from tests.test_native_clipboard_paste_target import (
        test_real_main_chat_paste_reads_exact_source_and_waits_before_actual_approved_send,
    )

    original_factory = test_chat_api._make_agent_runtime_service
    secret = "api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789\r\n  literal  "
    checked = []

    def service_with_storage_check(*args, **kwargs):
        service = original_factory(*args, **kwargs)
        approve = service.approve_run_approval

        def inspect_then_approve(run_id, *args, **kwargs):
            persisted = service.runs.pending_approval_private(run_id)
            serialized = service.runs.pending_approval_json(run_id)
            receipt = persisted["tool_request"][te.RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY]
            assert receipt["version"] == 2
            assert "content" not in receipt and receipt["content_length"] == len(secret)
            assert (
                secret not in serialized
                and "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in serialized
            )
            public = json.dumps(service.list_run_events(run_id)["events"])
            assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in public
            checked.append(True)
            return approve(run_id, *args, **kwargs)

        monkeypatch.setattr(service, "approve_run_approval", inspect_then_approve)
        return service

    monkeypatch.setattr(test_chat_api, "_make_agent_runtime_service", service_with_storage_check)
    test_real_main_chat_paste_reads_exact_source_and_waits_before_actual_approved_send(
        tmp_path,
        monkeypatch,
        secret,
    )
    assert checked == [True]
