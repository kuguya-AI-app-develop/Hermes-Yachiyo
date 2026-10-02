"""Native observation capabilities never survive an approval checkpoint."""

import json

import pytest

from apps.shell.agent.runtime.tool_approvals import (
    ToolPendingApprovalBuilder,
    approval_request_fingerprint,
    ensure_pending_approval_request_fingerprint,
)

_PRIVATE_KEYS = (
    "_runtime_private_select_all_copy",
    "_runtime_private_clipboard_target",
    "_runtime_private_typed_observation",
    "_runtime_private_copy_observation",
    "_runtime_private_clipboard_observation",
    "_runtime_private_typed_raw_result",
    "_runtime_private_clipboard_source_request",
    "_runtime_private_prepared_submit_context",
    "_runtime_private_exact_submit_receipt",
    "_runtime_private_exact_file_readback",
)


def _assert_no_private_values(value):
    if isinstance(value, dict):
        assert not any(key.startswith("_runtime_private_") for key in value)
        for item in value.values():
            _assert_no_private_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_private_values(item)


@pytest.mark.parametrize("key", _PRIVATE_KEYS)
def test_approval_checkpoint_and_fingerprint_ignore_process_private_capabilities(key):
    clean = {"tool": "desktop.submit_foreground", "input": {"action": "send"}}
    private = {**clean, key: {"_authority": object(), "raw": "secret\r\n\tbytes"}}
    pending = ToolPendingApprovalBuilder(
        approval_id_factory=lambda: "approval", now=lambda: "time",
    ).build(
        private,
        messages=[{"role": "user", "content": "send", "nested": private}],
        next_iteration=2,
        remaining_tool_requests=[{"tool": "desktop.ui_elements", key: object()}],
    )
    _assert_no_private_values(pending)
    assert "secret" not in json.dumps(pending)
    assert key in private  # The live Runner still owns its capability.
    assert approval_request_fingerprint(private) == approval_request_fingerprint(clean)
    assert ensure_pending_approval_request_fingerprint(pending)


def test_actual_native_typed_pending_approval_and_events_are_json_safe(tmp_path, monkeypatch):
    from tests.test_native_typed_draft_target import _fixture, _send

    bridge, service, store, state = _fixture(tmp_path, monkeypatch, native_shape=True)
    try:
        response = _send(bridge)
        run_id = service.get_task_run_link(response["task_id"])["run_id"]
        run = service.get_run(run_id)
        assert run["status"] == "approval_required"
        assert state["sent"] == 0
        assert ("type", "你好") in state["calls"]
        for document in (
            run["pending_approval"], run["timeline"],
            service.list_run_events(run_id, include_internal=True),
            service.list_run_events(run_id),
        ):
            _assert_no_private_values(document)
            json.dumps(document)
        approved = service.approve_run_approval(run_id, run["pending_approval"]["approval_id"])
        assert approved["status"] == "completed"
        assert state["sent"] == 1
    finally:
        service.close()
        store.close()
