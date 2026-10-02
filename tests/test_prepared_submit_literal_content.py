"""Approval receipts keep exact content facts without persisting literal text."""

import hashlib

import pytest

from apps.shell.agent.runtime import tool_execution as te


@pytest.mark.parametrize(
    "content",
    [
        "  literal\n\t汉字🤖 tail  ",
        "  literal\r\n\tbytes  ",
        r"literal\n\tquoted\"bytes",
        "\t\n  ",
    ],
)
def test_persisted_prepared_submit_receipt_preserves_literal_content(content):
    context = {
        key: " identity "
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
        )
    }
    context.update(
        {
            "_authority": te._RUNTIME_PRIVATE_PREPARED_SUBMIT_AUTHORITY,
            "content": content,
            "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
        }
    )
    receipt = te.persisted_prepared_submit_receipt_from_private_context(context)
    assert "content" not in receipt
    assert receipt["version"] == 2
    assert receipt["content_length"] == len(content)
    assert receipt["content_sha256"] == hashlib.sha256(content.encode()).hexdigest()
    assert receipt["run_id"] == "identity"
