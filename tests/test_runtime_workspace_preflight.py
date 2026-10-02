from pathlib import Path

import pytest

from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.runtime.tool_execution import _pre_execution_approval_required_result


def _request(path, **extra):
    return {"tool": "workspace.write_patch", "approval_required": True,
            "input": {"path": path, "patch": "@@ -1 +1 @@\n-before\n+after\n", **extra}}


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink", "write_scope"])
def test_planned_patch_rejects_workspace_escape_before_approval(tmp_path, escape):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("before\n")
    writable = ["."]
    if escape == "parent": path = "../outside.txt"
    elif escape == "absolute": path = str(outside)
    elif escape == "symlink":
        (workdir / "link.txt").symlink_to(outside)
        path = "link.txt"
    else:
        (workdir / "inaccessible.txt").write_text("before\n")
        path, writable = "inaccessible.txt", ["permitted"]
    broker = ToolBroker({"default_workdir": str(workdir), "readable_scopes": ["."],
                         "writable_scopes": writable}, tmp_path / "artifacts")
    with pytest.raises(AgentRuntimeError):
        _pre_execution_approval_required_result("workspace.write_patch", _request(path),
                                                broker, approved=False)
    assert outside.read_text() == "before\n"
    assert not (workdir / "permitted").exists()


def test_valid_patch_preflight_preserves_approval_and_performs_no_write(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    broker = ToolBroker({"default_workdir": str(workdir), "writable_scopes": ["."]},
                        tmp_path / "artifacts")
    result = _pre_execution_approval_required_result(
        "workspace.write_patch", _request("pending/new.txt"), broker, approved=False)
    assert result["approval_required"] is True
    assert result["ok"] is False
    assert not (workdir / "pending").exists()


def test_deprecated_full_content_write_cannot_reach_planner_approval(tmp_path):
    broker = ToolBroker({"default_workdir": str(tmp_path), "writable_scopes": ["."]},
                        tmp_path / "artifacts")
    with pytest.raises(AgentRuntimeError, match="unified diff patch"):
        _pre_execution_approval_required_result("workspace.write_patch",
                                                _request("file.txt", content="replacement"),
                                                broker, approved=False)
