"""Explicit project creation keeps native patch approval and file boundaries."""

import hashlib
import os

import pytest

from apps.shell.agent.runtime.errors import AgentRuntimeError, AgentWorkspaceBoundaryError
from apps.shell.agent.runtime.model_intent_planning import (
    ModelIntentPlanningError,
    ModelIntentProposal,
    direct_tool_selection_from_model_intent_proposal,
)
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.yachiyo_agent.runtime_planner import _authorized_project_creation_requested

GOAL = "请 Design Agent 和 Coding Agent 一起做一个小项目"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SOURCE = 'print("Hello, World!")\n'
PATCH = "--- /dev/null\n+++ app.py\n@@ -0,0 +1,1 @@\n+" + SOURCE


@pytest.fixture
def broker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return ToolBroker(
        {
            "default_workdir": str(workspace),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    ), workspace


def _create(tool, *, path="app.py", patch=PATCH, expected_sha256=EMPTY_SHA256, approved=True):
    return tool.call(
        "workspace.write_patch",
        {
            "path": path,
            "patch": patch,
            "expected_sha256": expected_sha256,
        },
        approved=approved,
    )


def test_project_source_is_created_only_after_approval_with_native_full_readback(broker):
    tool, workspace = broker
    pending = _create(tool, approved=False)
    assert pending["approval_required"]
    assert not (workspace / "app.py").exists()
    result = _create(tool)
    assert result["ok"] and result["postcondition_verified"]
    assert result["mode"] == "create"
    assert result["sha256_before"] == EMPTY_SHA256
    assert result["sha256_after"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert result["bytes"] == len(SOURCE.encode())
    assert (workspace / "app.py").read_bytes() == SOURCE.encode()
    assert not list(workspace.glob(".*.tmp"))


@pytest.mark.parametrize("expected_sha256", ["", "0" * 64, "bad"])
def test_creation_requires_explicit_empty_base_hash(broker, expected_sha256):
    tool, workspace = broker
    result = _create(tool, expected_sha256=expected_sha256)
    assert not result["ok"]
    assert not (workspace / "app.py").exists()


@pytest.mark.parametrize("existing", ["", "existing project\n"])
def test_creation_never_replaces_an_existing_target(broker, existing):
    tool, workspace = broker
    target = workspace / "app.py"
    target.write_text(existing)
    assert not _create(tool)["ok"]
    assert target.read_text() == existing


def test_non_creation_patch_does_not_create_missing_file(broker):
    tool, workspace = broker
    result = _create(tool, patch=PATCH.replace("--- /dev/null", "--- app.py"))
    assert not result["ok"]
    assert not (workspace / "app.py").exists()


@pytest.mark.parametrize(
    "patch",
    [
        PATCH.replace("+++ app.py", "+++ other.py"),
        PATCH.replace("-0,0", "-1,0"),
        PATCH.replace("-0,0", "-0,1"),
        PATCH.replace("+1,1", "+2,1"),
        PATCH.replace("+1,1", "+1,2"),
        PATCH + "@@ -0,0 +1,1 @@\n+extra\n",
        PATCH + "--- /dev/null\n+++ other.py\n@@ -0,0 +1,1 @@\n+extra\n",
        PATCH.replace("+++ app.py", "+++ /dev/null"),
        PATCH + "GIT binary patch\n",
        "new file mode 100644\n" + PATCH,
        PATCH.replace('+print("Hello, World!")', ' print("Hello, World!")'),
    ],
)
def test_invalid_creation_diff_has_no_file_effect(broker, patch):
    tool, workspace = broker
    with pytest.raises(AgentRuntimeError):
        _create(tool, patch=patch)
    assert not list(workspace.iterdir())


def test_delete_diff_is_still_rejected_without_changing_existing_file(broker):
    tool, workspace = broker
    target = workspace / "app.py"
    target.write_text(SOURCE)
    with pytest.raises(AgentRuntimeError):
        _create(
            tool,
            patch="--- app.py\n+++ /dev/null\n@@ -1,1 +0,0 @@\n-" + SOURCE,
            expected_sha256=hashlib.sha256(SOURCE.encode()).hexdigest(),
        )
    assert target.read_text() == SOURCE


def test_concurrent_creation_cannot_be_overwritten_by_publication(broker, monkeypatch):
    tool, workspace = broker
    link = os.link

    def competing_link(source, target, **kwargs):
        (workspace / "app.py").write_text("concurrent project\n")
        return link(source, target, **kwargs)

    monkeypatch.setattr("apps.shell.agent.tools.workspace.os.link", competing_link)
    result = _create(tool)
    assert not result["ok"] and "FileExistsError" in result["error"]
    assert (workspace / "app.py").read_text() == "concurrent project\n"
    assert not list(workspace.glob(".*.tmp"))


def test_failed_exclusive_temp_claim_never_deletes_a_foreign_temp_file(broker, monkeypatch):
    from types import SimpleNamespace

    tool, workspace = broker
    foreign = workspace / ".app.py.reserved.tmp"
    foreign.write_text("foreign staging data")
    monkeypatch.setattr(
        "apps.shell.agent.tools.workspace.uuid4", lambda: SimpleNamespace(hex="reserved")
    )
    result = _create(tool)
    assert not result["ok"]
    assert not (workspace / "app.py").exists()
    assert foreign.read_text() == "foreign staging data"


def test_creation_cannot_escape_workspace_through_symlink_or_relative_path(broker, tmp_path):
    tool, workspace = broker
    outside = tmp_path / "outside"
    outside.mkdir()
    (workspace / "linked").symlink_to(outside, target_is_directory=True)
    for path in ("../outside/app.py", "linked/app.py"):
        with pytest.raises(AgentWorkspaceBoundaryError):
            _create(tool, path=path, patch=PATCH.replace("+++ app.py", "+++ " + path))
    assert not list(outside.iterdir())


def test_failed_readback_is_not_reported_as_verified_success(broker, monkeypatch):
    tool, workspace = broker
    observed = iter((b"X" * len(SOURCE.encode()), b""))
    monkeypatch.setattr("apps.shell.agent.tools.workspace.os.read", lambda *_a: next(observed))
    result = _create(tool)
    assert not result["ok"] and result["verification_failed"]
    assert result.get("postcondition_verified") is not True
    assert (workspace / "app.py").read_text() == SOURCE


@pytest.mark.parametrize("ancestor", [False, True])
def test_parent_swapped_before_pinning_never_creates_an_outside_file(
    broker, monkeypatch, tmp_path, ancestor
):
    from apps.shell.agent.tools import workspace as workspace_mod

    tool, workdir = broker
    parent = workdir / "child" / "nested"
    parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    if ancestor:
        (outside / "nested").mkdir()
    original = workspace_mod._atomic_create_text
    swapped = parent.parent if ancestor else parent
    moved = workdir / "original-parent"

    def replace_parent(target, content):
        swapped.rename(moved)
        swapped.symlink_to(outside, target_is_directory=True)
        return original(target, content)

    monkeypatch.setattr("apps.shell.agent.tools.broker._atomic_create_text", replace_parent)
    result = _create(
        tool,
        path="child/nested/app.py",
        patch=PATCH.replace("+++ app.py", "+++ child/nested/app.py"),
    )
    assert not result["ok"]
    assert result.get("postcondition_verified") is not True
    assert not any(outside.rglob("app.py"))
    assert not any(moved.rglob("app.py"))


@pytest.mark.parametrize("moment", ["before_publish", "after_publish"])
def test_pinned_parent_swap_cannot_redirect_publishing_or_claim_current_path(
    broker, monkeypatch, tmp_path, moment
):
    from apps.shell.agent.tools import workspace as workspace_mod

    tool, workdir = broker
    parent = workdir / "child"
    parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = workdir / "original-parent"
    check = workspace_mod._assert_directory_namespace
    checked = 0

    def swap_and_check(path, fd):
        nonlocal checked
        checked += 1
        if checked == (1 if moment == "before_publish" else 2):
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
        return check(path, fd)

    monkeypatch.setattr(
        "apps.shell.agent.tools.workspace._assert_directory_namespace", swap_and_check
    )
    result = _create(
        tool, path="child/app.py", patch=PATCH.replace("+++ app.py", "+++ child/app.py")
    )
    assert not result["ok"]
    assert result.get("postcondition_verified") is not True
    assert not list(outside.iterdir())
    assert not list(moved.glob(".*.tmp"))
    if moment == "before_publish":
        assert not (moved / "app.py").exists()
    else:
        # Publication affected only the originally approved directory inode;
        # a changed namespace cannot be reported as the requested-path success.
        assert (moved / "app.py").read_text() == SOURCE


def test_readback_cannot_follow_a_replaced_target_symlink(broker, monkeypatch, tmp_path):
    tool, workdir = broker
    outside = tmp_path / "outside.py"
    outside.write_text(SOURCE)
    link = os.link

    def replace_published_target(source, target, **kwargs):
        link(source, target, **kwargs)
        (workdir / "app.py").unlink()
        (workdir / "app.py").symlink_to(outside)

    monkeypatch.setattr("apps.shell.agent.tools.workspace.os.link", replace_published_target)
    result = _create(tool)
    assert not result["ok"]
    assert result.get("postcondition_verified") is not True
    assert outside.read_text() == SOURCE


def test_readback_requires_the_published_inode_even_when_replacement_bytes_match(
    broker, monkeypatch
):
    tool, workdir = broker
    link = os.link

    def replace_published_target(source, target, **kwargs):
        link(source, target, **kwargs)
        (workdir / "app.py").unlink()
        (workdir / "app.py").write_text(SOURCE)

    monkeypatch.setattr("apps.shell.agent.tools.workspace.os.link", replace_published_target)
    result = _create(tool)
    assert not result["ok"]
    assert result.get("postcondition_verified") is not True


def test_new_nested_project_parent_is_created_without_following_symlinks(broker):
    tool, workdir = broker
    result = _create(
        tool, path="new/nested/app.py", patch=PATCH.replace("+++ app.py", "+++ new/nested/app.py")
    )
    assert result["ok"] and result["postcondition_verified"]
    assert (workdir / "new" / "nested" / "app.py").read_text() == SOURCE


@pytest.mark.parametrize("phrase", ["做一个小项目", "做个小项目", "做一个项目", "做个项目"])
def test_bounded_project_clause_compiles_original_goal_and_approval(phrase):
    goal = GOAL.replace("做一个小项目", phrase)
    selected = direct_tool_selection_from_model_intent_proposal(
        ModelIntentProposal(intent_kind="code_task", planning_goal=goal, action_evidence=phrase),
        goal,
        ["workspace.list", "workspace.write_patch"],
    )
    assert selected.decision.plan.task_core.goal_contract.original_goal == goal
    steps = selected.decision.plan.tool_plan.steps
    assert [step.tool_name for step in steps] == ["workspace.list", "workspace.write_patch"]
    assert steps[-1].input_preview == {
        "mode": "create",
        "patch_source": "model_after_workspace_inspection",
    }
    assert steps[-1].approval_required
    assert steps[-1].risk_level == "high"


@pytest.mark.parametrize(
    "goal",
    [
        "不要做一个小项目",
        "如果我说可以再做一个小项目",
        "解释“做一个小项目”是什么意思",
        "请解释做一个小项目的含义",
        "请回复‘做一个小项目’",
        '请回复"做一个小项目"',
        "他说要做一个小项目",
        "做饭",
        "做一次搜索",
    ],
)
def test_project_negation_condition_quote_and_unrelated_verbs_cannot_grant_creation(goal):
    assert not _authorized_project_creation_requested(goal)
    with pytest.raises(ModelIntentPlanningError):
        direct_tool_selection_from_model_intent_proposal(
            ModelIntentProposal(
                intent_kind="code_task", planning_goal=GOAL, action_evidence="做一个小项目"
            ),
            goal,
            ["workspace.list", "workspace.write_patch"],
        )


@pytest.mark.parametrize("suffix", ["并打开Slack", "然后删除README.md", "并发送给张三"])
def test_project_proposal_cannot_discard_other_user_actions(suffix):
    with pytest.raises(ModelIntentPlanningError, match="abstract_subgoals_required"):
        direct_tool_selection_from_model_intent_proposal(
            ModelIntentProposal(
                intent_kind="code_task", planning_goal=GOAL, action_evidence="做一个小项目"
            ),
            GOAL + suffix,
            ["workspace.list", "workspace.write_patch", "app.open", "desktop.type_text"],
        )


@pytest.mark.parametrize("decision", ["reject", "cancel", "second_approval"])
def test_actual_project_parent_preserves_child_approval_and_cancellation(
    tmp_path, monkeypatch, decision
):
    from tests.test_chat_api import _exercise_real_group_project_dispatch

    _exercise_real_group_project_dispatch(tmp_path, monkeypatch, decision=decision)


def test_direct_project_targets_keep_declared_order_when_catalog_is_reversed():
    from apps.shell.agent.runtime.main_chat_delegation import compile_plan

    catalog = [
        {"id": "coding", "name": "Coding Agent", "nickname": "furina", "kind": "agent"},
        {"id": "design", "name": "Design Agent", "nickname": "Design", "kind": "agent"},
    ]
    plan = compile_plan(
        run={"run_id": "parent", "user_goal": GOAL},
        targets=catalog,
        parent_runtime={},
        selected_ids={"design", "coding"},
        group_scope=True,
        direct_group=True,
    )
    assert plan is not None
    assert [target["runnable_id"] for target in plan["binding"]["targets"]] == ["design", "coding"]
    assert all(target["goal"] == GOAL for target in plan["binding"]["targets"])
