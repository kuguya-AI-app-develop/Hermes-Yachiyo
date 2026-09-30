"""Regression coverage for approved workspace edits using zero-context diffs."""

import difflib
import hashlib

import pytest

from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.tools.workspace import _apply_single_file_unified_diff


def make_patch(before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/example.txt",
            tofile="b/example.txt",
            n=0,
        )
    )


@pytest.mark.parametrize(
    "before,after",
    [
        ("first\nsecond\nthird\n", "inserted\nfirst\nsecond\nthird\n"),
        ("first\nsecond\nthird\n", "first\nsecond\ninserted\nthird\n"),
        ("first\nsecond\nthird\n", "first\nsecond\nthird\ninserted\n"),
        ("", "inserted\n"),
        ("first\nsecond\nthird\n", "first\ninserted\nsecond\nthird\nlast\n"),
        ("first\nsecond\nthird\n", "first\ninserted\nsecond\nTHIRD\n"),
    ],
)
def test_approved_zero_context_patch_writes_exact_requested_content(tmp_path, before, after):
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    target = workdir / "example.txt"
    target.write_text(before, encoding="utf-8")
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    result = broker.workspace_write_patch(
        "example.txt",
        patch=make_patch(before, after),
        expected_sha256=hashlib.sha256(before.encode()).hexdigest(),
        approved=True,
    )

    assert result["ok"] is True
    assert result["postcondition_verified"] is True
    assert target.read_text(encoding="utf-8") == after
    assert result["sha256_after"] == hashlib.sha256(after.encode()).hexdigest()


def test_zero_context_insertion_rejects_position_beyond_eof():
    patch = "--- a/example.txt\n+++ b/example.txt\n@@ -9,0 +10 @@\n+inserted\n"

    with pytest.raises(AgentRuntimeError, match="范围"):
        _apply_single_file_unified_diff("first\n", patch, expected_path="example.txt")
