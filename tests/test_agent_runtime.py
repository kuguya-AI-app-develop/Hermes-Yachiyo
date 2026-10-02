"""Agent Runtime Service tests."""

from __future__ import annotations

import hashlib
import json
import shlex
import sqlite3
import subprocess
import sys
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.runtime_contract_fixtures import (
    semantic_fixture_goal, semantic_fixture_model_plan, semantic_fixture_execution_envelope,
)

from apps.shell.agent_runtime import (
    _MAX_AGENT_TOOL_ITERATIONS,
    AgentApprovalRequired,
    AgentRuntimeError,
    AgentRuntimeService,
    ApprovalCoordinator,
    ApprovalResumeCoordinator,
    ApprovalResumeProjectionCoordinator,
    NativeRunEngine,
    RunCancellationProjection,
    RunProjectionCoordinator,
    RunTransitionProjectionCoordinator,
    TaskRunLinkRepository,
    ToolApprovalClaimProjection,
    ToolApprovalContinuationHandoff,
    ToolApprovalContinuationOutcome,
    ToolApprovalCustomApiContinuationRequest,
    ToolApprovalExecutionFailureProjection,
    ToolApprovalExecutionFollowup,
    ToolApprovalExecutionRequest,
    ToolApprovalResumeContext,
    ToolApprovalTransitionContext,
    ToolBroker,
    WorkflowAgentNodeExecution,
    WorkflowAgentNodeHandoff,
    WorkflowApprovalPauseProjection,
    WorkflowApprovalResumeContext,
    WorkflowApprovalResumeCoordinator,
    WorkflowApprovalTransitionContext,
    WorkflowArtifactNodeWrite,
    WorkflowCancellationProjectionCoordinator,
    WorkflowCancellationTarget,
    WorkflowChildOutcomeCoordinator,
    WorkflowChildRunProjection,
    WorkflowChildStatusProjection,
    WorkflowConditionNodeProjection,
    WorkflowContinuationCoordinator,
    WorkflowContinuationFailureProjection,
    WorkflowLoopNodeProjection,
    WorkflowParallelNodeProjection,
    WorkflowParentResumeCoordinator,
    WorkflowParentResumeFailureProjection,
    WorkflowParentRunLocator,
    WorkflowPathPlanner,
    WorkflowResumePlanner,
    WorkflowRunCompletionProjection,
    WorkflowRunStartProjector,
    WorkflowStartNodeProjection,
    WorkflowSubworkflowNodeExecution,
)
from apps.shell.credential_store import MemoryCredentialStore
from apps.shell.agent.runtime.goal_runtime import DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from scripts.verify_secret_redaction import verify_secret_redaction


def make_service(tmp_path, *, seed_templates: bool = False) -> AgentRuntimeService:
    return AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=seed_templates,
    )


def explicit_foreground_takeover_metadata() -> dict[str, Any]:
    """Opt a legacy foreground-execution test into the supervised desktop path."""

    return {
        "allow_user_foreground_takeover": True,
        "desktop_execution_policy": {
            "mode": "allow",
            "allow_live_foreground": True,
            "prefer_background_desktop": False,
            "prefer_isolated_desktop": False,
            "avoid_user_foreground_takeover": False,
            "require_sandbox_for_keyboard_mouse": False,
        },
    }


def install_fake_run_owned_browser_target(
    service: AgentRuntimeService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Give brokers a fake target owned by the current Run, without user-tab access."""

    target_id = "page-target-runtime-test"
    original_factory = service.tool_brokers._tool_broker_factory

    def broker_factory(*args, **kwargs):
        broker = original_factory(*args, **kwargs)
        broker.restore_owned_browser_target(target_id)
        return broker

    monkeypatch.setattr(service.tool_brokers, "_tool_broker_factory", broker_factory)
    monkeypatch.setattr(
        "apps.shell.agent.tools.broker.browser.is_valid_target_id",
        lambda value: str(value or "").strip() == target_id,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.broker.browser.owned_browser_target",
        lambda _target_id: nullcontext(),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.broker.browser.close_target",
        lambda requested_target_id: {
            "ok": requested_target_id == target_id,
            "action": "browser.close_target",
            "data": {"target_id": requested_target_id},
        },
    )


def test_agent_runtime_service_is_native_run_engine_compatibility_name():
    assert AgentRuntimeService is NativeRunEngine


def test_run_projection_coordinator_syncs_run_projections():
    calls: list[tuple[str, str, object]] = []

    class Projection:
        def __init__(self, name: str) -> None:
            self.name = name

        def sync(self, run_id: str, *args, **kwargs) -> None:
            calls.append((self.name, run_id, kwargs or args))

        def sync_projection(self, run_id: str, **kwargs) -> None:
            calls.append((self.name, run_id, kwargs))

    coordinator = RunProjectionCoordinator(
        run_artifacts=Projection("artifacts"),
        run_approvals=Projection("approvals"),
        task_run_links=Projection("task_links"),
    )

    artifacts = [{"kind": "file", "path": "report.md", "meta": {"revision": 1}}]
    pending_approval = {
        "tool": "workspace.write_patch",
        "input_preview": {"path": "report.md"},
    }
    coordinator.sync(
        "run-projection",
        status="approval_required",
        artifacts=artifacts,
        pending_approval=pending_approval,
    )
    coordinator.sync_event_cursor("run-projection", sequence=7)

    projected_artifacts = calls[0][2][0]
    projected_pending = calls[1][2]["pending_approval"]
    assert calls == [
        ("artifacts", "run-projection", (projected_artifacts,)),
        (
            "approvals",
            "run-projection",
            {"status": "approval_required", "pending_approval": projected_pending},
        ),
        ("task_links", "run-projection", {"status": "approval_required"}),
        ("task_links", "run-projection", {"last_event_sequence": 7}),
    ]
    assert projected_artifacts == artifacts
    assert projected_artifacts is not artifacts
    assert projected_artifacts[0] is not artifacts[0]
    assert projected_artifacts[0]["meta"] is not artifacts[0]["meta"]
    assert projected_pending == pending_approval
    assert projected_pending is not pending_approval
    assert projected_pending["input_preview"] is not pending_approval["input_preview"]

    artifacts[0]["path"] = "changed.md"
    artifacts[0]["meta"]["revision"] = 2
    pending_approval["input_preview"]["path"] = "changed.md"
    assert projected_artifacts == [{"kind": "file", "path": "report.md", "meta": {"revision": 1}}]
    assert projected_pending == {
        "tool": "workspace.write_patch",
        "input_preview": {"path": "report.md"},
    }


def test_run_transition_projection_coordinator_projects_child_and_workflow_group():
    agent_group_updates: list[dict[str, object]] = []
    parent_updates: list[dict[str, object]] = []
    workflow_group_updates: list[dict[str, object]] = []
    stored_runs = {
        "workflow_root": {
            "run_id": "workflow_root",
            "status": "cancelled",
            "result": "Workflow cancelled",
        },
        "agent_root": {
            "run_id": "agent_root",
            "status": "completed",
            "result": "Agent complete",
        },
    }

    coordinator = RunTransitionProjectionCoordinator(
        update_agent_run_group_if_root=lambda run: agent_group_updates.append(run),
        resume_parent_workflows_after_child_update=lambda run: parent_updates.append(run),
        workflow_run_is_group_root=lambda run: bool(run.get("is_root")),
        update_run_group=lambda run_group_id, **kwargs: workflow_group_updates.append(
            {"run_group_id": run_group_id, **kwargs}
        ),
        get_run=lambda run_id: stored_runs[run_id],
    )

    child = {"run_id": "agent_child", "kind": "agent_run", "status": "completed"}
    agent_root = {"run_id": "agent_root", "kind": "agent_run", "status": "completed"}
    non_root_workflow = {"run_id": "workflow_child", "run_group_id": "group-child", "is_root": False}
    root_workflow = {"run_id": "workflow_root", "run_group_id": "group-root", "is_root": True}
    cancelled = {"run_id": "workflow_root", "status": "cancelled", "result": "Workflow cancelled"}

    assert coordinator.project_child_run_transition(child) is child
    assert coordinator.project_agent_run_group_if_root(agent_root) == stored_runs["agent_root"]
    assert coordinator.project_cancelled_workflow_group_if_root(non_root_workflow, cancelled) is cancelled
    root_projection = coordinator.project_cancelled_workflow_group_if_root(root_workflow, cancelled)

    assert agent_group_updates == [child, agent_root]
    assert parent_updates == [child]
    assert workflow_group_updates == [
        {
            "run_group_id": "group-root",
            "status": "cancelled",
            "summary": "Workflow cancelled",
        }
    ]
    assert root_projection == stored_runs["workflow_root"]


def test_approval_coordinator_snapshots_input_previews():
    run_events: list[tuple[str, str, dict[str, object]]] = []
    updates: list[dict[str, object]] = []

    def update_run(run_id, **kwargs):
        updates.append({"run_id": run_id, **kwargs})
        return {"run_id": run_id, **kwargs}

    coordinator = ApprovalCoordinator(
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda run_id, event_type, payload: run_events.append((run_id, event_type, payload)),
        update_run=update_run,
    )
    previews = [
        {"input": {"label": "approve-tool"}},
        {"input": {"label": "reject-tool"}},
        {"input": {"label": "timeout-tool"}},
        {"input": {"label": "approve-workflow"}},
        {"input": {"label": "reject-workflow"}},
        {"input": {"label": "timeout-workflow"}},
    ]
    tool_approve_timeline: list[dict[str, object]] = []
    tool_reject_timeline: list[dict[str, object]] = []
    tool_timeout_timeline: list[dict[str, object]] = []
    workflow_approve_timeline: list[dict[str, object]] = []
    workflow_reject_timeline: list[dict[str, object]] = []
    workflow_timeout_timeline: list[dict[str, object]] = []

    coordinator.approve_tool_run(
        "run-tool-approve",
        timeline=tool_approve_timeline,
        artifacts=[],
        tool_name="terminal.run",
        input_preview=previews[0],
        resumed_detail="resumed",
        running_result="running",
    )
    coordinator.reject_tool_run(
        "run-tool-reject",
        timeline=tool_reject_timeline,
        reason="no",
        tool_name="terminal.run",
        input_preview=previews[1],
    )
    coordinator.timeout_tool_run(
        "run-tool-timeout",
        timeline=tool_timeout_timeline,
        reason="late",
        tool_name="terminal.run",
        input_preview=previews[2],
    )
    coordinator.approve_workflow_node(
        "run-workflow-approve",
        timeline=workflow_approve_timeline,
        artifacts=[],
        result_context="context",
        workflow_node_id="gate",
        label="Gate",
        criteria="Review",
        input_preview=previews[3],
        expected_approval_id="approval-workflow-approve",
    )
    coordinator.reject_workflow_node(
        "run-workflow-reject",
        timeline=workflow_reject_timeline,
        reason="no",
        workflow_node_id="gate",
        label="Gate",
        criteria="Review",
        input_preview=previews[4],
    )
    coordinator.timeout_workflow_node(
        "run-workflow-timeout",
        timeline=workflow_timeout_timeline,
        reason="late",
        workflow_node_id="gate",
        label="Gate",
        criteria="Review",
        input_preview=previews[5],
    )

    for preview in previews:
        preview["input"]["label"] = "changed"

    projected_timelines = [
        tool_approve_timeline,
        tool_reject_timeline,
        tool_timeout_timeline,
        workflow_approve_timeline,
        workflow_reject_timeline,
        workflow_timeout_timeline,
    ]
    projected_json = json.dumps(
        {"timelines": projected_timelines, "run_events": run_events, "updates": updates},
        ensure_ascii=False,
    )
    assert "changed" not in projected_json
    for original in previews:
        for timeline in projected_timelines:
            for event in timeline:
                if isinstance(event.get("input_preview"), dict):
                    assert event["input_preview"] is not original
        for _run_id, _event_type, payload in run_events:
            if isinstance(payload.get("input_preview"), dict):
                assert payload["input_preview"] is not original


def test_workflow_child_outcome_coordinator_projects_child_artifacts_and_timeline():
    coordinator = WorkflowChildOutcomeCoordinator()
    timeline = [
        {
            "event": "workflow.node.agent",
            "detail": "Research Agent",
            "child_run_id": "child_run",
            "workflow_node_id": "agent-node",
            "workflow_node_kind": "agent",
            "workflow_node_label": "Research Step",
        }
    ]
    artifacts = [
        {
            "kind": "workflow_child_artifact",
            "source_run_id": "child_run",
            "path": "reports/existing.md",
        }
    ]
    child_run = {
        "run_id": "child_run",
        "kind": "agent_run",
        "runnable_id": "agent_research",
        "runnable_name": "Research Agent",
        "status": "completed",
        "result": "Child Agent completed with token=sk-workflow-child-secret123456 and visible summary.",
        "artifacts": [
            {"kind": "context", "path": "context.md"},
            {"kind": "agent_artifact", "path": "reports/existing.md"},
            {"kind": "agent_artifact", "path": "reports/fresh.md"},
            {"kind": "agent_artifact", "path": ""},
            "not-an-artifact",
        ],
    }

    label, node_info = coordinator.child_node_context(timeline, child_run)
    coordinator.merge_child_run_outcome(timeline, artifacts, child_run, label)

    assert label == "Research Agent"
    assert node_info == {
        "workflow_node_id": "agent-node",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research Step",
    }
    assert timeline[0]["status"] == "completed"
    assert timeline[0]["result"] == "Child Agent completed with token=[redacted] and visible summary."
    assert "sk-workflow-child-secret123456" not in json.dumps(timeline, ensure_ascii=False)
    assert timeline[0]["artifact_count"] == 2
    assert artifacts == [
        {
            "kind": "workflow_child_artifact",
            "source_run_id": "child_run",
            "path": "reports/existing.md",
        },
        {
            "kind": "workflow_child_artifact",
            "path": "reports/fresh.md",
            "source_run_id": "child_run",
            "source_run_kind": "agent_run",
            "source_runnable_id": "agent_research",
            "source_runnable_name": "Research Agent",
            "workflow_step_label": "Research Agent",
            "artifact_kind": "agent_artifact",
        },
    ]


def test_workflow_parent_run_locator_finds_waiting_parents_and_root_groups():
    waiting_parent = {
        "run_id": "workflow_waiting",
        "kind": "workflow_run",
        "run_group_id": "group",
        "status": "approval_required",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "child_run",
            }
        ],
    }
    running_unrelated = {
        "run_id": "workflow_unrelated",
        "kind": "workflow_run",
        "run_group_id": "group",
        "status": "running",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "other_child",
            }
        ],
    }
    waiting_on_workflow_parent = {
        "run_id": "workflow_waiting_on_workflow",
        "kind": "workflow_run",
        "run_group_id": "group",
        "status": "approval_required",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "child_workflow",
            }
        ],
    }
    completed_parent = {
        "run_id": "workflow_completed",
        "kind": "workflow_run",
        "run_group_id": "group",
        "status": "completed",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "child_run",
            }
        ],
    }
    runs = {
        "workflow_waiting": waiting_parent,
        "workflow_unrelated": running_unrelated,
        "workflow_waiting_on_workflow": waiting_on_workflow_parent,
        "workflow_completed": completed_parent,
        "child_run": {"run_id": "child_run", "kind": "agent_run"},
        "child_workflow": {"run_id": "child_workflow", "kind": "workflow_run"},
        "not_workflow": {"run_id": "not_workflow", "kind": "agent_run", "status": "approval_required"},
    }
    groups = {
        "group": {
            "run_group_id": "group",
            "source": "chat",
            "child_run_ids": [
                "workflow_waiting",
                "child_run",
                "child_workflow",
                "workflow_waiting_on_workflow",
                "workflow_unrelated",
                "missing_run",
                "workflow_completed",
                "not_workflow",
            ],
        },
        "workflow_group": {
            "run_group_id": "workflow_group",
            "source": "workflow",
            "child_run_ids": ["workflow_external", "workflow_nested_child"],
        },
    }

    locator = WorkflowParentRunLocator(
        get_run_group=lambda group_id: groups[group_id],
        get_run=lambda run_id: runs[run_id],
    )

    parents = locator.parent_runs_waiting_for_child(
        {
            "run_id": "child_run",
            "kind": "agent_run",
            "run_group_id": "group",
        }
    )

    assert parents == [waiting_parent]
    assert locator.parent_runs_waiting_for_child(
        {"run_id": "child_workflow", "kind": "workflow_run", "run_group_id": "group"}
    ) == [waiting_on_workflow_parent]
    assert locator.parent_runs_waiting_for_child({"kind": "workflow_run", "run_group_id": "group"}) == []
    assert locator.parent_runs_waiting_for_child({"kind": "agent_run", "run_group_id": "missing"}) == []
    assert locator.workflow_run_is_group_root(waiting_parent) is True
    assert locator.workflow_run_is_group_root({"run_id": "workflow_external", "run_group_id": "workflow_group"}) is True
    assert locator.workflow_run_is_group_root(
        {"run_id": "workflow_nested_child", "run_group_id": "workflow_group"}
    ) is False
    assert locator.workflow_run_is_group_root({"run_id": "workflow_missing", "run_group_id": "missing"}) is False
    assert locator.workflow_run_is_group_root({"run_id": "workflow_without_group"}) is False


def test_workflow_resume_planner_uses_snapshot_and_child_agent_ordinal():
    fallback_workflow = {"workflow_id": "workflow_db", "nodes": [], "edges": [], "enabled": True}
    snapshot_nodes = [
        {"id": "start", "type": "start"},
        {"id": "agent-a", "type": "agent"},
        {"id": "approval", "type": "approval"},
        {"id": "agent-b", "type": "agent"},
        {"id": "artifact", "type": "artifact"},
    ]
    snapshot_edges = [
        {"source": "start", "target": "agent-a"},
        {"source": "agent-a", "target": "approval"},
        {"source": "approval", "target": "agent-b"},
        {"source": "agent-b", "target": "artifact"},
    ]
    workflow_run = {
        "run_id": "workflow_run",
        "runnable_id": "workflow_db",
        "timeline": [
            "not-an-event",
            {
                "event": "workflow.run.started",
                "workflow_snapshot": {
                    "workflow_id": "workflow_snapshot",
                    "name": "Snapshot Workflow",
                    "nodes": snapshot_nodes,
                    "edges": snapshot_edges,
                },
            },
            {"event": "workflow.node.agent", "child_run_id": "agent_run_a"},
            {"event": "workflow.node.approval_required"},
            {"event": "workflow.node.agent", "child_run_id": "agent_run_b"},
        ],
    }
    planner = WorkflowResumePlanner(
        get_workflow=lambda workflow_id: {**fallback_workflow, "workflow_id": workflow_id},
        workflow_path=lambda workflow: list(workflow["nodes"]),
        node_kind=lambda node: str(node.get("type") or ""),
    )

    workflow = planner.workflow_for_run_resume(workflow_run)

    assert workflow == {
        "workflow_id": "workflow_snapshot",
        "name": "Snapshot Workflow",
        "nodes": snapshot_nodes,
        "edges": snapshot_edges,
        "enabled": True,
    }
    assert planner.resume_start_index(workflow, workflow_run, "agent_run_b") == 4
    assert planner.resume_start_index(workflow, workflow_run, "missing_child") is None
    assert planner.workflow_for_run_resume(
        {"run_id": "workflow_run", "runnable_id": "workflow_db", "timeline": []}
    ) == fallback_workflow


def test_workflow_path_planner_builds_path_snapshot_and_artifact_paths():
    planner = WorkflowPathPlanner(node_kind=lambda node: str(node.get("type") or ""))
    workflow = {
        "workflow_id": "workflow_plan",
        "name": "Plan Workflow",
        "nodes": [
            {"id": "artifact-2", "type": "artifact", "data": {"label": "Final", "artifactPath": "reports/final.md"}},
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {"id": "agent", "type": "agent", "data": {"label": "Agent", "instructions": "Summarize"}},
            {"id": "approval", "type": "approval", "data": {"label": "Gate", "approval_criteria": "Review output"}},
            {"id": "artifact-1", "type": "artifact", "data": {"label": "Draft", "artifact_path": "reports/final.md"}},
        ],
        "edges": [
            {"source": "start", "target": "agent"},
            {"source": "agent", "target": "approval"},
            {"source": "approval", "target": "artifact-1"},
            {"source": "artifact-1", "target": "artifact-2"},
        ],
    }

    path = planner.workflow_path(workflow)
    snapshot = planner.path_snapshot(workflow)
    runtime_snapshot = planner.runtime_snapshot(workflow)

    assert [node["id"] for node in path] == ["start", "agent", "approval", "artifact-1", "artifact-2"]
    assert snapshot == [
        {"id": "start", "kind": "start", "label": "Start"},
        {"id": "agent", "kind": "agent", "label": "Agent", "task": "Summarize"},
        {"id": "approval", "kind": "approval", "label": "Gate", "criteria": "Review output"},
        {"id": "artifact-1", "kind": "artifact", "label": "Draft", "artifact_path": "reports/final.md"},
        {"id": "artifact-2", "kind": "artifact", "label": "Final", "artifact_path": "reports/final-2.md"},
    ]
    assert planner.artifact_path(
        "Fallback Artifact",
        [{"kind": "workflow_artifact", "path": "fallback-artifact.md"}],
        "",
    ) == "fallback-artifact-2.md"
    assert planner.node_task({"data": {"prompt": "Prompt task"}}) == "Prompt task"
    assert planner.approval_criteria({"data": {"task": "Task fallback"}}) == "Task fallback"
    assert planner.child_goal("Workflow goal", "Step task") == "Step task\n\nWorkflow Goal:\nWorkflow goal"
    assert runtime_snapshot == {
        "workflow_id": "workflow_plan",
        "name": "Plan Workflow",
        "nodes": workflow["nodes"],
        "edges": workflow["edges"],
    }
    assert runtime_snapshot["nodes"] is not workflow["nodes"]
    assert runtime_snapshot["edges"] is not workflow["edges"]


def test_workflow_path_planner_supports_condition_branch_snapshot_and_selection():
    planner = WorkflowPathPlanner(node_kind=lambda node: str(node.get("type") or ""))
    workflow = {
        "workflow_id": "workflow_condition_plan",
        "name": "Condition Workflow",
        "nodes": [
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {
                "id": "route",
                "type": "condition",
                "data": {"label": "Route", "condition": "ship", "operator": "contains"},
            },
            {"id": "ship", "type": "agent", "data": {"label": "Ship"}},
            {"id": "ship-report", "type": "artifact", "data": {"label": "Ship Report"}},
            {"id": "skip", "type": "agent", "data": {"label": "Skip"}},
            {"id": "skip-report", "type": "artifact", "data": {"label": "Skip Report"}},
        ],
        "edges": [
            {"source": "start", "target": "route"},
            {"source": "route", "target": "ship", "data": {"branch": "true"}},
            {"source": "route", "target": "skip", "data": {"branch": "false"}},
            {"source": "ship", "target": "ship-report"},
            {"source": "skip", "target": "skip-report"},
        ],
    }

    snapshot = planner.path_snapshot(workflow)
    matched = planner.condition_selection(workflow, workflow["nodes"][1], "decision: SHIP")
    missed = planner.condition_selection(workflow, workflow["nodes"][1], "decision: skip")
    projection = WorkflowConditionNodeProjection.from_node(
        SimpleNamespace(_workflow_condition_selection=planner.condition_selection),
        workflow,
        workflow["nodes"][1],
        label="Route",
        kind="condition",
        context="decision: SHIP",
    )

    assert [step["id"] for step in snapshot] == [
        "start",
        "route",
        "ship",
        "ship-report",
        "skip",
        "skip-report",
    ]
    assert snapshot[1] == {
        "id": "route",
        "kind": "condition",
        "label": "Route",
        "condition": "ship",
        "operator": "contains",
    }
    assert matched["matched"] is True
    assert matched["branch"] == "true"
    assert matched["target_node_id"] == "ship"
    assert missed["matched"] is False
    assert missed["branch"] == "false"
    assert missed["target_node_id"] == "skip"
    assert projection.event_payload()["workflow_node_selected_target"] == "ship"


def test_workflow_path_planner_supports_parallel_fanout_plan_and_snapshot():
    planner = WorkflowPathPlanner(node_kind=lambda node: str(node.get("type") or ""))
    workflow = {
        "workflow_id": "workflow_parallel_plan",
        "name": "Parallel Workflow",
        "nodes": [
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {"id": "fanout", "type": "parallel", "data": {"label": "Parallel Work"}},
            {"id": "design", "type": "agent", "data": {"label": "Design"}},
            {"id": "code", "type": "agent", "data": {"label": "Code"}},
            {"id": "report", "type": "artifact", "data": {"label": "Report"}},
        ],
        "edges": [
            {"source": "start", "target": "fanout"},
            {"source": "fanout", "target": "design"},
            {"source": "fanout", "target": "code"},
            {"source": "design", "target": "report"},
            {"source": "code", "target": "report"},
        ],
    }

    plan = planner.parallel_plan(workflow, workflow["nodes"][1])
    snapshot = planner.path_snapshot(workflow)
    projection = WorkflowParallelNodeProjection(
        node_id="fanout",
        node_kind="parallel",
        node_label="Parallel Work",
        branch_count=2,
        completed_count=2,
        join_node_id="report",
        branch_results=[
            {"entry_node_id": "design", "label": "Design", "result": "Design ready"},
            {"entry_node_id": "code", "label": "Code", "result": "Code ready"},
        ],
    )

    assert plan == {
        "join_node_id": "report",
        "branches": [
            {"entry_node_id": "design", "label": "Design", "node_ids": ["design"]},
            {"entry_node_id": "code", "label": "Code", "node_ids": ["code"]},
        ],
    }
    assert snapshot[1] == {
        "id": "fanout",
        "kind": "parallel",
        "label": "Parallel Work",
        "branch_count": "2",
    }
    assert projection.event_payload()["workflow_node_join_target"] == "report"
    assert projection.event_payload()["workflow_node_completed_branch_count"] == 2


def test_workflow_path_planner_supports_subworkflow_snapshot():
    planner = WorkflowPathPlanner(node_kind=lambda node: str(node.get("type") or ""))
    workflow = {
        "workflow_id": "workflow_parent_plan",
        "name": "Parent Workflow",
        "nodes": [
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {
                "id": "child",
                "type": "workflow",
                "data": {
                    "label": "Child Workflow",
                    "workflow_id": "workflow_child_plan",
                    "task": "Run the child flow",
                },
            },
            {"id": "report", "type": "artifact", "data": {"label": "Parent Report"}},
        ],
        "edges": [
            {"source": "start", "target": "child"},
            {"source": "child", "target": "report"},
        ],
    }

    snapshot = planner.path_snapshot(workflow)

    assert planner.workflow_id(workflow["nodes"][1]) == "workflow_child_plan"
    assert snapshot[1] == {
        "id": "child",
        "kind": "workflow",
        "label": "Child Workflow",
        "workflow_id": "workflow_child_plan",
        "task": "Run the child flow",
    }


def test_workflow_path_planner_supports_loop_snapshot_and_selection():
    planner = WorkflowPathPlanner(node_kind=lambda node: str(node.get("type") or ""))
    workflow = {
        "workflow_id": "workflow_loop_plan",
        "name": "Loop Workflow",
        "nodes": [
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {"id": "worker", "type": "agent", "data": {"label": "Worker"}},
            {
                "id": "repeat",
                "type": "loop",
                "data": {"label": "Repeat While Needed", "condition": "again", "max_iterations": 2},
            },
            {"id": "report", "type": "artifact", "data": {"label": "Loop Report"}},
        ],
        "edges": [
            {"source": "start", "target": "worker"},
            {"source": "worker", "target": "repeat"},
            {"source": "repeat", "target": "worker", "data": {"branch": "continue"}},
            {"source": "repeat", "target": "report", "data": {"branch": "exit"}},
        ],
    }

    snapshot = planner.path_snapshot(workflow)
    continued = planner.loop_selection(workflow, workflow["nodes"][2], "again please", previous_iterations=0)
    capped = planner.loop_selection(workflow, workflow["nodes"][2], "again please", previous_iterations=2)
    exited = planner.loop_selection(workflow, workflow["nodes"][2], "done", previous_iterations=2)
    projection = WorkflowLoopNodeProjection.from_node(
        SimpleNamespace(_workflow_loop_selection=planner.loop_selection),
        workflow,
        workflow["nodes"][2],
        label="Repeat While Needed",
        kind="loop",
        context="again please",
        previous_iterations=0,
    )

    assert snapshot[2] == {
        "id": "repeat",
        "kind": "loop",
        "label": "Repeat While Needed",
        "condition": "again",
        "operator": "contains",
        "max_iterations": "2",
    }
    assert continued["branch"] == "continue"
    assert continued["target_node_id"] == "worker"
    assert continued["iteration"] == 1
    assert capped["branch"] == "exit"
    assert capped["target_node_id"] == "report"
    assert capped["limit_reached"] is True
    assert exited["branch"] == "exit"
    assert exited["limit_reached"] is False
    assert projection.event_payload()["workflow_node_loop_iteration"] == 1
    assert projection.event_payload()["workflow_node_selected_branch"] == "continue"


def test_workflow_run_start_projector_builds_timeline_and_replay_payload():
    workflow = {"workflow_id": "workflow_run_start", "name": "Start Projection"}
    workflow_path = [{"id": "start", "kind": "start"}, {"id": "agent", "kind": "agent"}]
    runtime_snapshot = {"workflow_id": "workflow_run_start", "nodes": [{"id": "start"}], "edges": []}
    projector = WorkflowRunStartProjector(
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        path_snapshot=lambda received_workflow: workflow_path if received_workflow is workflow else [],
        runtime_snapshot=lambda received_workflow: runtime_snapshot if received_workflow is workflow else {},
    )

    timeline, event_payload = projector.started_projection("workflow_run_start", workflow)

    assert timeline == [
        {
            "event": "workflow.run.started",
            "detail": "Start Projection",
            "workflow_path": workflow_path,
            "workflow_snapshot": runtime_snapshot,
        }
    ]
    assert event_payload == {
        "workflow_id": "workflow_run_start",
        "workflow_name": "Start Projection",
        "workflow_path": workflow_path,
    }
    assert event_payload["workflow_path"] is not workflow_path


def test_workflow_approval_resume_context_parses_pending_payload():
    workflow = {"workflow_id": "workflow_resume", "nodes": [{"id": "gate"}]}
    run = {
        "run_id": "workflow_run",
        "result": "fallback context",
        "user_goal": "fallback goal",
        "timeline": [
            {"event": "workflow.node.approval_required"},
            "not-an-event",
        ],
        "artifacts": [
            {"kind": "workflow_artifact", "path": "summary.md"},
            "not-an-artifact",
        ],
    }
    pending = {
        "approval_id": "approval-context",
        "tool": "workflow.approval",
        "workflow_context": "approved context",
        "workflow_next_index": "4",
        "workflow_next_node_id": "after-gate",
        "workflow_node_id": "gate",
        "workflow_node_label": "Human Gate",
        "workflow_node_approval_criteria": "Review before continuing.",
        "input_preview": {"checkpoint": "Human Gate"},
    }

    context = WorkflowApprovalResumeContext.from_run(
        run,
        pending,
        workflow=workflow,
        root_group=True,
    )

    assert context.workflow == workflow
    assert context.workflow is not workflow
    assert context.result_context == "approved context"
    assert context.start_index == 4
    assert context.start_node_id == "after-gate"
    assert context.root_group is True
    assert context.expected_approval_id == "approval-context"
    assert context.timeline == [{"event": "workflow.node.approval_required"}]
    assert context.artifacts == [{"kind": "workflow_artifact", "path": "summary.md"}]
    assert context.approval.workflow_node_id == "gate"
    assert context.approval.label == "Human Gate"
    assert context.approval.criteria == "Review before continuing."
    assert context.approval.input_preview == {"checkpoint": "Human Gate"}
    assert context.approval.input_preview is not pending["input_preview"]
    context.workflow["nodes"][0]["id"] = "changed"
    context.timeline[0]["event"] = "changed"
    context.artifacts[0]["path"] = "changed.md"
    context.approval.input_preview["checkpoint"] = "changed"
    assert workflow == {"workflow_id": "workflow_resume", "nodes": [{"id": "gate"}]}
    assert run["timeline"][0] == {"event": "workflow.node.approval_required"}
    assert run["artifacts"][0] == {"kind": "workflow_artifact", "path": "summary.md"}
    assert pending["input_preview"] == {"checkpoint": "Human Gate"}

    bad_pending = {**pending, "workflow_next_index": "not-an-int"}
    with pytest.raises(AgentRuntimeError, match="Workflow Run 待审批恢复位置无效"):
        WorkflowApprovalResumeContext.from_run(
            run,
            bad_pending,
            workflow=workflow,
            root_group=False,
        )
    negative_pending = {**pending, "workflow_next_index": "-1"}
    with pytest.raises(AgentRuntimeError, match="Workflow Run 待审批恢复位置无效"):
        WorkflowApprovalResumeContext.from_run(
            run,
            negative_pending,
            workflow=workflow,
            root_group=False,
        )


def test_workflow_approval_resume_coordinator_claims_and_handoffs():
    calls: list[tuple[str, dict[str, object]]] = []
    claim_result = {"value": True}
    run = {"run_id": "workflow_run"}
    pending = {"tool": "workflow.approval", "approval_id": "approval-1"}
    workflow = {"workflow_id": "workflow_resume"}
    timeline = [{"event": "workflow.node.approval_required"}]
    artifacts = [{"path": "summary.md"}]
    context = WorkflowApprovalResumeContext(
        approval=WorkflowApprovalTransitionContext(
            label="Human Gate",
            workflow_node_id="gate",
            criteria="Review before continuing.",
            input_preview={"checkpoint": "Human Gate"},
        ),
        workflow=workflow,
        result_context="approved context",
        timeline=timeline,
        artifacts=artifacts,
        start_index=3,
        root_group=True,
        expected_approval_id="approval-1",
    )

    def claim_pending_approval(run_id, approval_payload):
        calls.append(("claim_pending_approval", {"run_id": run_id, "pending": approval_payload}))
        return claim_result["value"]

    def get_current_run(run_id):
        calls.append(("get_current_run", {"run_id": run_id}))
        return {"run_id": run_id, "status": "approval_required"}

    def resume_after_approval_node(received_run, received_workflow, **kwargs):
        calls.append(
            (
                "resume_after_approval_node",
                {
                    "run": received_run,
                    "workflow": received_workflow,
                    **kwargs,
                },
            )
        )
        return {"run_id": received_run["run_id"], "status": "completed"}

    coordinator = WorkflowApprovalResumeCoordinator(
        claim_pending_approval=claim_pending_approval,
        get_current_run=get_current_run,
        resume_after_approval_node=resume_after_approval_node,
    )

    completed = coordinator.resume_after_approval(run, pending, context)
    claim_result["value"] = False
    duplicate = coordinator.resume_after_approval(run, pending, context)

    assert completed == {"run_id": "workflow_run", "status": "completed"}
    assert duplicate == {"run_id": "workflow_run", "status": "approval_required"}
    assert [name for name, _payload in calls] == [
        "claim_pending_approval",
        "resume_after_approval_node",
        "claim_pending_approval",
        "get_current_run",
    ]
    handoff = calls[1][1]
    assert handoff["run"] is run
    assert handoff["workflow"] is workflow
    assert handoff["context"] == "approved context"
    assert handoff["timeline"] is timeline
    assert handoff["artifacts"] is artifacts
    assert handoff["start_index"] == 3
    assert handoff["root_group"] is True
    assert handoff["workflow_node_id"] == "gate"
    assert handoff["label"] == "Human Gate"
    assert handoff["criteria"] == "Review before continuing."
    assert handoff["input_preview"] == {"checkpoint": "Human Gate"}
    assert handoff["expected_approval_id"] == "approval-1"


def test_approval_resume_coordinator_executes_approved_tool_and_remaining_requests():
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [{"role": "user", "content": "run approved tool"}]
    tool_request = {"name": "terminal.run", "input": {"command": "printf ok"}}
    remaining_requests = [{"name": "artifact.write", "input": {"path": "report.md"}}]

    def call_agent_tool(
        request,
        allowed_tools,
        tool_broker,
        run_timeline,
        *,
        artifacts,
        approved,
        run_id,
        budget,
    ):
        calls.append(
            (
                "call_agent_tool",
                {
                    "request": request,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "timeline": run_timeline,
                    "artifacts": artifacts,
                    "approved": approved,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )
        return {"ok": True, "stdout": "ok"}

    def append_tool_result_message(run_messages, request, tool_result):
        calls.append(("append_tool_result_message", {"request": request, "tool_result": tool_result}))
        run_messages.append({"role": "tool", "content": json.dumps(tool_result)})

    def run_tool_requests(
        requests,
        allowed_tools,
        tool_broker,
        run_messages,
        run_timeline,
        run_artifacts,
        *,
        next_iteration,
        run_id,
        budget,
    ):
        calls.append(
            (
                "run_tool_requests",
                {
                    "requests": requests,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "messages": run_messages,
                    "timeline": run_timeline,
                    "artifacts": run_artifacts,
                    "next_iteration": next_iteration,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=call_agent_tool,
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=append_tool_result_message,
        run_tool_requests=run_tool_requests,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
    )

    coordinator.execute_approved_tool(
        ToolApprovalResumeContext(
            run_id="run_approved",
            timeline=timeline,
            artifacts=artifacts,
            broker=broker,
            allowed_tools=["terminal.run", "artifact.write"],
            budget=budget,
            messages=messages,
            tool_request=tool_request,
            tool_name="terminal.run",
            input_preview={"command": "printf ok"},
            remaining_requests=remaining_requests,
            next_iteration=7,
        )
    )

    assert [name for name, _payload in calls] == [
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
    ]
    assert calls[0][1]["approved"] is True
    assert calls[0][1]["run_id"] == "run_approved"
    assert calls[0][1]["broker"] is broker
    assert calls[0][1]["budget"] is budget
    assert calls[1][1]["tool_result"] == {"ok": True, "stdout": "ok"}
    assert calls[2][1]["requests"] == remaining_requests
    assert calls[2][1]["next_iteration"] == 7
    assert messages[-1] == {"role": "tool", "content": '{"ok": true, "stdout": "ok"}'}


def test_tool_approval_execution_request_calls_approved_tool_with_context_payload():
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = [{"event": "agent.tool.approval_required"}]
    artifacts: list[dict[str, object]] = [{"path": "report.md"}]
    tool_request = {"tool": "terminal.run", "input": {"command": "printf ok"}}
    context = ToolApprovalResumeContext(
        run_id="run_approved",
        timeline=timeline,
        artifacts=artifacts,
        broker=broker,
        allowed_tools=["terminal.run", "artifact.write"],
        budget=budget,
        messages=[],
        tool_request=tool_request,
        tool_name="terminal.run",
        input_preview={"command": "printf ok"},
        remaining_requests=[],
        next_iteration=7,
    )

    def call_agent_tool(
        request,
        allowed_tools,
        tool_broker,
        run_timeline,
        *,
        artifacts,
        approved,
        run_id,
        budget,
    ):
        calls.append(
            (
                "call_agent_tool",
                {
                    "request": request,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "timeline": run_timeline,
                    "artifacts": artifacts,
                    "approved": approved,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )
        return {"ok": True, "stdout": "ok"}

    request = ToolApprovalExecutionRequest.from_context(context)

    assert request.execute(call_agent_tool) == {"ok": True, "stdout": "ok"}
    assert calls == [
        (
            "call_agent_tool",
            {
                "request": tool_request,
                "allowed_tools": ["terminal.run", "artifact.write"],
                "broker": broker,
                "timeline": timeline,
                "artifacts": artifacts,
                "approved": True,
                "run_id": "run_approved",
                "budget": budget,
            },
        )
    ]
    assert request.timeline is timeline
    assert request.artifacts is artifacts


def test_tool_approval_execution_followup_appends_result_and_runs_remaining_requests():
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = [{"event": "agent.tool.approval_required"}]
    artifacts: list[dict[str, object]] = [{"path": "report.md"}]
    messages: list[dict[str, object]] = [{"role": "assistant", "content": "Need approval"}]
    tool_request = {"tool": "terminal.run", "input": {"command": "printf ok"}}
    remaining_requests = [{"tool": "artifact.write", "input": {"path": "report.md"}}]
    tool_result = {"ok": True, "stdout": "ok"}
    context = ToolApprovalResumeContext(
        run_id="run_approved",
        timeline=timeline,
        artifacts=artifacts,
        broker=broker,
        allowed_tools=["terminal.run", "artifact.write"],
        budget=budget,
        messages=messages,
        tool_request=tool_request,
        tool_name="terminal.run",
        input_preview={"command": "printf ok"},
        remaining_requests=remaining_requests,
        next_iteration=7,
    )

    def append_tool_result_message(run_messages, request, result):
        calls.append(("append_tool_result_message", {"messages": run_messages, "request": request, "result": result}))
        run_messages.append({"role": "tool", "content": json.dumps(result)})

    def run_tool_requests(
        requests,
        allowed_tools,
        tool_broker,
        run_messages,
        run_timeline,
        run_artifacts,
        *,
        next_iteration,
        run_id,
        budget,
    ):
        calls.append(
            (
                "run_tool_requests",
                {
                    "requests": requests,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "messages": run_messages,
                    "timeline": run_timeline,
                    "artifacts": run_artifacts,
                    "next_iteration": next_iteration,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )

    followup = ToolApprovalExecutionFollowup.from_context(context, tool_result)
    followup.apply(append_tool_result_message, run_tool_requests)

    assert [name for name, _payload in calls] == [
        "append_tool_result_message",
        "run_tool_requests",
    ]
    assert calls[0][1] == {
        "messages": messages,
        "request": tool_request,
        "result": tool_result,
    }
    assert calls[1][1] == {
        "requests": remaining_requests,
        "allowed_tools": ["terminal.run", "artifact.write"],
        "broker": broker,
        "messages": messages,
        "timeline": timeline,
        "artifacts": artifacts,
        "next_iteration": 7,
        "run_id": "run_approved",
        "budget": budget,
    }
    assert messages[-1] == {"role": "tool", "content": '{"ok": true, "stdout": "ok"}'}
    assert followup.timeline is timeline
    assert followup.artifacts is artifacts


def test_approval_resume_schedules_code_verification_after_approved_patch():
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = [
        {
            "event": "agent.task_core.created",
            "payload": {
                "decision_id": "decision-code",
                "plan_id": "plan-code",
                "core_id": "task-core-code",
                "task_core": {
                    "core_id": "task-core-code",
                    "workspace": {"workspace_id": "workspace-code"},
                    "todos": [
                        {
                            "todo_id": "todo-patch",
                            "title": "Apply code changes",
                            "status": "pending",
                            "step_id": "apply-code-changes",
                            "tool_name": "workspace.write_patch",
                        },
                        {
                            "todo_id": "todo-verify",
                            "title": "Verify code changes",
                            "status": "pending",
                            "step_id": "verify-code-changes",
                            "tool_name": "terminal.run",
                        },
                    ],
                    "checkpoints": [
                        {
                            "checkpoint_id": "checkpoint-patch",
                            "title": "Verify Apply code changes",
                            "status": "waiting_approval",
                            "after_step_id": "apply-code-changes",
                        },
                        {
                            "checkpoint_id": "checkpoint-verify",
                            "title": "Verify Verify code changes",
                            "status": "waiting_approval",
                            "after_step_id": "verify-code-changes",
                        },
                    ],
                },
            },
        },
        {
            "event": "agent.model.followup_context",
            "planning_reason": "planner_fallback_code_diagnostic",
            "pending_plan_steps": [
                {
                    "step_id": "apply-code-changes",
                    "tool_name": "workspace.write_patch",
                    "capability_id": "file.workspace_write",
                    "action": "apply_patch",
                    "depends_on": ["run-code-diagnostic"],
                },
                {
                    "step_id": "verify-code-changes",
                    "tool_name": "terminal.run",
                    "capability_id": "terminal.execution",
                    "input_preview": {"command": "python -m pytest"},
                    "depends_on": ["apply-code-changes"],
                },
            ],
        }
    ]
    artifacts: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [{"role": "assistant", "content": "Need approval"}]
    tool_request = {
        "tool": "workspace.write_patch",
        "input": {"path": "app.py", "patch": "--- app.py\n+++ app.py\n"},
        "step_id": "apply-code-changes",
        "capability_id": "file.workspace_write",
    }

    def call_agent_tool(
        request,
        allowed_tools,
        tool_broker,
        run_timeline,
        *,
        artifacts,
        approved,
        run_id,
        budget,
    ):
        calls.append(
            (
                "call_agent_tool",
                {
                    "request": request,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "timeline": run_timeline,
                    "artifacts": artifacts,
                    "approved": approved,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )
        result = {"ok": True, "mode": "patch", "path": "app.py"}
        run_timeline.append(
            {
                "event": "agent.tool.call",
                "detail": "workspace.write_patch",
                "result": result,
            }
        )
        return result

    def append_tool_result_message(run_messages, request, result):
        calls.append(("append_tool_result_message", {"request": request, "result": result}))
        run_messages.append({"role": "tool", "content": json.dumps(result)})

    def run_tool_requests(
        requests,
        allowed_tools,
        tool_broker,
        run_messages,
        run_timeline,
        run_artifacts,
        *,
        next_iteration,
        run_id,
        budget,
    ):
        calls.append(
            (
                "run_tool_requests",
                {
                    "requests": requests,
                    "allowed_tools": allowed_tools,
                    "broker": tool_broker,
                    "messages": run_messages,
                    "timeline": run_timeline,
                    "artifacts": run_artifacts,
                    "next_iteration": next_iteration,
                    "run_id": run_id,
                    "budget": budget,
                },
            )
        )

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=call_agent_tool,
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=append_tool_result_message,
        run_tool_requests=run_tool_requests,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
    )

    coordinator.execute_approved_tool(
        ToolApprovalResumeContext(
            run_id="run_patch",
            timeline=timeline,
            artifacts=artifacts,
            broker=broker,
            allowed_tools=["workspace.write_patch", "terminal.run"],
            budget=budget,
            messages=messages,
            tool_request=tool_request,
            tool_name="workspace.write_patch",
            input_preview={"path": "app.py"},
            remaining_requests=[],
            next_iteration=4,
        )
    )

    assert [name for name, _payload in calls] == [
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
    ]
    assert calls[2][1]["requests"] == [
        {
            "protocol": "json_fallback",
            "tool": "terminal.run",
            "input": {"command": "python -m pytest"},
            "source": "runtime_planner",
            "planning_reason": "planner_followup_verify_code_changes",
            "continue_to_model": True,
            "step_id": "verify-code-changes",
            "capability_id": "terminal.execution",
        }
    ]
    assert calls[2][1]["next_iteration"] == 4
    assert messages[-1] == {
        "role": "tool",
        "content": '{"ok": true, "mode": "patch", "path": "app.py"}',
    }
    todo_event = next(
        event
        for event in timeline
        if event.get("event") == "agent.task.todo.updated"
        and event.get("step_id") == "apply-code-changes"
    )
    checkpoint_event = next(
        event
        for event in timeline
        if event.get("event") == "agent.task.checkpoint.updated"
        and event.get("step_id") == "apply-code-changes"
    )
    assert todo_event["status"] == "completed"
    assert todo_event["todo"]["status"] == "completed"
    assert checkpoint_event["status"] == "completed"
    assert checkpoint_event["checkpoint"]["status"] == "completed"


def test_approval_resume_coordinator_claims_and_projects_approved_tool_once():
    calls: list[tuple[str, dict[str, object]]] = []
    timeline: list[dict[str, object]] = [{"event": "agent.tool.approval_required"}]
    artifacts: list[dict[str, object]] = [{"path": "report.md"}]
    pending = {"tool": "terminal.run", "approval_id": "approval-1"}
    claim_result = True

    def claim_pending_approval(run_id, approval_payload):
        calls.append(
            (
                "claim_pending_approval",
                {"run_id": run_id, "pending": approval_payload},
            )
        )
        return claim_result

    def approve_tool_run(run_id, **kwargs):
        calls.append(("approve_tool_run", {"run_id": run_id, **kwargs}))
        return {"run_id": run_id, "status": "running", "result": kwargs["running_result"]}

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=lambda *_args, **_kwargs: {"ok": True},
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=lambda *_args: None,
        run_tool_requests=lambda *_args, **_kwargs: None,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        claim_pending_approval=claim_pending_approval,
        approve_tool_run=approve_tool_run,
    )
    context = ToolApprovalResumeContext(
        run_id="run_approval",
        timeline=timeline,
        artifacts=artifacts,
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["terminal.run"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"name": "terminal.run", "input": {"command": "printf ok"}},
        tool_name="terminal.run",
        input_preview={"command": "printf ok"},
        remaining_requests=[],
        next_iteration=3,
    )

    running = coordinator.claim_and_project_approved_tool(
        "run_approval",
        pending,
        context,
        resumed_detail="Agent resumed after approval",
        running_result="已批准，Agent 正在继续执行",
    )
    claim_result = False
    duplicate = coordinator.claim_and_project_approved_tool(
        "run_approval",
        pending,
        context,
        resumed_detail="Agent resumed after approval",
        running_result="已批准，Agent 正在继续执行",
    )

    assert running == {
        "run_id": "run_approval",
        "status": "running",
        "result": "已批准，Agent 正在继续执行",
    }
    assert duplicate is None
    assert [name for name, _payload in calls] == [
        "claim_pending_approval",
        "approve_tool_run",
        "claim_pending_approval",
    ]
    assert calls[1][1] == {
        "run_id": "run_approval",
        "timeline": timeline,
        "artifacts": artifacts,
        "tool_name": "terminal.run",
        "input_preview": {"command": "printf ok"},
        "resumed_detail": "Agent resumed after approval",
        "running_result": "已批准，Agent 正在继续执行",
    }


def test_tool_approval_claim_projection_builds_running_payload():
    calls: list[tuple[str, dict[str, object]]] = []
    timeline: list[dict[str, object]] = [{"event": "agent.tool.approval_required"}]
    artifacts: list[dict[str, object]] = [{"path": "report.md"}]
    context = ToolApprovalResumeContext(
        run_id="context_run",
        timeline=timeline,
        artifacts=artifacts,
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["terminal.run"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"tool": "terminal.run", "input": {"command": "printf ok"}},
        tool_name="terminal.run",
        input_preview={"command": "printf ok"},
        remaining_requests=[],
        next_iteration=3,
    )
    projection = ToolApprovalClaimProjection.from_context(
        "run_approval",
        context,
        resumed_detail="Agent resumed after approval",
        running_result="已批准，Agent 正在继续执行",
    )

    def approve_tool_run(run_id, **kwargs):
        calls.append(("approve_tool_run", {"run_id": run_id, **kwargs}))
        return {"run_id": run_id, "status": "running", "result": kwargs["running_result"]}

    assert projection.project(approve_tool_run) == {
        "run_id": "run_approval",
        "status": "running",
        "result": "已批准，Agent 正在继续执行",
    }
    assert calls == [
        (
            "approve_tool_run",
            {
                "run_id": "run_approval",
                "timeline": timeline,
                "artifacts": artifacts,
                "tool_name": "terminal.run",
                "input_preview": {"command": "printf ok"},
                "resumed_detail": "Agent resumed after approval",
                "running_result": "已批准，Agent 正在继续执行",
            },
        )
    ]
    assert projection.timeline is timeline
    assert projection.artifacts is artifacts


def test_approval_resume_coordinator_orchestrates_resume_projection_states():
    calls: list[str] = []
    mode = {"value": "completed"}
    original_goal = "Run the approved commands and report their results"
    next_pending = {
        "tool": "terminal.run",
        "approval_id": "next",
        "input": {"command": "printf next"},
        "tool_request": {"tool": "terminal.run", "input": {"command": "printf next"}},
        "messages": [{"role": "user", "content": original_goal}],
        "remaining_tool_requests": [],
        "next_iteration": 3,
    }

    def continue_custom_api_agent(_agent, user_goal, *_args, **kwargs):
        assert user_goal == original_goal
        assert kwargs["original_goal"] == original_goal
        calls.append("continue_custom_api_agent")
        if mode["value"] == "required":
            raise AgentApprovalRequired(dict(next_pending))
        if mode["value"] == "failed":
            raise RuntimeError("provider raw failure")
        return "resumed output"

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=lambda *_args, **_kwargs: {"ok": True},
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=lambda *_args: calls.append("append_tool_result_message"),
        run_tool_requests=lambda *_args, **_kwargs: calls.append("run_tool_requests"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        claim_pending_approval=lambda *_args: calls.append("claim_pending_approval") or True,
        approve_tool_run=lambda run_id, **_kwargs: calls.append("approve_tool_run") or {"run_id": run_id, "status": "running"},
        continue_custom_api_agent=continue_custom_api_agent,
    )
    def make_context():
        contract = GoalContract(
            contract_id="contract-resume-projection",
            run_id="run_resume_projection",
            original_goal=original_goal,
            criteria=(GoalCriterion(
                criterion_id="approved-commands",
                description="Execute the approved commands",
                effectful=True,
                response_satisfiable=False,
            ),),
        )
        return ToolApprovalResumeContext.from_run(
            {"run_id": "run_resume_projection", "user_goal": original_goal,
             "goal_contract": contract.to_payload()},
            {
                "approval_id": "approval-resume-projection",
                "tool": "terminal.run",
                "messages": [{"role": "user", "content": original_goal}],
                "tool_request": {"tool": "terminal.run", "input": {"command": "printf ok"}},
                "remaining_tool_requests": [],
                "next_iteration": 2,
            },
            broker=SimpleNamespace(name="broker"),
            allowed_tools=["terminal.run"],
            budget=SimpleNamespace(name="budget"),
        )

    context = make_context()

    def run_mode(value: str) -> dict[str, object]:
        mode["value"] = value
        # Each outcome represents an independent claimed approval generation.
        context = make_context()
        return coordinator.resume_approved_tool_run(
            run_id=context.run_id,
            pending={
                "approval_id": "approval-resume-projection",
                "tool": "terminal.run",
            },
            context=context,
            agent={"agent_id": "agent_resume"},
            resumed_detail="Agent resumed after approval",
            running_result="已批准，Agent 正在继续执行",
            project_running=lambda running: calls.append("project_running") or {**running, "projected_running": True},
            project_completed=lambda _context, result_text: {
                "status": "completed",
                "result": result_text,
                "projection": calls.append("project_completed") or "completed",
            },
            prepare_required=lambda pending: {**pending, "prepared": pending["tool"]},
            project_required=lambda _context, pending: {
                "status": "approval_required",
                "pending": pending,
                "projection": calls.append("project_required") or "required",
            },
            project_failed=lambda _context, safe_error: {
                "status": "failed",
                "error": safe_error,
                "projection": calls.append("project_failed") or "failed",
            },
            get_current_run=lambda run_id: {"run_id": run_id, "status": "current"},
            project_result=lambda result: {**result, "finalized": True},
            redact_error=lambda exc: f"safe {type(exc).__name__}",
        )

    assert run_mode("completed") == {
        "status": "completed",
        "result": "resumed output",
        "projection": "completed",
        "finalized": True,
    }
    assert run_mode("required") == {
        "status": "approval_required",
        "pending": {**next_pending, "prepared": "terminal.run"},
        "projection": "required",
        "finalized": True,
    }
    assert run_mode("failed") == {
        "status": "failed",
        "error": "safe RuntimeError",
        "projection": "failed",
        "finalized": True,
    }
    assert calls == [
        "claim_pending_approval",
        "approve_tool_run",
        "project_running",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "project_completed",
        "claim_pending_approval",
        "approve_tool_run",
        "project_running",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "project_required",
        "claim_pending_approval",
        "approve_tool_run",
        "project_running",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "project_failed",
    ]

    duplicate_calls: list[str] = []

    def unexpected_resume_callback(*_args, **_kwargs):
        raise AssertionError("approval resume must not execute after duplicate claim")

    duplicate_coordinator = ApprovalResumeCoordinator(
        call_agent_tool=unexpected_resume_callback,
        fatal_tool_failure_detail=unexpected_resume_callback,
        append_tool_result_message=unexpected_resume_callback,
        run_tool_requests=unexpected_resume_callback,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        claim_pending_approval=lambda *_args: duplicate_calls.append("claim_pending_approval") or False,
        approve_tool_run=unexpected_resume_callback,
        continue_custom_api_agent=unexpected_resume_callback,
    )

    current = duplicate_coordinator.resume_approved_tool_run(
        run_id=context.run_id,
        pending={
            "approval_id": "approval-resume-projection",
            "tool": "terminal.run",
        },
        context=context,
        agent={"agent_id": "agent_resume"},
        resumed_detail="Agent resumed after approval",
        running_result="已批准，Agent 正在继续执行",
        project_running=unexpected_resume_callback,
        project_completed=unexpected_resume_callback,
        project_required=unexpected_resume_callback,
        project_failed=unexpected_resume_callback,
        get_current_run=lambda run_id: duplicate_calls.append("get_current_run") or {
            "run_id": run_id,
            "status": "approval_required",
        },
        project_result=unexpected_resume_callback,
    )

    assert current == {"run_id": context.run_id, "status": "approval_required"}
    assert duplicate_calls == ["claim_pending_approval", "get_current_run"]


def test_approval_resume_projection_coordinator_projects_resume_states():
    appended_events: list[tuple[str, str, dict[str, object]]] = []
    updated_runs: list[dict[str, object]] = []
    group_updates: list[dict[str, object]] = []
    parent_marks: list[dict[str, object]] = []

    def make_context(run_id: str) -> ToolApprovalResumeContext:
        return ToolApprovalResumeContext(
            run_id=run_id,
            timeline=[{"event": "agent.tool.approval_approved"}],
            artifacts=[{"path": f"{run_id}.md"}],
            broker=SimpleNamespace(name="broker"),
            allowed_tools=["terminal.run"],
            budget=SimpleNamespace(name="budget"),
            messages=[],
            tool_request={"name": "terminal.run", "input": {"command": "printf ok"}},
            tool_name="terminal.run",
            input_preview={"command": "printf ok"},
            remaining_requests=[],
            next_iteration=3,
            approval_id=f"approval-{run_id}",
        )

    def update_run(run_id, **kwargs):
        updated_runs.append({"run_id": run_id, **kwargs})
        return {"run_id": run_id, **kwargs}

    coordinator = ApprovalResumeProjectionCoordinator(
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda run_id, event_type, payload: appended_events.append((run_id, event_type, payload)),
        update_run=update_run,
        update_agent_run_group_if_root=lambda run: group_updates.append(run),
        mark_parent_workflows_child_running=lambda run: parent_marks.append(run),
    )

    running = {"run_id": "agent_run_running", "kind": "agent_run", "status": "running"}
    assert coordinator.project_agent_running(running) is running
    agent_completed = coordinator.project_agent_completed(make_context("agent_run_completed"), "Agent done")
    main_completed = coordinator.project_main_chat_completed(make_context("main_chat_run"), "Main done")
    required_context = make_context("agent_run_required")
    required_pending = {
        "approval_id": "approval-next",
        "tool": "terminal.run",
        "input_preview": {"command": "printf next"},
        "requested_at": "now",
    }
    required = coordinator.project_required(required_context, required_pending)
    required_pending["input_preview"]["command"] = "mutated after projection"
    failed = coordinator.project_failed(make_context("agent_run_failed"), "safe failure")

    assert group_updates == [running, agent_completed, failed]
    assert parent_marks == [running]
    assert agent_completed["status"] == "completed"
    assert main_completed["status"] == "running"
    assert required["status"] == "approval_required"
    assert failed["status"] == "failed"
    assert [item["status"] for item in updated_runs] == [
        "completed",
        "running",
        "approval_required",
        "failed",
    ]
    assert updated_runs[0]["timeline"][-1] == {
        "event": "agent.run.completed",
        "detail": "Agent run completed",
    }
    assert updated_runs[1]["timeline"][-1] == {
        "event": "model.output.ready",
        "detail": "Main done",
        "output_chars": 9,
    }
    assert required_context.timeline[-1]["pending_approval"]["input_preview"] == {
        "command": "printf next"
    }
    assert updated_runs[2]["result"] == "等待审批：terminal.run"
    assert updated_runs[2]["pending_approval"]["approval_id"] == "approval-next"
    assert updated_runs[2]["pending_approval"]["input_preview"] == {"command": "printf next"}
    assert updated_runs[2]["pending_approval"] is not required_pending
    assert updated_runs[3]["result"] == "safe failure"
    assert appended_events == [
        ("agent_run_completed", "agent.run.completed", {"result": "Agent done"}),
        (
            "main_chat_run",
            "model.output.completed",
            {"content": "Main done", "output_chars": 9},
        ),
        (
            "agent_run_required",
            "agent.tool.approval_required",
            {
                "approval_id": "approval-next",
                "tool": "terminal.run",
                "input_preview": {"command": "printf next"},
                "requested_at": "now",
                "risk_level": "high",
                "policy_reason": "terminal.run 可执行本地命令，按工具策略必须人工确认。",
            },
        ),
        ("agent_run_failed", "agent.run.failed", {"error": "safe failure"}),
    ]


def test_main_chat_direct_approval_projection_finishes_through_chat_lifecycle():
    events: list[tuple[str, str, dict[str, object], dict[str, str]]] = []
    lifecycle_calls: list[tuple[str, str]] = []
    context = ToolApprovalResumeContext(
        run_id="main-chat-direct",
        timeline=[
            {
                "event": "agent.desktop.intent_completed",
                "source": "runtime_planner",
            }
        ],
        artifacts=[],
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["desktop.quit_app"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"tool": "desktop.quit_app", "source": "runtime_planner"},
        tool_name="desktop.quit_app",
        input_preview={},
        remaining_requests=[],
        next_iteration=1,
        approval_id="approval-main-chat-direct",
    )

    def append_event(run_id, event_type, payload, **fence):
        events.append((run_id, event_type, payload, fence))
        return {"event_type": event_type}

    coordinator = ApprovalResumeProjectionCoordinator(
        timeline_factory=lambda event, detail, **payload: {
            "event": event,
            "detail": detail,
            **payload,
        },
        append_run_event=append_event,
        update_run=lambda run_id, **fields: {
            "run_id": run_id,
            "updated_at": "version-2",
            **fields,
        },
        update_agent_run_group_if_root=lambda _run: None,
        mark_parent_workflows_child_running=lambda _run: None,
        complete_main_chat_run=lambda run_id, result: lifecycle_calls.append(
            (run_id, result)
        )
        or {"run_id": run_id, "status": "completed", "result": result},
    )

    projected = coordinator.project_main_chat_completed(context, "done")

    assert projected["status"] == "completed"
    assert lifecycle_calls == [("main-chat-direct", "done")]
    assert events == [
        (
            "main-chat-direct",
            "model.output.completed",
            {"content": "done", "output_chars": 4},
            {"expected_status": "running", "expected_updated_at": "version-2"},
        )
    ]


def test_main_chat_approval_failure_uses_chat_lifecycle_not_agent_projection():
    failure_calls: list[dict[str, object]] = []
    context = ToolApprovalResumeContext(
        run_id="main-chat-failed",
        timeline=[{"event": "agent.tool.failed"}],
        artifacts=[{"path": "failure.txt"}],
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["terminal.run"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"tool": "terminal.run"},
        tool_name="terminal.run",
        input_preview={},
        remaining_requests=[],
        next_iteration=1,
    )
    coordinator = ApprovalResumeProjectionCoordinator(
        timeline_factory=lambda event, detail, **payload: {
            "event": event,
            "detail": detail,
            **payload,
        },
        append_run_event=lambda *_args, **_kwargs: pytest.fail(
            "main-chat lifecycle owns failure events"
        ),
        update_run=lambda *_args, **_kwargs: pytest.fail(
            "main-chat lifecycle owns the terminal row"
        ),
        update_agent_run_group_if_root=lambda _run: None,
        mark_parent_workflows_child_running=lambda _run: None,
        fail_main_chat_run=lambda run_id, error, **kwargs: failure_calls.append(
            {"run_id": run_id, "error": error, **kwargs}
        )
        or {"run_id": run_id, "status": "failed", "result": error},
    )

    failed = coordinator.project_main_chat_failed(context, "safe failure")

    assert failed["status"] == "failed"
    assert failure_calls == [
        {
            "run_id": "main-chat-failed",
            "error": "safe failure",
            "timeline": context.timeline,
            "artifacts": context.artifacts,
        }
    ]


def test_pending_approval_snapshot_is_isolated_before_resume():
    messages = [{"role": "user", "content": "run approved tool", "meta": {"turn": 1}}]
    tool_request = {
        "tool": "terminal.run",
        "input": {"command": "printf ok", "options": {"timeout": 3}},
    }
    remaining_requests = [
        {
            "tool": "artifact.write",
            "input": {"path": "report.md", "content": "ok"},
        }
    ]

    pending = NativeRunEngine._make_pending_approval(
        tool_request,
        messages=messages,
        next_iteration=5,
        remaining_tool_requests=remaining_requests,
    )

    assert pending["input"] == {"command": "printf ok", "options": {"timeout": 3}}
    assert pending["messages"] == messages
    assert pending["tool_request"] == tool_request
    assert pending["remaining_tool_requests"] == remaining_requests
    assert pending["next_iteration"] == 5
    assert pending["input"] is not tool_request["input"]
    assert pending["input"] is not pending["tool_request"]["input"]
    assert pending["input"]["options"] is not tool_request["input"]["options"]
    assert pending["messages"] is not messages
    assert pending["messages"][0] is not messages[0]
    assert pending["tool_request"] is not tool_request
    assert pending["tool_request"]["input"] is not tool_request["input"]
    assert pending["remaining_tool_requests"] is not remaining_requests
    assert pending["remaining_tool_requests"][0] is not remaining_requests[0]
    assert pending["remaining_tool_requests"][0]["input"] is not remaining_requests[0]["input"]

    messages[0]["meta"]["turn"] = 2
    tool_request["input"]["command"] = "changed"
    tool_request["input"]["options"]["timeout"] = 9
    remaining_requests[0]["input"]["path"] = "changed.md"

    assert pending["input"] == {"command": "printf ok", "options": {"timeout": 3}}
    assert pending["messages"] == [{"role": "user", "content": "run approved tool", "meta": {"turn": 1}}]
    assert pending["tool_request"] == {
        "tool": "terminal.run",
        "input": {"command": "printf ok", "options": {"timeout": 3}},
    }
    assert pending["remaining_tool_requests"] == [
        {
            "tool": "artifact.write",
            "input": {"path": "report.md", "content": "ok"},
        }
    ]

    pending["input"]["command"] = "pending input changed"
    pending["tool_request"]["input"]["command"] = "pending request changed"
    pending["remaining_tool_requests"][0]["input"]["content"] = "pending content changed"

    assert tool_request == {
        "tool": "terminal.run",
        "input": {"command": "changed", "options": {"timeout": 9}},
    }
    assert remaining_requests == [
        {
            "tool": "artifact.write",
            "input": {"path": "changed.md", "content": "ok"},
        }
    ]
    assert pending["input"]["command"] == "pending input changed"
    assert pending["tool_request"]["input"]["command"] == "pending request changed"
    assert pending["remaining_tool_requests"][0]["input"]["content"] == "pending content changed"


def test_tool_approval_resume_context_parses_pending_payload():
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    budget_calls: list[dict[str, object]] = []
    run = {
        "run_id": "agent_run_resume",
        "user_goal": "Run the approved command and write its result",
        "timeline": [
            {"event": "agent.tool.approval_required"},
            "not-an-event",
        ],
        "artifacts": [
            {"path": "report.md"},
            "not-an-artifact",
        ],
    }
    run["goal_contract"] = GoalContract(
        contract_id="contract-agent-run-resume",
        run_id=run["run_id"],
        original_goal=run["user_goal"],
        criteria=(GoalCriterion(
            criterion_id="approved-command-and-artifact",
            description="Execute the command and write its result",
            effectful=True,
            response_satisfiable=False,
        ),),
    ).to_payload()
    messages = [{"role": "user", "content": "run approved tool"}]
    tool_request = {
        "tool": "terminal.run",
        "input": {"command": "printf ok"},
    }
    pending = {
        "tool": "terminal.run",
        "messages": messages,
        "tool_request": tool_request,
        "remaining_tool_requests": [
            {"tool": "artifact.write", "input": {"path": "report.md"}},
            "not-a-request",
        ],
        "next_iteration": "5",
    }

    def budget_factory(run_id, timeline):
        budget_calls.append({"run_id": run_id, "timeline": timeline})
        return budget

    allowed_tools = ["terminal.run", "artifact.write"]
    context = ToolApprovalResumeContext.from_run(
        run,
        pending,
        broker=broker,
        allowed_tools=allowed_tools,
        budget_factory=budget_factory,
    )

    assert context.run_id == "agent_run_resume"
    assert context.goal_contract.original_goal == run["user_goal"]
    assert context.goal_contract.run_id == run["run_id"]
    assert context.goal_contract.criteria[0].effectful is True
    assert context.goal_contract.criteria[0].response_satisfiable is False
    assert len(context.approval_request_fingerprint) == 64
    assert context.timeline == [{"event": "agent.tool.approval_required"}]
    assert context.artifacts == [{"path": "report.md"}]
    assert context.broker is broker
    assert context.allowed_tools == ["terminal.run", "artifact.write"]
    assert context.allowed_tools is not allowed_tools
    assert context.budget is budget
    assert context.messages == messages
    assert context.messages is not messages
    assert context.tool_request == tool_request
    assert context.tool_request is not tool_request
    assert context.tool_name == "terminal.run"
    assert context.input_preview == {"command": "printf ok"}
    assert context.remaining_requests == [{"tool": "artifact.write", "input": {"path": "report.md"}}]
    assert context.next_iteration == 5
    assert budget_calls == [{"run_id": "agent_run_resume", "timeline": context.timeline}]
    assert budget_calls[0]["timeline"] is context.timeline
    context.messages.append({"role": "tool", "content": "ok"})
    context.tool_request["input"]["command"] = "changed"
    context.remaining_requests[0]["input"]["path"] = "changed.md"
    context.timeline[0]["event"] = "changed"
    context.artifacts[0]["path"] = "changed.md"
    context.allowed_tools.append("workspace.read")
    assert pending["messages"] == [{"role": "user", "content": "run approved tool"}]
    assert pending["tool_request"] == {
        "tool": "terminal.run",
        "input": {"command": "printf ok"},
    }
    assert pending["remaining_tool_requests"][0] == {
        "tool": "artifact.write",
        "input": {"path": "report.md"},
    }
    assert run["timeline"][0] == {"event": "agent.tool.approval_required"}
    assert run["artifacts"][0] == {"path": "report.md"}
    assert pending["messages"] is messages
    assert pending["tool_request"] is tool_request
    pending["messages"][0]["content"] = "Replace the original objective"
    assert context.goal_contract.original_goal == run["user_goal"]

    fallback_iteration = ToolApprovalResumeContext.from_run(
        run,
        {**pending, "next_iteration": "not-an-int"},
        broker=broker,
        allowed_tools=[],
        budget=budget,
    )
    assert fallback_iteration.next_iteration == 0
    negative_iteration = ToolApprovalResumeContext.from_run(
        run,
        {**pending, "next_iteration": "-3"},
        broker=broker,
        allowed_tools=[],
        budget=budget,
    )
    assert negative_iteration.next_iteration == 0
    capped_iteration = ToolApprovalResumeContext.from_run(
        run,
        {**pending, "next_iteration": "999"},
        broker=broker,
        allowed_tools=[],
        budget=budget,
    )
    assert capped_iteration.next_iteration == _MAX_AGENT_TOOL_ITERATIONS

    with pytest.raises(AgentRuntimeError, match="Run 待审批上下文不完整，无法恢复"):
        ToolApprovalResumeContext.from_run(
            run,
            {"tool": "terminal.run", "messages": [], "tool_request": tool_request},
            broker=broker,
            allowed_tools=[],
            budget=budget,
        )


    for missing_goal in (None, "", " "):
        with pytest.raises(AgentRuntimeError, match="approval_resume_goal_contract_missing"):
            ToolApprovalResumeContext.from_run(
                {**run, "user_goal": missing_goal},
                pending,
                broker=broker,
                allowed_tools=allowed_tools,
                budget=budget,
            )
    with pytest.raises(AgentRuntimeError, match="approval_resume_goal_contract_invalid"):
        ToolApprovalResumeContext.from_run(
            {**run, "user_goal": "Replace the original objective"},
            pending,
            broker=broker,
            allowed_tools=allowed_tools,
            budget=budget,
        )

def test_approval_resume_coordinator_stops_on_fatal_tool_failure():
    calls: list[str] = []
    timeline: list[dict[str, object]] = []

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=lambda *_args, **_kwargs: {"ok": False, "stderr": "denied"},
        fatal_tool_failure_detail=lambda *_args: "terminal.run failed fatally",
        append_tool_result_message=lambda *_args: calls.append("append_tool_result_message"),
        run_tool_requests=lambda *_args, **_kwargs: calls.append("run_tool_requests"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
    )

    with pytest.raises(AgentRuntimeError, match="terminal.run failed fatally"):
        coordinator.execute_approved_tool(
            ToolApprovalResumeContext(
                run_id="run_failed",
                timeline=timeline,
                artifacts=[],
                broker=SimpleNamespace(name="broker"),
                allowed_tools=["terminal.run"],
                budget=SimpleNamespace(name="budget"),
                messages=[],
                tool_request={"name": "terminal.run", "input": {"command": "false"}},
                tool_name="terminal.run",
                input_preview={"command": "false"},
                remaining_requests=[{"name": "artifact.write"}],
                next_iteration=2,
            )
        )

    assert calls == []
    assert timeline == [
        {
            "event": "agent.tool.failed",
            "detail": "terminal.run",
            "input_preview": {"command": "false"},
            "result": {"ok": False, "stderr": "denied"},
            "status": "failed",
        }
    ]


def test_tool_approval_execution_failure_projection_builds_timeline_event():
    context = ToolApprovalResumeContext(
        run_id="run_failed_tool",
        timeline=[],
        artifacts=[],
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["terminal.run"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"name": "terminal.run", "input": {"command": "false"}},
        tool_name="",
        input_preview={"command": "false"},
        remaining_requests=[],
        next_iteration=2,
    )
    tool_result = {"ok": False, "stderr": "denied"}

    projection = ToolApprovalExecutionFailureProjection.from_context(
        context,
        tool_result,
        "terminal.run failed fatally",
    )

    assert projection.detail == "terminal.run failed fatally"
    assert projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    ) == {
        "event": "agent.tool.failed",
        "detail": "tool",
        "input_preview": {"command": "false"},
        "result": tool_result,
        "status": "failed",
    }


def test_approval_resume_coordinator_builds_continuation_handoff_after_approved_tool():
    calls: list[str] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [{"role": "user", "content": "resume"}]
    agent = {"agent_id": "agent_resume", "name": "Resume Agent"}

    def append_tool_result_message(run_messages, _request, tool_result):
        calls.append("append_tool_result_message")
        run_messages.append({"role": "tool", "content": json.dumps(tool_result)})

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=lambda *_args, **_kwargs: calls.append("call_agent_tool") or {"ok": True},
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=append_tool_result_message,
        run_tool_requests=lambda *_args, **_kwargs: calls.append("run_tool_requests"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
    )

    handoff = coordinator.continuation_handoff_after_approved_tool(
        agent,
        ToolApprovalResumeContext(
            run_id="run_resume_handoff",
            timeline=timeline,
            artifacts=artifacts,
            broker=broker,
            allowed_tools=["terminal.run"],
            budget=budget,
            messages=messages,
            tool_request={"name": "terminal.run", "input": {"command": "printf ok"}},
            tool_name="terminal.run",
            input_preview={"command": "printf ok"},
            remaining_requests=[],
            next_iteration=5,
        ),
    )

    assert isinstance(handoff, ToolApprovalContinuationHandoff)
    assert calls == ["call_agent_tool", "append_tool_result_message", "run_tool_requests"]
    assert handoff.agent is agent
    assert handoff.user_goal == ""
    assert handoff.broker is broker
    assert handoff.timeline is timeline
    assert handoff.artifacts is artifacts
    assert handoff.messages is messages
    assert handoff.start_iteration == 5
    assert handoff.run_id == "run_resume_handoff"
    assert handoff.budget is budget
    assert messages[-1] == {"role": "tool", "content": '{"ok": true}'}


def test_tool_approval_custom_api_continuation_request_calls_model_with_handoff_payload():
    calls: list[tuple[str, dict[str, object]]] = []
    agent = {"agent_id": "agent_resume", "name": "Resume Agent"}
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = [{"event": "agent.tool.completed"}]
    artifacts: list[dict[str, object]] = [{"path": "report.md"}]
    messages: list[dict[str, object]] = [{"role": "user", "content": "resume"}]
    handoff = ToolApprovalContinuationHandoff(
        agent=agent,
        user_goal="",
        broker=broker,
        timeline=timeline,
        artifacts=artifacts,
        messages=messages,
        start_iteration=5,
        run_id="run_resume",
        budget=budget,
    )

    def continue_custom_api_agent(
        received_agent,
        user_goal,
        tool_broker,
        run_timeline,
        run_artifacts,
        **kwargs,
    ):
        calls.append(
            (
                "continue_custom_api_agent",
                {
                    "agent": received_agent,
                    "user_goal": user_goal,
                    "broker": tool_broker,
                    "timeline": run_timeline,
                    "artifacts": run_artifacts,
                    "messages": kwargs["messages"],
                    "start_iteration": kwargs["start_iteration"],
                    "run_id": kwargs["run_id"],
                    "budget": kwargs["budget"],
                },
            )
        )
        return "resumed model output"

    request = ToolApprovalCustomApiContinuationRequest.from_handoff(handoff)

    assert request.execute(continue_custom_api_agent) == "resumed model output"
    assert calls == [
        (
            "continue_custom_api_agent",
            {
                "agent": agent,
                "user_goal": "",
                "broker": broker,
                "timeline": timeline,
                "artifacts": artifacts,
                "messages": messages,
                "start_iteration": 5,
                "run_id": "run_resume",
                "budget": budget,
            },
        )
    ]
    assert request.handoff is handoff


def test_approval_resume_coordinator_continues_custom_api_agent_after_approved_tool():
    calls: list[tuple[str, dict[str, object]]] = []
    broker = SimpleNamespace(name="broker")
    budget = SimpleNamespace(name="budget")
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    messages: list[dict[str, object]] = [{"role": "user", "content": "resume"}]
    agent = {"agent_id": "agent_resume", "name": "Resume Agent"}

    def call_agent_tool(*_args, **kwargs):
        calls.append(("call_agent_tool", {"approved": kwargs["approved"]}))
        return {"ok": True}

    def append_tool_result_message(run_messages, _request, tool_result):
        calls.append(("append_tool_result_message", {"tool_result": tool_result}))
        run_messages.append({"role": "tool", "content": json.dumps(tool_result)})

    def run_tool_requests(*_args, **kwargs):
        calls.append(
            (
                "run_tool_requests",
                {
                    "next_iteration": kwargs["next_iteration"],
                    "run_id": kwargs["run_id"],
                    "budget": kwargs["budget"],
                },
            )
        )

    def continue_custom_api_agent(
        received_agent,
        user_goal,
        tool_broker,
        run_timeline,
        run_artifacts,
        **kwargs,
    ):
        calls.append(
            (
                "continue_custom_api_agent",
                {
                    "agent": received_agent,
                    "user_goal": user_goal,
                    "broker": tool_broker,
                    "timeline": run_timeline,
                    "artifacts": run_artifacts,
                    "messages": kwargs["messages"],
                    "start_iteration": kwargs["start_iteration"],
                    "run_id": kwargs["run_id"],
                    "budget": kwargs["budget"],
                },
            )
        )
        return "resumed model output"

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=call_agent_tool,
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=append_tool_result_message,
        run_tool_requests=run_tool_requests,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        continue_custom_api_agent=continue_custom_api_agent,
    )

    result = coordinator.continue_custom_api_agent_after_approved_tool(
        agent,
        ToolApprovalResumeContext(
            run_id="run_resume",
            timeline=timeline,
            artifacts=artifacts,
            broker=broker,
            allowed_tools=["terminal.run"],
            budget=budget,
            messages=messages,
            tool_request={"name": "terminal.run", "input": {"command": "printf ok"}},
            tool_name="terminal.run",
            input_preview={"command": "printf ok"},
            remaining_requests=[],
            next_iteration=4,
        ),
    )

    assert result == "resumed model output"
    assert [name for name, _payload in calls] == [
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
    ]
    assert calls[-1][1]["agent"] is agent
    assert calls[-1][1]["user_goal"] == ""
    assert calls[-1][1]["broker"] is broker
    assert calls[-1][1]["timeline"] is timeline
    assert calls[-1][1]["artifacts"] is artifacts
    assert calls[-1][1]["messages"] is messages
    assert calls[-1][1]["start_iteration"] == 4
    assert calls[-1][1]["run_id"] == "run_resume"
    assert calls[-1][1]["budget"] is budget


def test_tool_approval_continuation_outcome_projects_resume_states():
    calls: list[tuple[str, dict[str, object]]] = []
    context = ToolApprovalResumeContext(
        run_id="run_resume_outcome",
        timeline=[],
        artifacts=[],
        broker=SimpleNamespace(name="broker"),
        allowed_tools=["terminal.run"],
        budget=SimpleNamespace(name="budget"),
        messages=[],
        tool_request={"tool": "terminal.run", "input": {"command": "printf ok"}},
        tool_name="terminal.run",
        input_preview={"command": "printf ok"},
        remaining_requests=[],
        next_iteration=2,
    )

    def project_completed(received_context, result_text):
        calls.append(("project_completed", {"context": received_context, "result_text": result_text}))
        return {"status": "completed", "result": result_text}

    def project_required(received_context, pending):
        calls.append(("project_required", {"context": received_context, "pending": pending}))
        return {"status": "approval_required", "pending": pending}

    def project_failed(received_context, safe_error):
        calls.append(("project_failed", {"context": received_context, "safe_error": safe_error}))
        return {"status": "failed", "error": safe_error}

    completed = ToolApprovalContinuationOutcome.completed("resumed output")
    required = ToolApprovalContinuationOutcome.approval_required(
        {"tool": "terminal.run", "approval_id": "next"},
        prepare_required=lambda pending: {**pending, "prepared": True},
    )
    failed = ToolApprovalContinuationOutcome.failed(
        RuntimeError("raw provider failure"),
        redact_error=lambda exc: f"safe {type(exc).__name__}",
    )

    assert completed.project(
        context,
        project_completed=project_completed,
        project_required=project_required,
        project_failed=project_failed,
    ) == {"status": "completed", "result": "resumed output"}
    assert required.project(
        context,
        project_completed=project_completed,
        project_required=project_required,
        project_failed=project_failed,
    ) == {
        "status": "approval_required",
        "pending": {"tool": "terminal.run", "approval_id": "next", "prepared": True},
    }
    assert failed.project(
        context,
        project_completed=project_completed,
        project_required=project_required,
        project_failed=project_failed,
    ) == {"status": "failed", "error": "safe RuntimeError"}
    assert calls == [
        ("project_completed", {"context": context, "result_text": "resumed output"}),
        (
            "project_required",
            {
                "context": context,
                "pending": {"tool": "terminal.run", "approval_id": "next", "prepared": True},
            },
        ),
        ("project_failed", {"context": context, "safe_error": "safe RuntimeError"}),
    ]

    with pytest.raises(AgentRuntimeError, match="Unknown approved-tool continuation outcome"):
        ToolApprovalContinuationOutcome(kind="unknown").project(
            context,
            project_completed=project_completed,
            project_required=project_required,
            project_failed=project_failed,
        )


def test_approval_resume_coordinator_projects_continuation_outcome_after_approved_tool():
    calls: list[tuple[str, dict[str, object]]] = []
    mode = {"value": "completed"}
    agent = {"agent_id": "agent_resume", "name": "Resume Agent"}

    def append_tool_result_message(run_messages, _request, tool_result):
        calls.append(("append_tool_result_message", {"tool_result": tool_result}))
        run_messages.append({"role": "tool", "content": json.dumps(tool_result)})

    def continue_custom_api_agent(
        _agent,
        _user_goal,
        _broker,
        _timeline,
        _artifacts,
        **kwargs,
    ):
        calls.append(
            (
                "continue_custom_api_agent",
                {
                    "messages": kwargs["messages"],
                    "run_id": kwargs["run_id"],
                    "start_iteration": kwargs["start_iteration"],
                },
            )
        )
        if mode["value"] == "required":
            raise AgentApprovalRequired({"tool": "terminal.run", "approval_id": "next"})
        if mode["value"] == "failed":
            raise RuntimeError("provider raw failure")
        return "resumed model output"

    coordinator = ApprovalResumeCoordinator(
        call_agent_tool=lambda *_args, **_kwargs: calls.append(("call_agent_tool", {})) or {"ok": True},
        fatal_tool_failure_detail=lambda *_args: "",
        append_tool_result_message=append_tool_result_message,
        run_tool_requests=lambda *_args, **_kwargs: calls.append(("run_tool_requests", {})),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        continue_custom_api_agent=continue_custom_api_agent,
    )

    def make_context(run_id: str) -> ToolApprovalResumeContext:
        return ToolApprovalResumeContext(
            run_id=run_id,
            timeline=[],
            artifacts=[],
            broker=SimpleNamespace(name="broker"),
            allowed_tools=["terminal.run"],
            budget=SimpleNamespace(name="budget"),
            messages=[{"role": "user", "content": "resume"}],
            tool_request={"name": "terminal.run", "input": {"command": "printf ok"}},
            tool_name="terminal.run",
            input_preview={"command": "printf ok"},
            remaining_requests=[],
            next_iteration=6,
        )

    def run_mode(value: str) -> dict[str, object]:
        mode["value"] = value
        context = make_context(f"run_{value}")
        return coordinator.continue_and_project_after_approved_tool(
            agent=agent,
            context=context,
            project_completed=lambda _context, result_text: calls.append(("project_completed", {})) or {
                "status": "completed",
                "result": result_text,
            },
            prepare_required=lambda pending: calls.append(("prepare_required", {"pending": pending})) or {
                **pending,
                "prepared": True,
            },
            project_required=lambda _context, pending: calls.append(("project_required", {"pending": pending})) or {
                "status": "approval_required",
                "pending": pending,
            },
            project_failed=lambda _context, safe_error: calls.append(("project_failed", {"safe_error": safe_error})) or {
                "status": "failed",
                "error": safe_error,
            },
            redact_error=lambda exc: calls.append(("redact_error", {"type": type(exc).__name__})) or "safe RuntimeError",
        )

    assert run_mode("completed") == {
        "status": "completed",
        "result": "resumed model output",
    }
    assert run_mode("required") == {
        "status": "approval_required",
        "pending": {"tool": "terminal.run", "approval_id": "next", "prepared": True},
    }
    assert run_mode("failed") == {
        "status": "failed",
        "error": "safe RuntimeError",
    }
    assert [name for name, _payload in calls] == [
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "project_completed",
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "prepare_required",
        "project_required",
        "call_agent_tool",
        "append_tool_result_message",
        "run_tool_requests",
        "continue_custom_api_agent",
        "redact_error",
        "project_failed",
    ]
    assert calls[3][1]["run_id"] == "run_completed"
    assert calls[3][1]["start_iteration"] == 6
    assert calls[10][1]["pending"] == {
        "tool": "terminal.run",
        "approval_id": "next",
        "prepared": True,
    }
    assert calls[15][1] == {"type": "RuntimeError"}


def test_workflow_child_run_projection_builds_replay_payloads():
    child_run = {
        "run_id": "child_run",
        "status": "completed",
        "result": "Child result",
    }
    artifacts = [
        {"kind": "workflow_child_artifact", "source_run_id": "child_run", "path": "a.md"},
        {"kind": "workflow_child_artifact", "source_run_id": "child_run", "path": "b.md"},
        {"kind": "workflow_child_artifact", "source_run_id": "other_child", "path": "c.md"},
    ]
    node_info = {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
    }

    projection = WorkflowChildRunProjection.from_child_run(child_run, node_info, artifacts)

    assert projection is not None
    assert projection.agent_event_payload() == {
        "child_run_id": "child_run",
        "status": "completed",
        "result": "Child result",
        "artifact_count": 2,
        **node_info,
    }
    assert projection.status_event_payload("running") == {
        "child_run_id": "child_run",
        "status": "running",
        **node_info,
    }
    assert WorkflowChildRunProjection.from_child_run({}, node_info, artifacts) is None


def test_workflow_child_status_projection_builds_projected_and_fallback_payloads():
    node_info = {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
    }
    projected = WorkflowChildStatusProjection.from_child_run(
        {
            "run_id": "child_run",
            "status": "failed",
            "result": "Child failed",
        },
        node_info,
        [
            {"kind": "workflow_child_artifact", "source_run_id": "child_run", "path": "a.md"},
            {"kind": "other", "source_run_id": "child_run", "path": "ignored.md"},
        ],
    )
    fallback = WorkflowChildStatusProjection.from_child_run(
        {
            "status": "cancelled",
            "result": "Child cancelled",
        },
        node_info,
        [],
    )

    assert projected.projection is not None
    assert projected.status_event_payload("running") == {
        "child_run_id": "child_run",
        "status": "running",
        **node_info,
    }
    assert projected.result_event_payload("failed") == {
        "child_run_id": "child_run",
        "status": "failed",
        "result": "Child failed",
        **node_info,
    }
    assert fallback.projection is None
    assert fallback.status_event_payload() == {
        "child_run_id": "",
        "status": "cancelled",
        **node_info,
    }
    assert fallback.result_event_payload("failed") == {
        "child_run_id": "",
        "status": "failed",
        "result": "Child cancelled",
        **node_info,
    }


def test_workflow_parent_resume_failure_projection_redacts_and_builds_update_fields():
    raw_secret = "sk-workflow-parent-resume-secret123456"
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = [{"kind": "workflow_child_artifact", "path": "child.md"}]

    projection = WorkflowParentResumeFailureProjection.from_error(
        RuntimeError(f"snapshot failed with {raw_secret}"),
        child_run_id="child_run",
        child_status="completed",
        child_node_info={
            "workflow_node_id": "agent",
            "workflow_node_kind": "agent",
            "workflow_node_label": f"Research {raw_secret}",
        },
    )
    event = projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    )
    timeline.append(event)

    assert raw_secret not in json.dumps({"event": event, "projection": projection.event_payload}, ensure_ascii=False)
    assert event["event"] == "workflow.run.failed"
    assert event["detail"] == projection.safe_error
    assert event["status"] == "failed"
    assert event["workflow_node_id"] == "agent"
    assert event["workflow_node_kind"] == "agent"
    assert event["workflow_node_label"] == "Research [redacted]"
    assert event["child_run_id"] == "child_run"
    assert event["child_run_status"] == "completed"
    assert projection.update_fields(timeline=timeline, artifacts=artifacts) == {
        "status": "failed",
        "result": projection.safe_error,
        "timeline": timeline,
        "artifacts": artifacts,
    }


def test_workflow_parent_resume_coordinator_continues_completed_child():
    appended_events: list[tuple[str, str, dict[str, object]]] = []
    run_updates: list[dict[str, object]] = []
    group_updates: list[dict[str, object]] = []
    continued: dict[str, object] = {}
    child_node_info = {
        "workflow_node_id": "agent",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research Agent",
    }
    child_artifact = {
        "kind": "workflow_child_artifact",
        "source_run_id": "child_run",
        "path": "reports/result.md",
    }

    workflow_run = {
        "run_id": "workflow_parent",
        "run_group_id": "run_group_parent",
        "status": "running",
        "result": "waiting for child",
        "pending_approval": {},
        "updated_at": "run-version-1",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "child_run",
                **child_node_info,
            }
        ],
        "artifacts": [],
    }
    child_run = {
        "run_id": "child_run",
        "status": "completed",
        "result": "Child Agent completed after approval.",
        "runnable_name": "Research Agent",
    }

    def merge_child_outcome(timeline, artifacts, run, label):
        assert run is child_run
        assert label == "Research Agent"
        artifacts.append(dict(child_artifact))

    def continue_workflow_run(
        run,
        workflow,
        *,
        context,
        timeline,
        artifacts,
        start_index,
        root_group,
    ):
        continued.update(
            {
                "run": run,
                "workflow": workflow,
                "context": context,
                "timeline": timeline,
                "artifacts": artifacts,
                "start_index": start_index,
                "root_group": root_group,
            }
        )
        return {"run_id": run["run_id"], "status": "completed", "result": context}

    def update_run(run_id, **fields):
        run_updates.append({"run_id": run_id, **fields})
        return {
            **workflow_run,
            **fields,
            "run_id": run_id,
            "updated_at": "run-version-2",
        }

    def update_run_group(run_group_id, **fields):
        group_updates.append({"run_group_id": run_group_id, **fields})
        return {"run_group_id": run_group_id, **fields}

    coordinator = WorkflowParentResumeCoordinator(
        parent_runs_waiting_for_child=lambda _child: [workflow_run],
        workflow_run_is_group_root=lambda _run: True,
        workflow_child_node_context=lambda _timeline, _child: (
            "Research Agent",
            dict(child_node_info),
        ),
        merge_workflow_child_run_outcome=merge_child_outcome,
        workflow_for_run_resume=lambda _run: {"workflow_id": "workflow_demo"},
        workflow_resume_start_index=lambda _workflow, _run, child_run_id: (
            3 if child_run_id == "child_run" else None
        ),
        continue_workflow_run=continue_workflow_run,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda run_id, event_type, payload: appended_events.append((run_id, event_type, payload)),
        update_run=update_run,
        update_run_group=update_run_group,
    )

    result = coordinator.resume_parent_after_child_update(workflow_run, child_run)

    assert result == {
        "run_id": "workflow_parent",
        "status": "completed",
        "result": "Child Agent completed after approval.",
    }
    assert continued["run"]["status"] == "running"
    assert continued["run"]["result"] == "Child Agent completed after approval."
    assert continued["workflow"] == {"workflow_id": "workflow_demo"}
    assert continued["context"] == "Child Agent completed after approval."
    assert continued["start_index"] == 3
    assert continued["root_group"] is True
    assert continued["artifacts"] == [child_artifact]
    assert run_updates[-1]["expected_status"] == "running"
    assert run_updates[-1]["expected_updated_at"] == "run-version-1"
    assert group_updates == [
        {
            "run_group_id": "run_group_parent",
            "status": "running",
            "summary": "Child Agent completed after approval.",
        }
    ]
    continued_timeline = continued["timeline"]
    assert isinstance(continued_timeline, list)
    assert continued_timeline[-1] == {
        "event": "workflow.run.resumed",
        "detail": "Workflow resumed after child Agent approval",
        "child_run_id": "child_run",
        "status": "running",
        **child_node_info,
    }
    assert appended_events == [
        (
            "workflow_parent",
            "workflow.node.agent",
            {
                "child_run_id": "child_run",
                "status": "completed",
                "result": "Child Agent completed after approval.",
                "artifact_count": 1,
                **child_node_info,
            },
        ),
        (
            "workflow_parent",
            "workflow.run.resumed",
            {
                "child_run_id": "child_run",
                "status": "running",
                **child_node_info,
            },
        ),
    ]


def test_workflow_parent_resume_coordinator_does_not_resume_completed_child_twice():
    workflow_run = {
        "run_id": "workflow_parent",
        "run_group_id": "run_group_parent",
        "status": "completed",
        "timeline": [
            {
                "event": "workflow.run.resumed",
                "child_run_id": "child_run",
                "status": "running",
                "workflow_node_id": "agent",
            },
            {
                "event": "workflow.run.completed",
                "detail": "Workflow run completed",
            },
        ],
        "artifacts": [{"kind": "workflow_artifact", "path": "summary.md"}],
        "result": "Child Agent completed after approval.",
    }
    child_run = {
        "run_id": "child_run",
        "status": "completed",
        "result": "Child Agent completed after approval.",
        "runnable_name": "Research Agent",
    }

    coordinator = WorkflowParentResumeCoordinator(
        parent_runs_waiting_for_child=lambda _child: [workflow_run],
        workflow_run_is_group_root=lambda _run: True,
        workflow_child_node_context=lambda *_args: pytest.fail("already resumed child should not be re-projected"),
        merge_workflow_child_run_outcome=lambda *_args: pytest.fail("already resumed child should not merge again"),
        workflow_for_run_resume=lambda *_args: pytest.fail("already resumed child should not load workflow"),
        workflow_resume_start_index=lambda *_args: pytest.fail("already resumed child should not compute start index"),
        continue_workflow_run=lambda *_args, **_kwargs: pytest.fail("already resumed child should not continue twice"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda *_args: pytest.fail("already resumed child should not append replay facts"),
        update_run=lambda *_args, **_kwargs: pytest.fail("already resumed child should not update parent"),
        update_run_group=lambda *_args, **_kwargs: pytest.fail("already resumed child should not update group"),
    )

    result = coordinator.resume_parent_after_child_update(workflow_run, child_run)

    assert result is workflow_run
    assert workflow_run["timeline"] == [
        {
            "event": "workflow.run.resumed",
            "child_run_id": "child_run",
            "status": "running",
            "workflow_node_id": "agent",
        },
        {
            "event": "workflow.run.completed",
            "detail": "Workflow run completed",
        },
    ]
    assert workflow_run["artifacts"] == [{"kind": "workflow_artifact", "path": "summary.md"}]


def test_workflow_parent_resume_coordinator_does_not_project_child_approval_twice():
    workflow_run = {
        "run_id": "workflow_parent",
        "run_group_id": "run_group_parent",
        "status": "approval_required",
        "timeline": [
            {
                "event": "workflow.run.approval_required",
                "child_run_id": "child_run",
                "status": "approval_required",
                "workflow_node_id": "agent",
            },
        ],
        "artifacts": [],
        "result": "Child needs approval",
    }
    child_run = {
        "run_id": "child_run",
        "status": "approval_required",
        "result": "Child needs approval",
        "runnable_name": "Research Agent",
    }

    coordinator = WorkflowParentResumeCoordinator(
        parent_runs_waiting_for_child=lambda _child: [workflow_run],
        workflow_run_is_group_root=lambda _run: True,
        workflow_child_node_context=lambda *_args: pytest.fail("already projected child approval should not re-project"),
        merge_workflow_child_run_outcome=lambda *_args: pytest.fail("already projected child approval should not merge again"),
        workflow_for_run_resume=lambda *_args: pytest.fail("approval child should not load workflow"),
        workflow_resume_start_index=lambda *_args: pytest.fail("approval child should not compute start index"),
        continue_workflow_run=lambda *_args, **_kwargs: pytest.fail("approval child should not continue workflow"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda *_args: pytest.fail("already projected child approval should not append replay facts"),
        update_run=lambda *_args, **_kwargs: pytest.fail("already projected child approval should not update parent"),
        update_run_group=lambda *_args, **_kwargs: pytest.fail("already projected child approval should not update group"),
    )

    result = coordinator.resume_parent_after_child_update(workflow_run, child_run)

    assert result is workflow_run
    assert workflow_run["timeline"] == [
        {
            "event": "workflow.run.approval_required",
            "child_run_id": "child_run",
            "status": "approval_required",
            "workflow_node_id": "agent",
        },
    ]


def test_workflow_parent_resume_coordinator_does_not_project_child_cancel_twice():
    workflow_run = {
        "run_id": "workflow_parent",
        "run_group_id": "run_group_parent",
        "status": "cancelled",
        "timeline": [
            {
                "event": "workflow.run.cancelled",
                "child_run_id": "child_run",
                "status": "cancelled",
                "workflow_node_id": "agent",
            },
        ],
        "artifacts": [],
        "result": "Child cancelled",
    }
    child_run = {
        "run_id": "child_run",
        "status": "cancelled",
        "result": "Child cancelled",
        "runnable_name": "Research Agent",
    }

    coordinator = WorkflowParentResumeCoordinator(
        parent_runs_waiting_for_child=lambda _child: [workflow_run],
        workflow_run_is_group_root=lambda _run: True,
        workflow_child_node_context=lambda *_args: pytest.fail("already projected child cancellation should not re-project"),
        merge_workflow_child_run_outcome=lambda *_args: pytest.fail("already projected child cancellation should not merge again"),
        workflow_for_run_resume=lambda *_args: pytest.fail("cancelled child should not load workflow"),
        workflow_resume_start_index=lambda *_args: pytest.fail("cancelled child should not compute start index"),
        continue_workflow_run=lambda *_args, **_kwargs: pytest.fail("cancelled child should not continue workflow"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda *_args: pytest.fail("already projected child cancellation should not append replay facts"),
        update_run=lambda *_args, **_kwargs: pytest.fail("already projected child cancellation should not update parent"),
        update_run_group=lambda *_args, **_kwargs: pytest.fail("already projected child cancellation should not update group"),
    )

    result = coordinator.resume_parent_after_child_update(workflow_run, child_run)

    assert result is workflow_run
    assert workflow_run["timeline"] == [
        {
            "event": "workflow.run.cancelled",
            "child_run_id": "child_run",
            "status": "cancelled",
            "workflow_node_id": "agent",
        },
    ]


def test_workflow_cancellation_target_builds_event_payloads():
    pending_target = WorkflowCancellationTarget.from_pending_approval(
        {
            "workflow_node_id": "gate",
            "workflow_node_label": "Human Gate",
            "workflow_node_approval_criteria": "Review output",
        }
    )

    assert pending_target.event_detail() == "Human Gate cancelled"
    assert pending_target.result_text() == "Workflow 已取消：Human Gate"
    assert pending_target.event_payload() == {
        "status": "cancelled",
        "workflow_node_id": "gate",
        "workflow_node_kind": "approval",
        "workflow_node_label": "Human Gate",
        "workflow_node_approval_criteria": "Review output",
    }

    child_target = WorkflowCancellationTarget.from_child(
        child_run_id="child_run",
        label="Research Agent",
        node_info={
            "workflow_node_id": "agent",
            "workflow_node_kind": "agent",
            "workflow_node_label": "Research Agent",
        },
    )

    assert child_target.event_detail() == "Research Agent cancelled"
    assert child_target.result_text() == "Workflow 已取消：Research Agent"
    assert child_target.event_payload() == {
        "status": "cancelled",
        "workflow_node_id": "agent",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research Agent",
        "child_run_id": "child_run",
    }


def test_run_cancellation_projection_builds_update_fields():
    timeline = [{"event": "run.started"}]

    plain = RunCancellationProjection.plain(
        timeline,
        lambda event, detail: {"event": event, "detail": detail},
    )

    assert plain.update_fields() == {
        "status": "cancelled",
        "result": "Run cancelled",
        "timeline": [
            {"event": "run.started"},
            {"event": "run.cancelled", "detail": "Run cancelled"},
        ],
        "artifacts": None,
        "pending_approval": None,
    }
    assert timeline == [{"event": "run.started"}]

    workflow_timeline = [{"event": "workflow.run.cancelled"}]
    workflow_artifacts = [{"kind": "workflow_child_artifact", "path": "child.md"}]
    workflow = RunCancellationProjection.workflow(
        workflow_timeline,
        workflow_artifacts,
        "Workflow 已取消：Research Agent",
    )

    assert workflow.update_fields() == {
        "status": "cancelled",
        "result": "Workflow 已取消：Research Agent",
        "timeline": workflow_timeline,
        "artifacts": workflow_artifacts,
        "pending_approval": None,
    }


def test_workflow_cancellation_projection_coordinator_cancels_waiting_child_run():
    child_runs = {
        "child_run": {
            "run_id": "child_run",
            "status": "approval_required",
            "result": "Waiting for approval",
            "timeline": [{"event": "agent.tool.approval_required", "detail": "terminal.run"}],
            "artifacts": [],
        }
    }
    appended_events: list[tuple[str, str, dict[str, object]]] = []
    updated_runs: list[dict[str, object]] = []
    merged_children: list[dict[str, object]] = []

    def update_run(run_id, **kwargs):
        updated_runs.append({"run_id": run_id, **kwargs})
        child_runs[run_id] = {**child_runs[run_id], **kwargs}
        return child_runs[run_id]

    def merge_child_outcome(timeline, artifacts, child_run, label):
        merged_children.append(
            {
                "child_run_id": child_run["run_id"],
                "child_status": child_run["status"],
                "label": label,
            }
        )
        timeline.append(
            {
                "event": "workflow.node.agent",
                "detail": label,
                "child_run_id": child_run["run_id"],
                "status": child_run["status"],
                "workflow_node_id": "agent",
                "workflow_node_kind": "agent",
                "workflow_node_label": label,
            }
        )
        artifacts.append({"kind": "workflow_child_artifact", "source_run_id": child_run["run_id"]})

    coordinator = WorkflowCancellationProjectionCoordinator(
        pending_approval_private=lambda _run_id: None,
        get_run=lambda run_id: child_runs[run_id],
        merge_workflow_child_run_outcome=merge_child_outcome,
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda run_id, event_type, payload: appended_events.append((run_id, event_type, payload)),
        update_run=update_run,
    )

    timeline = [
        {
            "event": "workflow.node.agent",
            "detail": "Research Agent",
            "child_run_id": "child_run",
            "status": "approval_required",
            "workflow_node_id": "agent",
            "workflow_node_kind": "agent",
            "workflow_node_label": "Research Agent",
        },
        {
            "event": "workflow.run.approval_required",
            "detail": "Research Agent",
            "child_run_id": "child_run",
            "status": "approval_required",
        },
    ]
    run = {"run_id": "workflow_parent", "artifacts": [{"kind": "existing", "path": "summary.md"}]}

    cancelled_timeline, artifacts, result_text = coordinator.project_cancelled_workflow_run(
        "workflow_parent",
        run,
        timeline,
    )

    assert result_text == "Workflow 已取消：Research Agent"
    assert updated_runs == [
        {
            "run_id": "child_run",
            "status": "cancelled",
            "result": "父 Workflow 已取消",
            "timeline": [
                {"event": "agent.tool.approval_required", "detail": "terminal.run"},
                {"event": "run.cancelled", "detail": "Parent Workflow cancelled"},
            ],
            "pending_approval": None,
        }
    ]
    assert appended_events == [
        (
            "child_run",
            "run.cancelled",
            {"reason": "Parent Workflow cancelled", "parent_run_id": "workflow_parent"},
        )
    ]
    assert merged_children == [
        {
            "child_run_id": "child_run",
            "child_status": "cancelled",
            "label": "Research Agent",
        }
    ]
    assert artifacts == [
        {"kind": "existing", "path": "summary.md"},
        {"kind": "workflow_child_artifact", "source_run_id": "child_run"},
    ]
    assert cancelled_timeline[-1] == {
        "event": "workflow.run.cancelled",
        "detail": "Research Agent cancelled",
        "status": "cancelled",
        "workflow_node_id": "agent",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research Agent",
        "child_run_id": "child_run",
    }


def test_workflow_parent_resume_coordinator_does_not_project_child_failure_twice():
    workflow_run = {
        "run_id": "workflow_parent",
        "run_group_id": "run_group_parent",
        "status": "failed",
        "timeline": [
            {
                "event": "workflow.run.failed",
                "child_run_id": "child_run",
                "status": "failed",
                "workflow_node_id": "agent",
            },
        ],
        "artifacts": [],
        "result": "Child failed",
    }
    child_run = {
        "run_id": "child_run",
        "status": "failed",
        "result": "Child failed",
        "runnable_name": "Research Agent",
    }

    coordinator = WorkflowParentResumeCoordinator(
        parent_runs_waiting_for_child=lambda _child: [workflow_run],
        workflow_run_is_group_root=lambda _run: True,
        workflow_child_node_context=lambda *_args: pytest.fail("already projected child failure should not re-project"),
        merge_workflow_child_run_outcome=lambda *_args: pytest.fail("already projected child failure should not merge again"),
        workflow_for_run_resume=lambda *_args: pytest.fail("failed child should not load workflow"),
        workflow_resume_start_index=lambda *_args: pytest.fail("failed child should not compute start index"),
        continue_workflow_run=lambda *_args, **_kwargs: pytest.fail("failed child should not continue workflow"),
        timeline_factory=lambda event, detail, **payload: {"event": event, "detail": detail, **payload},
        append_run_event=lambda *_args: pytest.fail("already projected child failure should not append replay facts"),
        update_run=lambda *_args, **_kwargs: pytest.fail("already projected child failure should not update parent"),
        update_run_group=lambda *_args, **_kwargs: pytest.fail("already projected child failure should not update group"),
    )

    result = coordinator.resume_parent_after_child_update(workflow_run, child_run)

    assert result is workflow_run
    assert workflow_run["timeline"] == [
        {
            "event": "workflow.run.failed",
            "child_run_id": "child_run",
            "status": "failed",
            "workflow_node_id": "agent",
        },
    ]


def test_workflow_agent_node_handoff_builds_child_run_payload():
    agent = {"agent_id": "agent_research", "name": "Research Agent"}

    class FakeEngine:
        def _workflow_agent_for_node(self, node):
            assert node["id"] == "research"
            return agent

        def _workflow_node_task(self, node):
            return str((node.get("data") or {}).get("task") or "")

        def _workflow_child_goal(self, workflow_goal, step_task):
            return f"{workflow_goal}\n\nStep: {step_task}"

    node = {
        "id": "research",
        "type": "agent",
        "data": {"agentId": "fallback_agent", "task": "Summarize launch risk."},
    }
    handoff = WorkflowAgentNodeHandoff.from_node(
        FakeEngine(),
        node,
        label="Research",
        kind="agent",
        workflow_goal="Ship release candidate",
        context="Previous result",
        has_agent_upstream=True,
    )
    child_run = {
        "run_id": "child_run",
        "status": "completed",
        "result": "Launch risk summary",
    }

    assert handoff.agent is agent
    assert handoff.agent_id == "agent_research"
    assert handoff.node_info() == {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
    }
    assert handoff.step_task == "Summarize launch risk."
    assert handoff.child_goal == "Ship release candidate\n\nStep: Summarize launch risk."
    assert handoff.upstream == "Previous result"
    assert handoff.agent_event_payload(child_run, artifact_count=2) == {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
        "workflow_node_task": "Summarize launch risk.",
        "child_run_id": "child_run",
        "status": "completed",
        "result": "Launch risk summary",
        "artifact_count": 2,
    }
    assert handoff.status_event_payload(child_run) == {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
        "child_run_id": "child_run",
        "status": "completed",
    }
    assert WorkflowAgentNodeHandoff.from_node(
        FakeEngine(),
        node,
        label="Research",
        kind="agent",
        workflow_goal="Ship release candidate",
        context="Previous result",
        has_agent_upstream=False,
    ).upstream == ""


def test_workflow_agent_node_execution_runs_child_and_builds_replay_payloads():
    calls: list[tuple[str, dict[str, object]]] = []
    agent = {"agent_id": "agent_research", "name": "Research Agent"}
    handoff = WorkflowAgentNodeHandoff(
        agent=agent,
        agent_id="agent_research",
        node_id="research",
        node_kind="agent",
        node_label="Research",
        step_task="Summarize launch risk.",
        child_goal="Ship release candidate\n\nStep: Summarize launch risk.",
        upstream="Previous result",
    )
    child_result = {
        "run_id": "child_run",
        "kind": "agent_run",
        "status": "completed",
        "result": "Launch risk summary",
        "artifacts": [
            {"kind": "context", "path": "context.md"},
            {"kind": "artifact", "path": "risk.md"},
            {"kind": "artifact", "path": "plan.md"},
        ],
    }

    class FakeEngine:
        def _insert_run(self, **kwargs):
            calls.append(("insert_run", kwargs))
            return {"run_id": "child_run"}

        def _execute_agent_run(self, run_id, received_agent, user_goal, *, upstream):
            calls.append(
                (
                    "execute_agent_run",
                    {
                        "run_id": run_id,
                        "agent": received_agent,
                        "user_goal": user_goal,
                        "upstream": upstream,
                    },
                )
            )
            return child_result

        def _workflow_child_artifact_refs(self, child_run, label):
            calls.append(("workflow_child_artifact_refs", {"child_run": child_run, "label": label}))
            return [
                artifact
                for artifact in child_run.get("artifacts") or []
                if artifact.get("kind") != "context"
            ]

    execution = WorkflowAgentNodeExecution.from_handoff(
        FakeEngine(),
        handoff,
        run_group_id="workflow_group",
    )

    assert execution.handoff is handoff
    assert execution.child_run is child_result
    assert execution.next_context == "Launch risk summary"
    assert execution.status == "completed"
    assert execution.artifact_count == 2
    assert execution.agent_event_payload() == {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
        "workflow_node_task": "Summarize launch risk.",
        "child_run_id": "child_run",
        "status": "completed",
        "result": "Launch risk summary",
        "artifact_count": 2,
    }
    assert execution.status_event_payload() == {
        "workflow_node_id": "research",
        "workflow_node_kind": "agent",
        "workflow_node_label": "Research",
        "child_run_id": "child_run",
        "status": "completed",
    }
    assert calls == [
        (
            "insert_run",
            {
                "kind": "agent_run",
                "runnable_id": "agent_research",
                "user_goal": "Summarize launch risk.",
                "run_group_id": "workflow_group",
            },
        ),
        (
            "execute_agent_run",
            {
                "run_id": "child_run",
                "agent": {
                    **agent,
                    "_runtime_planner_entrypoint": True,
                    "_runtime_planner_entrypoint_context": "Summarize launch risk.",
                    "_runtime_agent_goal_context": (
                        "Ship release candidate\n\nStep: Summarize launch risk."
                    ),
                },
                "user_goal": "Summarize launch risk.",
                "upstream": "Previous result",
            },
        ),
        (
            "workflow_child_artifact_refs",
            {
                "child_run": child_result,
                "label": "Research",
            },
        ),
    ]


def test_workflow_agent_node_execution_forwards_scoped_direct_requests():
    calls: list[tuple[str, dict[str, object]]] = []
    agent = {
        "agent_id": "agent_research",
        "name": "Research Agent",
        "tool_policy": {
            "allowed_tools": ["app.open"],
            "approval_required": {},
        },
    }
    handoff = WorkflowAgentNodeHandoff(
        agent=agent,
        agent_id="agent_research",
        node_id="research",
        node_kind="agent",
        node_label="Research",
        step_task="Open Music",
        child_goal="Open Music from workflow",
        upstream="",
    )

    class FakeEngine:
        def _insert_run(self, **kwargs):
            return {"run_id": "child_run"}

        def _execute_agent_run(
            self,
            run_id,
            received_agent,
            user_goal,
            *,
            upstream,
            run_group_id="",
            workflow_run_id="",
            direct_tool_requests=None,
            daily_desktop_planning_context=None,
        ):
            calls.append(
                (
                    "execute_agent_run",
                    {
                        "run_id": run_id,
                        "agent": received_agent,
                        "user_goal": user_goal,
                        "upstream": upstream,
                        "run_group_id": run_group_id,
                        "workflow_run_id": workflow_run_id,
                        "direct_tool_requests": direct_tool_requests,
                        "daily_desktop_planning_context": daily_desktop_planning_context,
                    },
                )
            )
            return {
                "run_id": run_id,
                "kind": "agent_run",
                "status": "completed",
                "result": "opened",
                "artifacts": [],
            }

        def _workflow_child_artifact_refs(self, _child_run, _label):
            return []

    WorkflowAgentNodeExecution.from_handoff(
        FakeEngine(),
        handoff,
        run_group_id="workflow_group",
        workflow_run_id="workflow_run",
        runtime_execution_envelope={
            "decision_id": "decision-workflow",
            "plan_id": "plan-workflow",
            "requests": [
                {
                    "request_id": "request-open",
                    "tool": "app.open",
                    "input": {"app_name": "Music"},
                    "workflow_node_id": "research",
                },
                {
                    "request_id": "request-other",
                    "tool": "app.open",
                    "input": {"app_name": "Notes"},
                    "workflow_node_id": "other",
                },
                {
                    "request_id": "request-terminal",
                    "tool": "terminal.run",
                    "input": {"command": "echo no"},
                    "workflow_node_id": "research",
                },
            ],
        },
        direct_request_fallback_node_id="research",
    )

    execute_payload = calls[0][1]
    direct_requests = execute_payload["direct_tool_requests"]
    assert [request["request_id"] for request in direct_requests] == ["request-open"]
    direct_request = direct_requests[0]
    assert direct_request["tool"] == "app.open"
    assert direct_request["input"] == {"app_name": "Music"}
    assert direct_request["workflow_run_id"] == "workflow_run"
    assert direct_request["workflow_node_id"] == "research"
    assert direct_request["workflow_node_label"] == "Research"
    assert direct_request["agent_id"] == "agent_research"
    assert execute_payload["daily_desktop_planning_context"] == "Open Music"


def test_workflow_approval_pause_projection_builds_private_and_public_payloads():
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = [{"kind": "workflow_artifact", "path": "notes.md"}]
    projection = WorkflowApprovalPauseProjection(
        approval_id="approval_manual",
        node_id="gate",
        node_kind="approval",
        label="Human Gate",
        criteria="Review output",
        context="Child result ready",
        next_index=4,
        next_node_id="after-gate",
        requested_at="2026-06-12T00:00:00+00:00",
    )
    pending = projection.pending_approval()
    event = projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    )
    timeline.append(event)

    assert pending == {
        "approval_id": "approval_manual",
        "tool": "workflow.approval",
        "input_preview": {
            "checkpoint": "Human Gate",
            "context": "Child result ready",
            "criteria": "Review output",
        },
        "requested_at": "2026-06-12T00:00:00+00:00",
        "workflow_context": "Child result ready",
        "workflow_next_index": 4,
        "workflow_next_node_id": "after-gate",
        "workflow_node_id": "gate",
        "workflow_node_label": "Human Gate",
        "workflow_node_approval_criteria": "Review output",
    }
    assert projection.public_pending_approval() == {
        "approval_id": "approval_manual",
        "tool": "workflow.approval",
        "input_preview": {
            "checkpoint": "Human Gate",
            "context": "Child result ready",
            "criteria": "Review output",
        },
        "requested_at": "2026-06-12T00:00:00+00:00",
        "workflow_node_id": "gate",
        "workflow_node_label": "Human Gate",
        "policy_reason": "Workflow 审批节点要求人工确认：Review output",
    }
    assert "workflow_context" not in projection.public_pending_approval()
    assert event == {
        "event": "workflow.node.approval_required",
        "detail": "Human Gate",
        "workflow_node_id": "gate",
        "workflow_node_kind": "approval",
        "workflow_node_label": "Human Gate",
        "workflow_node_approval_criteria": "Review output",
        "status": "approval_required",
        "pending_approval": projection.public_pending_approval(),
    }
    assert projection.update_fields(timeline=timeline, artifacts=artifacts) == {
        "status": "approval_required",
        "result": "等待审批：Human Gate",
        "timeline": timeline,
        "artifacts": artifacts,
        "pending_approval": pending,
    }


def test_workflow_artifact_node_write_builds_record_and_replay_payload(tmp_path):
    class FakeEngine:
        workflow_artifacts_dir = tmp_path / "workflow-artifacts"

        def _default_workspace_policy(self):
            return {"default_workdir": "", "readable_scopes": ["."], "writable_scopes": []}

        def _workflow_artifact_path(self, label, artifacts, requested):
            assert label == "Final Report"
            assert artifacts == [{"kind": "existing", "path": "prior.md"}]
            return requested or "final.md"

    artifacts = [{"kind": "existing", "path": "prior.md"}]
    content = "Final workflow summary"
    write = WorkflowArtifactNodeWrite.from_node(
        FakeEngine(),
        {"run_id": "workflow_run"},
        {
            "id": "report",
            "type": "artifact",
            "data": {
                "label": "Final Report",
                "artifact_path": "reports/final.md",
            },
        },
        label="Final Report",
        kind="artifact",
        context=content,
        artifacts=artifacts,
    )

    assert (tmp_path / "workflow-artifacts" / "workflow_run" / "reports" / "final.md").read_text(
        encoding="utf-8"
    ) == content
    assert write.artifact_record() == {
        "kind": "workflow_artifact",
        "workflow_node_id": "report",
        "workflow_node_label": "Final Report",
        "ok": True,
        "path": "reports/final.md",
        "bytes": len(content.encode("utf-8")),
        "postcondition_verified": True,
    }
    assert write.event_payload() == {
        "workflow_node_id": "report",
        "workflow_node_kind": "artifact",
        "workflow_node_label": "Final Report",
        "status": "completed",
        "artifact": {
            "ok": True,
            "path": "reports/final.md",
            "bytes": len(content.encode("utf-8")),
        "postcondition_verified": True,
        },
    }
    assert write.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    ) == {
        "event": "workflow.node.artifact",
        "detail": "Final Report",
        **write.event_payload(),
    }


def test_workflow_start_node_projection_builds_timeline_and_replay_payloads():
    projection = WorkflowStartNodeProjection.from_node(
        {"id": "start", "type": "start"},
        label="Start",
        kind="start",
    )

    assert projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    ) == {
        "event": "workflow.node.start",
        "detail": "Start",
        "workflow_node_id": "start",
        "status": "completed",
    }
    assert projection.event_payload() == {
        "workflow_node_id": "start",
        "workflow_node_kind": "start",
        "workflow_node_label": "Start",
        "status": "completed",
    }


def test_workflow_run_completion_projection_builds_update_and_replay_payloads():
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = [{"kind": "workflow_artifact", "path": "report.md"}]
    projection = WorkflowRunCompletionProjection("Workflow result")
    event = projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    )
    timeline.append(event)

    assert event == {
        "event": "workflow.run.completed",
        "detail": "Workflow run completed",
    }
    assert projection.event_payload() == {"result": "Workflow result"}
    assert projection.update_fields(timeline=timeline, artifacts=artifacts) == {
        "status": "completed",
        "result": "Workflow result",
        "timeline": timeline,
        "artifacts": artifacts,
    }


def test_workflow_continuation_failure_projection_redacts_and_builds_update_fields():
    leaked_secret = "sk-workflow-continuation-secret123456"
    timeline: list[dict[str, object]] = [{"event": "workflow.node.start", "detail": "Start"}]
    artifacts: list[dict[str, object]] = [{"kind": "workflow_artifact", "path": "report.md"}]
    projection = WorkflowContinuationFailureProjection.from_error(
        RuntimeError(f"provider failed token={leaked_secret}"),
        {
            "workflow_node_id": "bad",
            "workflow_node_kind": f"custom_api_key={leaked_secret}",
            "workflow_node_label": f"Bad {leaked_secret}",
        },
    )
    event = projection.timeline_event(
        lambda event, detail, **payload: {"event": event, "detail": detail, **payload}
    )
    timeline.append(event)

    assert event == {
        "event": "workflow.run.failed",
        "detail": projection.safe_error,
        "status": "failed",
        "workflow_node_id": "bad",
        "workflow_node_kind": "custom_api_key=[redacted]",
        "workflow_node_label": "Bad [redacted]",
    }
    assert projection.event_payload() == {
        "error": projection.safe_error,
        "workflow_node_id": "bad",
        "workflow_node_kind": "custom_api_key=[redacted]",
        "workflow_node_label": "Bad [redacted]",
    }
    assert projection.update_fields(timeline=timeline, artifacts=artifacts) == {
        "status": "failed",
        "result": projection.safe_error,
        "timeline": timeline,
        "artifacts": artifacts,
    }
    serialized = json.dumps(
        {
            "safe_error": projection.safe_error,
            "node_info": projection.node_info,
            "event": event,
            "payload": projection.event_payload(),
            "update": projection.update_fields(timeline=timeline, artifacts=artifacts),
        },
        ensure_ascii=False,
    )
    assert leaked_secret not in serialized
    assert "[redacted]" in serialized


def test_workflow_continuation_coordinator_pauses_for_approval_node():
    class FakeEngine:
        def __init__(self):
            self.events: list[tuple[str, str, dict[str, object]]] = []
            self.run_updates: list[tuple[str, dict[str, object]]] = []
            self.group_updates: list[tuple[str, dict[str, object]]] = []

        def _workflow_path(self, workflow):
            return workflow["nodes"]

        def _node_kind(self, node):
            return node["type"]

        def _workflow_approval_criteria(self, node):
            return str((node.get("data") or {}).get("criteria") or "")

        def _timeline(self, event, detail, **payload):
            return {"event": event, "detail": detail, **payload}

        def append_run_event(self, run_id, event_type, payload):
            self.events.append((run_id, event_type, payload))

        def _update_run(self, run_id, **fields):
            self.run_updates.append((run_id, fields))
            return self.get_run(run_id)

        def _update_run_group(self, run_group_id, **fields):
            self.group_updates.append((run_group_id, fields))
            return {
                "run_group_id": run_group_id,
                **fields,
                "updated_at": "2026-07-11T10:00:01+00:00",
            }

        def get_run_group(self, run_group_id):
            assert run_group_id == "run_group"
            return {
                "run_group_id": run_group_id,
                "status": "running",
                "updated_at": "2026-07-11T10:00:00+00:00",
            }

        def get_run(self, run_id):
            assert run_id == "workflow_run"
            if not self.run_updates:
                return {
                    "run_id": run_id,
                    "run_group_id": "run_group",
                    "status": "running",
                    "pending_approval": {},
                    "timeline": [],
                    "artifacts": [],
                    "updated_at": "2026-07-11T10:00:00+00:00",
                    "project_root_group": True,
                }
            fields = dict(self.run_updates[-1][1])
            private_pending = fields.get("pending_approval")
            if isinstance(private_pending, dict):
                fields["pending_approval"] = {
                    "approval_id": str(private_pending.get("approval_id") or ""),
                    "tool": str(private_pending.get("tool") or ""),
                    "input_preview": private_pending.get("input_preview") or {},
                    "requested_at": str(private_pending.get("requested_at") or ""),
                    "workflow_node_id": str(private_pending.get("workflow_node_id") or ""),
                    "workflow_node_label": str(private_pending.get("workflow_node_label") or ""),
                }
            return {
                "run_id": run_id,
                "run_group_id": "run_group",
                **fields,
            }

    engine = FakeEngine()
    coordinator = WorkflowContinuationCoordinator(engine)
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    run = {
        "run_id": "workflow_run",
        "run_group_id": "run_group",
        "user_goal": "Ship workflow",
    }
    workflow = {
        "nodes": [
            {
                "id": "gate",
                "type": "approval",
                "data": {
                    "label": "Human Gate",
                    "criteria": "Review child output before continuing.",
                },
            }
        ]
    }

    result = coordinator.continue_run(
        run,
        workflow,
        context="Child result ready",
        timeline=timeline,
        artifacts=artifacts,
        start_index=0,
        root_group=True,
    )

    pending = result["pending_approval"]
    assert result["status"] == "approval_required"
    assert result["result"] == "等待审批：Human Gate"
    assert pending["tool"] == "workflow.approval"
    assert pending["input_preview"] == {
        "checkpoint": "Human Gate",
        "context": "Child result ready",
        "criteria": "Review child output before continuing.",
    }
    assert pending["workflow_node_id"] == "gate"
    assert pending["workflow_node_label"] == "Human Gate"
    assert "workflow_context" not in pending
    public_pending = {
        **pending,
        "policy_reason": (
            "Workflow 审批节点要求人工确认：Review child output before continuing."
        ),
    }
    assert timeline == [
        {
            "event": "workflow.node.approval_required",
            "detail": "Human Gate",
            "workflow_node_id": "gate",
            "workflow_node_kind": "approval",
            "workflow_node_label": "Human Gate",
            "workflow_node_approval_criteria": "Review child output before continuing.",
            "status": "approval_required",
            "pending_approval": public_pending,
        }
    ]
    assert engine.events[0] == (
        "workflow_run",
        "workflow.node.approval_required",
        {
            "workflow_node_id": "gate",
            "workflow_node_kind": "approval",
            "workflow_node_label": "Human Gate",
            "workflow_node_approval_criteria": "Review child output before continuing.",
            "status": "approval_required",
            "pending_approval": public_pending,
        },
    )
    assert engine.events[1][0:2] == (
        "workflow_run",
        "group.run.approval_required",
    )
    assert engine.events[1][2]["run_group_id"] == "run_group"
    assert engine.events[1][2]["pending_approval"] == public_pending
    assert len(engine.run_updates) == 1
    run_id, run_update = engine.run_updates[0]
    assert run_id == "workflow_run"
    assert run_update["status"] == "approval_required"
    assert run_update["result"] == "等待审批：Human Gate"
    assert run_update["timeline"] == timeline
    assert run_update["artifacts"] is artifacts
    private_pending = run_update["pending_approval"]
    assert private_pending["approval_id"].startswith("approval_")
    assert private_pending["workflow_context"] == "Child result ready"
    assert private_pending["workflow_next_index"] == 1
    assert private_pending["workflow_next_node_id"] == ""
    assert private_pending["workflow_node_id"] == "gate"
    assert private_pending["workflow_node_label"] == "Human Gate"
    assert private_pending["workflow_node_approval_criteria"] == "Review child output before continuing."
    assert engine.group_updates == [
        (
            "run_group",
            {
                "status": "approval_required",
                "summary": "等待审批：Human Gate",
                "expected_status": "running",
                "expected_updated_at": "2026-07-11T10:00:00+00:00",
            },
        )
    ]


def test_workflow_continuation_coordinator_resumes_after_approval_node(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []
    timeline = [{"event": "workflow.node.approval_required"}]
    artifacts = [{"kind": "workflow_artifact", "path": "notes.md"}]
    workflow = {"workflow_id": "workflow_resume"}
    run = {
        "run_id": "workflow_run",
        "run_group_id": "run_group",
        "status": "approval_required",
    }

    class FakeApprovals:
        def approve_workflow_node(
            self,
            run_id,
            *,
            timeline,
            artifacts,
            result_context,
            workflow_node_id,
            label,
            criteria,
            input_preview,
            expected_approval_id,
        ):
            calls.append(
                (
                    "approve_workflow_node",
                    {
                        "run_id": run_id,
                        "timeline": timeline,
                        "artifacts": artifacts,
                        "context": result_context,
                        "workflow_node_id": workflow_node_id,
                        "label": label,
                        "criteria": criteria,
                        "input_preview": input_preview,
                        "expected_approval_id": expected_approval_id,
                    },
                )
            )
            return {"run_id": run_id, "run_group_id": "run_group", "status": "running"}

    class FakeEngine:
        approvals = FakeApprovals()

        def _update_run_group(self, run_group_id, **fields):
            calls.append(("update_run_group", {"run_group_id": run_group_id, **fields}))

        def get_run(self, run_id):
            calls.append(("get_run", {"run_id": run_id}))
            return {
                "run_id": run_id,
                "run_group_id": "run_group",
                "status": "running",
                "result": "approved context",
            }

    coordinator = WorkflowContinuationCoordinator(FakeEngine())

    def continue_run(received_run, received_workflow, **kwargs):
        calls.append(
            (
                "continue_run",
                {
                    "run": received_run,
                    "workflow": received_workflow,
                    **kwargs,
                },
            )
        )
        return {"run_id": received_run["run_id"], "status": "completed", "result": kwargs["context"]}

    monkeypatch.setattr(coordinator, "continue_run", continue_run)

    result = coordinator.resume_after_approval_node(
        run,
        workflow,
        context="approved context",
        timeline=timeline,
        artifacts=artifacts,
        start_index=3,
        root_group=True,
        workflow_node_id="gate",
        label="Manual Gate",
        criteria="Check output",
        input_preview={"checkpoint": "Manual Gate"},
        expected_approval_id="approval-workflow",
    )

    assert result == {
        "run_id": "workflow_run",
        "status": "completed",
        "result": "approved context",
    }
    assert [name for name, _payload in calls] == [
        "approve_workflow_node",
        "continue_run",
    ]
    assert calls[0][1]["timeline"] is timeline
    assert calls[0][1]["artifacts"] is artifacts
    assert calls[0][1]["workflow_node_id"] == "gate"
    assert calls[0][1]["expected_approval_id"] == "approval-workflow"
    assert calls[-1][1]["run"]["status"] == "running"
    assert calls[-1][1]["workflow"] is workflow
    assert calls[-1][1]["context"] == "approved context"
    assert calls[-1][1]["start_index"] == 3
    assert calls[-1][1]["root_group"] is True


def test_workflow_continuation_coordinator_projects_background_failure_without_secret_leak():
    leaked_secret = "sk-workflow-background-secret123456"

    class FakeEngine:
        def __init__(self):
            self.events: list[tuple[str, str, dict[str, object]]] = []
            self.run_updates: list[tuple[str, dict[str, object]]] = []
            self.group_updates: list[tuple[str, dict[str, object]]] = []

        def _timeline(self, event, detail, **payload):
            return {"event": event, "detail": detail, **payload}

        def _update_run(self, run_id, **fields):
            self.run_updates.append((run_id, fields))
            return {"run_id": run_id, "run_group_id": "run_group", **fields}

        def append_run_event(self, run_id, event_type, payload):
            self.events.append((run_id, event_type, payload))

        def _update_run_group(self, run_group_id, **fields):
            self.group_updates.append((run_group_id, fields))

    engine = FakeEngine()
    coordinator = WorkflowContinuationCoordinator(engine)
    timeline = [{"event": "workflow.run.started", "detail": "Start"}]
    run = {"run_id": "workflow_run", "run_group_id": "run_group"}

    result = coordinator.project_background_failure(
        run,
        timeline=timeline,
        error=RuntimeError(f"provider failed with token={leaked_secret}"),
        root_group=True,
    )

    serialized = json.dumps(
        {
            "result": result,
            "events": engine.events,
            "run_updates": engine.run_updates,
            "group_updates": engine.group_updates,
        },
        ensure_ascii=False,
    )
    assert result["status"] == "failed"
    assert leaked_secret not in serialized
    assert "[redacted]" in result["result"]
    assert timeline == [{"event": "workflow.run.started", "detail": "Start"}]
    assert engine.run_updates == [
        (
            "workflow_run",
            {
                "status": "failed",
                "result": result["result"],
                "timeline": [
                    {"event": "workflow.run.started", "detail": "Start"},
                    {
                        "event": "workflow.run.failed",
                        "detail": result["result"],
                        "status": "failed",
                    },
                ],
                "artifacts": [],
                "pending_approval": None,
            },
        )
    ]
    assert engine.events == [
        ("workflow_run", "workflow.run.failed", {"error": result["result"]})
    ]
    assert engine.group_updates == [
        ("run_group", {"status": "failed", "summary": result["result"]})
    ]


def test_workflow_continuation_coordinator_fails_unknown_node_without_secret_leak():
    leaked_secret = "sk-workflow-continuation-secret123456"

    class FakeEngine:
        def __init__(self):
            self.events: list[tuple[str, str, dict[str, object]]] = []
            self.run_updates: list[tuple[str, dict[str, object]]] = []
            self.group_updates: list[tuple[str, dict[str, object]]] = []

        def _workflow_path(self, workflow):
            return workflow["nodes"]

        def _node_kind(self, node):
            return node["type"]

        def _timeline(self, event, detail, **payload):
            return {"event": event, "detail": detail, **payload}

        def append_run_event(self, run_id, event_type, payload):
            self.events.append((run_id, event_type, payload))

        def _update_run(self, run_id, **fields):
            self.run_updates.append((run_id, fields))
            return {"run_id": run_id, "run_group_id": "run_group", **fields}

        def _update_run_group(self, run_group_id, **fields):
            self.group_updates.append((run_group_id, fields))

        def get_run(self, run_id):
            assert run_id == "workflow_run"
            assert self.run_updates
            return {
                "run_id": run_id,
                "run_group_id": "run_group",
                **self.run_updates[-1][1],
            }

    engine = FakeEngine()
    coordinator = WorkflowContinuationCoordinator(engine)
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    workflow = {
        "nodes": [
            {
                "id": "bad",
                "type": f"custom_api_key={leaked_secret}",
                "data": {"label": "Bad Node"},
            }
        ]
    }

    result = coordinator.continue_run(
        {"run_id": "workflow_run", "run_group_id": "run_group", "user_goal": "Ship workflow"},
        workflow,
        context="previous context",
        timeline=timeline,
        artifacts=artifacts,
        start_index=0,
        root_group=True,
    )

    serialized = json.dumps(
        {
            "result": result,
            "timeline": timeline,
            "events": engine.events,
            "run_updates": engine.run_updates,
            "group_updates": engine.group_updates,
        },
        ensure_ascii=False,
    )
    assert result["status"] == "failed"
    assert leaked_secret not in serialized
    assert "[redacted]" in result["result"]
    assert timeline == [
        {
            "event": "workflow.run.failed",
            "detail": result["result"],
            "status": "failed",
            "workflow_node_id": "bad",
            "workflow_node_kind": "custom_api_key=[redacted]",
            "workflow_node_label": "Bad Node",
        }
    ]
    assert engine.events == [
        (
            "workflow_run",
            "workflow.run.failed",
            {
                "error": result["result"],
                "workflow_node_id": "bad",
                "workflow_node_kind": "custom_api_key=[redacted]",
                "workflow_node_label": "Bad Node",
            },
        )
    ]
    assert engine.run_updates == [
        (
            "workflow_run",
            {
                "status": "failed",
                "result": result["result"],
                "timeline": timeline,
                "artifacts": artifacts,
            },
        )
    ]
    assert engine.group_updates == [
        (
            "run_group",
            {
                "status": "failed",
                "summary": result["result"],
            },
        )
    ]


def test_workflow_continuation_coordinator_writes_artifact_node(tmp_path):
    class FakeEngine:
        def __init__(self):
            self.workflow_artifacts_dir = tmp_path / "workflow-artifacts"
            self.events: list[tuple[str, str, dict[str, object]]] = []
            self.run_updates: list[tuple[str, dict[str, object]]] = []
            self.group_updates: list[tuple[str, dict[str, object]]] = []

        def _workflow_path(self, workflow):
            return workflow["nodes"]

        def _node_kind(self, node):
            return node["type"]

        def _timeline(self, event, detail, **payload):
            return {"event": event, "detail": detail, **payload}

        def append_run_event(self, run_id, event_type, payload):
            self.events.append((run_id, event_type, payload))

        def _update_run(self, run_id, **fields):
            self.run_updates.append((run_id, fields))
            return {"run_id": run_id, "run_group_id": "run_group", **fields}

        def _update_run_group(self, run_group_id, **fields):
            self.group_updates.append((run_group_id, fields))

        def get_run(self, run_id):
            assert run_id == "workflow_run"
            assert self.run_updates
            return {
                "run_id": run_id,
                "run_group_id": "run_group",
                **self.run_updates[-1][1],
            }

        def _default_workspace_policy(self):
            return {"default_workdir": "", "readable_scopes": ["."], "writable_scopes": []}

        def _workflow_artifact_path(self, label, artifacts, requested):
            assert label == "Final Report"
            assert artifacts == []
            return requested or "final.md"

    engine = FakeEngine()
    coordinator = WorkflowContinuationCoordinator(engine)
    artifact_content = "Final workflow summary"
    artifact_bytes = len(artifact_content.encode("utf-8"))
    timeline: list[dict[str, object]] = []
    artifacts: list[dict[str, object]] = []
    workflow = {
        "nodes": [
            {
                "id": "report",
                "type": "artifact",
                "data": {
                    "label": "Final Report",
                    "artifact_path": "reports/final.md",
                },
            }
        ]
    }

    result = coordinator.continue_run(
        {"run_id": "workflow_run", "run_group_id": "run_group", "user_goal": "Ship workflow"},
        workflow,
        context=artifact_content,
        timeline=timeline,
        artifacts=artifacts,
        start_index=0,
        root_group=True,
    )

    artifact_path = tmp_path / "workflow-artifacts" / "workflow_run" / "reports" / "final.md"
    assert artifact_path.read_text(encoding="utf-8") == artifact_content
    assert result["status"] == "completed"
    assert result["result"] == artifact_content
    assert artifacts == [
        {
            "kind": "workflow_artifact",
            "workflow_node_id": "report",
            "workflow_node_label": "Final Report",
            "ok": True,
            "path": "reports/final.md",
            "bytes": artifact_bytes,
            "postcondition_verified": True,
        }
    ]
    assert timeline == [
        {
            "event": "workflow.node.artifact",
            "detail": "Final Report",
            "workflow_node_id": "report",
            "workflow_node_kind": "artifact",
            "workflow_node_label": "Final Report",
            "status": "completed",
            "artifact": {"ok": True, "path": "reports/final.md", "bytes": artifact_bytes, "postcondition_verified": True},
        },
        {
            "event": "workflow.run.completed",
            "detail": "Workflow run completed",
        },
    ]
    assert engine.events == [
        (
            "workflow_run",
            "workflow.node.artifact",
            {
                "workflow_node_id": "report",
                "workflow_node_kind": "artifact",
                "workflow_node_label": "Final Report",
                "status": "completed",
                "artifact": {"ok": True, "path": "reports/final.md", "bytes": artifact_bytes, "postcondition_verified": True},
            },
        ),
        (
            "workflow_run",
            "workflow.run.completed",
            {"result": artifact_content},
        ),
    ]
    assert engine.run_updates == [
        (
            "workflow_run",
            {
                "status": "completed",
                "result": artifact_content,
                "timeline": timeline,
                "artifacts": artifacts,
            },
        )
    ]
    assert engine.group_updates == [
        (
            "run_group",
            {"status": "completed", "summary": artifact_content},
        )
    ]


class FakeDefaultProfileService:
    def get_defaults(self):
        return {"chat": "profile_default"}

    def get_profile_private(self, profile_id):
        assert profile_id == "profile_default"
        return {
            "profile_id": profile_id,
            "provider": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
            "capability": "chat",
            "status": "available",
            "enabled": True,
        }


class FakeNoDefaultProfileService:
    def get_defaults(self):
        return {"chat": ""}

    def get_profile_private(self, profile_id):
        raise KeyError(profile_id)


def test_runtime_migrates_legacy_runs_before_index_creation(tmp_path):
    db_path = tmp_path / "agent-runtime.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            runnable_id TEXT NOT NULL,
            status TEXT NOT NULL,
            user_goal TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            timeline_json TEXT NOT NULL DEFAULT '[]',
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
    )
    conn.close()

    service = make_service(tmp_path)
    try:
        columns = {row["name"] for row in service._conn.execute("PRAGMA table_info(runs)").fetchall()}
        assert "run_group_id" in columns
        indexes = {row["name"] for row in service._conn.execute("PRAGMA index_list(runs)").fetchall()}
        assert "idx_runs_group_updated" in indexes
    finally:
        service.close()


def test_runtime_migrates_task_run_link_projection_columns(tmp_path):
    db_path = tmp_path / "agent-runtime.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys=ON;
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            run_group_id TEXT NOT NULL DEFAULT '',
            client_request_id TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            runnable_id TEXT NOT NULL,
            status TEXT NOT NULL,
            user_goal TEXT NOT NULL DEFAULT '',
            result TEXT NOT NULL DEFAULT '',
            timeline_json TEXT NOT NULL DEFAULT '[]',
            artifacts_json TEXT NOT NULL DEFAULT '[]',
            pending_approval_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE task_run_links (
            task_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL UNIQUE,
            session_id TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
        );
        CREATE TABLE run_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            schema_version INTEGER NOT NULL DEFAULT 1,
            event_type TEXT NOT NULL,
            actor TEXT NOT NULL DEFAULT 'native_runtime',
            visibility TEXT NOT NULL DEFAULT 'user',
            sensitivity TEXT NOT NULL DEFAULT 'public',
            payload_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
            UNIQUE (run_id, sequence)
        );
        INSERT INTO runs (
            run_id, kind, runnable_id, status, user_goal, result,
            timeline_json, artifacts_json, pending_approval_json, created_at, updated_at
        ) VALUES (
            'main_chat_run_legacy_link', 'main_chat_run', 'builtin:yachiyo-main', 'completed',
            'legacy task', 'done', '[]', '[]', '{}', '2026-06-09T00:00:00+00:00',
            '2026-06-09T00:00:01+00:00'
        );
        INSERT INTO task_run_links (task_id, run_id, session_id, created_at)
        VALUES (
            'task-legacy-link', 'main_chat_run_legacy_link', 'session-legacy-link',
            '2026-06-09T00:00:00+00:00'
        );
        INSERT INTO run_events (
            event_id, run_id, sequence, event_type, payload_json, created_at
        ) VALUES
            ('event_legacy_1', 'main_chat_run_legacy_link', 1, 'run.started', '{}', '2026-06-09T00:00:00+00:00'),
            ('event_legacy_2', 'main_chat_run_legacy_link', 2, 'run.completed', '{}', '2026-06-09T00:00:01+00:00');
        """
    )
    conn.commit()
    conn.close()

    service = make_service(tmp_path)
    try:
        link_columns = {row["name"] for row in service._conn.execute("PRAGMA table_info(task_run_links)").fetchall()}
        assert {"run_status", "last_event_sequence", "updated_at"}.issubset(link_columns)
        link = service.get_task_run_link("task-legacy-link")
        assert link["run_status"] == "completed"
        assert link["last_event_sequence"] == 2
        assert link["updated_at"] == "2026-06-09T00:00:00+00:00"
    finally:
        service.close()


def test_legacy_run_group_secret_projection_migration_vacuums_plaintext_secret(tmp_path):
    db_path = tmp_path / "agent-runtime.db"
    title_secret = "sk-legacy-run-group-title123456"
    source_secret = "sk-legacy-run-group-source123456"
    workspace_secret = "sk-legacy-run-group-workspace123456"
    summary_secret = "sk-legacy-run-group-summary123456"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE run_groups (
                run_group_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                workspace_dir TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running',
                summary TEXT NOT NULL DEFAULT '',
                child_run_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO run_groups (
                run_group_id, title, source, workspace_dir, status, summary,
                child_run_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_group_legacy_secret",
                f"Legacy {title_secret}",
                f"workflow-{source_secret}",
                f"/tmp/{workspace_secret}/project",
                "failed",
                f"Failed with token={summary_secret}",
                "[]",
                "2026-06-09T00:00:00+00:00",
                "2026-06-09T00:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    service = make_service(tmp_path)
    try:
        group = service.get_run_group("run_group_legacy_secret")
        row = service._conn.execute(
            "SELECT title, source, workspace_dir, summary FROM run_groups WHERE run_group_id=?",
            ("run_group_legacy_secret",),
        ).fetchone()

        assert group["title"] == "Legacy [redacted]"
        assert group["source"] == "workflow-[redacted]"
        assert group["workspace_dir"] == "/tmp/[redacted]/project"
        assert group["summary"] == "Failed with token=[redacted]"
        assert row["title"] == "Legacy [redacted]"
        assert row["source"] == "workflow-[redacted]"
        assert row["workspace_dir"] == "/tmp/[redacted]/project"
        assert row["summary"] == "Failed with token=[redacted]"
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_runtime_sqlite_enables_required_database_guards(tmp_path):
    service = make_service(tmp_path)
    try:
        metadata = {
            row["key"]: row["value"]
            for row in service._conn.execute("SELECT key, value FROM runtime_schema_metadata").fetchall()
        }
        assert metadata["schema_version"] == "1"
        assert service._conn.execute("PRAGMA foreign_keys").fetchone()["foreign_keys"] == 1
        assert service._conn.execute("PRAGMA journal_mode").fetchone()["journal_mode"].lower() == "wal"
        assert service._conn.execute("PRAGMA busy_timeout").fetchone()["timeout"] == 5000
        link_columns = {row["name"] for row in service._conn.execute("PRAGMA table_info(task_run_links)").fetchall()}
        assert {"run_status", "last_event_sequence", "updated_at"}.issubset(link_columns)

        run = service.start_main_chat_run(task_id="task-db-guard", session_id="session-db-guard", user_goal="db guard")
        link = service.get_task_run_link("task-db-guard")
        assert link["run_id"] == run["run_id"]
        assert link["run_status"] == "running"
        assert link["last_event_sequence"] == 4

        service._conn.execute("DELETE FROM runs WHERE run_id=?", (run["run_id"],))
        service._conn.commit()

        with pytest.raises(KeyError):
            service.get_task_run_link("task-db-guard")
    finally:
        service.close()


def test_task_run_link_repository_tracks_run_projection(tmp_path):
    service = make_service(tmp_path)
    try:
        assert isinstance(service.task_run_links, TaskRunLinkRepository)
        assert isinstance(service.run_projections, RunProjectionCoordinator)
        assert service.runs._sync_projections.__self__ is service.run_projections
        assert service.run_events._sync_event_cursor.__self__ is service.run_projections

        run = service.start_main_chat_run(
            task_id="task-link-repo",
            session_id="session-link-repo",
            user_goal="link repository",
        )
        link = service.task_run_links.get("task-link-repo")
        assert link["run_id"] == run["run_id"]
        assert link["session_id"] == "session-link-repo"
        assert link["run_status"] == "running"
        assert link["last_event_sequence"] == 4
        assert service.task_run_links.for_run(run["run_id"])["task_id"] == "task-link-repo"

        event = service.append_run_event(run["run_id"], "repo.projection.checked", {"ok": True})
        link = service.task_run_links.get("task-link-repo")
        assert link["last_event_sequence"] == event["sequence"]

        service.runs.update(run["run_id"], status="completed", result="done")
        link = service.task_run_links.get("task-link-repo")
        assert link["run_status"] == "completed"
    finally:
        service.close()


def test_main_chat_run_links_task_and_records_replayable_events(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {"role": "assistant", "content": "完成 sk-secret-value"},
    )
    try:
        run = service.start_main_chat_run(
            task_id="task-main-1",
            session_id="session-main-1",
            user_goal="请处理",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "请处理"}],
        )
        completed = service.complete_main_chat_run(run["run_id"], result)
        link = service.get_task_run_link("task-main-1")
        events = service.list_run_events(run["run_id"])["events"]
        assert link["run_id"] == run["run_id"]
        assert link["session_id"] == "session-main-1"
        assert link["run_status"] == "completed"
        assert link["last_event_sequence"] == len(events)
        assert completed["kind"] == "main_chat_run"
        assert completed["runnable_name"] == "Yachiyo"
        assert completed["status"] == "completed"
        assert completed["result"] == "完成 [redacted]"
        assert completed["task_id"] == "task-main-1"
        assert completed["session_id"] == "session-main-1"
        listed_run = next(item for item in service.list_runs()["runs"] if item["run_id"] == run["run_id"])
        assert listed_run["task_id"] == "task-main-1"
        assert listed_run["session_id"] == "session-main-1"
        assert listed_run["task_run_link_run_status"] == "completed"
        assert listed_run["task_run_link_last_event_sequence"] == len(events)
        assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))
        assert [event["event_type"] for event in events] == [
            "run.started",
            "task.created",
            "task.started",
            "task.linked",
            "model.request.started",
            "model.requested",
            "model.output.completed",
            "model.completed",
            "task.completed",
            "run.completed",
        ]
    finally:
        service.close()


def test_main_chat_model_loop_executes_generic_apple_music_intent_before_model(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    open_and_play_calls = 0
    ui_element_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("daily desktop intent should execute before model call"),
    )

    def fake_music_open_and_play() -> dict[str, Any]:
        nonlocal open_and_play_calls
        open_and_play_calls += 1
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": "Opened Music and started playback",
            "data": {
                "app_name": "Music",
                "open_ok": True,
                "playback_ok": True,
                "control": "play",
                "player_state": "playing",
                "track": "超时空辉夜姬",
                "artist": "Yachiyo",
            },
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        ui_element_calls.append(
            {"role_filter": role_filter, "limit": limit, "app_name": app_name}
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Music playback controls",
            "data": {
                "app_name": app_name,
                "elements": [{"role": "AXButton", "name": "Pause"}],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.apple_music_open_and_play", fake_music_open_and_play)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-apple-music-generic",
            session_id="session-main-apple-music-generic",
            user_goal="能否帮我播放 Apple Music?",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "能否帮我播放 Apple Music?"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_event = next(event for event in events if event["event_type"] == "agent.desktop.intent_planned")
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")
        completed_event = next(event for event in events if event["event_type"] == "agent.desktop.intent_completed")

        assert open_and_play_calls == 1
        assert ui_element_calls == []
        assert "已打开 Apple Music，并开始播放" in updated["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert planned_event["payload"]["tool"] == "media.music_app_open_and_play"
        assert planned_event["payload"]["input_preview"] == {"app_name": "Music"}
        assert tool_event["payload"]["tool"] == "media.music_app_open_and_play"
        assert tool_event["payload"]["result"]["ok"] is True
        assert tool_event["payload"]["result"]["action"] == "media.apple_music_open_and_play"
        assert completed_event["payload"]["source"] == "runtime_planner"
    finally:
        service.close()


def test_agent_run_daily_desktop_overlay_plans_from_user_goal_before_context(tmp_path, monkeypatch):
    from apps.shell.agent.runtime import agent_runs as agent_runs_mod

    service = make_service(tmp_path)
    open_and_play_calls = 0
    ui_element_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("agent daily desktop intent should execute before model call"),
    )

    def fake_music_open_and_play() -> dict[str, Any]:
        nonlocal open_and_play_calls
        open_and_play_calls += 1
        return {
            "ok": True,
            "action": "media.apple_music_open_and_play",
            "summary": "Opened Music and started playback",
            "data": {
                "app_name": "Music",
                "open_ok": True,
                "playback_ok": True,
                "control": "play",
                "player_state": "playing",
                "track": "超时空辉夜姬",
                "artist": "Yachiyo",
            },
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        ui_element_calls.append(
            {"role_filter": role_filter, "limit": limit, "app_name": app_name}
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Music playback controls",
            "data": {
                "app_name": app_name,
                "elements": [{"role": "AXButton", "name": "Pause"}],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.apple_music_open_and_play", fake_music_open_and_play)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        agent = service.create_agent(
            {
                "name": "Native Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": ["workspace.read"],
                    "approval_required": {},
                },
            }
        )

        run = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "能否帮我播放apple Music?",
                "daily_desktop_policy_overlay": True,
                "metadata": explicit_foreground_takeover_metadata(),
            }
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_event = next(event for event in events if event["event_type"] == "agent.desktop.intent_planned")
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert not hasattr(agent_runs_mod, "daily_desktop_entrypoint_tool_requests")
        assert open_and_play_calls == 1
        assert ui_element_calls == []
        assert run["status"] == "completed"
        assert "已打开 Apple Music，并开始播放" in run["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert planned_event["payload"]["tool"] == "media.music_app_open_and_play"
        assert tool_event["payload"]["tool"] == "media.music_app_open_and_play"
        assert tool_event["payload"]["result"]["action"] == "media.apple_music_open_and_play"
        assert all(
            event.get("payload", {}).get("tool") != "browser.open_url_and_extract_text"
            for event in events
        )
    finally:
        service.close()


def test_agent_run_runtime_planner_entrypoint_analyzes_data_before_model(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "data-workdir"
    (workdir / "inputs").mkdir(parents=True)
    (workdir / "inputs" / "sales.csv").write_text(
        "region,revenue,units\nEast,10,1\nWest,20,2\nEast,30,3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("agent data analysis entrypoint should execute before model call"),
    )
    try:
        agent = service.create_agent(
            {
                "name": "Data Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": ["workspace.read", "data.analyze", "artifact.write"],
                    "approval_required": {},
                },
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                },
            }
        )

        run = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "请分析 inputs/sales.csv 并输出报告",
                "runtime_planner_entrypoint": True,
            }
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        selection_event = next(event for event in events if event["event_type"] == "agent.plan.selection")
        planned_event = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_planned"
            and event.get("payload", {}).get("tool") == "data.analyze"
        )
        tool_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.call"
            and event.get("payload", {}).get("tool") == "data.analyze"
        )
        artifact_event = next(
            event
            for event in events
            if event["event_type"] == "artifact.created"
            and event.get("payload", {}).get("path") == "analysis-report.md"
        )
        completed_event = next(
            event for event in events if event["event_type"] == "agent.desktop.intent_completed"
        )

        assert run["status"] == "completed"
        assert "已分析「inputs/sales.csv」（3 行、3 列）" in run["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert selection_event["payload"]["intent_kind"] == "data_analysis"
        assert selection_event["payload"]["selection_reason"] == "runtime_planner_full_plan_execution"
        assert planned_event["payload"]["tool"] == "data.analyze"
        assert planned_event["payload"]["input_preview"]["path"] == "inputs/sales.csv"
        assert tool_event["payload"]["tool"] == "data.analyze"
        assert tool_event["payload"]["result"]["ok"] is True
        assert tool_event["payload"]["result"]["rows"] == 3
        assert artifact_event["payload"]["source_tool"] == "data.analyze"
        assert artifact_event["payload"]["path"] == "analysis-report.md"
        assert completed_event["payload"]["source"] == "runtime_planner"
        assert completed_event["payload"]["result"]["artifact_path"] == "analysis-report.md"
        assert any(
            artifact.get("path") == "analysis-report.md"
            and artifact.get("source_tool") == "data.analyze"
            for artifact in run["artifacts"]
            if isinstance(artifact, dict)
        )
    finally:
        service.close()


def test_agent_runtime_file_namespace_tools_alias_workspace_access(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "file-namespace-workdir"
    (workdir / "inputs").mkdir(parents=True)
    (workdir / "inputs" / "sales.csv").write_text(
        "region,revenue\nEast,10\nWest,20\n",
        encoding="utf-8",
    )
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            tool_names = {
                (tool.get("function") or {}).get("name")
                for tool in tools or []
            }
            assert {"file_search", "file_read"} <= tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_file_search",
                        "type": "function",
                        "function": {
                            "name": "file_search",
                            "arguments": json.dumps(
                                {"path": "inputs", "pattern": "*.csv", "file_type": "csv"}
                            ),
                        },
                    }
                ],
            }
        if len(calls) == 2:
            assert calls[-1]["messages"][-1]["role"] == "tool"
            assert "sales.csv" in calls[-1]["messages"][-1]["content"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_file_read",
                        "type": "function",
                        "function": {
                            "name": "file_read",
                            "arguments": json.dumps({"path": "inputs/sales.csv"}),
                        },
                    }
                ],
            }
        assert calls[-1]["messages"][-1]["role"] == "tool"
        assert "East,10" in calls[-1]["messages"][-1]["content"]
        return {"content": "File namespace aliases worked"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "File Namespace Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["file.search", "file.read"]},
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                },
            }
        )
        run = service.create_agent_run(
            {"agent_id": agent["agent_id"], "user_goal": "Inspect sales data"}
        )
        events = service.list_run_events(run["run_id"])["events"]
        tool_events = [
            event for event in events if event["event_type"] == "agent.tool.call"
        ]

        assert run["status"] == "completed"
        assert run["result"] == "File namespace aliases worked"
        assert [event["payload"]["tool"] for event in tool_events] == [
            "file.search",
            "file.read",
        ]
        assert tool_events[0]["payload"]["result"]["ok"] is True
        assert tool_events[0]["payload"]["result"]["entries"] == [
            {"name": "sales.csv", "type": "file"}
        ]
        assert tool_events[1]["payload"]["result"]["content"].startswith(
            "region,revenue"
        )
    finally:
        service.close()


def test_agent_run_runtime_planner_entrypoint_opens_desktop_app_before_model(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    app_open_calls: list[str] = []

    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("desktop planner entrypoint should execute before model call"),
    )

    def fake_app_open(app_name: str) -> dict[str, Any]:
        app_open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {
                "app_name": app_name,
                "opened": True,
                "launch_verified": True,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    try:
        agent = service.create_agent(
            {
                "name": "Desktop Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": ["desktop.open_app"],
                    "approval_required": {},
                },
            }
        )

        run = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "打开 Apple Music",
                "runtime_planner_entrypoint": True,
                "metadata": explicit_foreground_takeover_metadata(),
            }
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_event = next(event for event in events if event["event_type"] == "agent.desktop.intent_planned")
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert app_open_calls == ["Music"]
        assert run["status"] == "completed"
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert planned_event["payload"]["planning_reason"] == "planner_full_plan_desktop_operation"
        assert planned_event["payload"]["tool"] == "desktop.open_app"
        assert tool_event["payload"]["tool"] == "desktop.open_app"
        assert tool_event["payload"]["result"]["action"] == "desktop.open_app"
    finally:
        service.close()


def test_agent_run_runtime_planner_entrypoint_surfaces_deferred_ui_context(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    desktop_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        desktop_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found PixelForge for {query}",
            "data": {
                "query": query,
                "apps": [{"name": "PixelForge", "path": "/Applications/PixelForge.app"}],
                "best_match": {
                    "name": "PixelForge",
                    "path": "/Applications/PixelForge.app",
                    "match_score": 96,
                    "match_confidence": "high",
                },
            },
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.open", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read PixelForge export controls",
            "data": {
                "app_name": "PixelForge",
                "elements": [{"role": "AXButton", "name": "Export"}],
            },
        }

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        assert any(
            "Tool result for desktop.ui_elements" in str(message.get("content") or "")
            for message in messages
        )
        return {"content": "已观察到 Export 按钮，等待确认后可继续点击。"}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Deferred Desktop Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": [
                        "desktop.list_apps",
                        "app.open",
                        "desktop.click_ui_element",
                        "desktop.ui_elements",
                    ],
                    "approval_required": {},
                },
            }
        )

        run = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "打开 PixelForge 并点击 Export",
                "runtime_planner_entrypoint": True,
                "metadata": explicit_foreground_takeover_metadata(),
            }
        )
        events = service.list_run_events(run["run_id"])["events"]
        planned_events = [
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_planned"
        ]
        policy_events = [
            event
            for event in events
            if event["event_type"] == "agent.tool.policy_decision"
        ]
        deferred_ui = next(
            event
            for event in planned_events
            if event["payload"]["tool"] == "desktop.ui_elements"
            and event["payload"].get("deferred_tool") == "desktop.click_ui_element"
        )
        policy_deferred_ui = next(
            event
            for event in policy_events
            if event["payload"]["tool"] == "desktop.ui_elements"
            and event["payload"].get("deferred_tool") == "desktop.click_ui_element"
        )
        policy_click = next(
            event
            for event in policy_events
            if event["payload"]["tool"] == "desktop.click_ui_element"
        )
        pending_approval = run.get("pending_approval")
        assert isinstance(pending_approval, dict)

        assert run["status"] == "approval_required"
        assert pending_approval["tool"] == "desktop.click_ui_element"
        for internal_key in (
            "step_id",
            "runtime_stage",
            "runtime_role",
            "replan_triggers",
        ):
            assert internal_key not in pending_approval
        assert desktop_calls == [
            ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
            ("app.open", {"app_name": "PixelForge"}),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": "PixelForge"},
            ),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": "PixelForge"},
            ),
        ]
        assert model_calls == []
        assert [event["payload"]["tool"] for event in planned_events] == [
            "desktop.list_apps",
            "app.open",
            "desktop.ui_elements",
            "desktop.ui_elements",
            "desktop.click_ui_element",
            "desktop.ui_elements",
        ]
        assert deferred_ui["payload"]["step_id"] == "operate-foreground-ui"
        assert deferred_ui["payload"]["runtime_stage"] == "operate"
        assert deferred_ui["payload"]["runtime_role"] == "click_ui"
        assert deferred_ui["payload"]["deferred_tool"] == "desktop.click_ui_element"
        assert deferred_ui["payload"]["deferred_input"] == {
            "target": "Export",
            "role_filter": "",
            "click_count": 1,
            "limit": 80,
        }
        assert deferred_ui["payload"]["deferred_context"]["step_id"] == (
            "operate-foreground-ui"
        )
        assert deferred_ui["payload"]["replan_triggers"] == ["verification_failed"]
        assert deferred_ui["payload"]["requires_post_action_verification"] is True
        assert deferred_ui["payload"]["execution_mode"] == "read_only_observation"
        assert deferred_ui["payload"]["keyboard_mouse_capture"] is False
        assert deferred_ui["payload"]["desktop_execution_mode"]["mode"] == (
            "read_only_observation"
        )
        assert policy_deferred_ui["payload"]["deferred_tool"] == "desktop.click_ui_element"
        assert policy_deferred_ui["payload"]["deferred_input"] == deferred_ui["payload"][
            "deferred_input"
        ]
        assert policy_deferred_ui["payload"]["runtime_stage"] == "operate"
        assert policy_deferred_ui["payload"]["replan_triggers"] == ["verification_failed"]
        assert policy_click["payload"]["step_id"] == "operate-foreground-ui"
        assert policy_click["payload"]["runtime_stage"] == "operate"
        assert policy_click["payload"]["replan_triggers"] == ["verification_failed"]
        assert policy_click["payload"]["execution_mode"] == "supervised_live"
        assert policy_click["payload"]["keyboard_mouse_capture"] is True
        assert policy_click["payload"]["sandbox_recommended"] is True
        assert policy_click["payload"]["desktop_execution_mode"]["mode"] == (
            "supervised_live"
        )
    finally:
        service.close()


def test_agent_run_runtime_planner_entrypoint_resumes_deferred_ui_approval_without_model(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    desktop_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        desktop_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found PixelForge for {query}",
            "data": {
                "query": query,
                "apps": [{"name": "PixelForge", "path": "/Applications/PixelForge.app"}],
                "best_match": {
                    "name": "PixelForge",
                    "path": "/Applications/PixelForge.app",
                    "match_score": 96,
                    "match_confidence": "high",
                },
            },
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.open", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read PixelForge export controls",
            "data": {
                "app_name": "PixelForge",
                "elements": [{"role": "AXButton", "name": "Export"}],
            },
        }

    def fake_click_ui_element(
        target: str,
        *,
        role_filter: str = "",
        limit: Any = 80,
        click_count: Any = 1,
        expected_app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.click_ui_element",
                {
                    "target": target,
                    "role_filter": role_filter,
                    "limit": limit,
                    "click_count": click_count,
                    "expected_app_name": expected_app_name,
                },
            )
        )
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "summary": f"Clicked {target}",
            "data": {"target": target, "matched_label": target, "click_count": click_count},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.click_ui_element", fake_click_ui_element)

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        return {"content": "已执行 Export 点击；已读取操作后界面。"}

    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        fake_chat,
    )
    try:
        agent = service.create_agent(
            {
                "name": "Deferred Desktop Approval Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": [
                        "desktop.list_apps",
                        "app.open",
                        "desktop.click_ui_element",
                        "desktop.ui_elements",
                    ],
                    "approval_required": {},
                },
            }
        )

        waiting = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "打开 PixelForge 并点击 Export",
                "runtime_planner_entrypoint": True,
                "metadata": explicit_foreground_takeover_metadata(),
            }
        )

        assert waiting["status"] == "approval_required"
        assert waiting["pending_approval"]["tool"] == "desktop.click_ui_element"

        resumed = service.approve_run_approval(
            waiting["run_id"],
            expected_approval_id=waiting["pending_approval"]["approval_id"],
        )
        events = service.list_run_events(waiting["run_id"])["events"]
        event_types = [event["event_type"] for event in events]

        # The approval resume executes both the exact approved click and its
        # declared verifier.  An ordinary ``ok`` UI read is not a trusted
        # postcondition receipt, so Runtime must fail honestly instead of
        # asking the model to turn the acknowledgement into success prose.
        assert resumed["status"] == "failed"
        assert not resumed.get("pending_approval")
        assert resumed["result"] == (
            "已点击前台控件：Export，但未能确认界面已按预期变化；请确认后重试。"
        )
        assert "desktop.open_path" not in resumed["result"]
        assert model_calls == []
        assert desktop_calls == [
            ("desktop.list_apps", {"query": "PixelForge", "limit": 20}),
            ("app.open", {"app_name": "PixelForge"}),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": "PixelForge"},
            ),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": "PixelForge"},
            ),
            (
                "desktop.click_ui_element",
                {
                    "target": "Export",
                    "role_filter": "",
                    "limit": 80,
                    "click_count": 1,
                    "expected_app_name": "",
                },
            ),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": ""},
            ),
        ]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert "agent.desktop.intent_completed" not in event_types
        assert "agent.run.completed" not in event_types
        assert "agent.run.failed" in event_types
    finally:
        service.close()


def test_agent_runtime_planner_file_search_auto_analyzes_single_data_file(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    workdir = tmp_path / "file-search-analysis-workdir"
    (workdir / "inputs").mkdir(parents=True)
    (workdir / "inputs" / "sales.csv").write_text(
        "region,revenue,units\nEast,10,1\nWest,20,2\nEast,30,3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail(
            "file.search data-analysis follow-up should not call the model"
        ),
    )
    try:
        agent = service.create_agent(
            {
                "name": "File Search Data Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": ["file.search", "data.analyze", "artifact.write"],
                    "approval_required": {},
                },
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                },
            }
        )

        run = service.create_agent_run(
            {
                "agent_id": agent["agent_id"],
                "user_goal": "请分析 inputs 里的销售 CSV 并输出报告",
                "runtime_planner_entrypoint": True,
            }
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [
            event for event in events if event["event_type"] == "agent.tool.call"
        ]
        artifact_event = next(
            event
            for event in events
            if event["event_type"] == "artifact.created"
            and event.get("payload", {}).get("path") == "analysis-report.md"
        )

        assert run["status"] == "completed"
        assert "已分析「inputs/sales.csv」（3 行、3 列）" in run["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert [event["payload"]["tool"] for event in tool_events] == [
            "file.search",
            "data.analyze",
        ]
        assert tool_events[0]["payload"]["result"]["entries"] == [
            {"name": "sales.csv", "type": "file"}
        ]
        assert tool_events[1]["payload"]["input_preview"]["path"] == "inputs/sales.csv"
        resolved_target = tool_events[1]["payload"]["action_target"]
        assert resolved_target["expected_path"] == (
            "<selected file from workspace.list>"
        )
        assert resolved_target["path"] == "inputs/sales.csv"
        assert resolved_target["resolution_required"] is True
        resolution_receipt = resolved_target["workspace_file_resolution"]
        assert resolution_receipt["source_tool_name"] == "file.search"
        assert resolution_receipt["source_tool_call_id"] == (
            tool_events[0]["payload"]["tool_call_id"]
        )
        assert resolution_receipt["source_step_id"] == "inspect-data-source"
        assert resolution_receipt["plan_id"] == tool_events[1]["payload"]["plan_id"]
        assert artifact_event["payload"]["source_tool"] == "data.analyze"
        assert artifact_event["payload"]["path"] == "analysis-report.md"
    finally:
        service.close()


def test_workflow_agent_node_runtime_planner_entrypoint_analyzes_data_before_model(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    workdir = tmp_path / "workflow-data-workdir"
    (workdir / "inputs").mkdir(parents=True)
    (workdir / "inputs" / "sales.csv").write_text(
        "region,revenue,units\nEast,10,1\nWest,20,2\nEast,30,3\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("workflow agent node should execute planner tools before model call"),
    )
    try:
        agent = service.create_agent(
            {
                "name": "Workflow Data Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {
                    "allowed_tools": ["workspace.read", "data.analyze", "artifact.write"],
                    "approval_required": {},
                },
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Workflow Data Analysis Entrypoint",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "analysis",
                        "type": "agent",
                        "data": {
                            "label": "Analyze",
                            "agent_id": agent["agent_id"],
                            "task": "请分析 inputs/sales.csv 并输出报告",
                        },
                    },
                ],
                "edges": [{"source": "start", "target": "analysis"}],
            }
        )

        run = service.create_workflow_run(
            {
                "workflow_id": workflow["workflow_id"],
                "user_goal": "请分析 inputs/sales.csv 并输出报告",
            }
        )

        group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        child = child_runs[0]
        events = service.list_run_events(child["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        selection_event = next(event for event in events if event["event_type"] == "agent.plan.selection")
        tool_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.call"
            and event.get("payload", {}).get("tool") == "data.analyze"
        )
        artifact_event = next(
            event
            for event in events
            if event["event_type"] == "artifact.created"
            and event.get("payload", {}).get("path") == "analysis-report.md"
        )
        parent_agent_event = next(
            event for event in run["timeline"] if event["event"] == "workflow.node.agent"
        )

        assert run["status"] == "completed"
        assert child["status"] == "completed"
        assert "已分析「inputs/sales.csv」（3 行、3 列）" in child["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert selection_event["payload"]["intent_kind"] == "data_analysis"
        assert selection_event["payload"]["selection_reason"] == "runtime_planner_full_plan_execution"
        assert tool_event["payload"]["tool"] == "data.analyze"
        assert tool_event["payload"]["result"]["ok"] is True
        assert tool_event["payload"]["result"]["rows"] == 3
        assert artifact_event["payload"]["source_tool"] == "data.analyze"
        assert parent_agent_event["workflow_node_id"] == "analysis"
        assert "已分析「inputs/sales.csv」（3 行、3 列）" in parent_agent_event["result"]
    finally:
        service.close()


def test_main_chat_model_loop_executes_daily_desktop_intent_without_chat_model_profile(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    open_calls: list[str] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("model should not be required for direct desktop intent"),
    )

    def fake_app_open(app_name: str) -> dict[str, Any]:
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

    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-apple-music-no-profile",
            session_id="session-main-apple-music-no-profile",
            user_goal="打开 Apple Music",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "打开 Apple Music"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        tool_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.call"
            and event["payload"]["tool"] == "app.open"
        )

        assert open_calls == ["Music"]
        assert updated["result"] == "已打开 Music。"
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert [event["payload"]["tool"] for event in planned_events] == [
            "desktop.list_apps",
            "app.open",
            "desktop.verify",
        ]
        assert planned_events[1]["payload"]["source"] == "runtime_planner"
        assert planned_events[1]["payload"]["input_preview"] == {"app_name": "Music"}
        assert planned_events[2]["payload"]["runtime_stage"] == "verify"
        assert planned_events[2]["payload"]["planning_reason"] == "planner_full_plan_desktop_operation"
        assert tool_event["payload"]["tool"] == "app.open"
        assert tool_event["payload"]["input_preview"]["app_name"] == "Music"
    finally:
        service.close()


def test_main_chat_model_loop_creates_app_task_without_chat_model_profile(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    desktop_calls: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("model should not be required for direct app task creation"),
    )

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        desktop_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "matches": [{"name": "Linear", "path": "/Applications/Linear.app"}],
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.open", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.focus", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active window: Linear - New task",
            "data": {"app_name": "Linear", "pid": 4242, "title": "New task"},
        }

    def fake_inspect_app(
        app_name: str,
        *,
        open_if_needed: Any = True,
        focus: Any = True,
        role_filter: str = "",
        limit: Any = 80,
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.inspect_app",
                {
                    "app_name": app_name,
                    "open_if_needed": open_if_needed,
                    "focus": focus,
                    "role_filter": role_filter,
                    "limit": limit,
                },
            )
        )
        return {
            "ok": True,
            "action": "desktop.inspect_app",
            "summary": "Inspected Linear task editor",
            "data": {
                "app_name": "Linear",
                "app_found": True,
                "running": True,
                "focused": True,
                "count": 1,
                "elements": [{"role": "AXTextField", "value": ""}],
            },
        }

    def fake_safe_shortcut(action: str) -> dict[str, Any]:
        desktop_calls.append(("desktop.safe_shortcut", {"action": action}))
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"action": action}}

    def fake_safe_type_text(text: str) -> dict[str, Any]:
        desktop_calls.append(("desktop.safe_type_text", {"text": text}))
        return {"ok": True, "action": "desktop.safe_type_text", "data": {"text": text}}

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Linear task editor",
            "data": {
                "app_name": "Linear",
                "title": "New task",
                "count": 1,
                "elements": [{"role": "AXTextField", "value": "修复登录错误"}],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.inspect_app", fake_inspect_app)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-linear-create-task-no-profile",
            session_id="session-main-linear-create-task-no-profile",
            user_goal="帮我打开 Linear 并创建一个任务：修复登录错误",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "帮我打开 Linear 并创建一个任务：修复登录错误"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
            tool_policy={
                "allowed_tools": [
                    "desktop.list_apps",
                    "desktop.inspect_app",
                    "app.open",
                    "app.focus",
                    "desktop.safe_shortcut",
                    "desktop.safe_type_text",
                    "desktop.ui_elements",
                ]
            },
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        completed_event = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_completed"
        )

        assert [name for name, _payload in desktop_calls] == [
            "desktop.list_apps",
            "app.open",
            "app.focus",
            "desktop.safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ]
        assert desktop_calls[3] == ("desktop.safe_shortcut", {"action": "new_task"})
        assert desktop_calls[4] == ("desktop.safe_type_text", {"text": "修复登录错误"})
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert [event["payload"]["tool"] for event in planned_events] == [
            "desktop.list_apps",
            "app.open",
            "desktop.ui_elements",
            "app.focus_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ]
        assert completed_event["payload"]["tools"] == [
            "desktop.list_apps",
            "app.open",
            "desktop.ui_elements",
            "app.focus_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ]
        assert completed_event["payload"]["planning_reason"] == "planner_full_plan_desktop_operation"
        assert "已打开 Linear 并发送“新建任务”快捷键" in updated["result"]
        assert "已向前台输入文字" in updated["result"]
    finally:
        service.close()


def test_main_chat_model_loop_auto_opens_path_with_discovered_app_without_model(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    desktop_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[object] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(*_args, **_kwargs):
        model_calls.append((_args, _kwargs))
        raise AssertionError("model should not be called after app discovery")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        desktop_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        best_match = {
            "name": "Visual Studio Code",
            "path": "/Applications/Visual Studio Code.app",
            "match_score": 96,
            "match_confidence": "high",
            "match_reason": "capability query",
        }
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": "Installed apps matching code: Visual Studio Code",
            "data": {
                "query": query,
                "apps": [best_match],
                "best_match": best_match,
                "count": 1,
                "total_count": 1,
                "truncated": False,
            },
        }

    def fake_open_path_with_app(path: str, app_name: str) -> dict[str, Any]:
        desktop_calls.append(
            ("desktop.open_path_with_app", {"path": path, "app_name": app_name})
        )
        return {
            "ok": True,
            "action": "desktop.open_path_with_app",
            "summary": f"Opened {path} with {app_name}",
            "data": {
                "path": path,
                "app_name": app_name,
                "exists": True,
                "is_dir": False,
                "suffix": ".md",
            },
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Verified Visual Studio Code opened README.md",
            "data": {
                "app_name": "Visual Studio Code",
                "count": 1,
                "elements": [{"role": "AXWindow", "name": "README.md"}],
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.open_path_with_app",
        fake_open_path_with_app,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-open-readme-with-discovered-editor",
            session_id="session-main-open-readme-with-discovered-editor",
            user_goal="找一个代码编辑器打开 README.md",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "找一个代码编辑器打开 README.md"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        resolved_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.input_resolved"
            and event["payload"]["tool"] == "desktop.open_path_with_app"
        )
        open_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.call"
            and event["payload"]["tool"] == "desktop.open_path_with_app"
        )

        assert desktop_calls == [
            ("desktop.list_apps", {"query": "code", "limit": 20}),
            (
                "desktop.open_path_with_app",
                {"path": "README.md", "app_name": "Visual Studio Code"},
            ),
            (
                "desktop.ui_elements",
                {
                    "role_filter": "",
                    "limit": 80,
                    "app_name": "Visual Studio Code",
                },
            ),
        ]
        assert updated["result"] == "已用 Visual Studio Code 打开文件：README.md。"
        assert model_calls == []
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert [event["payload"]["tool"] for event in planned_events] == [
            "desktop.list_apps",
            "desktop.open_path_with_app",
            "desktop.ui_elements",
        ]
        assert planned_events[1]["payload"]["planning_reason"] == (
            "planner_full_plan_desktop_operation"
        )
        assert resolved_event["payload"]["requested_app_name"] == "code"
        assert resolved_event["payload"]["resolved_app_name"] == "Visual Studio Code"
        assert resolved_event["payload"]["resolved_app_path"] == (
            "/Applications/Visual Studio Code.app"
        )
        expected_open_input_preview = {
            "app_name": "Visual Studio Code",
            "path": "README.md",
            "resolved_app_name": "Visual Studio Code",
            "requested_app_name": "code",
            "app_resolution_source": "desktop.list_apps",
            "app_resolution_score": "96",
            "app_resolution_confidence": "high",
            "app_resolution_reason": "capability query",
            "resolved_app_path": "/Applications/Visual Studio Code.app",
        }
        assert expected_open_input_preview.items() <= open_event["payload"][
            "input_preview"
        ].items()
    finally:
        service.close()


def test_main_chat_model_loop_prefetches_desktop_content_before_artifact_write(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    desktop_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        desktop_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "matches": [{"name": "Obsidian", "path": "/Applications/Obsidian.app"}],
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.open", {"app_name": app_name}))
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    def fake_app_focus(app_name: str) -> dict[str, Any]:
        desktop_calls.append(("app.focus", {"app_name": app_name}))
        return {"ok": True, "action": "app.focus", "data": {"app_name": app_name}}

    def fake_active_window() -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Obsidian", "title": "Obsidian"},
        }

    def fake_safe_shortcut(action: str) -> dict[str, Any]:
        desktop_calls.append(("desktop.safe_shortcut", {"action": action}))
        return {"ok": True, "action": "desktop.safe_shortcut", "data": {"action": action}}

    def fake_safe_type_text(text: str) -> dict[str, Any]:
        desktop_calls.append(("desktop.safe_type_text", {"text": text}))
        return {"ok": True, "action": "desktop.safe_type_text", "data": {"text": text}}

    def fake_search_submit() -> dict[str, Any]:
        desktop_calls.append(("desktop.search_submit", {}))
        return {"ok": True, "action": "desktop.search_submit"}

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        desktop_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Obsidian search result content",
            "data": {
                "app_name": "Obsidian",
                "title": "Search: yachiyo runtime",
                "count": 4,
                "truncated": False,
                "elements": [
                    {"role": "AXGroup", "name": "group", "value": ""},
                    {
                        "role": "AXStaticText",
                        "value": "Yachiyo runtime supports discover, act, verify, artifact.",
                    },
                    {
                        "role": "AXStaticText",
                        "value": "Yachiyo runtime supports discover, act, verify, artifact.",
                    },
                    {
                        "role": "AXStaticText",
                        "value": "Desktop content snapshots reduce raw UI tree noise.",
                    },
                ],
            },
        }

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        if len(model_calls) == 1:
            assert messages[-1]["role"] == "user"
            assert messages[-1]["content"].startswith("Runtime follow-up context:")
            assert "desktop-content-report.md" in messages[-1]["content"]
            assert "Desktop content snapshot:" in messages[-1]["content"]
            assert "Yachiyo runtime supports discover, act, verify, artifact." in messages[-1]["content"]
            assert "Desktop content snapshots reduce raw UI tree noise." in messages[-1]["content"]
            assert messages[-1]["content"].count(
                "Yachiyo runtime supports discover, act, verify, artifact."
            ) == 1
            assert any(
                (
                    "Tool result for desktop.read_ui" in str(message.get("content") or "")
                    or "Tool result for desktop.ui_elements" in str(message.get("content") or "")
                )
                and "Yachiyo runtime supports discover" in str(message.get("content") or "")
                for message in messages
            )
            return {
                "content": json.dumps(
                    {
                        "action": "tool",
                        "tool": "artifact.write",
                        "input": {
                            "path": "desktop-content-report.md",
                            "content": "# Desktop Content Report\n\nYachiyo runtime supports discover, act, verify, artifact.",
                        },
                    }
                )
            }
        assert "Tool result for artifact.write" in messages[-1]["content"]
        return {"content": "已生成桌面内容报告。"}

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_search_submit",
        fake_search_submit,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-desktop-content-artifact",
            session_id="session-main-desktop-content-artifact",
            user_goal="打开 Obsidian，搜索 yachiyo runtime，然后把当前内容总结成报告",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [
                {
                    "role": "user",
                    "content": "打开 Obsidian，搜索 yachiyo runtime，然后把当前内容总结成报告",
                }
            ],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
            tool_policy={
                "allowed_tools": [
                    "desktop.list_apps",
                    "app.open",
                    "app.focus",
                    "desktop.safe_shortcut",
                    "desktop.safe_type_text",
                    "desktop.search_submit",
                    "desktop.read_ui",
                    "desktop.ui_elements",
                    "artifact.write",
                ]
            },
        )
        events = service.list_run_events(run["run_id"])["events"]
        internal_events = service.list_run_events(
            run["run_id"], include_internal=True
        )["events"]
        planned_ui = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_planned"
            and event["payload"]["tool"] in {"desktop.read_ui", "desktop.ui_elements"}
        )
        assert not any(
            event["event_type"] == "agent.model.followup_context"
            for event in events
        )
        followup = next(
            event
            for event in internal_events
            if event["event_type"] == "agent.model.followup_context"
        )
        artifact_path = service.agent_artifacts_dir / run["run_id"] / "desktop-content-report.md"

        assert updated["result"] == "已生成桌面内容报告。"
        desktop_tool_names = [name for name, _payload in desktop_calls]
        assert desktop_tool_names[0] == "desktop.list_apps"
        assert "desktop.safe_type_text" in desktop_tool_names
        assert "desktop.search_submit" in desktop_tool_names
        assert "desktop.ui_elements" in desktop_tool_names
        assert planned_ui["payload"]["planning_reason"] == "planner_full_plan_desktop_operation"
        assert planned_ui["payload"].get("continue_to_model") is not True
        assert followup["payload"]["planning_reason"] == "planner_full_plan_desktop_operation"
        assert followup["payload"]["artifacts_expected"] == ["desktop-content-report.md"]
        assert followup["payload"]["artifact_write_allowed"] is True
        assert followup["payload"]["content_snapshot"] == {
            "source_tool": "desktop.ui_elements",
            "ok": True,
            "app_name": "Obsidian",
            "title": "Search: yachiyo runtime",
            "element_count": 4,
            "text_item_count": 2,
            "truncated": False,
            "text": (
                "Yachiyo runtime supports discover, act, verify, artifact.\n"
                "Desktop content snapshots reduce raw UI tree noise."
            ),
        }
        assert len(model_calls) == 2
        assert artifact_path.read_text(encoding="utf-8").startswith("# Desktop Content Report")
    finally:
        service.close()


def test_main_chat_model_loop_writes_generated_page_summary_to_notes(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    install_fake_run_owned_browser_target(service, monkeypatch)
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_extract_text(selector: str = "") -> dict[str, Any]:
        tool_calls.append(("browser.extract_text", {"selector": selector}))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted current page text",
            "data": {
                "selector": selector,
                "text": "Oha-Yachiyo can discover desktop apps, execute tools, and verify results.",
                "truncated": False,
            },
        }

    def fake_notes_create(
        body: str,
        *,
        title: str = "",
        folder_name: str = "",
    ) -> dict[str, Any]:
        tool_calls.append(
            (
                "notes.create",
                {"body": body, "title": title, "folder_name": folder_name},
            )
        )
        return {
            "ok": True,
            "action": "notes.create",
            "summary": "Created note",
            "data": {
                "title": title or "网页摘要",
                "body_length": len(body),
                "folder_name": folder_name,
            },
        }

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith("Runtime follow-up context:")
        assert "saved as a note in Notes" in messages[-1]["content"]
        assert "call notes.create next" in messages[-1]["content"]
        assert "Oha-Yachiyo can discover desktop apps" in messages[-1]["content"]
        return {"content": "网页摘要：Oha-Yachiyo 可以发现桌面应用、执行工具并验证结果。"}

    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.notes_create", fake_notes_create)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-page-summary-note",
            session_id="session-main-page-summary-note",
            user_goal="把当前网页总结一下，保存到备忘录",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "把当前网页总结一下，保存到备忘录"}],
            tool_policy={
                "allowed_tools": [
                    "browser.extract_text",
                    "notes.create",
                ]
            },
        )
        events = service.list_run_events(run["run_id"])["events"]
        internal_events = service.list_run_events(
            run["run_id"], include_internal=True
        )["events"]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        resolved_note_input = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.input_resolved"
            and event["payload"].get("tool") == "notes.create"
            and event["payload"].get("resolution_source") == "model_followup"
        )
        assert not any(
            event["event_type"] == "agent.model.followup_context"
            for event in events
        )
        followup = next(
            event
            for event in internal_events
            if event["event_type"] == "agent.model.followup_context"
        )
        completed_event = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_completed"
        )

        assert updated["result"] == "已创建备忘录：网页摘要（36 个字符）。"
        assert tool_calls == [
            ("browser.extract_text", {"selector": ""}),
            (
                "notes.create",
                {
                    "body": "网页摘要：Oha-Yachiyo 可以发现桌面应用、执行工具并验证结果。",
                    "title": "",
                    "folder_name": "",
                },
            ),
        ]
        assert len(model_calls) == 1
        assert [event["payload"]["tool"] for event in planned_events] == [
            "browser.extract_text",
            "notes.create",
        ]
        assert planned_events[1]["payload"]["planning_reason"] == (
            "planner_full_plan_information_capture"
        )
        assert resolved_note_input["payload"]["planning_reason"] == "planner_followup_note_write"
        assert followup["payload"]["followup_target"] == {
            "kind": "note_write",
            "target_action": "create_note",
            "body_source": "model_generated_content",
            "write_allowed": True,
            "recommended_tools": ["notes.create"],
            "context_source": "current_page_content",
        }
        assert completed_event["payload"]["tool"] == "notes.create"
        assert completed_event["payload"]["tools"] == ["notes.create"]
        assert completed_event["payload"]["planning_reason"] == "planner_followup_note_write"
        assert "model.request.started" in [event["event_type"] for event in events]
    finally:
        service.close()


def test_main_chat_model_loop_writes_generated_page_summary_to_discovered_document_app(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    install_fake_run_owned_browser_target(service, monkeypatch)
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []
    generated = "网页摘要：Oha-Yachiyo 可以发现桌面应用、执行工具并验证结果。"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_extract_text(selector: str = "") -> dict[str, Any]:
        tool_calls.append(("browser.extract_text", {"selector": selector}))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted current page text",
            "data": {
                "selector": selector,
                "text": "Oha-Yachiyo can discover desktop apps, execute tools, and verify results.",
                "truncated": False,
            },
        }

    def fake_list_apps(query: str = "", limit: Any = 200) -> dict[str, Any]:
        tool_calls.append(("desktop.list_apps", {"query": query, "limit": limit}))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "summary": f"Found Typora for {query}",
            "data": {
                "query": query,
                "apps": [
                    {
                        "name": "Typora",
                        "path": "/Applications/Typora.app",
                        "match_score": 94,
                    }
                ],
            },
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        tool_calls.append(("app.open", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict[str, Any]:
        tool_calls.append(("app.focus", {"app_name": app_name}))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_active_window() -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active window: Typora - Untitled",
            "data": {"app_name": "Typora", "pid": 4242, "title": "Untitled"},
        }

    def fake_safe_shortcut(action: str) -> dict[str, Any]:
        tool_calls.append(("desktop.safe_shortcut", {"action": action}))
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "summary": f"Executed safe shortcut: {action}",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict[str, Any]:
        tool_calls.append(("desktop.safe_type_text", {"text": text}))
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "summary": f"Typed {len(text)} chars",
            "data": {"text_length": len(text)},
        }

    def fake_ui_elements(
        role_filter: str = "",
        limit: Any = 80,
        app_name: str = "",
    ) -> dict[str, Any]:
        tool_calls.append(
            (
                "desktop.ui_elements",
                {"role_filter": role_filter, "limit": limit, "app_name": app_name},
            )
        )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Read Typora document controls",
            "data": {
                "app_name": "Typora",
                "title": "Untitled",
                "count": 1,
                "elements": [{"role": "AXTextArea", "value": generated}],
            },
        }

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith("Runtime follow-up context:")
        assert "call desktop.list_apps for 'document'" in messages[-1]["content"]
        assert "insert the generated content" in messages[-1]["content"]
        assert "Oha-Yachiyo can discover desktop apps" in messages[-1]["content"]
        return {"content": generated}

    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-page-summary-discovered-document-app",
            session_id="session-main-page-summary-discovered-document-app",
            user_goal="把当前网页总结到任意文档应用",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "把当前网页总结到任意文档应用"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
            tool_policy={
                "allowed_tools": [
                    "browser.extract_text",
                    "desktop.list_apps",
                    "app.open_and_safe_shortcut",
                    "desktop.safe_type_text",
                    "desktop.ui_elements",
                ]
            },
        )
        events = service.list_run_events(run["run_id"])["events"]
        internal_events = service.list_run_events(
            run["run_id"], include_internal=True
        )["events"]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        assert not any(
            event["event_type"] == "agent.model.followup_context"
            for event in events
        )
        followup = next(
            event
            for event in internal_events
            if event["event_type"] == "agent.model.followup_context"
        )
        completed_event = next(
            event
            for event in reversed(events)
            if event["event_type"] == "agent.desktop.intent_completed"
        )
        resolved_event = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.input_resolved"
            and event["payload"]["tool"] == "app.open_and_safe_shortcut"
        )

        assert "Typora" in str(updated["result"])
        assert "输入文字" in str(updated["result"])
        assert tool_calls == [
            ("browser.extract_text", {"selector": ""}),
            ("desktop.list_apps", {"query": "document", "limit": 20}),
            ("app.open", {"app_name": "Typora"}),
            ("app.focus", {"app_name": "Typora"}),
            ("desktop.safe_shortcut", {"action": "new_document"}),
            ("desktop.safe_type_text", {"text": generated}),
            (
                "desktop.ui_elements",
                {"role_filter": "", "limit": 80, "app_name": "Typora"},
            ),
        ]
        assert len(model_calls) == 1
        assert [event["payload"]["tool"] for event in planned_events] == [
            "browser.extract_text",
            "artifact.write",
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ]
        assert [event["payload"]["planning_reason"] for event in planned_events] == [
            "planner_full_plan_report_generation",
            "planner_full_plan_report_generation",
            "planner_followup_discovered_app_write",
            "planner_followup_discovered_app_write",
            "planner_followup_discovered_app_write",
            "planner_followup_discovered_app_write",
        ]
        followup_target = followup["payload"]["followup_target"]
        assert {
            "kind": "desktop_discovered_app_action",
            "app_query": "document",
            "app_name_source": "desktop.list_apps",
            "target_action": "safe_shortcut",
            "safe_shortcut_action": "new_document",
            "body_source": "model_generated_content",
        }.items() <= followup_target.items()
        assert "desktop.list_apps" in followup_target["recommended_tools"]
        assert "app.open_and_safe_shortcut" in followup_target["recommended_tools"]
        assert "desktop.safe_type_text" in followup_target["recommended_tools"]
        assert "desktop.ui_elements" in followup_target["verify_tools"]
        assert resolved_event["payload"]["requested_app_name"] == "document"
        assert resolved_event["payload"]["resolved_app_name"] == "Typora"
        assert completed_event["payload"]["tools"] == [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ]
        assert completed_event["payload"]["planning_reason"] == (
            "planner_followup_discovered_app_write"
        )
        assert "model.request.started" in [event["event_type"] for event in events]
    finally:
        service.close()


def test_main_chat_model_loop_writes_generated_page_summary_to_artifact(
    tmp_path,
    monkeypatch,
):
    service = make_service(tmp_path)
    install_fake_run_owned_browser_target(service, monkeypatch)
    tool_calls: list[tuple[str, dict[str, Any]]] = []
    model_calls: list[list[dict[str, Any]]] = []
    generated = "# 网页摘要\n\nOha-Yachiyo 可以发现桌面应用、执行工具并验证结果。"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_extract_text(selector: str = "") -> dict[str, Any]:
        tool_calls.append(("browser.extract_text", {"selector": selector}))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted current page text",
            "data": {
                "selector": selector,
                "text": "Oha-Yachiyo can discover desktop apps, execute tools, and verify results.",
                "truncated": False,
            },
        }

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        model_calls.append([dict(message) for message in messages])
        assert messages[-1]["role"] == "user"
        assert messages[-1]["content"].startswith("Runtime follow-up context:")
        assert "durable artifact" in messages[-1]["content"]
        assert "call artifact.write next" in messages[-1]["content"]
        assert "Downloads/research-summary.md" in messages[-1]["content"]
        assert "Oha-Yachiyo can discover desktop apps" in messages[-1]["content"]
        return {"content": generated}

    monkeypatch.setattr("apps.shell.agent.tools.browser.extract_text", fake_extract_text)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-page-summary-artifact",
            session_id="session-main-page-summary-artifact",
            user_goal="把当前网页总结成 markdown 保存到 Downloads",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "把当前网页总结成 markdown 保存到 Downloads"}],
            tool_policy={
                "allowed_tools": [
                    "browser.extract_text",
                    "artifact.write",
                ]
            },
        )
        events = service.list_run_events(run["run_id"])["events"]
        internal_events = service.list_run_events(
            run["run_id"], include_internal=True
        )["events"]
        planned_events = [
            event for event in events if event["event_type"] == "agent.desktop.intent_planned"
        ]
        resolved_artifact_input = next(
            event
            for event in events
            if event["event_type"] == "agent.tool.input_resolved"
            and event["payload"].get("tool") == "artifact.write"
            and event["payload"].get("resolution_source") == "model_followup"
        )
        assert not any(
            event["event_type"] == "agent.model.followup_context"
            for event in events
        )
        followup = next(
            event
            for event in internal_events
            if event["event_type"] == "agent.model.followup_context"
        )
        completed_event = next(
            event
            for event in events
            if event["event_type"] == "agent.desktop.intent_completed"
            and event["payload"]["tool"] == "artifact.write"
        )
        artifact = service.read_run_artifact(run["run_id"], "Downloads/research-summary.md")

        assert updated["result"] == generated
        assert tool_calls == [("browser.extract_text", {"selector": ""})]
        assert len(model_calls) == 1
        planned_tools = [event["payload"]["tool"] for event in planned_events]
        assert planned_tools[0] == "browser.extract_text"
        assert planned_tools[-1] == "artifact.write"
        assert planned_events[-1]["payload"]["planning_reason"] == (
            "planner_full_plan_web_research"
        )
        assert resolved_artifact_input["payload"]["planning_reason"] == (
            "planner_followup_artifact_write"
        )
        assert followup["payload"]["followup_target"] == {
            "kind": "artifact_write",
            "target_action": "write_artifact",
            "path": "Downloads/research-summary.md",
            "body_source": "model_generated_content",
            "write_allowed": True,
            "recommended_tools": ["artifact.write"],
            "intent_kind": "web_research",
        }
        assert completed_event["payload"]["tool"] == "artifact.write"
        assert completed_event["payload"]["tools"] == ["artifact.write"]
        assert completed_event["payload"]["planning_reason"] == "planner_followup_artifact_write"
        assert artifact["content"] == generated
        assert "model.request.started" in [event["event_type"] for event in events]
    finally:
        service.close()


def test_main_chat_daily_desktop_approval_resumes_without_chat_model_profile(tmp_path, monkeypatch):
    from apps.shell.yachiyo_agent import YachiyoAgentService
    from apps.shell.yachiyo_agent.legacy_tasks import LegacyRuntimePort

    service = make_service(tmp_path)
    quit_calls = 0
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeNoDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("approved desktop intent should not require model"),
    )

    def fake_desktop_quit_app() -> dict[str, Any]:
        nonlocal quit_calls
        quit_calls += 1
        return {
            "ok": True,
            "action": "desktop.quit_app",
            "summary": "Quit foreground app",
            "data": {},
        }

    def fake_active_window() -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active window: Finder",
            "data": {"app_name": "Finder", "pid": 4242, "title": ""},
        }

    def fake_ui_elements(**_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "summary": "Finder UI elements",
            "data": {"app_name": "Finder", "elements": []},
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_quit_app", fake_desktop_quit_app)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-quit-no-profile",
            session_id="session-main-quit-no-profile",
            user_goal="退出当前应用",
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "退出当前应用"}],
        )

        assert waiting["status"] == "approval_required"
        assert waiting["pending_approval"]["tool"] == "desktop.quit_app"
        assert waiting["pending_approval"]["input_preview"] == {}
        assert quit_calls == 0

        resumed = service.approve_run_approval(
            run["run_id"],
            expected_approval_id=waiting["pending_approval"]["approval_id"],
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        assert resumed["status"] == "completed"
        completed_event = next(event for event in events if event["event_type"] == "agent.desktop.intent_completed")

        assert resumed["pending_approval"] == {}
        assert resumed["result"] == "已请求退出当前应用。"
        assert quit_calls == 1
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert event_types.count("task.completed") == 1
        assert event_types.count("run.completed") == 1
        assert event_types.index("model.output.completed") < event_types.index(
            "task.completed"
        )
        assert event_types.index("task.completed") < event_types.index("run.completed")
        assert completed_event["payload"]["tool"] == "desktop.quit_app"
        assert completed_event["payload"]["tools"] == [
            "desktop.quit_app",
        ]
        assert completed_event["payload"]["source"] == "runtime_planner"
        public_timeline = YachiyoAgentService(LegacyRuntimePort(service)).get_task_timeline(
            "task-main-quit-no-profile"
        )
        assert public_timeline.run_id == run["run_id"]
        assert public_timeline.task_id == "task-main-quit-no-profile"
        quit_tool_calls = [
            call for call in public_timeline.tool_calls if call.tool_name == "desktop.quit_app"
        ]
        assert quit_tool_calls[-1].status == "completed"
        public_event_types = [event.event_type for event in public_timeline.events]
        assert (
            "agent.desktop.intent_completed",
            "model.output.ready",
        ) in zip(public_event_types, public_event_types[1:])
    finally:
        service.close()


def test_main_chat_model_loop_executes_specific_music_query_before_model(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    list_app_calls: list[tuple[str, int]] = []
    open_calls: list[str] = []
    focus_calls: list[str] = []
    shortcut_calls: list[str] = []
    type_calls: list[str] = []
    submit_calls: list[dict[str, Any]] = []
    play_calls: list[str] = []
    ui_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: pytest.fail("specific music intent should execute before model call"),
    )

    def fake_list_apps(query: str = "", limit: int = 10) -> dict[str, Any]:
        list_app_calls.append((query, limit))
        return {
            "ok": True,
            "action": "desktop.list_apps",
            "query": query,
            "matches": [{"name": "Music", "bundle_id": "com.apple.Music", "score": 0.95}],
            "data": {
                "query": query,
                "matches": [{"name": "Music", "bundle_id": "com.apple.Music", "score": 0.95}],
            },
        }

    def fake_app_open(app_name: str) -> dict[str, Any]:
        open_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.open",
            "data": {"app_name": app_name, "launch_verified": True},
        }

    def fake_app_focus(app_name: str) -> dict[str, Any]:
        focus_calls.append(app_name)
        return {
            "ok": True,
            "action": "app.focus",
            "data": {
                "app_name": app_name,
                "focus_verified": True,
                "foreground_ready": True,
            },
        }

    def fake_active_window() -> dict[str, Any]:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "title": "Music"},
        }

    def fake_safe_shortcut(action: str) -> dict[str, Any]:
        shortcut_calls.append(action)
        return {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "data": {"shortcut_action": action},
        }

    def fake_safe_type_text(text: str) -> dict[str, Any]:
        type_calls.append(text)
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "data": {"text_length": len(text)},
        }

    def fake_search_submit(**kwargs: Any) -> dict[str, Any]:
        submit_calls.append(dict(kwargs))
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "data": {"submitted": True},
        }

    def fake_music_app_open_and_play(app_name: str) -> dict[str, Any]:
        play_calls.append(app_name)
        return {
            "ok": True,
            "action": "media.music_app_open_and_play",
            "data": {
                "app_name": app_name,
                "playback_ok": True,
                "player_state": "playing",
                "track": "超时空辉夜姬",
            },
        }

    def fake_ui_elements(**kwargs: Any) -> dict[str, Any]:
        ui_calls.append(dict(kwargs))
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "Music",
                "elements": [{"role": "AXButton", "name": "Pause"}],
                "count": 1,
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.list_apps", fake_list_apps)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_open", fake_app_open)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.app_focus", fake_app_focus)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.active_window", fake_active_window)
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        fake_safe_shortcut,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_type_text",
        fake_safe_type_text,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_search_submit",
        fake_search_submit,
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.music_app_open_and_play",
        fake_music_app_open_and_play,
    )
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", fake_ui_elements)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-apple-music-song",
            session_id="session-main-apple-music-song",
            user_goal="播放超时空辉夜姬",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "播放超时空辉夜姬"}],
            runtime_execution_metadata=explicit_foreground_takeover_metadata(),
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        planned_tools = [
            event["payload"]["tool"]
            for event in events
            if event["event_type"] == "agent.desktop.intent_planned"
        ]
        tool_events = [
            event["payload"]
            for event in events
            if event["event_type"] == "agent.tool.call"
        ]
        tool_names = [event["tool"] for event in tool_events]

        assert list_app_calls == [("music", 20)]
        assert open_calls == ["Music"]
        assert focus_calls == ["Music"]
        assert shortcut_calls == ["find"]
        assert type_calls == ["超时空辉夜姬"]
        assert submit_calls == [{}]
        assert play_calls == ["Music"]
        assert ui_calls == [
            {"app_name": "Music", "limit": 80, "role_filter": ""}
        ]
        assert "已打开 Apple Music" in updated["result"]
        assert "没能打开查找" not in updated["result"]
        assert "model.request.started" not in event_types
        assert "model.requested" not in event_types
        assert "media.music_app_open_and_play" in planned_tools
        assert "media.apple_music_play" not in planned_tools
        assert tool_names == [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "media.music_app_open_and_play",
            "desktop.ui_elements",
        ]
        assert tool_events[2]["input_preview"] == {"text": "超时空辉夜姬"}
        assert tool_events[4]["input_preview"] == {"app_name": "Music"}
    finally:
        service.close()


def test_main_chat_cancelled_run_ignores_late_model_output(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    run = service.start_main_chat_run(
        task_id="task-main-cancel",
        session_id="session-main-cancel",
        user_goal="cancel me",
    )

    def fake_chat(*_args, **_kwargs):
        service.cancel_run(run["run_id"])
        return {"role": "assistant", "content": "late model output should not win"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    try:
        cancelled = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "cancel me"}],
        )
        completed_after_cancel = service.complete_main_chat_run(
            run["run_id"],
            "late model output should not win",
        )
        failed_after_cancel = service.fail_main_chat_run(
            run["run_id"],
            "late failure should not win",
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        stored = service.get_run(run["run_id"])

        assert cancelled["status"] == "cancelled"
        assert completed_after_cancel["status"] == "cancelled"
        assert failed_after_cancel["status"] == "cancelled"
        assert stored["status"] == "cancelled"
        assert "late model output should not win" not in stored["result"]
        assert "model.output.completed" not in event_types
        assert "run.completed" not in event_types
        assert "run.failed" not in event_types
        assert "agent.run.cancelled" not in event_types
        assert event_types[-1] == "run.cancelled"
    finally:
        service.close()


def test_main_chat_failed_run_emits_task_failed_before_run_failed(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-failed",
            session_id="session-main-failed",
            user_goal="fail me",
        )

        failed = service.fail_main_chat_run(
            run["run_id"],
            "api_key=sk-task-failed-secret123456",
        )
        events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        task_failed = next(event for event in events if event["event_type"] == "task.failed")

        assert failed["status"] == "failed"
        assert event_types.index("task.failed") < event_types.index("run.failed")
        assert task_failed["payload"]["task_id"] == "task-main-failed"
        assert task_failed["payload"]["session_id"] == "session-main-failed"
        assert task_failed["payload"]["status"] == "failed"
        assert "sk-task-failed-secret123456" not in task_failed["payload"]["error"]
    finally:
        service.close()


def test_main_chat_model_output_is_truncated_by_runtime_budget(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_model_output_chars=20)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {"role": "assistant", "content": "x" * 60},
    )
    try:
        run = service.start_main_chat_run(
            task_id="task-main-budget",
            session_id="session-main-budget",
            user_goal="请处理",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "请处理"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_event = next(event for event in events if event["event_type"] == "model.output.completed")

        assert len(result) <= 20
        assert "[truncated]" in result
        assert output_event["payload"]["content"] == result
        assert output_event["payload"]["truncated"] is True
    finally:
        service.close()


def test_main_chat_model_persists_batched_output_event_not_token_deltas(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    model_output = "chunk-" * 1000
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {"role": "assistant", "content": model_output},
    )
    try:
        run = service.start_main_chat_run(
            task_id="task-main-batched-output",
            session_id="session-main-batched-output",
            user_goal="请处理长输出",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "请处理长输出"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert result == model_output
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == model_output
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()


def provider_transport_events(service, run_id):
    """Inspect provider transport facts separately from Runtime discovery.

    The Runtime can discover the workspace before the provider's tool turn.
    Both facts remain recorded; transport tests assert the provider turn and
    additionally ensure that the public event stream hides internal tool data.
    """
    public_events = service.list_run_events(run_id, limit=500)["events"]
    assert not any(
        event["event_type"] in {
            "tool.requested", "tool.started", "tool.completed", "agent.tool.call"
        }
        for event in public_events
    )
    events = service.list_run_events(run_id, limit=500, include_internal=True)["events"]
    contracts = [event["payload"]["goal_contract"] for event in events
                 if event["event_type"] == "agent.goal.contract"]
    assert len(contracts) == 1
    assert contracts[0]["run_id"] == run_id
    assert contracts[0]["original_goal"] == service.get_run(run_id)["user_goal"]
    assert contracts[0]["criteria"]
    return {"events": [
        event for event in events
        if not (
            (event["event_type"].startswith("tool.")
             or event["event_type"] == "agent.tool.call")
            and event["payload"].get("source") == "runtime_planner"
        )
    ]}


def test_main_chat_model_loop_coalesces_stream_chunks_before_persisting(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    chunks = [f"chunk-{index};" for index in range(300)]
    expected = "".join(chunks)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            for chunk in chunks:
                yield {"choices": [{"delta": {"content": chunk}}]}

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-stream-batched-output",
            session_id="session-main-stream-batched-output",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_preserves_stream_finish_reason_and_usage_in_completed_event(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is None
        assert stream is True

        def stream_chunks():
            yield {"choices": [{"delta": {"content": "provider "}, "finish_reason": None}]}
            yield {
                "choices": [{"delta": {"content": "metadata"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
            }

        return stream_chunks()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-stream-metadata",
            session_id="session-main-stream-metadata",
            user_goal="Preserve provider metadata",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Preserve provider metadata"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]
        payload = json.loads(output_rows[0]["payload_json"])

        assert result == "provider metadata"
        assert len(output_rows) == 1
        assert payload["content"] == "provider metadata"
        assert payload["finish_reason"] == "stop"
        assert payload["usage"] == {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        }
        assert payload["truncated"] is False
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()


def test_main_chat_model_preserves_stream_stop_reason_as_finish_reason_in_completed_event(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is None
        assert stream is True

        def stream_chunks():
            yield {"choices": [{"delta": {"content": "provider stop "}, "stop_reason": None}]}
            yield {"choices": [{"delta": {"content": "metadata"}, "stop_reason": "stop"}]}

        return stream_chunks()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-stream-stop-reason",
            session_id="session-main-stream-stop-reason",
            user_goal="Preserve provider stop_reason metadata",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Preserve provider stop_reason metadata"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]
        payload = json.loads(output_rows[0]["payload_json"])

        assert result == "provider stop metadata"
        assert len(output_rows) == 1
        assert payload["content"] == "provider stop metadata"
        assert payload["finish_reason"] == "stop"
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()


def test_main_chat_model_loop_uses_responses_output_text_done_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "final Responses snapshot"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "draft ",
            }
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "partial",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-output-text-done",
            session_id="session-main-responses-output-text-done",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_uses_responses_output_text_done_list_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "final Responses list\nsnapshot"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "draft value that should be replaced",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": [
                    {"type": "output_text", "text": "final Responses list"},
                    {"type": "output_text", "text": {"value": "snapshot"}},
                ],
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-output-text-done-list",
            session_id="session-main-responses-output-text-done-list",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert "draft value" not in json.dumps({"run": updated, "events": [dict(row) for row in rows]}, ensure_ascii=False)
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_uses_responses_output_item_message_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "final message item snapshot"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "msg_response",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": expected}],
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-output-item-message",
            session_id="session-main-responses-output-item-message",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_uses_responses_content_part_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "final content part snapshot"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": expected},
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-content-part",
            session_id="session-main-responses-content-part",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_discards_responses_reasoning_summary_stream(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "visible Responses answer"
    private_reasoning = "private Responses reasoning summary"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": f"{private_reasoning} draft",
            }
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "visible draft",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.reasoning_summary_text.done",
                "output_index": 0,
                "summary_index": 0,
                "text": private_reasoning,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-reasoning-summary",
            session_id="session-main-responses-reasoning-summary",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]
        events_json = json.dumps(events, ensure_ascii=False)

        assert updated["result"] == expected
        assert len(output_events) == 1
        assert output_events[0]["payload"]["content"] == expected
        assert private_reasoning not in updated["result"]
        assert private_reasoning not in events_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()



def test_main_chat_model_loop_discards_responses_reasoning_list_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "visible answer after private reasoning list"
    private_reasoning = "private list reasoning summary"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "rs_main_private_reasoning",
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "private list reasoning"},
                        {"type": "summary_text", "text": {"value": "summary"}},
                    ],
                },
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 1,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-reasoning-list",
            session_id="session-main-responses-reasoning-list",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]
        events_json = json.dumps(events, ensure_ascii=False)

        assert updated["result"] == expected
        assert len(output_events) == 1
        assert output_events[0]["payload"]["content"] == expected
        assert private_reasoning not in updated["result"]
        assert private_reasoning not in events_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()



def test_main_chat_model_loop_persists_streaming_refusal_delta(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "I cannot help with that request."
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {"choices": [{"delta": {"refusal": "I cannot help "}}]}
            yield {"choices": [{"delta": {"refusal": "with that request."}, "finish_reason": "stop"}]}

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-streaming-refusal",
            session_id="session-main-streaming-refusal",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_accepts_refusal_message_field(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "I cannot help with that request."
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True
        return {"role": "assistant", "content": None, "refusal": expected}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-message-refusal",
            session_id="session-main-message-refusal",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_events) == 1
        assert output_events[0]["payload"]["content"] == expected
    finally:
        service.close()



def test_main_chat_model_loop_uses_responses_refusal_done_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Final Responses refusal"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.refusal.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "draft refusal",
            }
            yield {
                "type": "response.refusal.done",
                "output_index": 0,
                "content_index": 0,
                "refusal": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-refusal-done",
            session_id="session-main-responses-refusal-done",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_consumes_openai_compatible_sse_stream(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    chunks = ["native ", "http ", "sse"]
    expected = "".join(chunks)
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for chunk in chunks:
                payload = json.dumps({"choices": [{"delta": {"content": chunk}}]})
                yield f"data: {payload}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        assert "tools" not in body
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-stream",
            session_id="session-main-http-sse-stream",
            user_goal="Use native HTTP SSE stream",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use native HTTP SSE stream"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert result == expected
        assert len(requests) == 1
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()


def test_main_chat_model_consumes_message_level_openai_compatible_sse_frame(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "message-level HTTP SSE result"
    private_reasoning = "provider private reasoning"
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payload = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": expected,
                            "reasoning_content": private_reasoning,
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
            yield f"data: {json.dumps(payload)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-message-level-sse",
            session_id="session-main-http-message-level-sse",
            user_goal="Use message-level native HTTP SSE frame",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use message-level native HTTP SSE frame"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]
        payload_json = json.dumps(output_events[-1]["payload"], ensure_ascii=False)

        assert result == expected
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == expected
        assert private_reasoning not in result
        assert private_reasoning not in payload_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_does_not_persist_streaming_reasoning_as_visible_output(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    private_reasoning = "provider hidden reasoning"
    expected = "visible final answer"
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield (
                f'data: {{"choices":[{{"delta":{{"reasoning_content":"{private_reasoning}"}}}}]}}\n\n'.encode(
                    "utf-8"
                )
            )
            yield f'data: {{"choices":[{{"delta":{{"content":"{expected}"}}}}]}}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-reasoning-hidden",
            session_id="session-main-http-sse-reasoning-hidden",
            user_goal="Keep provider reasoning private",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Keep provider reasoning private"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]
        payload_json = json.dumps(output_events[-1]["payload"], ensure_ascii=False)

        assert result == expected
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == expected
        assert private_reasoning not in result
        assert private_reasoning not in payload_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_rejects_non_stream_reasoning_only_output(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    private_reasoning = "provider private non-stream reasoning"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert stream is True
        return {
            "role": "assistant",
            "content": "",
            "reasoning_content": private_reasoning,
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-non-stream-reasoning-only",
            session_id="session-main-non-stream-reasoning-only",
            user_goal="Do not persist provider reasoning",
        )

        with pytest.raises(AgentRuntimeError, match="空回复"):
            service.call_main_chat_model(
                run["run_id"],
                [{"role": "user", "content": "Do not persist provider reasoning"}],
            )

        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
        serialized_events = json.dumps(events, ensure_ascii=False)
        assert "model.request.failed" in event_types
        assert "model.output.completed" not in event_types
        assert private_reasoning not in serialized_events
    finally:
        service.close()


def test_main_chat_model_loop_rejects_non_stream_reasoning_only_output(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    private_reasoning = "provider private loop reasoning"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert stream is True
        assert tools is not None
        return {
            "role": "assistant",
            "content": "",
            "reasoning_content": private_reasoning,
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-loop-non-stream-reasoning-only",
            session_id="session-main-loop-non-stream-reasoning-only",
            user_goal="Summarize the current conversation",
        )

        with pytest.raises(AgentRuntimeError, match="空回复"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Summarize the current conversation"}],
                tool_policy={"allowed_tools": ["workspace.read"]},
                workspace_policy={"default_workdir": str(tmp_path), "readable_scopes": ["."]},
            )

        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        serialized_events = json.dumps(events, ensure_ascii=False)
        assert "model.request.failed" in event_types
        assert "model.output.completed" not in event_types
        assert "agent.tool.call" not in event_types
        assert private_reasoning not in serialized_events
    finally:
        service.close()



def test_main_chat_model_consumes_coalesced_openai_compatible_sse_frames(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            first = json.dumps({"choices": [{"delta": {"content": "coalesced "}}]})
            second = json.dumps({"choices": [{"delta": {"content": "frames"}}]})
            yield f": keepalive\n\ndata: {first}\n\ndata: {second}\n\ndata: [DONE]\n\n".encode("utf-8")

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-coalesced-sse-stream",
            session_id="session-main-http-coalesced-sse-stream",
            user_goal="Use coalesced native HTTP SSE stream",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use coalesced native HTTP SSE stream"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]

        assert result == "coalesced frames"
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == "coalesced frames"
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_consumes_split_openai_compatible_sse_frame_chunks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": "split runtime frame"}}]})
            frame = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
            yield frame[:8]
            yield frame[8:29]
            yield frame[29:53]
            yield frame[53:]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-split-sse-frame",
            session_id="session-main-http-split-sse-frame",
            user_goal="Use split native HTTP SSE frame",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use split native HTTP SSE frame"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]

        assert result == "split runtime frame"
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == "split runtime frame"
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_consumes_split_utf8_openai_compatible_sse_frame_chunks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "跨块 runtime 文本"
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": expected}}]}, ensure_ascii=False)
            frame = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
            split_at = frame.index("跨".encode("utf-8")) + 1
            yield frame[:split_at]
            yield frame[split_at : split_at + 2]
            yield frame[split_at + 2 :]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-split-utf8-sse-frame",
            session_id="session-main-http-split-utf8-sse-frame",
            user_goal="Use split UTF-8 native HTTP SSE frame",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use split UTF-8 native HTTP SSE frame"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]
        payload_json = json.dumps(output_events[-1]["payload"], ensure_ascii=False)

        assert result == expected
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == expected
        assert "\ufffd" not in result
        assert "\ufffd" not in payload_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_consumes_multiline_openai_compatible_sse_data_event(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield (
                b"id: runtime-chunk-1\r\n"
                b"event: completion.chunk\r\n"
                b'data: {"choices":[{"delta":{"content":"runtime multiline"}\r\n'
                b'data: ,"finish_reason":"stop"}]}\r\n\r\n'
                b"data: [DONE]\r\n\r\n"
            )

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-multiline-sse-data",
            session_id="session-main-http-multiline-sse-data",
            user_goal="Use multiline native HTTP SSE data event",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use multiline native HTTP SSE data event"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]

        assert result == "runtime multiline"
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == "runtime multiline"
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_consumes_openai_compatible_sse_content_parts(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []
    private_reasoning_parts = ["hidden content-part reasoning", "hidden content-part thinking"]
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            yield (
                b'data: {"choices":[{"delta":{"content":'
                b'[{"type":"reasoning","text":{"value":"hidden content-part reasoning"}},'
                b'{"type":"text","text":{"value":"content-part "}}]}}]}\n\n'
            )
            yield (
                b'data: {"choices":[{"message":{"role":"assistant","content":'
                b'[{"type":"thinking","text":{"value":"hidden content-part thinking"}},'
                b'{"type":"text","text":{"value":"stream output"}}]},"finish_reason":"stop"}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-content-parts",
            session_id="session-main-http-sse-content-parts",
            user_goal="Use content part native HTTP SSE frame",
        )
        result = service.call_main_chat_model(
            run["run_id"],
            [{"role": "user", "content": "Use content part native HTTP SSE frame"}],
        )
        events = service.list_run_events(run["run_id"])["events"]
        output_events = [event for event in events if event["event_type"] == "model.output.completed"]

        assert result == "content-part stream output"
        assert len(requests) == 1
        assert output_events[-1]["payload"]["content"] == "content-part stream output"
        assert output_events[-1]["payload"]["output_chars"] == len("content-part stream output")
        for private_reasoning in private_reasoning_parts:
            assert private_reasoning not in result
            assert private_reasoning not in json.dumps(output_events)
        assert not any(str(event["event_type"]).endswith(".delta") for event in events)
    finally:
        service.close()


def test_main_chat_model_loop_executes_openai_compatible_sse_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("http sse tool content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            b": provider keepalive\n\n",
            b'event: ping\ndata: {"type":"ping"}\n\n',
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_http_sse_read",
                                        "type": "function",
                                        "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            b'data: {"type":"heartbeat","created":123}\n\n',
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"name": "read", "arguments": 'ME.md"}'}}
                                ]
                            }
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-tool-call",
            session_id="session-main-http-sse-tool-call",
            user_goal="Read README through HTTP SSE tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "HTTP SSE tool call complete"
        assert len(requests) == 2
        assert "tools" in requests[0]["body"]
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_read"
        assert "http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_singular_sse_tool_call_frames(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("singular http sse tool content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_call": {
                                    "index": 0,
                                    "id": "call_http_sse_singular_read",
                                    "type": "function",
                                    "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                }
                            }
                        }
                    ]
                }
            ),
            event(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_call": {
                                    "index": 0,
                                    "function": {"name": "read", "arguments": 'ME.md"}'},
                                }
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Singular HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-singular-tool-call",
            session_id="session-main-http-sse-singular-tool-call",
            user_goal="Read README through singular HTTP SSE tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Singular HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_singular_read"
        assert "singular http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_sse_delta_tool_call_object_arguments(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("http sse object arguments content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_http_sse_object_args_read",
                                        "type": "function",
                                        "function": {
                                            "name": "workspace_read",
                                            "arguments": {"path": "README.md"},
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "HTTP SSE object arguments complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-object-arguments",
            session_id="session-main-http-sse-object-arguments",
            user_goal="Read README through HTTP SSE object arguments",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert updated["result"] == "HTTP SSE object arguments complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_http_sse_object_args_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_object_args_read"
        assert "http sse object arguments content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_message_level_openai_compatible_sse_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("message-level http sse tool content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_http_sse_message_read",
                                        "type": "function",
                                        "function": {
                                            "name": "workspace_read",
                                            "arguments": {"path": "README.md"},
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"message": {"role": "assistant", "content": "Message-level SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-message-level-sse-tool-call",
            session_id="session-main-http-message-level-sse-tool-call",
            user_goal="Read README through message-level HTTP SSE tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Message-level SSE tool call complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_message_read"
        assert "message-level http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_multiline_openai_compatible_sse_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("multiline http sse tool content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for chunk in self._chunks:
                yield chunk

    first_response = FakeResponse(
        [
            (
                b"id: multiline-tool-1\r\n"
                b"event: completion.chunk\r\n"
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_http_sse_multiline_read",\r\n'
                b'data: "type":"function","function":{"name":"workspace_","arguments":"{\\"path\\": \\"READ"}}]}}]}\r\n\r\n'
            ),
            (
                b"id: multiline-tool-2\r\n"
                b"event: completion.chunk\r\n"
                b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,\r\n'
                b'data: "function":{"name":"read","arguments":"ME.md\\"}"}}]},"finish_reason":"tool_calls"}]}\r\n\r\n'
            ),
            b"data: [DONE]\r\n\r\n",
        ]
    )
    second_response = FakeResponse(
        [
            (
                b"id: multiline-completion\r\n"
                b"event: completion.chunk\r\n"
                b'data: {"choices":[{"delta":{"content":"Multiline HTTP SSE tool call complete"}\r\n'
                b'data: ,"finish_reason":"stop"}]}\r\n\r\n'
            ),
            b"data: [DONE]\r\n\r\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-multiline-http-sse-tool-call",
            session_id="session-main-multiline-http-sse-tool-call",
            user_goal="Read README through multiline HTTP SSE tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Multiline HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_multiline_read"
        assert "multiline http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_split_openai_compatible_sse_tool_call_frames(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("split http sse tool content", encoding="utf-8")
    requests = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __init__(self, chunks):
            self._chunks = chunks

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for chunk in self._chunks:
                yield chunk

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    def split_frame(frame: bytes) -> list[bytes]:
        return [frame[:11], frame[11:47], frame[47:93], frame[93:]]

    first_tool_delta = event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_http_sse_split_read",
                                "type": "function",
                                "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                            }
                        ]
                    }
                }
            ]
        }
    )
    second_tool_delta = event(
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"index": 0, "function": {"name": "read", "arguments": 'ME.md"}'}}
                        ]
                    }
                }
            ]
        }
    )
    first_response = FakeResponse([
        *split_frame(first_tool_delta),
        *split_frame(second_tool_delta),
        *split_frame(b"data: [DONE]\n\n"),
    ])
    second_response = FakeResponse(
        split_frame(event({"choices": [{"delta": {"content": "Split HTTP SSE tool call complete"}}]}))
        + [b"data: [DONE]\n\n"]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-split-http-sse-tool-call",
            session_id="session-main-split-http-sse-tool-call",
            user_goal="Read README through split HTTP SSE tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Split HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_http_sse_split_read"
        assert "split http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_fails_on_openai_compatible_sse_error(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-http-sse-provider-error123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            yield (
                "data: "
                + json.dumps(
                    {
                        "error": {
                            "message": f"provider stream rejected token={leaked_secret}",
                            "type": "rate_limit_error",
                            "code": "quota_exceeded",
                        }
                    }
                )
                + "\n\n"
            ).encode("utf-8")

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-sse-error",
            session_id="session-main-http-sse-error",
            user_goal=f"Reply with exactly '{leaked_secret}'",
        )

        with pytest.raises(Exception, match="OpenAI-compatible Profile 调用失败"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Trigger SSE provider error"}],
            )

        failed = service.get_run(run["run_id"])
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)

        assert failed["status"] == "failed"
        assert "rate_limit_error" in failed["result"]
        assert "quota_exceeded" in failed["result"]
        assert leaked_secret not in persisted_projection
        assert "[redacted]" in persisted_projection
        assert any(event["event_type"] == "model.request.failed" for event in events)
        assert event_types.index("model.request.failed") < event_types.index("task.failed")
        assert event_types.index("task.failed") < event_types.index("run.failed")
        assert not any(event["event_type"] == "model.output.completed" for event in events)
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []



def test_main_chat_model_loop_redacts_multiline_openai_compatible_sse_error(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-http-sse-multiline-error123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial multiline"}}]}\r\n\r\n'
            yield (
                b"id: multiline-error-1\r\n"
                b"event: error\r\n"
                b'data: {"error":{\r\n'
                + f'data: "message":"provider stream rejected token={leaked_secret}",\r\n'.encode("utf-8")
                + b'data: "type":"rate_limit_error","code":"quota_exceeded"}}\r\n\r\n'
            )

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-http-multiline-sse-error",
            session_id="session-main-http-multiline-sse-error",
            user_goal=f"Reply with exactly '{leaked_secret}'",
        )

        with pytest.raises(Exception, match="OpenAI-compatible Profile 调用失败"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Trigger multiline SSE provider error"}],
            )

        failed = service.get_run(run["run_id"])
        events = provider_transport_events(service, run["run_id"])["events"]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)

        assert failed["status"] == "failed"
        assert "rate_limit_error" in failed["result"]
        assert "quota_exceeded" in failed["result"]
        assert leaked_secret not in persisted_projection
        assert "[redacted]" in persisted_projection
        assert any(event["event_type"] == "model.request.failed" for event in events)
        assert not any(event["event_type"] == "model.output.completed" for event in events)
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []



def test_main_chat_model_loop_coalesces_openai_sdk_object_stream_before_persisting(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    chunks = [f"sdk-object-chunk-{index};" for index in range(180)]
    expected = "".join(chunks)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert tools is not None

        def stream():
            for chunk in chunks:
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content=chunk),
                            finish_reason=None,
                        )
                    ]
                )
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=""))])

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-sdk-object-stream-batched-output",
            session_id="session-main-sdk-object-stream-batched-output",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]

        assert updated["result"] == expected
        assert len(output_rows) == 1
        assert json.loads(output_rows[0]["payload_json"])["content"] == expected
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
        assert len(rows) < 20  # Includes immutable goal and assessment lifecycle facts.
    finally:
        service.close()



def test_main_chat_model_loop_preserves_openai_sdk_stream_usage_in_completed_event(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream_chunks():
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="sdk "),
                        finish_reason=None,
                    )
                ]
            )
            yield SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        delta=SimpleNamespace(content="metadata"),
                        finish_reason="stop",
                    )
                ],
                usage=SimpleNamespace(prompt_tokens=5, completion_tokens=6, total_tokens=11),
            )

        return stream_chunks()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-sdk-object-stream-metadata",
            session_id="session-main-sdk-object-stream-metadata",
            user_goal="Summarize the current conversation",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Summarize the current conversation"}],
        )
        rows = service._conn.execute(
            "SELECT event_type, payload_json FROM run_events WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        output_rows = [row for row in rows if row["event_type"] == "model.output.completed"]
        payload = json.loads(output_rows[0]["payload_json"])

        assert updated["result"] == "sdk metadata"
        assert len(output_rows) == 1
        assert payload["content"] == "sdk metadata"
        assert payload["finish_reason"] == "stop"
        assert payload["usage"] == {
            "prompt_tokens": 5,
            "completion_tokens": 6,
            "total_tokens": 11,
        }
        assert payload["truncated"] is False
        assert not any(str(row["event_type"]).endswith(".delta") for row in rows)
    finally:
        service.close()



def test_main_chat_model_loop_coalesces_streaming_tool_call_deltas(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("streamed tool call content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_stream_read",
                                        type="function",
                                        function=SimpleNamespace(name="workspace_", arguments=""),
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        function=SimpleNamespace(name="read", arguments='{"path": "READ'),
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        function=SimpleNamespace(arguments='ME.md"}'),
                                    )
                                ]
                            )
                        )
                    ]
                )

            return stream()
        assert messages[-1]["role"] == "tool"
        assert "streamed tool call content" in messages[-1]["content"]
        return {"content": "Streaming tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-streaming-tool-call",
            session_id="session-main-streaming-tool-call",
            user_goal="Read README through streaming tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Streaming tool call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_responses_style_streaming_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("responses streamed tool call content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield {"type": "response.output_text.delta", "delta": "checking responses "}
                yield {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "fc_response_read",
                        "type": "function_call",
                        "call_id": "call_response_read",
                        "name": "workspace_read",
                        "arguments": "",
                    },
                }
                yield {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_response_read",
                    "delta": '{"path": "READ',
                }
                yield {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_response_read",
                    "delta": 'ME.md"}',
                }
                yield {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "fc_response_read",
                        "type": "function_call",
                        "call_id": "call_response_read",
                        "name": "workspace_read",
                        "arguments": {"path": "README.md"},
                    },
                }
                yield {"type": "response.completed", "response": {"status": "completed"}}

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] == "checking responses "
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_response_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert "responses streamed tool call content" in messages[-1]["content"]
        return {"content": "Responses streaming tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-streaming-tool-call",
            session_id="session-main-responses-streaming-tool-call",
            user_goal="Read README through Responses-style streaming tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Responses streaming tool call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_multiple_responses_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("responses README content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("responses NOTES content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_response_readme",
                        "type": "function_call",
                        "call_id": "call_response_readme",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "id": "fc_response_notes",
                        "type": "function_call",
                        "call_id": "call_response_notes",
                        "name": "workspace_read",
                        "arguments": '{"path": "NOTES.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert [tool_call["id"] for tool_call in assistant_tool_messages[-1]["tool_calls"]] == [
            "call_response_readme",
            "call_response_notes",
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_response_readme",
            "call_response_notes",
        ]
        assert "responses README content" in tool_messages[0]["content"]
        assert "responses NOTES content" in tool_messages[1]["content"]
        return {"content": "Responses multiple tool calls complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-multiple-tool-calls",
            session_id="session-main-responses-multiple-tool-calls",
            user_goal="Read README and NOTES through Responses-style tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README and NOTES"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [event for event in events if event["event_type"] == "agent.tool.call"]

        assert updated["result"] == "Responses multiple tool calls complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("agent.tool.call") == 2
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
        assert len(calls) == 2
    finally:
        service.close()



def test_main_chat_model_loop_preserves_responses_zero_output_index(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("zero output index README content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("zero output index NOTES content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_response_zero",
                        "index": 1,
                        "type": "function_call",
                        "call_id": "call_response_zero",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "id": "fc_response_one",
                        "index": 0,
                        "type": "function_call",
                        "call_id": "call_response_one",
                        "name": "workspace_read",
                        "arguments": '{"path": "NOTES.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [tool_call["id"] for tool_call in assistant_tool_messages[-1]["tool_calls"]] == [
            "call_response_zero",
            "call_response_one",
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_response_zero",
            "call_response_one",
        ]
        assert "zero output index README content" in tool_messages[0]["content"]
        assert "zero output index NOTES content" in tool_messages[1]["content"]
        return {"content": "Responses zero output index complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-zero-output-index",
            session_id="session-main-responses-zero-output-index",
            user_goal="Read README and NOTES through zero-index Responses tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README and NOTES"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        tool_events = [
            event for event in provider_transport_events(service, run["run_id"])["events"] if event["event_type"] == "agent.tool.call"
        ]

        assert updated["result"] == "Responses zero output index complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == [
            "README.md",
            "NOTES.md",
        ]
        assert len(calls) == 2
    finally:
        service.close()



def test_main_chat_model_loop_uses_responses_call_id_without_item_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("responses call id only content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_response_only",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_response_only"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_response_only"
        assert "responses call id only content" in messages[-1]["content"]
        return {"content": "Responses call id only complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-responses-call-id-only",
            session_id="session-main-responses-call-id-only",
            user_goal="Read README through Responses call_id",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Responses call id only complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert len(calls) == 2
    finally:
        service.close()



def test_main_chat_model_loop_executes_legacy_streaming_function_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("legacy function call content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                function_call=SimpleNamespace(
                                    name="workspace_",
                                    arguments='{"path": "READ',
                                )
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                function_call=SimpleNamespace(
                                    name="read",
                                    arguments='ME.md"}',
                                )
                            ),
                            finish_reason="function_call",
                        )
                    ]
                )

            return stream()
        assert messages[-1]["role"] == "tool"
        assert "legacy function call content" in messages[-1]["content"]
        return {"content": "Legacy streaming function call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-legacy-streaming-function-call",
            session_id="session-main-legacy-streaming-function-call",
            user_goal="Read README through legacy streaming function_call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Legacy streaming function call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_top_level_delta_message_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("top-level delta message tool content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield {
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "call_top_level_delta_message_read",
                                "type": "function",
                                "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                            }
                        ]
                    }
                }
                yield {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_top_level_delta_message_read",
                                "type": "function",
                                "function": {"name": "read", "arguments": 'ME.md"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_top_level_delta_message_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_top_level_delta_message_read"
        assert "top-level delta message tool content" in messages[-1]["content"]
        return {"content": "Top-level delta message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-top-level-delta-message-tool-call",
            session_id="session-main-top-level-delta-message-tool-call",
            user_goal="Read README through top-level delta/message tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Top-level delta message tool call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()


def test_main_chat_model_loop_coalesces_interleaved_streaming_tool_call_deltas(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("readme streamed content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("notes streamed content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_stream_readme",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_read",
                                            arguments='{"path": "READ',
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=1,
                                        id="call_stream_notes",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "NOT',
                                        ),
                                    ),
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=1,
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ES.md"}',
                                        ),
                                    ),
                                    SimpleNamespace(
                                        index=0,
                                        function=SimpleNamespace(arguments='ME.md"}'),
                                    ),
                                ]
                            )
                        )
                    ]
                )

            return stream()
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_stream_readme",
            "call_stream_notes",
        ]
        assert "readme streamed content" in tool_messages[0]["content"]
        assert "notes streamed content" in tool_messages[1]["content"]
        return {"content": "Interleaved streaming tool calls complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-interleaved-streaming-tool-calls",
            session_id="session-main-interleaved-streaming-tool-calls",
            user_goal="Read README and NOTES through streaming tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README and NOTES"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [event for event in events if event["event_type"] == "agent.tool.call"]

        assert updated["result"] == "Interleaved streaming tool calls complete"
        assert [event["payload"]["tool"] for event in tool_events] == [
            "workspace.read",
            "workspace.read",
        ]
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_keeps_multi_choice_same_index_streaming_tool_calls_separate(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("readme multi-choice content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("notes multi-choice content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_choice_readme",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "READ',
                                        ),
                                    )
                                ]
                            ),
                        ),
                        SimpleNamespace(
                            index=1,
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        id="call_choice_notes",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "NOT',
                                        ),
                                    )
                                ]
                            ),
                        ),
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            index=0,
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ME.md"}',
                                        ),
                                    )
                                ]
                            ),
                        ),
                        SimpleNamespace(
                            index=1,
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        index=0,
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ES.md"}',
                                        ),
                                    )
                                ]
                            ),
                        ),
                    ]
                )

            return stream()
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_choice_readme",
            "call_choice_notes",
        ]
        assert "readme multi-choice content" in tool_messages[0]["content"]
        assert "notes multi-choice content" in tool_messages[1]["content"]
        return {"content": "Multi-choice same-index streaming tool calls complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-multi-choice-same-index-streaming-tool-calls",
            session_id="session-main-multi-choice-same-index-streaming-tool-calls",
            user_goal="Read README and NOTES through multi-choice streaming tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README and NOTES"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [event for event in events if event["event_type"] == "agent.tool.call"]

        assert updated["result"] == "Multi-choice same-index streaming tool calls complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("agent.tool.call") == 2
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_coalesces_indexless_streaming_tool_call_deltas(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("readme indexless stream content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        id="call_indexless_read",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "READ',
                                        ),
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ME.md"}',
                                        ),
                                    )
                                ]
                            ),
                            finish_reason="tool_calls",
                        )
                    ]
                )

            return stream()
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in tool_messages] == ["call_indexless_read"]
        assert "readme indexless stream content" in tool_messages[0]["content"]
        return {"content": "Indexless streaming tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-indexless-streaming-tool-call",
            session_id="session-main-indexless-streaming-tool-call",
            user_goal="Read README through indexless streaming tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [event for event in events if event["event_type"] == "agent.tool.call"]

        assert updated["result"] == "Indexless streaming tool call complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == ["README.md"]
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_coalesces_indexless_interleaved_tool_call_deltas_by_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("readme indexless id content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("notes indexless id content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        id="call_indexless_id_readme",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "READ',
                                        ),
                                    ),
                                    SimpleNamespace(
                                        id="call_indexless_id_notes",
                                        type="function",
                                        function=SimpleNamespace(
                                            name="workspace_",
                                            arguments='{"path": "NOT',
                                        ),
                                    ),
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        id="call_indexless_id_notes",
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ES.md"}',
                                        ),
                                    )
                                ]
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                tool_calls=[
                                    SimpleNamespace(
                                        id="call_indexless_id_readme",
                                        function=SimpleNamespace(
                                            name="read",
                                            arguments='ME.md"}',
                                        ),
                                    )
                                ]
                            ),
                            finish_reason="tool_calls",
                        )
                    ]
                )

            return stream()
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_indexless_id_readme",
            "call_indexless_id_notes",
        ]
        assert "readme indexless id content" in tool_messages[0]["content"]
        assert "notes indexless id content" in tool_messages[1]["content"]
        return {"content": "Indexless id streaming tool calls complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-indexless-id-streaming-tool-calls",
            session_id="session-main-indexless-id-streaming-tool-calls",
            user_goal="Read README and NOTES through indexless id streaming tool calls",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README and NOTES"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_events = [event for event in events if event["event_type"] == "agent.tool.call"]

        assert updated["result"] == "Indexless id streaming tool calls complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_events] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("agent.tool.call") == 2
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()


def test_main_chat_model_loop_executes_provider_message_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("provider message tool content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_provider_read",
                        "type": "function",
                        "function": {
                            "name": "workspace_read",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            }

        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_provider_read"
        arguments = assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"path": "README.md"}
        assert tool_messages[-1]["tool_call_id"] == "call_provider_read"
        assert "provider message tool content" in tool_messages[-1]["content"]
        return {"role": "assistant", "content": "Provider message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-provider-message-tool-calls",
            session_id="session-main-provider-message-tool-calls",
            user_goal="Read README through provider message tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "Provider message tool call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_model_loop_executes_openai_sdk_object_message_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("sdk object message tool content", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert tools is not None
            return SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_sdk_object_read",
                        type="function",
                        function=SimpleNamespace(
                            name="workspace_read",
                            arguments={"path": "README.md"},
                        ),
                    )
                ],
            )

        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_sdk_object_read"
        arguments = assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"path": "README.md"}
        assert tool_messages[-1]["tool_call_id"] == "call_sdk_object_read"
        assert "sdk object message tool content" in tool_messages[-1]["content"]
        return {"role": "assistant", "content": "SDK object message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-sdk-object-message-tool-calls",
            session_id="session-main-sdk-object-message-tool-calls",
            user_goal="Read README through SDK object message tool call",
        )
        updated = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )
        events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in events]
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")

        assert updated["result"] == "SDK object message tool call complete"
        assert tool_event["payload"]["tool"] == "workspace.read"
        assert tool_event["payload"]["input_preview"]["path"] == "README.md"
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("model.output.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_main_chat_provider_exception_is_redacted_from_run_events_and_storage(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-provider-exception123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        raise RuntimeError(f"provider failed api_key={leaked_secret}")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-provider-leak",
            session_id="session-provider-leak",
            user_goal=f"handle request token={leaked_secret}",
        )

        with pytest.raises(RuntimeError):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "trigger provider failure"}],
            )

        failed = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)

        assert failed["status"] == "failed"
        assert leaked_secret not in persisted_projection
        assert "[redacted]" in persisted_projection
        assert any(event["event_type"] == "model.request.failed" for event in events)
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_main_chat_model_loop_executes_native_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "out.txt").write_text("before\n", encoding="utf-8")
    (workdir / "README.md").write_text("hello main chat tools", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "README.md"})},
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "hello main chat tools" in messages[-1]["content"]
        return {"content": "Main chat read complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(task_id="task-main-tools", session_id="session-main-tools", user_goal="Read")
        result = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )

        assert result["status"] == "running"
        assert result["result"] == "Main chat read complete"
        tool_event = next(event for event in result["timeline"] if event["event"] == "agent.tool.call")
        assert tool_event["detail"] == "workspace.read"
        assert tool_event["result"]["ok"] is True
    finally:
        service.close()


def test_main_chat_tool_exception_is_redacted_from_tool_messages_events_and_storage(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []
    tool_attempts = []
    leaked_secret = "sk-tool-exception123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "README.md"})},
                    }
                ],
            }
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert tool_messages
        assert leaked_secret not in json.dumps(messages)
        assert "[redacted]" in json.dumps(messages)
        return {"content": "Recovered from redacted tool failure"}

    def failing_tool_call(self, name, payload, *, approved=False):
        assert name == "workspace.read"
        tool_attempts.append(name)
        raise AgentRuntimeError(f"workspace failed token={leaked_secret}")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    monkeypatch.setattr(ToolBroker, "call", failing_tool_call)
    try:
        run = service.start_main_chat_run(
            task_id="task-tool-exception-leak",
            session_id="session-tool-exception-leak",
            user_goal="Reply with exactly 'Recovered from redacted tool failure'",
        )
        result = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": run["user_goal"]}],
            tool_policy={"allowed_tools": ["workspace.read"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )

        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        persisted_projection = json.dumps({"run": result, "events": events}, ensure_ascii=False)
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call"
                          and event["payload"]["result"].get("ok") is False)

        assert result["status"] == "running"
        assert result["result"] == "Recovered from redacted tool failure"
        assert tool_event["payload"]["result"]["ok"] is False
        assert len(tool_attempts) == 1
        assert leaked_secret not in persisted_projection
        assert "[redacted]" in persisted_projection
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_main_chat_terminal_secret_payload_is_rejected_before_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-terminal-command-secret123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal_secret",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps(
                            {
                                "command": f"OPENAI_API_KEY={leaked_secret} python3 scripts/deploy.py",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-terminal-secret-command",
            session_id="session-terminal-secret-command",
            user_goal="Run terminal command",
        )

        with pytest.raises(AgentRuntimeError, match="参数包含敏感凭据"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Run terminal command"}],
                tool_policy={"allowed_tools": ["terminal.run"]},
                workspace_policy={"default_workdir": str(tmp_path), "readable_scopes": ["."]},
            )

        failed = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)

        assert failed["status"] == "failed"
        assert failed["pending_approval"] == {}
        assert not service._conn.execute(
            "SELECT 1 FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert "agent.tool.approval_required" not in [event["event_type"] for event in events]
        assert leaked_secret not in persisted_projection
        assert "OPENAI_API_KEY" not in persisted_projection
        assert "敏感凭据" in persisted_projection
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_main_chat_workspace_patch_secret_payload_is_rejected_before_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "config.env").write_text("OPENAI_API_KEY=\n", encoding="utf-8")
    leaked_secret = "sk-workspace-patch-secret123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "workspace_write_patch" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_workspace_patch_secret",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_patch",
                        "arguments": json.dumps(
                            {
                                "path": "config.env",
                                "patch": (
                                    "--- config.env\n"
                                    "+++ config.env\n"
                                    "@@ -1 +1 @@\n"
                                    "-OPENAI_API_KEY=\n"
                                    f"+OPENAI_API_KEY={leaked_secret}\n"
                                ),
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-workspace-patch-secret",
            session_id="session-workspace-patch-secret",
            user_goal="Patch workspace file",
        )

        with pytest.raises(AgentRuntimeError, match="参数包含敏感凭据"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Patch config.env"}],
                tool_policy={"allowed_tools": ["workspace.write_patch"]},
                workspace_policy={
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                },
            )

        failed = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)

        assert failed["status"] == "failed"
        assert failed["pending_approval"] == {}
        assert (workdir / "config.env").read_text(encoding="utf-8") == "OPENAI_API_KEY=\n"
        assert not service._conn.execute(
            "SELECT 1 FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert "agent.tool.approval_required" not in [event["event_type"] for event in events]
        assert leaked_secret not in persisted_projection
        assert "OPENAI_API_KEY=" not in persisted_projection
        assert "敏感凭据" in persisted_projection
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_main_chat_artifact_secret_payload_is_rejected_before_write(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-artifact-tool-secret123456"
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "artifact_write" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_artifact_secret",
                    "type": "function",
                    "function": {
                        "name": "artifact_write",
                        "arguments": json.dumps(
                            {
                                "path": "reports/secret.md",
                                "content": f"api_key={leaked_secret}\nwrite a report",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-artifact-secret",
            session_id="session-artifact-secret",
            user_goal="Credential guard probe",
        )

        with pytest.raises(AgentRuntimeError, match="参数包含敏感凭据"):
            service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Credential guard probe"}],
                tool_policy={"allowed_tools": ["artifact.write"]},
            )

        failed = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        persisted_projection = json.dumps({"run": failed, "events": events}, ensure_ascii=False)
        artifact_path = service.agent_artifacts_dir / run["run_id"] / "reports" / "secret.md"

        assert failed["status"] == "failed"
        assert failed["artifacts"] == []
        assert not artifact_path.exists()
        assert "agent.tool.call" not in [event["event_type"] for event in events]
        assert leaked_secret not in persisted_projection
        assert "api_key=" not in persisted_projection
        assert "敏感凭据" in persisted_projection
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_main_chat_default_tools_use_trusted_product_workspace(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    product_workspace = tmp_path / "oha-workspace"
    projects = product_workspace / "projects"
    projects.mkdir(parents=True)
    (projects / "README.md").write_text("trusted product workspace", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_workspace_status",
        lambda: {
            "initialized": True,
            "workspace_path": str(product_workspace),
            "dirs": {"projects": str(projects)},
        },
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools or []})
        if len(calls) == 1:
            tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}
            assert {"workspace_list", "workspace_read", "artifact_write"} <= tool_names
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "README.md"})},
                    }
                ],
            }
        assert "trusted product workspace" in messages[-1]["content"]
        return {"content": "Default workspace read complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(task_id="task-default-workspace", session_id="session-default-workspace", user_goal="Read")
        result = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Read README"}],
        )
        trusted = service.list_trusted_workspaces()["workspaces"]

        assert result["status"] == "running"
        assert result["result"] == "Default workspace read complete"
        assert any(item["path"] == str(projects.resolve()) and item["source"] == "main_chat" for item in trusted)
    finally:
        service.close()


def test_main_chat_model_loop_pauses_and_resumes_approved_tool(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    target = workdir / "out.txt"
    target.write_text("before\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "out.txt",
                                    "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+approved\n",
                                }
                            ),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "out.txt" in messages[-1]["content"]
        return {"content": "Main chat write complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        resume_contexts: list[ToolApprovalResumeContext] = []
        original_resume = service.approval_resume.execute_approved_tool

        def spy_resume(context: ToolApprovalResumeContext) -> None:
            resume_contexts.append(context)
            original_resume(context)

        monkeypatch.setattr(service.approval_resume, "execute_approved_tool", spy_resume)
        run = service.start_main_chat_run(task_id="task-main-approval", session_id="session-main-approval", user_goal="Write")
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Write out.txt"}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]},
        )

        assert waiting["status"] == "approval_required"
        assert waiting["pending_approval"]["tool"] == "workspace.write_patch"
        assert target.read_text(encoding="utf-8") == "before\n"

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "running"
        assert resumed["pending_approval"] == {}
        assert resumed["result"] == "Main chat write complete"
        assert target.read_text(encoding="utf-8") == "approved\n"
        assert len(resume_contexts) == 1
        assert resume_contexts[0].run_id == run["run_id"]
        assert resume_contexts[0].tool_name == "workspace.write_patch"
        assert resume_contexts[0].input_preview["path"] == "out.txt"
        approval_row = service._conn.execute(
            "SELECT status FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert approval_row["status"] == "approved"
    finally:
        service.close()


def test_main_chat_consecutive_tool_approvals_use_resume_required_projection(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    fixture_actions = [('terminal_run', {'command': 'printf main-first-approved'}), ('terminal_run', {'command': 'printf main-second-approved'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_first_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf main-first-approved"}),
                        },
                    },
                    {
                        "id": "call_second_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf main-second-approved"}),
                        },
                    },
                ],
            }
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert any("main-first-approved" in message.get("content", "") for message in tool_messages)
        assert any("main-second-approved" in message.get("content", "") for message in tool_messages)
        return {"content": "Main chat terminal approvals completed"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        required_projection_calls: list[dict[str, object]] = []
        original_required_projection = service._project_approval_resume_required

        def spy_project_approval_resume_required(context, pending_approval):
            required_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": pending_approval.get("tool"),
                    "command": (pending_approval.get("input_preview") or {}).get("command"),
                    "model_profile_id": pending_approval.get("model_profile_id"),
                }
            )
            return original_required_projection(context, pending_approval)

        monkeypatch.setattr(
            service,
            "_project_approval_resume_required",
            spy_project_approval_resume_required,
        )
        run = service.start_main_chat_run(
            task_id="task-main-consecutive-approval",
            session_id="session-main-consecutive-approval",
            user_goal=fixture_goal,
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": fixture_goal}],
            tool_policy={"allowed_tools": ["terminal.run"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )

        assert waiting["status"] == "approval_required"
        assert waiting["pending_approval"]["input_preview"]["command"] == "printf main-first-approved"

        after_first = service.approve_run_approval(run["run_id"])
        assert after_first["status"] == "approval_required"
        assert after_first["pending_approval"]["input_preview"]["command"] == "printf main-second-approved"
        assert required_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "terminal.run",
                "command": "printf main-second-approved",
                "model_profile_id": "profile_default",
            }
        ]

        after_second = service.approve_run_approval(run["run_id"])
        assert after_second["status"] == "completed"
        assert after_second["pending_approval"] == {}
        assert "first-approved" in after_second["result"]
        assert "second-approved" in after_second["result"]
        assert len(calls) == 1
    finally:
        service.close()



def test_main_chat_records_failed_run_event_when_approved_tool_fails(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.custom_api_agent_loop._max_tool_iterations = 3
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    fixture_actions = [('terminal_run', {'command': 'printf main-chat-terminal-failure; exit 7', 'shell': True})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {"content": "The exact requested process failed; no repair is authorized."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        failed_projection_calls: list[dict[str, object]] = []
        original_failed_projection = service._project_main_chat_approval_resume_failed

        def spy_project_approval_resume_failed(context, safe_error):
            failed_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": context.tool_name,
                    "safe_error": safe_error,
                }
            )
            return original_failed_projection(context, safe_error)

        monkeypatch.setattr(
            service,
            "_project_main_chat_approval_resume_failed",
            spy_project_approval_resume_failed,
        )
        run = service.start_main_chat_run(
            task_id="task-main-chat-terminal-failure",
            session_id="session-main-chat-terminal-failure",
            user_goal=fixture_goal,
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": fixture_goal}],
            runtime_execution_envelope=envelope,
            tool_policy={"allowed_tools": ["terminal.run"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."]},
        )

        assert waiting["status"] == "approval_required"
        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "failed"
        assert "最后一次工具调用：terminal.run" in resumed["result"]
        assert "退出码：7" in resumed["result"]
        assert failed_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "terminal.run",
                "safe_error": resumed["result"],
            }
        ]
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
        assert "agent.run.failed" not in event_types
        assert event_types.count("task.failed") == 1
        assert event_types.count("run.failed") == 1
        failed_event = next(event for event in events if event["event_type"] == "run.failed")
        assert "最后一次工具调用：terminal.run" in failed_event["payload"]["error"]
        assert any(
            event["event_type"] == "agent.tool.call"
            and event["payload"]["tool"] == "terminal.run"
            and event["payload"]["result"]["ok"] is False
            and event["payload"]["result"]["stdout"] == "main-chat-terminal-failure"
            and event["payload"]["result"]["returncode"] == 7
            and event["payload"]["approved"] is True
            for event in events
        )
        assert len(calls) == 3
    finally:
        service.close()



def test_main_chat_approval_timeout_records_replayable_fact_and_is_idempotent(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    fixture_actions = [('workspace_write_patch', {'path': 'out.txt', 'patch': '--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+timed out\n'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        planning_response = semantic_fixture_model_plan(_messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_patch",
                        "arguments": json.dumps(
                            {
                                "path": "out.txt",
                                "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+timed out\n",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-approval-timeout",
            session_id="session-main-approval-timeout",
            user_goal=fixture_goal,
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Write out.txt"}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={
                "default_workdir": str(workdir),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            },
        )

        assert waiting["status"] == "approval_required"

        timed_out = service.timeout_run_approval(run["run_id"], reason="approval_wait_timeout")
        events_after_timeout = service.list_run_events(run["run_id"], include_internal=True)["events"]
        timeout_events = [event for event in events_after_timeout if event["event_type"] == "approval.timeout"]

        assert timed_out["status"] == "cancelled"
        assert timed_out["pending_approval"] == {}
        assert "审批已超时" in timed_out["result"]
        assert any(event["event"] == "agent.tool.approval_timeout" for event in timed_out["timeline"])
        assert len(timeout_events) == 1
        assert timeout_events[0]["payload"]["tool"] == "workspace.write_patch"
        assert timeout_events[0]["payload"]["reason"] == "approval_wait_timeout"
        assert timeout_events[0]["payload"]["status"] == "cancelled"
        timeout_event_types = [event["event_type"] for event in events_after_timeout]
        assert timeout_event_types.count("agent.run.cancelled") == 1
        assert timeout_event_types.count("run.cancelled") == 1

        approval_row = service._conn.execute(
            "SELECT status FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert approval_row["status"] == "cancelled"

        repeated = service.timeout_run_approval(run["run_id"], reason="approval_wait_timeout")
        events_after_repeat = service.list_run_events(run["run_id"], include_internal=True)["events"]

        assert repeated["status"] == "cancelled"
        assert len([event for event in events_after_repeat if event["event_type"] == "approval.timeout"]) == 1
    finally:
        service.close()



def test_main_chat_reject_and_timeout_use_approval_coordinator_boundaries(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_patch",
                        "arguments": json.dumps(
                            {
                                "path": "out.txt",
                                "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+after\n",
                            }
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    reject_calls: list[dict[str, object]] = []
    timeout_calls: list[dict[str, object]] = []
    original_reject = service.approvals.reject_tool_run
    original_timeout = service.approvals.timeout_tool_run

    def spy_reject_tool_run(
        run_id,
        *,
        timeline,
        reason,
        tool_name,
        input_preview,
        expected_approval_id,
    ):
        reject_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
        )
        return original_reject(
            run_id,
            timeline=timeline,
            reason=reason,
            tool_name=tool_name,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    def spy_timeout_tool_run(
        run_id,
        *,
        timeline,
        reason,
        tool_name,
        input_preview,
        expected_approval_id,
    ):
        timeout_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
        )
        return original_timeout(
            run_id,
            timeline=timeline,
            reason=reason,
            tool_name=tool_name,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    monkeypatch.setattr(service.approvals, "reject_tool_run", spy_reject_tool_run)
    monkeypatch.setattr(service.approvals, "timeout_tool_run", spy_timeout_tool_run)

    def start_waiting_run(suffix: str) -> dict[str, object]:
        run = service.start_main_chat_run(
            task_id=f"task-main-approval-{suffix}",
            session_id=f"session-main-approval-{suffix}",
            user_goal="Write",
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Write out.txt"}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={
                "default_workdir": str(workdir),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            },
        )
        assert waiting["status"] == "approval_required"
        assert waiting["pending_approval"]["tool"] == "workspace.write_patch"
        return run

    try:
        rejected_run = start_waiting_run("reject-boundary")
        timed_out_run = start_waiting_run("timeout-boundary")

        rejected = service.reject_run_approval(rejected_run["run_id"], "not now")
        timed_out = service.timeout_run_approval(timed_out_run["run_id"], reason="approval_wait_timeout")

        assert rejected["status"] == "cancelled"
        assert timed_out["status"] == "cancelled"
        assert len(reject_calls) == 1
        assert len(timeout_calls) == 1
        assert reject_calls[0]["run_id"] == rejected_run["run_id"]
        assert reject_calls[0]["reason"] == "not now"
        assert reject_calls[0]["tool_name"] == "workspace.write_patch"
        assert "agent.tool.approval_required" in reject_calls[0]["timeline_events"]
        assert reject_calls[0]["input_preview"]["path"] == "out.txt"
        assert timeout_calls[0]["run_id"] == timed_out_run["run_id"]
        assert timeout_calls[0]["reason"] == "approval_wait_timeout"
        assert timeout_calls[0]["tool_name"] == "workspace.write_patch"
        assert "agent.tool.approval_required" in timeout_calls[0]["timeline_events"]
        assert timeout_calls[0]["input_preview"]["path"] == "out.txt"
    finally:
        service.close()


def test_tool_approval_transitions_use_shared_context_boundary(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "out.txt").write_text("before\n", encoding="utf-8")
    context_calls: list[dict[str, object]] = []
    projection_calls: list[dict[str, object]] = []
    original_context = ToolApprovalTransitionContext.from_pending
    original_projection = service._project_child_run_transition

    def spy_context(pending):
        context = original_context(pending)
        context_calls.append(
            {
                "tool_name": context.tool_name,
                "path": context.input_preview.get("path") if isinstance(context.input_preview, dict) else "",
                "command": context.input_preview.get("command") if isinstance(context.input_preview, dict) else "",
            }
        )
        return context

    def spy_project_child_run_transition(result):
        projection_calls.append(
            {
                "run_id": result.get("run_id"),
                "kind": result.get("kind"),
                "status": result.get("status"),
            }
        )
        return original_projection(result)

    monkeypatch.setattr(
        ToolApprovalTransitionContext,
        "from_pending",
        staticmethod(spy_context),
    )
    monkeypatch.setattr(
        service,
        "_project_child_run_transition",
        spy_project_child_run_transition,
    )

    patch_actions = [("workspace_write_patch", {"path": "out.txt", "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+after\n"})]
    terminal_actions = [("terminal_run", {"command": "printf boundary"})]
    patch_goal = semantic_fixture_goal(patch_actions)
    terminal_goal = semantic_fixture_goal(terminal_actions)

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        if any((tool.get("function") or {}).get("name") == "runtime_propose_task_intent" for tool in tools or []):
            original_goal = next(json.loads(message["content"])["original_goal"]
                                 for message in _messages if message.get("role") == "user"
                                 and str(message.get("content", "")).lstrip().startswith("{"))
            actions = patch_actions if original_goal == patch_goal else terminal_actions
            return semantic_fixture_model_plan(_messages, tools, actions, original_goal)
        tool_names = {(tool.get("function") or {}).get("name") for tool in tools or []}
        if "workspace_write_patch" in tool_names:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "out.txt",
                                    "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+after\n",
                                }
                            ),
                        },
                    }
                ],
            }
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf boundary"}),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        main_reject_run = service.start_main_chat_run(
            task_id="task-main-reject-context",
            session_id="session-main-reject-context",
            user_goal=patch_goal,
        )
        main_timeout_run = service.start_main_chat_run(
            task_id="task-main-timeout-context",
            session_id="session-main-timeout-context",
            user_goal=patch_goal,
        )
        for run in (main_reject_run, main_timeout_run):
            waiting = service.execute_main_chat_model_loop(
                run["run_id"],
                [{"role": "user", "content": "Patch"}],
                tool_policy={"allowed_tools": ["workspace.write_patch"]},
                workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]},
            )
            assert waiting["status"] == "approval_required"

        agent = service.create_agent(
            {
                "name": "Tool Context Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, terminal_actions, terminal_goal)
        agent_reject_run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": terminal_goal, "runtime_execution_envelope": envelope})
        agent_timeout_run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": terminal_goal, "runtime_execution_envelope": envelope})

        assert agent_reject_run["status"] == "approval_required"
        assert agent_timeout_run["status"] == "approval_required"

        main_rejected = service.reject_run_approval(main_reject_run["run_id"], "not now")
        main_timed_out = service.timeout_run_approval(main_timeout_run["run_id"])
        agent_rejected = service.reject_run_approval(agent_reject_run["run_id"], "not now")
        agent_timed_out = service.timeout_run_approval(agent_timeout_run["run_id"])

        assert main_rejected["status"] == "cancelled"
        assert main_timed_out["status"] == "cancelled"
        assert agent_rejected["status"] == "cancelled"
        assert agent_timed_out["status"] == "cancelled"
        assert context_calls == [
            {"tool_name": "workspace.write_patch", "path": "out.txt", "command": None},
            {"tool_name": "workspace.write_patch", "path": "out.txt", "command": None},
            {"tool_name": "terminal.run", "path": None, "command": "printf boundary"},
            {"tool_name": "terminal.run", "path": None, "command": "printf boundary"},
        ]
        assert projection_calls == [
            {"run_id": main_reject_run["run_id"], "kind": "main_chat_run", "status": "cancelled"},
            {"run_id": main_timeout_run["run_id"], "kind": "main_chat_run", "status": "cancelled"},
            {"run_id": agent_reject_run["run_id"], "kind": "agent_run", "status": "cancelled"},
            {"run_id": agent_timeout_run["run_id"], "kind": "agent_run", "status": "cancelled"},
        ]
    finally:
        service.close()


def test_agent_run_reject_and_timeout_use_approval_coordinator_boundaries(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf boundary-check"}),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    reject_calls: list[dict[str, object]] = []
    timeout_calls: list[dict[str, object]] = []
    original_reject = service.approvals.reject_tool_run
    original_timeout = service.approvals.timeout_tool_run

    def spy_reject_tool_run(
        run_id,
        *,
        timeline,
        reason,
        tool_name,
        input_preview,
        expected_approval_id,
    ):
        reject_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
        )
        return original_reject(
            run_id,
            timeline=timeline,
            reason=reason,
            tool_name=tool_name,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    def spy_timeout_tool_run(
        run_id,
        *,
        timeline,
        reason,
        tool_name,
        input_preview,
        expected_approval_id,
    ):
        timeout_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "tool_name": tool_name,
                "input_preview": input_preview,
            }
        )
        return original_timeout(
            run_id,
            timeline=timeline,
            reason=reason,
            tool_name=tool_name,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    monkeypatch.setattr(service.approvals, "reject_tool_run", spy_reject_tool_run)
    monkeypatch.setattr(service.approvals, "timeout_tool_run", spy_timeout_tool_run)

    try:
        agent = service.create_agent(
            {
                "name": "Boundary Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )

        rejected_run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Reject it"})
        timed_out_run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Timeout it"})

        assert rejected_run["status"] == "approval_required"
        assert timed_out_run["status"] == "approval_required"

        rejected = service.reject_run_approval(rejected_run["run_id"], "not now")
        timed_out = service.timeout_run_approval(timed_out_run["run_id"], reason="approval_wait_timeout")

        assert rejected["status"] == "cancelled"
        assert timed_out["status"] == "cancelled"
        assert len(reject_calls) == 1
        assert len(timeout_calls) == 1
        assert reject_calls[0]["run_id"] == rejected_run["run_id"]
        assert reject_calls[0]["reason"] == "not now"
        assert reject_calls[0]["tool_name"] == "terminal.run"
        assert "agent.tool.approval_required" in reject_calls[0]["timeline_events"]
        assert reject_calls[0]["input_preview"]["command"] == "printf boundary-check"
        assert timeout_calls[0]["run_id"] == timed_out_run["run_id"]
        assert timeout_calls[0]["reason"] == "approval_wait_timeout"
        assert timeout_calls[0]["tool_name"] == "terminal.run"
        assert "agent.tool.approval_required" in timeout_calls[0]["timeline_events"]
        assert timeout_calls[0]["input_preview"]["command"] == "printf boundary-check"
    finally:
        service.close()


def test_workflow_approval_reject_and_timeout_use_approval_coordinator_boundaries(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        return {"content": "Before gate complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    reject_calls: list[dict[str, object]] = []
    timeout_calls: list[dict[str, object]] = []
    original_reject = service.approvals.reject_workflow_node
    original_timeout = service.approvals.timeout_workflow_node

    def spy_reject_workflow_node(
        run_id,
        *,
        timeline,
        reason,
        workflow_node_id,
        label,
        criteria,
        input_preview,
        expected_approval_id,
    ):
        reject_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "workflow_node_id": workflow_node_id,
                "label": label,
                "criteria": criteria,
                "input_preview": input_preview,
            }
        )
        return original_reject(
            run_id,
            timeline=timeline,
            reason=reason,
            workflow_node_id=workflow_node_id,
            label=label,
            criteria=criteria,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    def spy_timeout_workflow_node(
        run_id,
        *,
        timeline,
        reason,
        workflow_node_id,
        label,
        criteria,
        input_preview,
        expected_approval_id,
    ):
        timeout_calls.append(
            {
                "run_id": run_id,
                "timeline_events": [event.get("event") for event in timeline if isinstance(event, dict)],
                "reason": reason,
                "workflow_node_id": workflow_node_id,
                "label": label,
                "criteria": criteria,
                "input_preview": input_preview,
            }
        )
        return original_timeout(
            run_id,
            timeline=timeline,
            reason=reason,
            workflow_node_id=workflow_node_id,
            label=label,
            criteria=criteria,
            input_preview=input_preview,
            expected_approval_id=expected_approval_id,
        )

    monkeypatch.setattr(service.approvals, "reject_workflow_node", spy_reject_workflow_node)
    monkeypatch.setattr(service.approvals, "timeout_workflow_node", spy_timeout_workflow_node)

    try:
        agent = service.create_agent(
            {
                "name": "Workflow Boundary Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Workflow Boundary",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Before Gate", "agent_id": agent["agent_id"]}},
                    {
                        "id": "gate",
                        "type": "approval",
                        "data": {
                            "label": "Human Gate",
                            "criteria": "Review before continuing.",
                        },
                    },
                ],
                "edges": [
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "gate"},
                ],
            }
        )

        rejected_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Reject it"})
        timed_out_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Timeout it"})

        assert rejected_run["status"] == "approval_required"
        assert timed_out_run["status"] == "approval_required"

        rejected = service.reject_run_approval(rejected_run["run_id"], "not now")
        timed_out = service.timeout_run_approval(timed_out_run["run_id"], reason="approval_wait_timeout")

        assert rejected["status"] == "cancelled"
        assert timed_out["status"] == "cancelled"
        assert len(reject_calls) == 1
        assert len(timeout_calls) == 1
        assert reject_calls[0]["run_id"] == rejected_run["run_id"]
        assert reject_calls[0]["reason"] == "not now"
        assert reject_calls[0]["workflow_node_id"] == "gate"
        assert reject_calls[0]["label"] == "Human Gate"
        assert reject_calls[0]["criteria"] == "Review before continuing."
        assert reject_calls[0]["input_preview"]["checkpoint"] == "Human Gate"
        assert "workflow.node.approval_required" in reject_calls[0]["timeline_events"]
        assert timeout_calls[0]["run_id"] == timed_out_run["run_id"]
        assert timeout_calls[0]["reason"] == "approval_wait_timeout"
        assert timeout_calls[0]["workflow_node_id"] == "gate"
        assert timeout_calls[0]["label"] == "Human Gate"
        assert timeout_calls[0]["criteria"] == "Review before continuing."
        assert timeout_calls[0]["input_preview"]["checkpoint"] == "Human Gate"
        assert "workflow.node.approval_required" in timeout_calls[0]["timeline_events"]
    finally:
        service.close()


def test_main_chat_repeated_approval_does_not_execute_tool_twice(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    target = workdir / "src" / "app.txt"
    target.write_text("before\n", encoding="utf-8")
    execution_calls = []
    original_tool_call = ToolBroker.call

    def count_patch_execution(self, name, payload, *, approved=False):
        if name == "workspace.write_patch" and approved:
            execution_calls.append(dict(payload))
        return original_tool_call(self, name, payload, approved=approved)

    monkeypatch.setattr(ToolBroker, "call", count_patch_execution)
    model_calls = 0
    resume_model_started = threading.Event()
    release_resume_model = threading.Event()

    fixture_actions = [('workspace_write_patch', {'path': 'src/app.txt', 'patch': '--- src/app.txt\n+++ src/app.txt\n@@ -1 +1 @@\n-before\n+after\n'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        nonlocal model_calls
        model_calls += 1
        planning_response = semantic_fixture_model_plan(_messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if model_calls == 1:
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
        resume_model_started.set()
        assert release_resume_model.wait(timeout=3)
        return {"role": "assistant", "content": "Patched once"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-approve-idempotent",
            session_id="session-main-approve-idempotent",
            user_goal=fixture_goal,
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": fixture_goal}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={
                "default_workdir": str(workdir),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            },
        )

        assert waiting["status"] == "approval_required"

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.approve_run_approval, run["run_id"])
            assert resume_model_started.wait(timeout=3)
            second = pool.submit(service.approve_run_approval, run["run_id"]).result(timeout=3)
            assert second["run_id"] == run["run_id"]
            release_resume_model.set()
            first_result = first.result(timeout=3)

        repeated_after = service.approve_run_approval(run["run_id"])

        assert first_result["status"] == "running"
        assert repeated_after["run_id"] == run["run_id"]
        assert model_calls == 2
        assert target.read_text(encoding="utf-8") == "after\n"
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        event_types = [event["event_type"] for event in events]
        assert event_types.count("agent.tool.approval_approved") == 1
        tool_calls = [
            event for event in events
            if event["event_type"] == "agent.tool.call"
            and event["payload"].get("tool") == "workspace.write_patch"
            and event["payload"].get("approved") is True
        ]
        assert tool_calls
        assert len(execution_calls) == 1
        assert execution_calls[0]["patch"] == fixture_actions[0][1]["patch"]
    finally:
        release_resume_model.set()
        service.close()



def test_main_chat_approval_uses_resume_coordinator_claim_boundary(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    target = workdir / "out.txt"
    target.write_text("before\n", encoding="utf-8")
    calls = []

    fixture_actions = [('workspace_write_patch', {'path': 'out.txt', 'patch': '--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+after\n'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "out.txt",
                                    "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+after\n",
                                }
                            ),
                        },
                    }
                ],
            }
        assert "sha256_after" in json.dumps(messages)
        return {"content": "Main chat claim boundary complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        claim_calls: list[dict[str, object]] = []
        resume_step_calls: list[dict[str, object]] = []
        completed_projection_calls: list[dict[str, object]] = []
        original_resume_approved_tool_run = service._resume_approved_tool_run
        original_claim = service.approval_resume.claim_and_project_approved_tool
        original_completed_projection = service._project_main_chat_approval_resume_completed

        def spy_resume_approved_tool_run(**kwargs):
            resume_step_calls.append(
                {
                    "run_id": kwargs.get("run_id"),
                    "tool": kwargs.get("pending", {}).get("tool"),
                    "context_run_id": kwargs.get("resume_context").run_id,
                    "context_tool_name": kwargs.get("resume_context").tool_name,
                    "resumed_detail": kwargs.get("resumed_detail"),
                    "running_result": kwargs.get("running_result"),
                    "has_running_projection": kwargs.get("project_running") is not None,
                    "has_required_projection": kwargs.get("project_required") is not None,
                    "has_result_projection": kwargs.get("project_result") is not None,
                }
            )
            return original_resume_approved_tool_run(**kwargs)

        def spy_claim(run_id, pending, context, **kwargs):
            claim_calls.append(
                {
                    "run_id": run_id,
                    "tool": pending.get("tool"),
                    "context_run_id": context.run_id,
                    "context_tool_name": context.tool_name,
                    "resumed_detail": kwargs.get("resumed_detail"),
                    "running_result": kwargs.get("running_result"),
                }
            )
            return original_claim(run_id, pending, context, **kwargs)

        def spy_project_main_chat_approval_resume_completed(context, result_text):
            completed_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": context.tool_name,
                    "result_text": result_text,
                }
            )
            return original_completed_projection(context, result_text)

        monkeypatch.setattr(service, "_resume_approved_tool_run", spy_resume_approved_tool_run)
        monkeypatch.setattr(service.approval_resume, "claim_and_project_approved_tool", spy_claim)
        monkeypatch.setattr(
            service,
            "_project_main_chat_approval_resume_completed",
            spy_project_main_chat_approval_resume_completed,
        )
        run = service.start_main_chat_run(
            task_id="task-main-claim-boundary",
            session_id="session-main-claim-boundary",
            user_goal=fixture_goal,
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": "Patch out.txt"}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]},
        )

        assert waiting["status"] == "approval_required"

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "running"
        assert resumed["result"] == "Main chat claim boundary complete"
        assert target.read_text(encoding="utf-8") == "after\n"
        assert resume_step_calls == [
            {
                "run_id": run["run_id"],
                "tool": "workspace.write_patch",
                "context_run_id": run["run_id"],
                "context_tool_name": "workspace.write_patch",
                "resumed_detail": "Main chat resumed after approval",
                "running_result": "已批准，Yachiyo 正在继续执行",
                "has_running_projection": False,
                "has_required_projection": True,
                "has_result_projection": False,
            }
        ]
        assert claim_calls == [
            {
                "run_id": run["run_id"],
                "tool": "workspace.write_patch",
                "context_run_id": run["run_id"],
                "context_tool_name": "workspace.write_patch",
                "resumed_detail": "Main chat resumed after approval",
                "running_result": "已批准，Yachiyo 正在继续执行",
            }
        ]
        assert completed_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "workspace.write_patch",
                "result_text": "Main chat claim boundary complete",
            }
        ]
    finally:
        service.close()



def test_main_chat_durable_approval_claim_blocks_duplicate_execution(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    claiming_service = None
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: FakeDefaultProfileService(),
    )
    workdir = tmp_path / "repo"
    workdir.mkdir()
    target = workdir / "out.txt"
    target.write_text("before\n", encoding="utf-8")
    model_calls = 0

    fixture_actions = [('workspace_write_patch', {'path': 'out.txt', 'patch': '--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+approved once\n'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        nonlocal model_calls
        model_calls += 1
        planning_response = semantic_fixture_model_plan(_messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if model_calls == 1:
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
                                    "path": "out.txt",
                                    "patch": "--- out.txt\n+++ out.txt\n@@ -1 +1 @@\n-before\n+approved once\n",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        raise AssertionError("durably claimed approval must not resume model again")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.start_main_chat_run(
            task_id="task-main-approve-durable-claim",
            session_id="session-main-approve-durable-claim",
            user_goal=fixture_goal,
        )
        waiting = service.execute_main_chat_model_loop(
            run["run_id"],
            [{"role": "user", "content": fixture_goal}],
            tool_policy={"allowed_tools": ["workspace.write_patch"]},
            workspace_policy={
                "default_workdir": str(workdir),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            },
        )

        assert waiting["status"] == "approval_required"
        pending = service.runs.pending_approval_private(run["run_id"])
        assert pending["tool"] == "workspace.write_patch"
        claiming_service = make_service(tmp_path)
        approval_id = str(pending["approval_id"])
        assert claiming_service.run_approvals.claim_pending_approval(
            run["run_id"],
            pending,
            expected_approval_id=approval_id,
        ) is True
        assert claiming_service.run_approvals.claim_pending_approval(
            run["run_id"],
            pending,
            expected_approval_id=approval_id,
        ) is False

        duplicate = service.approve_run_approval(run["run_id"])

        assert duplicate["status"] == "approval_required"
        assert model_calls == 1
        assert target.read_text(encoding="utf-8") == "before\n"
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert "agent.tool.approval_approved" not in [event["event_type"] for event in events]
        approved_tool_calls = [
            event for event in events
            if event["event_type"] == "agent.tool.call"
            and event["payload"].get("tool") == "workspace.write_patch"
            and event["payload"].get("approved") is True
        ]
        assert approved_tool_calls == []
        approval_row = service._conn.execute(
            "SELECT status, resolved_at FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert approval_row["status"] == "approved"
        assert approval_row["resolved_at"]
    finally:
        if claiming_service is not None:
            claiming_service.close()
        service.close()



def test_agent_explicit_workspace_is_recorded_as_trusted(tmp_path):
    service = make_service(tmp_path)
    workdir = tmp_path / "external-workspace"
    workdir.mkdir()
    try:
        agent = service.create_agent(
            {
                "name": "Trusted Writer",
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": [],
                },
            }
        )
        trusted = service.list_trusted_workspaces()["workspaces"]

        assert agent["workspace_policy"]["default_workdir"] == str(workdir)
        assert any(item["path"] == str(workdir.resolve()) and item["source"] == f"agent:{agent['agent_id']}" for item in trusted)
    finally:
        service.close()


def test_run_events_hide_internal_and_secret_by_default(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="test")
        service.append_run_event(run["run_id"], "user.visible", {"value": "ok"})
        service.append_run_event(run["run_id"], "user.token", {"token": "plain-token-value", "safe": "ok"})
        service.append_run_event(run["run_id"], "internal.fact", {"value": "hidden"}, visibility="internal")
        service.append_run_event(run["run_id"], "secret.fact", {"value": "sk-secret-value"}, sensitivity="secret")

        public = service.list_run_events(run["run_id"])["events"]
        debug = service.list_run_events(run["run_id"], include_internal=True)["events"]

        assert [event["event_type"] for event in public] == ["user.visible", "user.token"]
        assert [event["event_type"] for event in debug] == ["user.visible", "user.token", "internal.fact", "secret.fact"]
        assert public[1]["payload"]["token"] == "[redacted]"
        assert public[1]["payload"]["safe"] == "ok"
        assert debug[-1]["payload"]["value"] == "[redacted]"
    finally:
        service.close()


def test_run_event_repository_allocates_sequences_under_concurrent_writers(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="test")

        def append_event(index: int):
            return service.append_run_event(run["run_id"], "concurrent.fact", {"index": index})

        with ThreadPoolExecutor(max_workers=8) as pool:
            written = list(pool.map(append_event, range(40)))

        events = service.list_run_events(run["run_id"], limit=100)["events"]

        assert len(written) == 40
        assert [event["sequence"] for event in events] == list(range(1, 41))
        assert sorted(event["payload"]["index"] for event in events) == list(range(40))
    finally:
        service.close()


def test_run_event_repository_snapshots_payload_before_persistence(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="test")
        payload = {
            "input_preview": {"command": "printf ok"},
            "items": [{"path": "report.md"}],
        }

        written = service.append_run_event(run["run_id"], "snapshot.fact", payload)
        payload["input_preview"]["command"] = "changed"
        payload["items"][0]["path"] = "changed.md"

        replayed = service.list_run_events(run["run_id"])["events"]

        assert written["payload"] == {
            "input_preview": {"command": "printf ok"},
            "items": [{"path": "report.md"}],
        }
        assert written["payload"] is not payload
        assert written["payload"]["input_preview"] is not payload["input_preview"]
        assert replayed[0]["payload"] == written["payload"]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_run_events_route_paginates_user_visible_events(tmp_path, monkeypatch):
    from apps.bridge.routes import runs as run_routes

    service = make_service(tmp_path)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="test")
        service.append_run_event(run["run_id"], "first", {"value": 1})
        service.append_run_event(run["run_id"], "internal", {"value": 2}, visibility="internal")
        service.append_run_event(run["run_id"], "secret", {"value": "sk-secret-value"}, sensitivity="secret")
        service.append_run_event(run["run_id"], "third", {"value": 3})

        clamped = await run_routes.list_run_events(run["run_id"], after_sequence=-10, limit=5000)
        response = await run_routes.list_run_events(run["run_id"], after_sequence=1, limit=1)

        assert clamped["after_sequence"] == 0
        assert clamped["limit"] == 1000
        assert [event["event_type"] for event in clamped["events"]] == ["first", "third"]
        assert "sk-secret-value" not in json.dumps(clamped, ensure_ascii=False)
        assert response["limit"] == 1
        assert [event["event_type"] for event in response["events"]] == ["third"]
        assert response["events"][0]["sequence"] == 4
    finally:
        service.close()


@pytest.mark.asyncio
async def test_run_events_route_returns_404_for_missing_run(tmp_path, monkeypatch):
    from apps.bridge.routes import runs as run_routes

    service = make_service(tmp_path)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    try:
        with pytest.raises(run_routes.HTTPException) as exc_info:
            await run_routes.list_run_events("missing-run")

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail == "Run 不存在"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_post_runs_route_maps_idempotency_key_to_runnable_run(monkeypatch):
    from apps.bridge.routes import runs as run_routes

    recorded: dict[str, str] = {}

    class FakeRunEngine:
        def create_run_for_runnable(self, **kwargs):
            recorded.update({key: str(value) for key, value in kwargs.items()})
            return {
                "ok": True,
                "run_id": "run_post_runs",
                "client_request_id": kwargs.get("client_run_id") or kwargs.get("client_request_id") or "",
            }

    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: FakeRunEngine())

    response = await run_routes.create_run(
        run_routes.RunCreateRequest(runnable_id="agent_coding", user_goal="Run from generic API"),
        SimpleNamespace(headers={"idempotency-key": "post-runs-client-1"}),
    )

    assert response["run_id"] == "run_post_runs"
    assert response["client_request_id"] == "post-runs-client-1"
    assert recorded == {
        "runnable_id": "agent_coding",
        "name": "",
        "user_goal": "Run from generic API",
        "run_group_id": "",
        "upstream": "",
        "client_run_id": "post-runs-client-1",
        "client_request_id": "",
    }


def test_runtime_shutdown_cancels_active_runs_rejects_new_runs_and_records_fact(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    cancelled_process_groups = []
    monkeypatch.setattr("apps.shell.agent_runtime.cancel_terminal_process_groups", lambda: cancelled_process_groups.append(True))
    service.runtime_shutdown._cancel_terminal_process_groups = lambda: cancelled_process_groups.append(True)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="test")

        service.shutdown(close_db=False)

        assert cancelled_process_groups == [True]
        assert service.get_run(run["run_id"])["status"] == "cancelled"
        events = service.list_run_events(run["run_id"])["events"]
        assert events[-1]["event_type"] == "run.cancelled"
        with pytest.raises(AgentRuntimeError):
            service.start_main_chat_run(task_id="t2", session_id="s2", user_goal="blocked")
    finally:
        service.close()


def test_concurrent_cancel_run_is_idempotent(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="cancel once")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(lambda _index: service.cancel_run(run["run_id"]), range(20)))

        stored = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        cancel_facts = [event for event in events if event["event_type"] == "run.cancelled"]
        cancel_timeline = [event for event in stored["timeline"] if event["event"] == "run.cancelled"]

        assert {result["run_id"] for result in results} == {run["run_id"]}
        assert {result["status"] for result in results} == {"cancelled"}
        assert stored["status"] == "cancelled"
        assert len(cancel_facts) == 1
        assert len(cancel_timeline) == 1
    finally:
        service.close()


def test_runtime_shutdown_close_db_closes_runtime_resources(tmp_path):
    service = make_service(tmp_path)

    service.shutdown(close_db=True)

    with pytest.raises(sqlite3.ProgrammingError):
        service._conn.execute("SELECT 1")


def test_agent_run_client_run_id_is_idempotent(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    model_calls = 0

    def fake_chat(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return {"content": "Done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Idempotent Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )

        projection_calls: list[dict[str, str]] = []
        original_projection = service.run_transition_projection.project_agent_run_group_if_root

        def spy_project_agent_run_group_if_root(result: dict[str, object]) -> dict[str, object]:
            projection_calls.append(
                {
                    "run_id": str(result.get("run_id") or ""),
                    "status": str(result.get("status") or ""),
                    "result": str(result.get("result") or ""),
                }
            )
            return original_projection(result)

        monkeypatch.setattr(
            service.run_transition_projection,
            "project_agent_run_group_if_root",
            spy_project_agent_run_group_if_root,
        )

        first = service.create_agent_run(
            {"agent_id": agent["agent_id"], "user_goal": "Finish", "client_run_id": "run-client-1"}
        )
        second = service.create_agent_run(
            {"agent_id": agent["agent_id"], "user_goal": "Finish", "client_run_id": "run-client-1"}
        )

        assert second["idempotent"] is True
        assert second["run_id"] == first["run_id"]
        assert model_calls == 1
        assert projection_calls == [
            {
                "run_id": first["run_id"],
                "status": "completed",
                "result": "Done",
            }
        ]
        group = service.get_run_group(first["run_group_id"])
        assert group["status"] == "completed"
        assert group["summary"] == "Done"
        rows = service._conn.execute("SELECT run_id FROM runs WHERE client_request_id='run-client-1'").fetchall()
        assert len(rows) == 1
    finally:
        service.close()


def test_agent_run_rejects_sensitive_client_run_id_before_persistence(tmp_path):
    service = make_service(tmp_path)
    leaked_client_run_id = "sk-client-run-id-secret123456"
    try:
        agent = service.create_agent(
            {
                "name": "Sensitive Client Run Id Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-agent-secret",
                },
            }
        )

        with pytest.raises(AgentRuntimeError, match="client_run_id/idempotency_key"):
            service.create_agent_run(
                {
                    "agent_id": agent["agent_id"],
                    "user_goal": "Finish",
                    "client_run_id": leaked_client_run_id,
                }
            )

        rows = service._conn.execute(
            "SELECT client_request_id FROM runs WHERE client_request_id<>''"
        ).fetchall()
        assert rows == []
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_create_run_for_runnable_propagates_client_run_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    model_calls = 0

    def fake_chat(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return {"content": "Runnable done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Runnable Idempotent Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )

        first = service.create_run_for_runnable(
            runnable_id=agent["agent_id"],
            user_goal="Finish through runnable",
            client_run_id="runnable-client-1",
        )
        second = service.create_run_for_runnable(
            runnable_id=agent["agent_id"],
            user_goal="Finish through runnable",
            client_run_id="runnable-client-1",
        )

        assert first["runnable"]["id"] == agent["agent_id"]
        assert second["idempotent"] is True
        assert second["run_id"] == first["run_id"]
        assert model_calls == 1
        rows = service._conn.execute("SELECT run_id FROM runs WHERE client_request_id='runnable-client-1'").fetchall()
        assert len(rows) == 1
    finally:
        service.close()


def test_create_run_for_runnable_async_claims_client_run_id_before_dispatch(
    tmp_path,
) -> None:
    service = make_service(tmp_path)
    dispatched_threads: list[Any] = []

    class _DeferredThread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            dispatched_threads.append(self)

        def start(self) -> None:
            return None

    try:
        agent = service.create_agent(
            {
                "name": "Async Idempotent Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        service.agent_run_async_coordinator._thread_factory = _DeferredThread
        start_barrier = threading.Barrier(2)

        def start() -> dict[str, Any]:
            start_barrier.wait(timeout=5)
            return service.create_run_for_runnable_async(
                runnable_id=agent["agent_id"],
                user_goal="Finish asynchronously",
                client_run_id="async-runnable-client-1",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            first_future = pool.submit(start)
            second_future = pool.submit(start)
            concurrent_results = [first_future.result(), second_future.result()]

        sequential_retry = service.create_run_for_runnable_async(
            runnable_id=agent["agent_id"],
            user_goal="Finish asynchronously",
            client_run_id="async-runnable-client-1",
        )

        assert len({result["run_id"] for result in concurrent_results}) == 1
        assert sequential_retry["run_id"] == concurrent_results[0]["run_id"]
        assert sum(bool(result.get("idempotent")) for result in concurrent_results) == 1
        assert sequential_retry["idempotent"] is True
        assert len(dispatched_threads) == 1
        rows = service._conn.execute(
            "SELECT run_id FROM runs WHERE client_request_id=?",
            ("async-runnable-client-1",),
        ).fetchall()
        assert len(rows) == 1
    finally:
        service.close()


def test_agent_async_claim_recovers_cross_connection_unique_race_without_orphan_group(
    tmp_path,
) -> None:
    credential_store = MemoryCredentialStore()
    first_service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=credential_store,
        seed_templates=False,
    )
    second_service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=credential_store,
        seed_templates=False,
    )
    insert_barrier = threading.Barrier(2)
    dispatched_threads: list[Any] = []

    class _DeferredThread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon
            dispatched_threads.append(self)

        def start(self) -> None:
            return None

    try:
        agent = first_service.create_agent(
            {
                "name": "Cross Connection Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        for service in (first_service, second_service):
            service.agent_run_async_coordinator._lock = threading.RLock()
            service.agent_run_async_coordinator._thread_factory = _DeferredThread
            original_insert = service.agent_run_starter._insert_run

            def racing_insert(
                *,
                _insert=original_insert,
                **fields: Any,
            ) -> dict[str, Any]:
                insert_barrier.wait(timeout=5)
                return _insert(**fields)

            service.agent_run_starter._insert_run = racing_insert

        def start(service: AgentRuntimeService) -> dict[str, Any]:
            return service.create_run_for_runnable_async(
                runnable_id=agent["agent_id"],
                user_goal="Finish once",
                client_run_id="cross-connection-agent-client-1",
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, service) for service in (first_service, second_service)]
            results = [future.result() for future in futures]

        assert len({result["run_id"] for result in results}) == 1
        assert sum(bool(result.get("idempotent")) for result in results) == 1
        assert len(dispatched_threads) == 1
        runs = first_service._conn.execute(
            "SELECT run_id, run_group_id FROM runs WHERE client_request_id=?",
            ("cross-connection-agent-client-1",),
        ).fetchall()
        groups = first_service._conn.execute(
            "SELECT run_group_id FROM run_groups"
        ).fetchall()
        assert len(runs) == 1
        assert [row["run_group_id"] for row in groups] == [runs[0]["run_group_id"]]
    finally:
        second_service.close()
        first_service.close()


def test_main_chat_run_claims_client_run_id_under_concurrency(tmp_path) -> None:
    first_service = make_service(tmp_path)
    second_service = make_service(tmp_path)
    insert_barrier = threading.Barrier(2)

    for service in (first_service, second_service):
        service.main_chat_runs._lock = threading.RLock()
        original_insert = service.main_chat_runs._insert_run

        def racing_insert(
            *,
            _insert=original_insert,
            **fields: Any,
        ) -> dict[str, Any]:
            insert_barrier.wait(timeout=5)
            return _insert(**fields)

        service.main_chat_runs._insert_run = racing_insert

    def start(
        service: AgentRuntimeService,
        task_id: str,
        session_id: str,
    ) -> dict[str, Any]:
        return service.start_main_chat_run(
            task_id=task_id,
            session_id=session_id,
            user_goal="Continue recovery",
            client_run_id="main-chat-replan-client-1",
        )

    try:
        requests = [
            (first_service, "task-main-1", "chat-main-1"),
            (second_service, "task-main-2", "chat-main-2"),
        ]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(start, *request) for request in requests]
            results = [future.result() for future in futures]

        assert len({result["run_id"] for result in results}) == 1
        assert sum(bool(result.get("idempotent")) for result in results) == 1
        winner_index = next(
            index for index, result in enumerate(results) if not result.get("idempotent")
        )
        loser = next(result for result in results if result.get("idempotent"))
        winner_task_id = requests[winner_index][1]
        winner_session_id = requests[winner_index][2]
        link = first_service.task_run_links.for_run(results[0]["run_id"])
        assert link is not None
        assert link["task_id"] == winner_task_id
        assert link["session_id"] == winner_session_id
        assert loser["task_id"] == winner_task_id
        assert loser["session_id"] == winner_session_id
        rows = first_service._conn.execute(
            "SELECT run_id FROM runs WHERE client_request_id=?",
            ("main-chat-replan-client-1",),
        ).fetchall()
        assert len(rows) == 1
        link_rows = first_service._conn.execute(
            "SELECT task_id, session_id FROM task_run_links WHERE run_id=?",
            (results[0]["run_id"],),
        ).fetchall()
        assert [(row["task_id"], row["session_id"]) for row in link_rows] == [
            (winner_task_id, winner_session_id)
        ]
    finally:
        second_service.close()
        first_service.close()


def test_agent_async_rejects_client_id_claimed_by_different_run_identity(tmp_path) -> None:
    service = make_service(tmp_path)

    class _DeferredThread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            self.target = target

        def start(self) -> None:
            return None

    try:
        agent = service.create_agent(
            {
                "name": "Identity Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        service.agent_run_async_coordinator._thread_factory = _DeferredThread
        service.start_main_chat_run(
            task_id="task-identity-main",
            session_id="chat-identity",
            user_goal="Main identity",
            client_run_id="shared-identity-client-1",
        )
        with pytest.raises(AgentRuntimeError, match="idempotency.*conflict"):
            service.start_main_chat_run(
                task_id="task-identity-main-retry",
                session_id="chat-identity",
                user_goal="Different main identity",
                client_run_id="shared-identity-client-1",
            )

        with pytest.raises(AgentRuntimeError, match="idempotency.*conflict"):
            service.create_run_for_runnable_async(
                runnable_id=agent["agent_id"],
                user_goal="Agent identity",
                client_run_id="shared-identity-client-1",
            )

        first = service.create_run_for_runnable_async(
            runnable_id=agent["agent_id"],
            user_goal="Stable goal",
            client_run_id="agent-identity-client-1",
        )
        with pytest.raises(AgentRuntimeError, match="idempotency.*conflict"):
            service.create_run_for_runnable_async(
                runnable_id=agent["agent_id"],
                user_goal="Different goal",
                client_run_id="agent-identity-client-1",
            )
        other_agent = service.create_agent(
            {
                "name": "Other Identity Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        with pytest.raises(AgentRuntimeError, match="idempotency.*conflict"):
            service.create_run_for_runnable_async(
                runnable_id=other_agent["agent_id"],
                user_goal="Stable goal",
                client_run_id="agent-identity-client-1",
            )
        assert service.get_run(first["run_id"])["user_goal"] == "Stable goal"
    finally:
        service.close()


def test_run_repository_redacts_and_syncs_approval_projection(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service.runs.insert(
            kind="main_chat_run",
            runnable_id="builtin:yachiyo-main",
            user_goal="Use sk-secret-value",
            client_request_id="repo-client-1",
        )
        pending = {
            "approval_id": "approval_repo_1",
            "tool": "terminal.run",
            "input_preview": {"command": "printf ok"},
            "requested_at": "2026-06-09T00:00:00+00:00",
        }

        updated = service.runs.update(
            run["run_id"],
            result="Done sk-secret-value",
            timeline=[{"event": "test", "detail": "sk-secret-value"}],
            pending_approval=pending,
        )
        by_client_id = service.runs.by_client_request_id("repo-client-1")
        approval = service._conn.execute(
            "SELECT status, tool, input_preview_json FROM run_approvals WHERE approval_id='approval_repo_1'"
        ).fetchone()

        assert run["user_goal"] == "Use [redacted]"
        assert updated["result"] == "Done [redacted]"
        assert updated["timeline"][0]["detail"] == "[redacted]"
        assert by_client_id is not None
        assert by_client_id["idempotent"] is True
        assert by_client_id["run_id"] == run["run_id"]
        assert approval is not None
        assert approval["status"] == "pending"
        assert approval["tool"] == "terminal.run"
        assert json.loads(approval["input_preview_json"])["command"] == "printf ok"
    finally:
        service.close()


def test_run_repository_rejects_sensitive_client_request_id_before_persistence(tmp_path):
    service = make_service(tmp_path)
    leaked_client_request_id = "sk-run-repository-client-secret123456"
    try:
        with pytest.raises(AgentRuntimeError, match="client_request_id"):
            service.runs.insert(
                kind="main_chat_run",
                runnable_id="builtin:yachiyo-main",
                user_goal="Use safe idempotency key",
                client_request_id=leaked_client_request_id,
            )

        rows = service._conn.execute("SELECT client_request_id FROM runs WHERE client_request_id<>''").fetchall()
        assert rows == []
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_run_artifact_repository_redacts_projection_and_reads_files(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service.runs.insert(
            kind="agent_run",
            runnable_id="agent_artifact_test",
            user_goal="Write artifact",
        )
        artifact_dir = service.agent_artifacts_dir / run["run_id"]
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "notes.md").write_text("artifact sk-secret-value", encoding="utf-8")

        service.runs.update(
            run["run_id"],
            artifacts=[
                {
                    "kind": "tool_artifact",
                    "path": "notes.md",
                    "source_run_id": "source_run_1",
                    "token": "sk-secret-value",
                }
            ],
        )
        row = service._conn.execute(
            "SELECT kind, path, source_run_id, payload_json FROM run_artifacts WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        artifact = service.read_run_artifact(run["run_id"], "notes.md")

        assert row is not None
        assert row["kind"] == "tool_artifact"
        assert row["path"] == "notes.md"
        assert row["source_run_id"] == "source_run_1"
        assert json.loads(row["payload_json"])["token"] == "[redacted]"
        assert artifact["content"] == "artifact [redacted]"
    finally:
        service.close()


def test_run_group_repository_manages_membership_and_cleanup(tmp_path):
    service = make_service(tmp_path)
    try:
        group = service.run_groups.insert(title="Grouped Runs", source="agent")
        first = service.runs.insert(
            kind="agent_run",
            runnable_id="agent_group_first",
            user_goal="First",
            run_group_id=group["run_group_id"],
        )
        second = service.runs.insert(
            kind="agent_run",
            runnable_id="agent_group_second",
            user_goal="Second",
            run_group_id=group["run_group_id"],
        )

        service.run_groups.append_run(group["run_group_id"], first["run_id"])
        service.run_groups.update(group["run_group_id"], status="completed", summary="done")
        grouped = service.get_run_group(group["run_group_id"])
        listed = service.list_run_groups()["run_groups"]
        group_runs = service.run_groups.runs(group["run_group_id"])

        assert grouped["source"] == "agent"
        assert grouped["status"] == "completed"
        assert grouped["summary"] == "done"
        assert grouped["child_run_ids"] == [first["run_id"], second["run_id"]]
        assert any(item["run_group_id"] == group["run_group_id"] for item in listed)
        assert [run["run_id"] for run in group_runs] == [first["run_id"], second["run_id"]]

        service.runs.update(first["run_id"], status="completed")
        service.runs.update(second["run_id"], status="completed")
        service.delete_run(first["run_id"])
        assert service.get_run_group(group["run_group_id"])["child_run_ids"] == [second["run_id"]]
        service.delete_run(second["run_id"])
        with pytest.raises(KeyError):
            service.get_run_group(group["run_group_id"])
    finally:
        service.close()


def test_update_run_group_records_terminal_run_event(tmp_path):
    service = make_service(tmp_path)
    try:
        group = service.run_groups.insert(title="Grouped Runs", source="workflow")
        first = service.runs.insert(
            kind="workflow_run",
            runnable_id="workflow_group_root",
            user_goal="Ship workflow",
            run_group_id=group["run_group_id"],
        )
        second = service.runs.insert(
            kind="agent_run",
            runnable_id="agent_group_child",
            user_goal="Ship workflow",
            run_group_id=group["run_group_id"],
        )

        service._update_run_group(
            group["run_group_id"],
            status="completed",
            summary="Workflow complete",
        )
        service._update_run_group(
            group["run_group_id"],
            status="completed",
            summary="Workflow complete",
        )

        first_events = service.list_run_events(first["run_id"])["events"]
        second_events = service.list_run_events(second["run_id"])["events"]
        group_events = [
            event
            for event in first_events
            if event["event_type"] == "group.run.completed"
        ]

        assert len(group_events) == 1
        assert second_events == []
        assert group_events[0]["payload"]["run_group_id"] == group["run_group_id"]
        assert group_events[0]["payload"]["group_run_id"] == group["run_group_id"]
        assert group_events[0]["payload"]["child_run_ids"] == [
            first["run_id"],
            second["run_id"],
        ]
        assert group_events[0]["payload"]["status"] == "completed"
        assert group_events[0]["payload"]["summary"] == "Workflow complete"
        assert group_events[0]["payload"]["participant_count"] == 2
    finally:
        service.close()


def test_insert_workflow_run_records_group_started_event(tmp_path):
    service = make_service(tmp_path)
    try:
        workflow_group = service.run_groups.insert(title="Workflow group", source="workflow")
        workflow_run = service._insert_run(
            kind="workflow_run",
            runnable_id="workflow_group_root",
            user_goal="Ship workflow",
            run_group_id=workflow_group["run_group_id"],
        )
        service._insert_run(
            kind="agent_run",
            runnable_id="agent_group_child",
            user_goal="Ship workflow",
            run_group_id=workflow_group["run_group_id"],
        )
        agent_group = service.run_groups.insert(title="Agent group", source="agent")
        agent_run = service._insert_run(
            kind="agent_run",
            runnable_id="agent_root",
            user_goal="Ship agent",
            run_group_id=agent_group["run_group_id"],
        )

        workflow_events = service.list_run_events(workflow_run["run_id"])["events"]
        agent_events = service.list_run_events(agent_run["run_id"])["events"]
        group_events = [
            event
            for event in workflow_events
            if event["event_type"] == "group.run.started"
        ]

        assert len(group_events) == 1
        assert group_events[0]["payload"]["run_group_id"] == workflow_group["run_group_id"]
        assert group_events[0]["payload"]["group_run_id"] == workflow_group["run_group_id"]
        assert group_events[0]["payload"]["child_run_ids"] == [workflow_run["run_id"]]
        assert group_events[0]["payload"]["status"] == "running"
        assert group_events[0]["payload"]["source"] == "workflow"
        assert [
            event["event_type"]
            for event in agent_events
            if event["event_type"].startswith("group.run.")
        ] == []
    finally:
        service.close()


def test_update_run_group_records_failed_and_cancelled_run_events(tmp_path):
    service = make_service(tmp_path)
    try:
        failed_group = service.run_groups.insert(title="Failed group", source="workflow")
        failed_run = service.runs.insert(
            kind="workflow_run",
            runnable_id="workflow_failed",
            user_goal="Ship workflow",
            run_group_id=failed_group["run_group_id"],
        )
        cancelled_group = service.run_groups.insert(title="Cancelled group", source="workflow")
        cancelled_run = service.runs.insert(
            kind="workflow_run",
            runnable_id="workflow_cancelled",
            user_goal="Ship workflow",
            run_group_id=cancelled_group["run_group_id"],
        )

        service._update_run_group(
            failed_group["run_group_id"],
            status="failed",
            summary="Workflow failed",
        )
        service._update_run_group(
            cancelled_group["run_group_id"],
            status="cancelled",
            summary="Workflow cancelled",
        )

        failed_events = [
            event["event_type"]
            for event in service.list_run_events(failed_run["run_id"])["events"]
        ]
        cancelled_events = [
            event["event_type"]
            for event in service.list_run_events(cancelled_run["run_id"])["events"]
        ]

        assert "group.run.failed" in failed_events
        assert "group.run.cancelled" in cancelled_events
    finally:
        service.close()


def test_run_group_repository_redacts_summary_projection(tmp_path):
    service = make_service(tmp_path)
    leaked_secret = "sk-run-group-summary-secret123456"
    try:
        group = service.run_groups.insert(title="Grouped Runs", source="workflow")
        service.run_groups.update(
            group["run_group_id"],
            status="failed",
            summary=f"Workflow child failed with token={leaked_secret}",
        )

        grouped = service.get_run_group(group["run_group_id"])
        raw_summary = service._conn.execute(
            "SELECT summary FROM run_groups WHERE run_group_id=?",
            (group["run_group_id"],),
        ).fetchone()["summary"]

        assert grouped["summary"] == "Workflow child failed with token=[redacted]"
        assert raw_summary == "Workflow child failed with token=[redacted]"
        assert leaked_secret not in json.dumps({"grouped": grouped, "raw_summary": raw_summary}, ensure_ascii=False)
    finally:
        service.close()


def test_run_group_repository_redacts_insert_projection(tmp_path):
    service = make_service(tmp_path)
    title_secret = "sk-run-group-title-secret123456"
    source_secret = "sk-run-group-source-secret123456"
    workspace_secret = "sk-run-group-workspace-secret123456"
    try:
        group = service.run_groups.insert(
            title=f"Grouped Runs {title_secret}",
            source=f"agent-{source_secret}",
            workspace_dir=f"/tmp/{workspace_secret}/project",
        )

        raw_row = service._conn.execute(
            "SELECT title, source, workspace_dir FROM run_groups WHERE run_group_id=?",
            (group["run_group_id"],),
        ).fetchone()

        assert group["title"] == "Grouped Runs [redacted]"
        assert group["source"] == "agent-[redacted]"
        assert group["workspace_dir"] == "/tmp/[redacted]/project"
        assert raw_row["title"] == "Grouped Runs [redacted]"
        assert raw_row["source"] == "agent-[redacted]"
        assert raw_row["workspace_dir"] == "/tmp/[redacted]/project"
        serialized = json.dumps({"group": group, "raw_row": dict(raw_row)}, ensure_ascii=False)
        assert title_secret not in serialized
        assert source_secret not in serialized
        assert workspace_secret not in serialized
    finally:
        service.close()


def test_run_repository_deletes_rows_and_artifacts(tmp_path):
    service = make_service(tmp_path)
    try:
        run = service.runs.insert(
            kind="agent_run",
            runnable_id="agent_delete_repo",
            user_goal="Delete through repository",
        )
        artifact_root = service.agent_artifacts_dir / run["run_id"]
        artifact_root.mkdir(parents=True)
        (artifact_root / "result.md").write_text("artifact", encoding="utf-8")

        deleted = service.runs.delete_rows([run], delete_artifacts=service.run_artifacts.delete_files)
        service._conn.commit()

        assert deleted == [run["run_id"]]
        assert not artifact_root.exists()
        with pytest.raises(KeyError):
            service.get_run(run["run_id"])
    finally:
        service.close()


def test_agent_run_route_maps_idempotency_key_header():
    from apps.bridge.routes import agents as agent_routes

    payload = agent_routes._payload_with_idempotency(
        agent_routes.AgentRunRequest(agent_id="a1", user_goal="Run"),
        SimpleNamespace(headers={"idempotency-key": "header-run-1"}),
    )

    assert payload["client_run_id"] == "header-run-1"


def test_terminal_run_defaults_to_argv_and_requires_explicit_shell(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    marker = workdir / "shell-marker"
    broker = ToolBroker(
        {"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]},
        tmp_path / "artifacts",
    )

    argv_result = broker.terminal_run(f"printf safe; touch {marker}", approved=True)
    assert marker.exists() is False
    shell_result = broker.terminal_run(f"printf safe; touch {marker}", approved=True, shell=True)

    assert argv_result["shell"] is False
    assert marker.exists() is True
    assert shell_result["shell"] is True


def test_terminal_run_shell_mode_requires_approval_and_shows_full_command(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    marker = workdir / "shell-marker"
    command = f"printf safe; touch {marker}"
    broker = ToolBroker(
        {"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]},
        tmp_path / "artifacts",
    )

    result = broker.terminal_run(command, shell=True)

    assert result["approval_required"] is True
    assert result["tool"] == "terminal.run"
    assert result["input_preview"] == {"command": command, "shell": True}
    assert marker.exists() is False


def test_runtime_restores_row_factory_before_listing_runnables(tmp_path):
    service = make_service(tmp_path, seed_templates=True)
    try:
        service._conn.row_factory = None
        result = service.list_runnables()
        assert result["ok"] is True
        coding = next(item for item in result["runnables"] if item["id"] == "agent_coding")
        assert coding["output_contract"]
        assert "workspace.read" in coding["tool_policy"]["allowed_tools"]
        assert coding["tool_policy"]["approval_required"]["terminal.run"] is True
    finally:
        service.close()


def test_runtime_restores_row_factory_before_listing_agents(tmp_path):
    service = make_service(tmp_path, seed_templates=True)
    try:
        service._conn.row_factory = None
        service._ensure_row_factory = lambda: None  # type: ignore[method-assign]
        result = service.list_agents()
        assert result["ok"] is True
        assert any(agent["agent_id"] == "agent_coding" for agent in result["agents"])
    finally:
        service.close()


def test_runtime_agent_studio_reads_are_safe_under_parallel_refresh(tmp_path):
    service = make_service(tmp_path, seed_templates=True)
    try:
        def read_agent_studio_state(_index: int):
            return (
                service.list_agents()["agents"],
                service.list_skill_folders()["uncategorized"],
                service.list_runnables()["runnables"],
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(read_agent_studio_state, range(40)))

        assert results
        for agents, uncategorized, runnables in results:
            assert any(agent["agent_id"] == "agent_coding" for agent in agents)
            assert "skill_count" in uncategorized
            assert any(item["id"] == "agent_coding" for item in runnables)
    finally:
        service.close()


def test_builtin_yachiyo_main_is_virtual_system_agent_not_delegation_target(tmp_path):
    service = make_service(tmp_path, seed_templates=True)
    try:
        agents = service.list_agents()["agents"]
        main = next(agent for agent in agents if agent["agent_id"] == "builtin:yachiyo-main")

        assert main["name"] == "Yachiyo"
        assert main["system"] is True
        assert main["virtual"] is True
        assert main["deletable"] is False
        assert main["editable"] is False
        assert main["execution_backend"] == "native_profile"
        assert "workspace.read" in main["tool_policy"]["allowed_tools"]

        row = service._conn.execute(
            "SELECT 1 FROM agents WHERE agent_id=?",
            ("builtin:yachiyo-main",),
        ).fetchone()
        assert row is None
        assert service.get_agent("builtin:yachiyo-main")["system"] is True
        assert service.resolve_runnable(runnable_id="builtin:yachiyo-main")["id"] == "builtin:yachiyo-main"
        assert service.resolve_runnable(name="Yachiyo")["id"] == "builtin:yachiyo-main"
        assert any(item["id"] == "builtin:yachiyo-main" for item in service.list_runnables()["runnables"])
        assert all(
            item["id"] != "builtin:yachiyo-main"
            for item in service.list_delegation_targets()["agents"]
        )

        with pytest.raises(AgentRuntimeError, match="系统 Agent 不能删除"):
            service.delete_agent("builtin:yachiyo-main")
        with pytest.raises(AgentRuntimeError, match="系统 Agent 不能创建或覆盖"):
            service.create_agent({"agent_id": "builtin:yachiyo-main", "name": "Main"})
        with pytest.raises(AgentRuntimeError, match="系统 Agent 不能修改"):
            service.update_agent("builtin:yachiyo-main", {"description": "mutate"})
    finally:
        service.close()


def test_seed_templates_backfill_default_workflows_when_agents_exist(tmp_path):
    service = make_service(tmp_path)
    try:
        service.create_agent({"name": "Existing Agent"})
    finally:
        service.close()

    service = make_service(tmp_path, seed_templates=True)
    try:
        workflows = service.list_workflows()["workflows"]
        workflow_ids = {workflow["workflow_id"] for workflow in workflows}

        assert "workflow_web_idea_full" in workflow_ids
        assert "workflow_phase4_agent_line_smoke" in workflow_ids
        assert any(agent["agent_id"] == "agent_coding" for agent in service.list_agents()["agents"])
    finally:
        service.close()


def test_deleted_seed_templates_do_not_return_after_restart(tmp_path):
    service = make_service(tmp_path, seed_templates=True)
    try:
        service.delete_agent("agent_coding")
        service.delete_workflow("workflow_web_idea_full")
    finally:
        service.close()

    service = make_service(tmp_path, seed_templates=True)
    try:
        agent_ids = {agent["agent_id"] for agent in service.list_agents()["agents"]}
        workflow_ids = {workflow["workflow_id"] for workflow in service.list_workflows()["workflows"]}

        assert "agent_coding" not in agent_ids
        assert "workflow_web_idea_full" not in workflow_ids
    finally:
        service.close()


def test_phase4_seeded_workflow_executes_default_agent_line(tmp_path, monkeypatch):
    service = make_service(tmp_path, seed_templates=True)
    calls = []
    expected_step_tasks = [
        "拆解全局目标，明确后续 Agent 的交付边界、依赖关系、风险和验收口径。",
        "基于全局目标整理事实、约束、参考信息和不确定点，为设计与实现提供依据。",
        "基于研究结果提出信息架构、交互结构、视觉方向和需要交付的设计要点。",
        "根据上游设计与约束给出实现方案、必要代码或变更计划，并说明验证方式。",
        "审查上游实现或方案，列出问题优先级、风险、缺失测试和可验收结论。",
        "把整条流程的目标、关键决策、产物、风险和后续待办整理成最终汇报。",
    ]

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": f"Step {len(calls)} complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: FakeDefaultProfileService())
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        run = service.create_workflow_run(
            {
                "workflow_id": "workflow_phase4_agent_line_smoke",
                "user_goal": "跑一次 Phase 4 全线流通性测试",
            }
        )

        assert run["status"] == "completed"
        assert run["result"] == "Step 6 complete"
        assert len(calls) == 6
        for index, task in enumerate(expected_step_tasks):
            expected_context = (
                f"# User Goal\n{task}\n\n"
                "Workflow Goal:\n跑一次 Phase 4 全线流通性测试"
            )
            assert any(
                expected_context in str(message.get("content") or "")
                for message in calls[index]
            )
        assert [event["event"] for event in run["timeline"]].count("workflow.node.agent") == 6
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert [item.get("task") for item in started_event["workflow_path"] if item.get("kind") == "agent"] == expected_step_tasks
        assert [
            item.get("artifact_path")
            for item in started_event["workflow_path"]
            if item.get("kind") == "artifact"
        ] == ["reports/phase-4-flow-summary.md"]
        assert any(artifact.get("kind") == "workflow_artifact" for artifact in run["artifacts"])
        group = service.get_run_group(run["run_group_id"])
        assert group["status"] == "completed"
        assert len(group["child_run_ids"]) == 7
    finally:
        service.close()


def test_agent_crud_and_api_key_redaction(tmp_path):
    service = make_service(tmp_path)
    try:
        agent = service.create_agent(
            {
                "name": "Private Model",
                "nickname": "Private",
                "persona_prompt": "Keep a concise operator tone.",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-test-secret",
                },
            }
        )

        assert agent["model_config"]["api_key_configured"] is True
        assert "api_key" not in agent["model_config"]
        assert agent["nickname"] == "Private"
        assert agent["persona_prompt"] == "Keep a concise operator tone."

        updated = service.update_agent(
            agent["agent_id"],
            {
                "description": "updated",
                "nickname": "Private Ops",
                "model_config": {"base_url": "https://gateway.example.test/v1", "api_key": ""},
            },
        )
        assert updated["description"] == "updated"
        assert updated["nickname"] == "Private Ops"
        assert updated["model_config"]["base_url"] == "https://gateway.example.test/v1"
        assert updated["model_config"]["api_key_configured"] is True

        conn = sqlite3.connect(tmp_path / "agent-runtime.db")
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT model_api_key, model_credential_ref FROM agents WHERE agent_id=?",
                (agent["agent_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["model_api_key"] == ""
        assert row["model_credential_ref"] == f"agent:{agent['agent_id']}:model_api_key"
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_legacy_agent_model_api_key_migration_vacuums_plaintext_secret(tmp_path):
    db_path = tmp_path / "agent-runtime.db"
    legacy_secret = "sk-legacy-agent-secret123456"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        f"""
        CREATE TABLE agents (
            agent_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            nickname TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            avatar_url TEXT NOT NULL DEFAULT '',
            category TEXT NOT NULL DEFAULT 'custom',
            instructions TEXT NOT NULL DEFAULT '',
            persona_prompt TEXT NOT NULL DEFAULT '',
            model_mode TEXT NOT NULL DEFAULT 'custom_api',
            execution_backend TEXT NOT NULL DEFAULT 'native_profile',
            model_profile_id TEXT NOT NULL DEFAULT '',
            vision_model_profile_id TEXT NOT NULL DEFAULT '',
            model_provider TEXT NOT NULL DEFAULT 'openai_compatible',
            model_base_url TEXT NOT NULL DEFAULT 'https://api.example.test/v1',
            model_name TEXT NOT NULL DEFAULT 'demo-model',
            model_api_key TEXT NOT NULL DEFAULT '',
            tool_policy_json TEXT NOT NULL DEFAULT '{{}}',
            workspace_policy_json TEXT NOT NULL DEFAULT '{{}}',
            skill_ids_json TEXT NOT NULL DEFAULT '[]',
            output_contract TEXT NOT NULL DEFAULT 'chat',
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO agents (
            agent_id, name, model_api_key, created_at, updated_at
        ) VALUES (
            'agent_legacy_secret', 'Legacy Secret Agent', '{legacy_secret}', 'now', 'now'
        );
        """
    )
    conn.close()
    credential_store = MemoryCredentialStore()

    service = AgentRuntimeService(
        db_path=db_path,
        workspace_dir=tmp_path / "runtime",
        credential_store=credential_store,
        seed_templates=False,
    )
    try:
        agent = service.get_agent("agent_legacy_secret")
        assert agent["model_config"]["api_key_configured"] is True
        assert credential_store.get("agent:agent_legacy_secret:model_api_key") == legacy_secret

        row = service._conn.execute(
            "SELECT model_api_key, model_credential_ref FROM agents WHERE agent_id=?",
            ("agent_legacy_secret",),
        ).fetchone()
        assert row["model_api_key"] == ""
        assert row["model_credential_ref"] == "agent:agent_legacy_secret:model_api_key"
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_agents_receive_isolated_default_workdirs(tmp_path):
    service = make_service(tmp_path)
    try:
        coding = service.create_agent({"name": "Default Writer", "category": "coding"})
        reader = service.create_agent({"name": "Default Reader"})

        coding_workdir = Path(coding["workspace_policy"]["default_workdir"])
        reader_workdir = Path(reader["workspace_policy"]["default_workdir"])
        assert coding_workdir == service.agent_workspaces_dir / coding["agent_id"]
        assert reader_workdir == service.agent_workspaces_dir / reader["agent_id"]
        assert coding_workdir.is_dir()
        assert reader_workdir.is_dir()
        assert coding["workspace_policy"]["writable_scopes"] == ["."]
        assert reader["workspace_policy"]["writable_scopes"] == []
    finally:
        service.close()


def test_runtime_migrates_blank_agent_workdirs(tmp_path):
    service = make_service(tmp_path)
    try:
        agent = service.create_agent({"name": "Legacy Writer", "category": "coding"})
        service._conn.execute(
            "UPDATE agents SET workspace_policy_json=? WHERE agent_id=?",
            (json.dumps({"default_workdir": "", "readable_scopes": ["."], "writable_scopes": []}), agent["agent_id"]),
        )
        service._conn.commit()
    finally:
        service.close()

    service = make_service(tmp_path)
    try:
        migrated = service.get_agent(agent["agent_id"])
        assert Path(migrated["workspace_policy"]["default_workdir"]) == service.agent_workspaces_dir / agent["agent_id"]
        assert migrated["workspace_policy"]["writable_scopes"] == ["."]
    finally:
        service.close()


def test_explicit_agent_workdir_preserves_empty_writable_scopes(tmp_path):
    service = make_service(tmp_path)
    workdir = tmp_path / "custom-workdir"
    try:
        agent = service.create_agent(
            {
                "name": "Explicit Writer",
                "category": "coding",
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": [],
                },
            }
        )

        assert agent["workspace_policy"]["default_workdir"] == str(workdir)
        assert agent["workspace_policy"]["writable_scopes"] == []
        assert not workdir.exists()
    finally:
        service.close()


def test_agent_and_workflow_names_are_globally_unique(tmp_path):
    service = make_service(tmp_path)
    try:
        service.create_agent({"name": "Shared Name"})
        with pytest.raises(AgentRuntimeError):
            service.create_workflow(
                {
                    "name": "shared name",
                    "nodes": [{"id": "start", "type": "start", "data": {"label": "Start"}}],
                    "edges": [],
                }
            )
    finally:
        service.close()


def test_import_skill_directory_and_mount_to_agent(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    source = tmp_path / "demo-skill"
    (source / "assets").mkdir(parents=True)
    (source / "SKILL.md").write_text("# Demo Skill\n\nUseful instruction.", encoding="utf-8")
    (source / "assets" / "sample.txt").write_text("asset", encoding="utf-8")
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "Demo Skill used"})
    try:
        skill = service.import_skill(str(source))
        agent = service.create_agent(
            {
                "name": "Skill Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        mounted = service.attach_skill(agent["agent_id"], skill["skill_id"])

        assert skill["name"] == "Demo Skill"
        assert skill["source_path"] == "local:demo-skill"
        assert skill["local_path"].endswith(skill["skill_id"])
        assert skill["enabled"] is True
        assert skill["asset_paths"] == ["assets/sample.txt"]
        assert mounted["skill_ids"] == [skill["skill_id"]]
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Use the skill"})
        assert run["result"] == "Demo Skill used"
        artifact = service.read_run_artifact(run["run_id"], "agent-context.md")
        assert artifact["ok"] is True
        assert "Skill summary index (progressive disclosure)" in artifact["content"]
        assert f"skill_id: {skill['skill_id']}" in artifact["content"]
        assert "Call skill.read with skill_id" in artifact["content"]
        assert "skill_markdown" not in artifact["content"]
        assert "# Demo Skill\n\nUseful instruction" not in artifact["content"]
        assert run["run_group_id"]
        group = service.get_run_group(run["run_group_id"])
        assert group["source"] == "agent"
        assert group["child_run_ids"] == [run["run_id"]]
        disabled = service.update_skill(skill["skill_id"], {"enabled": False})
        assert disabled["enabled"] is False
        with pytest.raises(AgentRuntimeError, match="已停用"):
            service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Use disabled skill"})
        other_agent = service.create_agent({"name": "Other Skill Agent"})
        with pytest.raises(AgentRuntimeError, match="已停用"):
            service.attach_skill(other_agent["agent_id"], skill["skill_id"])
        with pytest.raises(AgentRuntimeError):
            service.read_run_artifact(run["run_id"], "../escape.md")
    finally:
        service.close()


def test_agent_can_read_mounted_skill_progressively(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    source = tmp_path / "demo-skill"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: Demo Skill\ndescription: Use the demo operation.\n---\n\n# Demo Skill\n\nUseful instruction.",
        encoding="utf-8",
    )
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            assert any(
                tool["function"]["name"] == "skill_read"
                for tool in tools or []
            )
            assert "skill_markdown" not in messages[1]["content"]
            assert "# Demo Skill\n\nUseful instruction" not in messages[1]["content"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_skill_read",
                        "type": "function",
                        "function": {
                            "name": "skill_read",
                            "arguments": json.dumps({"name": "Demo Skill"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        tool_result = json.loads(messages[-1]["content"])
        assert tool_result["ok"] is True
        assert tool_result["name"] == "Demo Skill"
        assert "Useful instruction" in tool_result["skill_markdown"]
        return {"content": "Skill instruction applied"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        skill = service.import_skill(str(source))
        agent = service.create_agent(
            {
                "name": "Progressive Skill Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        service.attach_skill(agent["agent_id"], skill["skill_id"])

        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Use Demo Skill"})

        assert run["status"] == "completed"
        assert run["result"] == "Skill instruction applied"
        assert any(event["event"] == "agent.tool.call" and event["detail"] == "skill.read" for event in run["timeline"])
    finally:
        service.close()


def test_tool_broker_memory_add_replace_remove_persists_audited_items(tmp_path):
    service = make_service(tmp_path)
    try:
        broker = ToolBroker(
            {"default_workdir": str(tmp_path), "readable_scopes": ["."]},
            tmp_path / "artifacts",
            memory_store=service._memory_store(source_run_id="run-memory-direct"),
        )

        added = broker.call(
            "memory.add",
            {"content": "User prefers concise Chinese replies. sk-direct-secret", "kind": "preference"},
        )
        memory_id = added["memory"]["memory_id"]
        replaced = broker.call(
            "memory.replace",
            {"memory_id": memory_id, "content": "User prefers concise bilingual replies.", "kind": "preference"},
        )
        removed = broker.call("memory.remove", {"memory_id": memory_id, "reason": "user correction"})
        active = service.list_memory_items()
        all_items = service.list_memory_items(include_deleted=True)
        events = service._conn.execute(
            "SELECT action, payload_json, source_run_id FROM memory_events WHERE memory_id=? ORDER BY created_at",
            (memory_id,),
        ).fetchall()

        assert added["ok"] is True
        assert added["memory"]["kind"] == "preference"
        assert "sk-direct-secret" not in added["memory"]["content"]
        assert "[redacted]" in added["memory"]["content"]
        assert replaced["ok"] is True
        assert replaced["memory"]["content"] == "User prefers concise bilingual replies."
        assert removed["ok"] is True
        assert active["memories"] == []
        assert all_items["memories"][0]["deleted_at"]
        assert [event["action"] for event in events] == ["memory.add", "memory.replace", "memory.remove"]
        assert {event["source_run_id"] for event in events} == {"run-memory-direct"}
        assert not any("sk-direct-secret" in event["payload_json"] for event in events)
    finally:
        service.close()


def test_agent_memory_tool_creates_candidate_not_recalled_without_user_consent(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            assert any(tool["function"]["name"] == "memory_add" for tool in tools or [])
            assert "No durable memories yet." in messages[1]["content"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_memory_add",
                        "type": "function",
                        "function": {
                            "name": "memory_add",
                            "arguments": json.dumps(
                                {
                                    "content": "User calls the project Oha-Yachiyo.",
                                    "kind": "fact",
                                    "scope": "global",
                                }
                            ),
                        },
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "tool"
            tool_result = json.loads(messages[-1]["content"])
            assert tool_result["ok"] is True
            assert tool_result["memory"]["content"] == "User calls the project Oha-Yachiyo."
            return {"content": "Memory saved"}
        assert "User calls the project Oha-Yachiyo." not in messages[1]["content"]
        assert "No durable memories yet." in messages[1]["content"]
        return {"content": "No confirmed memory yet"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Memory Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        first = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Remember this project name"})
        memories = service.list_memory_items()["memories"]
        second = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "What do you remember?"})

        assert first["status"] == "completed"
        assert first["result"] == "Memory saved"
        assert memories[0]["content"] == "User calls the project Oha-Yachiyo."
        assert memories[0]["user_confirmed"] is False
        assert second["status"] == "completed"
        assert second["result"] == "No confirmed memory yet"
        assert any(event["event"] == "agent.tool.call" and event["detail"] == "memory.add" for event in first["timeline"])
    finally:
        service.close()


def test_agent_can_schedule_and_trigger_durable_future_task(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            assert any(tool["function"]["name"] == "future_task_schedule" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_future_task_schedule",
                        "type": "function",
                        "function": {
                            "name": "future_task_schedule",
                            "arguments": json.dumps(
                                {
                                    "title": "Follow up",
                                    "prompt": "Check the release checklist tomorrow.",
                                    "delay_seconds": 0,
                                }
                            ),
                        },
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "tool"
            tool_result = json.loads(messages[-1]["content"])
            assert tool_result["ok"] is True
            assert tool_result["future_task"]["status"] == "scheduled"
            return {"content": "FutureTask scheduled"}
        assert "Check the release checklist tomorrow." in messages[1]["content"]
        return {"content": "FutureTask run completed"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "FutureTask Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        first = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Remind me tomorrow"})
        future_tasks = service.list_future_tasks()["future_tasks"]
        triggered = service.trigger_due_future_tasks()
        updated = service.list_future_tasks()["future_tasks"][0]

        assert first["status"] == "completed"
        assert first["result"] == "FutureTask scheduled"
        assert future_tasks[0]["title"] == "Follow up"
        assert future_tasks[0]["runnable_id"] == agent["agent_id"]
        assert len(triggered["triggered"]) == 1
        assert triggered["triggered"][0]["ok"] is True
        assert triggered["triggered"][0]["run"]["status"] == "completed"
        assert triggered["triggered"][0]["run"]["result"] == "FutureTask run completed"
        assert updated["status"] == "triggered"
        assert updated["run_count"] == 1
        assert updated["last_run_id"] == triggered["triggered"][0]["run"]["run_id"]
        assert any(event["event"] == "agent.tool.call" and event["detail"] == "future_task.schedule" for event in first["timeline"])
    finally:
        service.close()


@pytest.mark.asyncio
async def test_agent_bridge_exposes_memory_and_future_task_management(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    try:
        memory = await agent_routes.create_memory(
            agent_routes.MemoryRequest(content="User prefers compact status reports.", kind="preference")
        )
        memory_id = memory["memory"]["memory_id"]
        listed_memories = await agent_routes.list_memories()
        updated_memory = await agent_routes.update_memory(
            memory_id,
            agent_routes.MemoryRequest(content="User prefers compact bilingual status reports.", kind="preference"),
        )
        deleted_memory = await agent_routes.delete_memory(memory_id, reason="manual cleanup")
        future = await agent_routes.schedule_future_task(
            agent_routes.FutureTaskRequest(
                title="Daily review",
                prompt="Summarize today's open Agent work.",
                runnable_id="builtin:yachiyo-main",
                delay_seconds=3600,
            )
        )
        future_task_id = future["future_task"]["future_task_id"]
        listed_future_tasks = await agent_routes.list_future_tasks()
        cancelled = await agent_routes.cancel_future_task(
            future_task_id,
            agent_routes.FutureTaskCancelRequest(reason="user cancelled"),
        )

        assert listed_memories["memories"][0]["memory_id"] == memory_id
        assert updated_memory["memory"]["content"] == "User prefers compact bilingual status reports."
        assert deleted_memory["ok"] is True
        assert future["future_task"]["title"] == "Daily review"
        assert listed_future_tasks["future_tasks"][0]["future_task_id"] == future_task_id
        assert cancelled["future_task"]["status"] == "cancelled"
    finally:
        service.close()


def test_agent_context_includes_nickname_and_persona_prompt(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "ok"})
    try:
        agent = service.create_agent(
            {
                "name": "Context Agent",
                "nickname": "Ctx",
                "instructions": "Always inspect the local brief.",
                "persona_prompt": "Speak like a careful reviewer.",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Check context"})
        artifact = service.read_run_artifact(run["run_id"], "agent-context.md")

        assert "Nickname: Ctx" in artifact["content"]
        assert "# Functional Instructions" in artifact["content"]
        assert "Always inspect the local brief." in artifact["content"]
        assert "# Persona Prompt" in artifact["content"]
        assert "Speak like a careful reviewer." in artifact["content"]
        assert "# Operating Doctrine" in artifact["content"]
        assert "Market-grade Agent operating doctrine" in artifact["content"]
        assert "Respect safety boundaries" in artifact["content"]
        assert "# Long-term Memory" in artifact["content"]
        assert "No durable memories yet." in artifact["content"]
    finally:
        service.close()


def test_agent_run_rejects_unrunnable_config_before_start(tmp_path):
    service = make_service(tmp_path)
    try:
        agent = service.create_agent(
            {
                "name": "Broken Standalone Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "provider": "openai_compatible",
                    "model": "demo-model",
                },
            }
        )

        with pytest.raises(AgentRuntimeError, match="Custom API 配置不完整"):
            service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run it"})
        assert service.list_runs()["runs"] == []
    finally:
        service.close()


def test_import_skill_rejects_missing_skill_md(tmp_path):
    service = make_service(tmp_path)
    source = tmp_path / "bad-skill"
    source.mkdir()
    try:
        with pytest.raises(AgentRuntimeError):
            service.import_skill(str(source))
    finally:
        service.close()


def test_import_skill_zip_rejects_path_traversal(tmp_path):
    service = make_service(tmp_path)
    archive = tmp_path / "bad.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../SKILL.md", "# Bad")
    try:
        with pytest.raises(AgentRuntimeError):
            service.import_skill(str(archive))
    finally:
        service.close()


def test_import_skill_zip_uses_frontmatter_source_when_available(tmp_path):
    service = make_service(tmp_path)
    archive = tmp_path / "with-source.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(
            "skill/SKILL.md",
            "---\nname: Source Skill\nrepository: https://example.test/source-skill\n---\n\n# Source Skill\n",
        )
    try:
        skill = service.import_skill(str(archive))
        assert skill["source_type"] == "local_zip"
        assert skill["source_ref"] == "https://example.test/source-skill"
    finally:
        service.close()


def test_sync_native_skills_imports_skips_and_updates(tmp_path):
    service = make_service(tmp_path)
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills"
    skill_root = native_root / "research" / "demo-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\nname: demo-sync\ndescription: Synced skill.\n---\n\n# Demo Sync\n\nUse carefully.",
        encoding="utf-8",
    )
    (native_root / "not-a-skill").mkdir(parents=True)
    try:
        first = service.sync_native_skills(roots=[{"path": str(native_root), "source_type": "native_global"}])
        assert first["summary"]["imported"] == 1
        assert first["summary"]["skipped"] == 1
        skill = service.list_skills()["skills"][0]
        assert skill["name"] == "demo-sync"
        assert skill["description"] == "Synced skill."
        assert skill["source_type"] == "native_global"
        assert skill["origin_path"] == str(skill_root.resolve())
        assert skill["local_path"] == str(skill_root.resolve())
        assert skill["source_ref"] == "research/demo-skill"
        assert skill["content_hash"]
        assert skill["last_synced_at"]

        second = service.sync_native_skills(roots=[{"path": str(native_root), "source_type": "native_global"}])
        assert second["summary"]["imported"] == 0
        assert second["summary"]["skipped"] >= 1
        assert len(service.list_skills()["skills"]) == 1

        (skill_root / "SKILL.md").write_text(
            "---\nname: demo-sync\ndescription: Updated skill.\n---\n\n# Demo Sync\n\nUpdated instruction.",
            encoding="utf-8",
        )
        updated = service.sync_native_skills(roots=[{"path": str(native_root), "source_type": "native_global"}])
        assert updated["summary"]["updated"] == 1
        skills = service.list_skills()["skills"]
        assert len(skills) == 1
        assert skills[0]["skill_id"] == skill["skill_id"]
        assert skills[0]["description"] == "Updated skill."
        assert "Updated instruction" in skills[0]["skill_markdown"]
        service.delete_skill(skill["skill_id"])
        assert skill_root.exists()
    finally:
        service.close()


def test_deleted_synced_skill_stays_deleted_after_restart_and_sync(tmp_path):
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills"
    skill_root = native_root / "research" / "deleted-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Deleted Skill\n\nDo not restore automatically.", encoding="utf-8")

    service = make_service(tmp_path)
    try:
        synced = service.sync_native_skills(
            roots=[{"path": str(native_root), "source_type": "native_global"}]
        )
        skill_id = synced["results"][0]["skill_id"]
        service.delete_skill(skill_id)
    finally:
        service.close()

    service = make_service(tmp_path, seed_templates=True)
    try:
        synced = service.sync_native_skills(
            roots=[{"path": str(native_root), "source_type": "native_global"}]
        )

        assert service.list_skills()["skills"] == []
        assert synced["summary"]["imported"] == 0
        assert synced["results"][0]["status"] == "skipped"
        assert "用户已删除" in synced["results"][0]["message"]
    finally:
        service.close()


def test_explicit_skill_import_restores_deleted_synced_skill(tmp_path):
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills"
    skill_root = native_root / "research" / "restored-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("# Restored Skill\n\nRestore explicitly.", encoding="utf-8")

    service = make_service(tmp_path)
    try:
        synced = service.sync_native_skills(
            roots=[{"path": str(native_root), "source_type": "native_global"}]
        )
        service.delete_skill(synced["results"][0]["skill_id"])

        restored = service.import_skill(str(skill_root))

        assert restored["name"] == "Restored Skill"
        assert service.get_skill(restored["skill_id"])["source_type"] == "local_dir"
    finally:
        service.close()


def test_failed_skill_reimport_keeps_deletion_record(tmp_path):
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills"
    skill_root = native_root / "research" / "failed-restore-skill"
    skill_root.mkdir(parents=True)
    skill_md = skill_root / "SKILL.md"
    skill_md.write_text("# Failed Restore Skill\n\nKeep deletion.", encoding="utf-8")

    service = make_service(tmp_path)
    try:
        synced = service.sync_native_skills(
            roots=[{"path": str(native_root), "source_type": "native_global"}]
        )
        service.delete_skill(synced["results"][0]["skill_id"])
        skill_md.unlink()

        with pytest.raises(AgentRuntimeError, match="SKILL.md"):
            service.import_skill(str(skill_root))

        skill_md.write_text("# Failed Restore Skill\n\nKeep deletion.", encoding="utf-8")
        resynced = service.sync_native_skills(
            roots=[{"path": str(native_root), "source_type": "native_global"}]
        )

        assert resynced["summary"]["imported"] == 0
        assert service.list_skills()["skills"] == []
    finally:
        service.close()


def test_explicit_skill_reinstall_restores_deleted_installed_skill(tmp_path):
    service = make_service(tmp_path)
    skill_root = service.skill_installs_native_home / "skills" / "restored-installed-skill"
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "# Restored Installed Skill\n\nRestore through reinstall.",
        encoding="utf-8",
    )
    try:
        synced = service.sync_installed_skills()
        synced_skill = next(result for result in synced["results"] if result.get("skill_id"))
        service.delete_skill(synced_skill["skill_id"])

        skipped = service.sync_installed_skills()
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text(
            "# Restored Installed Skill\n\nRestore through reinstall.",
            encoding="utf-8",
        )
        restored = service.sync_installed_skills(restore_deleted=True)

        assert skipped["summary"]["imported"] == 0
        assert restored["summary"]["imported"] == 1
        assert service.list_skills()["skills"][0]["name"] == "Restored Installed Skill"
    finally:
        service.close()


def test_skill_install_command_validation_rejects_shell_and_unknown_commands(tmp_path):
    service = make_service(tmp_path)
    try:
        with pytest.raises(AgentRuntimeError, match="shell"):
            service.install_skill_command("npx skills add owner/repo && rm -rf /")
        with pytest.raises(AgentRuntimeError, match="只允许"):
            service.install_skill_command("npm install owner/repo")
    finally:
        service.close()


def test_skill_install_command_validation_accepts_latest_and_source_shortcuts(tmp_path):
    service = make_service(tmp_path)
    try:
        argv, installer = service._validated_skill_install_argv("skills@latest add owner/repo")
        assert installer == "npx_skills"
        assert argv == ["npx", "skills@latest", "add", "owner/repo", "-a", "oha-yachiyo", "--copy", "-y"]

        argv, installer = service._validated_skill_install_argv("npx -y skills@latest add owner/repo")
        assert installer == "npx_skills"
        assert argv == ["npx", "-y", "skills@latest", "add", "owner/repo", "-a", "oha-yachiyo", "--copy"]

        argv, installer = service._validated_skill_install_argv("owner/repo --skill docs")
        assert installer == "npx_skills"
        assert argv == ["npx", "skills@latest", "add", "owner/repo", "--skill", "docs", "-a", "oha-yachiyo", "--copy", "-y"]

        with pytest.raises(AgentRuntimeError, match="oha-yachiyo"):
            service._validated_skill_install_argv("npx skills@latest add owner/repo -a codex")
    finally:
        service.close()


def test_skill_dedup_is_scoped_to_yachiyo_or_native_library(tmp_path):
    service = make_service(tmp_path)
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills" / "dev" / "shared"
    yachiyo_root = tmp_path / "local-shared"
    content = "# Shared Skill\n\nSame instructions."
    native_root.mkdir(parents=True)
    yachiyo_root.mkdir()
    (native_root / "SKILL.md").write_text(content, encoding="utf-8")
    (yachiyo_root / "SKILL.md").write_text(content, encoding="utf-8")
    try:
        service.sync_native_skills(
            roots=[{"path": str(tmp_path / ".oha-yachiyo" / "skill-library" / "skills"), "source_type": "native_global"}]
        )
        service.import_skill(str(yachiyo_root))
        skills = service.list_skills()["skills"]
        assert len(skills) == 2
        assert {skill["source_type"] for skill in skills} == {"native_global", "local_dir"}
    finally:
        service.close()


def test_skill_folders_assign_move_and_delete_without_moving_files(tmp_path):
    service = make_service(tmp_path)
    skill_root = tmp_path / "laravel-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("# Laravel Skill\n\nUse Laravel conventions.", encoding="utf-8")
    try:
        folder = service.create_skill_folder({"name": "Laravel"})
        skill = service.import_skill(str(skill_root), folder["folder_id"])
        assert skill["folder_id"] == folder["folder_id"]
        assert skill["folder_name"] == "Laravel"

        folders = service.list_skill_folders()
        listed = next(item for item in folders["folders"] if item["folder_id"] == folder["folder_id"])
        assert listed["skill_count"] == 1
        assert listed["installed_count"] == 1

        moved = service.update_skill(skill["skill_id"], {"folder_id": ""})
        assert moved["folder_id"] == ""
        assert moved["local_path"].startswith(str(service.skills_dir))

        service.update_skill(skill["skill_id"], {"folder_id": folder["folder_id"]})
        service.delete_skill_folder(folder["folder_id"])
        after_delete = service.get_skill(skill["skill_id"])
        assert after_delete["folder_id"] == ""
        assert after_delete["local_path"].startswith(str(service.skills_dir))
    finally:
        service.close()


def test_delete_skill_folder_can_delete_contained_skills(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    skill_root = tmp_path / "folder-delete-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("# Folder Delete Skill\n\nDelete with folder.", encoding="utf-8")
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "ok"})
    try:
        folder = service.create_skill_folder({"name": "Disposable"})
        skill = service.import_skill(str(skill_root), folder["folder_id"])
        local_path = Path(skill["local_path"])
        agent = service.create_agent({"name": "Folder Delete Agent"})
        service.attach_skill(agent["agent_id"], skill["skill_id"])

        deleted = service.delete_skill_folder(folder["folder_id"], delete_skills=True)

        assert deleted["ok"] is True
        assert deleted["deleted_skill_count"] == 1
        with pytest.raises(KeyError):
            service.get_skill(skill["skill_id"])
        assert service.get_agent(agent["agent_id"])["skill_ids"] == []
        assert not local_path.exists()
    finally:
        service.close()


def test_skill_folder_validation_rejects_missing_folder(tmp_path):
    service = make_service(tmp_path)
    skill_root = tmp_path / "missing-folder-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text("# Missing Folder Skill\n\nDemo.", encoding="utf-8")
    try:
        with pytest.raises(AgentRuntimeError, match="文件夹不存在"):
            service.import_skill(str(skill_root), "folder_missing")
    finally:
        service.close()


def test_skill_folder_validation_rejects_duplicate_and_long_names(tmp_path):
    service = make_service(tmp_path)
    try:
        service.create_skill_folder({"name": "Design"})
        with pytest.raises(AgentRuntimeError, match="已存在"):
            service.create_skill_folder({"name": "design"})
        with pytest.raises(AgentRuntimeError, match="不能超过"):
            service.create_skill_folder({"name": "x" * 121})
    finally:
        service.close()


def test_native_skill_list_repairs_old_managed_copy_path(tmp_path):
    service = make_service(tmp_path)
    native_root = tmp_path / ".oha-yachiyo" / "skill-library" / "skills" / "productivity" / "powerpoint"
    native_root.mkdir(parents=True)
    (native_root / "SKILL.md").write_text("# Powerpoint\n\nCreate decks.", encoding="utf-8")
    try:
        skill = service.sync_native_skills(
            roots=[{"path": str(tmp_path / ".oha-yachiyo" / "skill-library" / "skills"), "source_type": "native_global"}]
        )["results"][0]
        skill_id = skill["skill_id"]
        old_copy = service.skills_dir / skill_id
        old_copy.mkdir(parents=True, exist_ok=True)
        (old_copy / "SKILL.md").write_text("# Old Copy\n\nold", encoding="utf-8")
        service._conn.execute("UPDATE skills SET local_path=? WHERE skill_id=?", (str(old_copy), skill_id))
        service._conn.commit()

        repaired = service.list_skills()["skills"][0]
        assert repaired["local_path"] == str(native_root.resolve())
        assert repaired["origin_path"] == str(native_root.resolve())
        assert not old_copy.exists()
    finally:
        service.close()


def test_skill_install_command_runs_whitelisted_npx_and_syncs(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    recorded: dict[str, object] = {}
    monkeypatch.setenv("SSH_AUTH_SOCK", "ssh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_skill_secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-skill-secret")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-skill-secret")

    def fake_run(argv, **_kwargs):
        recorded["argv"] = list(argv)
        recorded["env"] = dict(_kwargs["env"])
        skill_root = Path(_kwargs["cwd"]) / ".skills" / "skills" / "dev" / "installed-skill"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("# Installed Skill\n\nInstalled by npx.", encoding="utf-8")
        (Path(_kwargs["cwd"]) / "skills-lock.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "skills": {
                        "installed-skill": {
                            "source": "owner/repo",
                            "sourceType": "github",
                            "skillPath": "skills/dev/installed-skill/SKILL.md",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.run", fake_run)
    service.skill_install_service._run_command = fake_run
    try:
        result = service.install_skill_command("npx skills add owner/repo")
        assert result["ok"] is True
        assert result["installer"] == "npx_skills"
        assert recorded["argv"] == ["npx", "skills", "add", "owner/repo", "-a", "oha-yachiyo", "--copy", "-y"]
        env = recorded["env"]
        assert isinstance(env, dict)
        assert env["OHA_YACHIYO_HOME"] == str(service.skill_installs_native_home)
        assert "SSH_AUTH_SOCK" not in env
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "CUSTOM_API_KEY" not in env
        assert result["sync"]["summary"]["imported"] == 1
        skill = service.list_skills()["skills"][0]
        assert skill["name"] == "Installed Skill"
        assert skill["source_type"] == "npx_skills"
        assert skill["source_ref"] == "https://github.com/owner/repo/blob/main/skills/dev/installed-skill/SKILL.md"
        assert "/skill-installs/.skills/skills/" in skill["local_path"]
    finally:
        service.close()


def test_workflow_validation_rejects_branch_and_cycle(tmp_path):
    service = make_service(tmp_path)
    try:
        with pytest.raises(AgentRuntimeError, match="未知 Workflow 节点类型"):
            service.validate_workflow(
                [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "mystery", "type": "email", "data": {"label": "Email Step"}},
                ],
                [{"source": "start", "target": "mystery"}],
            )

        nodes = [
            {"id": "start", "type": "start", "data": {"label": "Start"}},
            {"id": "a", "type": "agent", "data": {"label": "A"}},
            {"id": "b", "type": "agent", "data": {"label": "B"}},
        ]
        with pytest.raises(AgentRuntimeError):
            service.validate_workflow(
                nodes,
                [
                    {"source": "start", "target": "a"},
                    {"source": "start", "target": "b"},
                ],
            )
        with pytest.raises(AgentRuntimeError):
            service.validate_workflow(
                nodes,
                [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                    {"source": "b", "target": "a"},
                ],
            )
        assert service.validate_workflow(
            [
                {"id": "start", "type": "start", "data": {"label": "Start"}},
                {"id": "a", "type": "agent", "data": {"label": "A"}},
                {
                    "id": "repeat",
                    "type": "loop",
                    "data": {"label": "Repeat", "condition": "again", "max_iterations": 2},
                },
                {"id": "done", "type": "artifact", "data": {"label": "Done"}},
            ],
            [
                {"source": "start", "target": "a"},
                {"source": "a", "target": "repeat"},
                {"source": "repeat", "target": "a", "data": {"branch": "continue"}},
                {"source": "repeat", "target": "done", "data": {"branch": "exit"}},
            ],
        ) == {"ok": True}
    finally:
        service.close()


def test_workflow_run_rejects_start_only_draft(tmp_path):
    service = make_service(tmp_path)
    try:
        workflow = service.create_workflow(
            {
                "name": "Start Only Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                ],
                "edges": [],
            }
        )

        assert service.validate_workflow(workflow["nodes"], workflow["edges"]) == {"ok": True}
        with pytest.raises(AgentRuntimeError, match="至少需要一个可执行节点"):
            service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})
    finally:
        service.close()


def test_workflow_name_validation_and_update_trim(tmp_path):
    service = make_service(tmp_path)
    try:
        nodes = [{"id": "start", "type": "start", "data": {"label": "Start"}}]
        with pytest.raises(AgentRuntimeError, match="名称不能为空"):
            service.create_workflow({"name": "  ", "nodes": nodes, "edges": []})

        workflow = service.create_workflow({"name": "Name Trim Flow", "nodes": nodes, "edges": []})
        updated = service.update_workflow(workflow["workflow_id"], {"name": "  Renamed Flow  "})

        assert updated["name"] == "Renamed Flow"
        with pytest.raises(AgentRuntimeError, match="名称不能为空"):
            service.update_workflow(workflow["workflow_id"], {"name": "   "})
    finally:
        service.close()


def test_workflow_run_rejects_unrunnable_agent_config_before_start(tmp_path):
    service = make_service(tmp_path)
    try:
        agent = service.create_agent(
            {
                "name": "Broken Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "provider": "openai_compatible",
                    "base_url": "https://api.example.test/v1",
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Broken Agent Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Broken Agent", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )

        with pytest.raises(AgentRuntimeError, match="Custom API 配置不完整"):
            service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})
        assert service.list_runs()["runs"] == []
    finally:
        service.close()


def test_workflow_run_rejects_follow_main_agent_without_default_profile(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.get_model_profile_service", lambda: FakeNoDefaultProfileService())
    try:
        agent = service.create_agent(
            {
                "name": "Follow Main Agent",
                "model_mode": "follow_main",
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Follow Main Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Follow Main", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )

        with pytest.raises(AgentRuntimeError, match="缺少可运行的 Chat Profile"):
            service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})
        assert service.list_runs()["runs"] == []
    finally:
        service.close()


def test_linear_workflow_executes_agent_nodes_in_order(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "Profile result"})
    try:
        continuation_calls: list[dict] = []
        original_continue = service.workflow_continuation.continue_run

        def spy_continue(run, workflow, **kwargs):
            continuation_calls.append({"run_id": run.get("run_id"), "workflow_id": workflow.get("workflow_id")})
            return original_continue(run, workflow, **kwargs)

        monkeypatch.setattr(service.workflow_continuation, "continue_run", spy_continue)
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent({"name": "Agent A", "model_mode": "custom_api", "model_config": model_config})
        agent_b = service.create_agent({"name": "Agent B", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Linear Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Agent A", "agent_id": agent_a["agent_id"]}},
                    {"id": "b", "type": "agent", "data": {"label": "Agent B", "agent_id": agent_b["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})

        assert continuation_calls == [{"run_id": run["run_id"], "workflow_id": workflow["workflow_id"]}]
        assert run["status"] == "completed"
        assert run["run_group_id"]
        assert [event["event"] for event in run["timeline"]].count("workflow.node.agent") == 2
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"] == [
            {"id": "start", "kind": "start", "label": "Start"},
            {"id": "a", "kind": "agent", "label": "Agent A"},
            {"id": "b", "kind": "agent", "label": "Agent B"},
        ]
        assert run["result"] == "Profile result"
        group = service.get_run_group(run["run_group_id"])
        assert group["source"] == "workflow"
        assert len(group["child_run_ids"]) == 3
    finally:
        service.close()


def test_updated_workflow_run_uses_latest_saved_graph(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    contexts: list[str] = []
    responses = iter(["Fresh design", "Fresh code"])

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        contexts.append(messages[-1]["content"])
        return {"content": next(responses)}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        old_agent = service.create_agent({"name": "Old Agent", "model_mode": "custom_api", "model_config": model_config})
        design_agent = service.create_agent({"name": "Fresh Design", "model_mode": "custom_api", "model_config": model_config})
        coding_agent = service.create_agent({"name": "Fresh Coding", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Save And Run Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "old", "type": "agent", "data": {"label": "Old Agent", "agent_id": old_agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "old"}],
            }
        )
        service.update_workflow(
            workflow["workflow_id"],
            {
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "design", "type": "agent", "data": {"label": "Fresh Design", "agent_id": design_agent["agent_id"]}},
                    {"id": "coding", "type": "agent", "data": {"label": "Fresh Coding", "agent_id": coding_agent["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "coding"},
                ],
            },
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship latest graph"})

        assert run["status"] == "completed"
        assert run["result"] == "Fresh code"
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"] == [
            {"id": "start", "kind": "start", "label": "Start"},
            {"id": "design", "kind": "agent", "label": "Fresh Design"},
            {"id": "coding", "kind": "agent", "label": "Fresh Coding"},
        ]
        agent_events = [event for event in run["timeline"] if event["event"] == "workflow.node.agent"]
        assert [event["workflow_node_id"] for event in agent_events] == ["design", "coding"]
        assert [event["workflow_node_label"] for event in agent_events] == ["Fresh Design", "Fresh Coding"]
        group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        assert [child["runnable_id"] for child in child_runs] == [design_agent["agent_id"], coding_agent["agent_id"]]
        assert len(contexts) == 2
        assert "Old Agent" not in "\n".join(contexts)
    finally:
        service.close()


def test_workflow_child_agents_keep_goal_and_receive_prior_result_as_upstream(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    contexts = []
    responses = iter(["Design output", "Code output"])

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        contexts.append(messages[-1]["content"])
        return {"content": next(responses)}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent({"name": "Design Agent", "model_mode": "custom_api", "model_config": model_config})
        agent_b = service.create_agent({"name": "Coding Agent", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Context Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Design Agent", "agent_id": agent_a["agent_id"]}},
                    {"id": "b", "type": "agent", "data": {"label": "Coding Agent", "agent_id": agent_b["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})

        assert run["status"] == "completed"
        assert run["result"] == "Code output"
        assert (
            f"# User Goal\n{DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL}\n\n"
            "Workflow Goal:\nShip it"
        ) in contexts[0]
        assert "# Upstream Context\nNone" in contexts[0]
        assert (
            f"# User Goal\n{DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL}\n\n"
            "Workflow Goal:\nShip it"
        ) in contexts[1]
        assert "# Upstream Context\nDesign output" in contexts[1]
        assert "# User Goal\nDesign output" not in contexts[1]
        assert contexts[1].count("Design output") == 1

        group = service.get_run_group(run["run_group_id"])
        agent_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        assert [child["user_goal"] for child in agent_runs] == [
            DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL,
            DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL,
        ]
    finally:
        service.close()


def test_workflow_agent_nodes_can_define_step_tasks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    contexts: list[str] = []
    responses = iter(["Research notes", "Implementation plan"])

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        contexts.append(
            "\n".join(
                str(message.get("content") or "")
                for message in messages
                if message.get("role") == "user"
            )
        )
        return {"content": next(responses)}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        research_agent = service.create_agent({"name": "Research Agent", "model_mode": "custom_api", "model_config": model_config})
        coding_agent = service.create_agent({"name": "Coding Agent", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Step Task Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "research",
                        "type": "agent",
                        "data": {
                            "label": "Research",
                            "agent_id": research_agent["agent_id"],
                            "task": "Collect constraints and summarize the tradeoffs.",
                        },
                    },
                    {
                        "id": "coding",
                        "type": "agent",
                        "data": {
                            "label": "Coding",
                            "agent_id": coding_agent["agent_id"],
                            "instructions": "Turn the research notes into an implementation plan.",
                        },
                    },
                ],
                "edges": [
                    {"source": "start", "target": "research"},
                    {"source": "research", "target": "coding"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship feature X"})

        assert run["status"] == "completed"
        assert "# User Goal\nCollect constraints and summarize the tradeoffs.\n\nWorkflow Goal:\nShip feature X" in contexts[0]
        assert "# Upstream Context\nNone" in contexts[0]
        assert "# User Goal\nTurn the research notes into an implementation plan.\n\nWorkflow Goal:\nShip feature X" in contexts[1]
        assert "# Upstream Context\nResearch notes" in contexts[1]

        group = service.get_run_group(run["run_group_id"])
        agent_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        assert [child["user_goal"] for child in agent_runs] == [
            "Collect constraints and summarize the tradeoffs.",
            "Turn the research notes into an implementation plan.",
        ]
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"][1]["task"] == "Collect constraints and summarize the tradeoffs."
        agent_events = [event for event in run["timeline"] if event["event"] == "workflow.node.agent"]
        assert [event["workflow_node_task"] for event in agent_events] == [
            "Collect constraints and summarize the tradeoffs.",
            "Turn the research notes into an implementation plan.",
        ]
    finally:
        service.close()


def test_workflow_rejects_missing_and_disabled_agent_nodes(tmp_path):
    service = make_service(tmp_path)
    try:
        with pytest.raises(AgentRuntimeError, match="没有选择 Agent"):
            service.create_workflow(
                {
                    "name": "Missing Agent Flow",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {"label": "Start"}},
                        {"id": "agent", "type": "agent", "data": {"label": "Agent Step"}},
                    ],
                    "edges": [{"source": "start", "target": "agent"}],
                }
            )

        with pytest.raises(AgentRuntimeError, match="引用了不存在的 Agent"):
            service.create_workflow(
                {
                    "name": "Unknown Agent Flow",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {"label": "Start"}},
                        {
                            "id": "agent",
                            "type": "agent",
                            "data": {"label": "Agent Step", "agent_id": "agent_missing"},
                        },
                    ],
                    "edges": [{"source": "start", "target": "agent"}],
                }
            )

        disabled = service.create_agent({"name": "Disabled Agent", "enabled": False})
        with pytest.raises(AgentRuntimeError, match="已停用"):
            service.create_workflow(
                {
                    "name": "Disabled Agent Flow",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {"label": "Start"}},
                        {
                            "id": "agent",
                            "type": "agent",
                            "data": {"label": "Agent Step", "agent_id": disabled["agent_id"]},
                        },
                    ],
                    "edges": [{"source": "start", "target": "agent"}],
                }
            )
    finally:
        service.close()


def test_workflow_run_rejects_agent_disabled_after_save(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": "Should not run"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Later Disabled",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Later Disabled Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Agent Step", "agent_id": agent["agent_id"]},
                    },
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )
        service.update_agent(agent["agent_id"], {"enabled": False})

        with pytest.raises(AgentRuntimeError, match="已停用"):
            service.create_workflow_run(
                {"workflow_id": workflow["workflow_id"], "user_goal": "Run disabled agent"}
            )

        assert calls == []
    finally:
        service.close()


def test_workflow_approval_node_pauses_and_resumes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": f"Agent {len(calls)} complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent({"name": "Before Approval", "model_mode": "custom_api", "model_config": model_config})
        agent_b = service.create_agent({"name": "After Approval", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Human Gate Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Before Approval", "agent_id": agent_a["agent_id"]}},
                    {
                        "id": "gate",
                        "type": "approval",
                        "data": {
                            "label": "人工确认",
                            "criteria": "确认设计输出已经覆盖验收点，再继续编码。",
                        },
                    },
                    {"id": "b", "type": "agent", "data": {"label": "After Approval", "agent_id": agent_b["agent_id"]}},
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "gate"},
                    {"source": "gate", "target": "b"},
                    {"source": "b", "target": "summary"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})

        assert run["status"] == "approval_required"
        assert run["result"] == "等待审批：人工确认"
        assert run["pending_approval"]["tool"] == "workflow.approval"
        assert run["pending_approval"]["input_preview"]["checkpoint"] == "人工确认"
        assert run["pending_approval"]["input_preview"]["criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert run["pending_approval"]["input_preview"]["context"] == "Agent 1 complete"
        assert "workflow_context" not in run["pending_approval"]
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"][2]["criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert [event["event"] for event in run["timeline"] if event["event"] == "workflow.node.agent"] == [
            "workflow.node.agent",
        ]
        start_event = next(event for event in run["timeline"] if event["event"] == "workflow.node.start")
        assert start_event["workflow_node_id"] == "start"
        assert start_event["status"] == "completed"
        approval_event = next(event for event in run["timeline"] if event["event"] == "workflow.node.approval_required")
        assert approval_event["workflow_node_id"] == "gate"
        assert approval_event["workflow_node_approval_criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert approval_event["status"] == "approval_required"
        replay_before = service.list_run_events(run["run_id"])["events"]
        approval_required_fact = next(
            event for event in replay_before
            if event["event_type"] == "workflow.node.approval_required"
        )
        assert approval_required_fact["payload"]["workflow_node_id"] == "gate"
        assert approval_required_fact["payload"]["workflow_node_label"] == "人工确认"
        assert approval_required_fact["payload"]["workflow_node_approval_criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert approval_required_fact["payload"]["pending_approval"]["tool"] == "workflow.approval"
        assert service.get_run_group(run["run_group_id"])["status"] == "approval_required"

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "completed"
        assert resumed["result"] == "Agent 2 complete"
        assert resumed["pending_approval"] == {}
        assert len(calls) == 2
        approval_approved = next(event for event in resumed["timeline"] if event["event"] == "workflow.node.approval_approved")
        assert approval_approved["detail"] == "人工确认"
        assert approval_approved["workflow_node_id"] == "gate"
        assert approval_approved["workflow_node_approval_criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert approval_approved["input_preview"]["checkpoint"] == "人工确认"
        assert approval_approved["input_preview"]["criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert approval_approved["input_preview"]["context"] == "Agent 1 complete"
        assert approval_approved["status"] == "completed"
        assert [event["event"] for event in resumed["timeline"]].count("workflow.node.agent") == 2
        artifact_event = next(event for event in resumed["timeline"] if event["event"] == "workflow.node.artifact")
        assert artifact_event["workflow_node_id"] == "summary"
        assert artifact_event["status"] == "completed"
        assert artifact_event["artifact"]["path"] == "summary.md"
        assert any(artifact.get("kind") == "workflow_artifact" for artifact in resumed["artifacts"])
        replay_after_types = [
            event["event_type"] for event in service.list_run_events(run["run_id"])["events"]
        ]
        assert replay_after_types.count("workflow.node.approval_required") == 1
        assert "workflow.node.approval_approved" in replay_after_types
        assert "workflow.run.completed" in replay_after_types
        assert service.get_run_group(run["run_group_id"])["status"] == "completed"
    finally:
        service.close()


def test_workflow_approval_transitions_use_shared_context_boundary(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    context_calls: list[dict[str, object]] = []
    original_context = WorkflowApprovalTransitionContext.from_pending

    def spy_context(pending):
        context = original_context(pending)
        context_calls.append(
            {
                "label": context.label,
                "workflow_node_id": context.workflow_node_id,
                "criteria": context.criteria,
                "checkpoint": context.input_preview.get("checkpoint"),
            }
        )
        return context

    monkeypatch.setattr(
        WorkflowApprovalTransitionContext,
        "from_pending",
        staticmethod(spy_context),
    )

    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": f"Agent {len(calls)} complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        before = service.create_agent({"name": "Before Gate", "model_mode": "custom_api", "model_config": model_config})
        after = service.create_agent({"name": "After Gate", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Shared Approval Context Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "before", "type": "agent", "data": {"label": "Before Gate", "agent_id": before["agent_id"]}},
                    {
                        "id": "gate",
                        "type": "approval",
                        "data": {"label": "Human Gate", "criteria": "Review before continuing."},
                    },
                    {"id": "after", "type": "agent", "data": {"label": "After Gate", "agent_id": after["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "before"},
                    {"source": "before", "target": "gate"},
                    {"source": "gate", "target": "after"},
                ],
            }
        )

        approved_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Approve"})
        rejected_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Reject"})
        timed_out_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Timeout"})

        assert approved_run["status"] == "approval_required"
        assert rejected_run["status"] == "approval_required"
        assert timed_out_run["status"] == "approval_required"

        approved = service.approve_run_approval(approved_run["run_id"])
        rejected = service.reject_run_approval(rejected_run["run_id"], "not now")
        timed_out = service.timeout_run_approval(timed_out_run["run_id"])

        assert approved["status"] == "completed"
        assert rejected["status"] == "cancelled"
        assert timed_out["status"] == "cancelled"
        assert context_calls == [
            {
                "label": "Human Gate",
                "workflow_node_id": "gate",
                "criteria": "Review before continuing.",
                "checkpoint": "Human Gate",
            },
            {
                "label": "Human Gate",
                "workflow_node_id": "gate",
                "criteria": "Review before continuing.",
                "checkpoint": "Human Gate",
            },
            {
                "label": "Human Gate",
                "workflow_node_id": "gate",
                "criteria": "Review before continuing.",
                "checkpoint": "Human Gate",
            },
        ]
    finally:
        service.close()


def test_cancel_workflow_approval_updates_group_and_step_info(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": f"Agent {len(calls)} complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent({"name": "Before Approval", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Cancelable Gate Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Before Approval", "agent_id": agent["agent_id"]}},
                    {
                        "id": "gate",
                        "type": "approval",
                        "data": {
                            "label": "人工确认",
                            "criteria": "确认设计输出已经覆盖验收点，再继续编码。",
                        },
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "gate"},
                    {"source": "gate", "target": "summary"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})
        assert run["status"] == "approval_required"
        assert service.get_run_group(run["run_group_id"])["status"] == "approval_required"

        projection_calls: list[dict[str, str]] = []
        original_projection = service.run_transition_projection.project_cancelled_workflow_group_if_root

        def spy_project_cancelled_workflow_group_if_root(
            run_arg: dict[str, object],
            result_arg: dict[str, object],
        ) -> dict[str, object]:
            projection_calls.append(
                {
                    "run_id": str(run_arg.get("run_id") or ""),
                    "result_run_id": str(result_arg.get("run_id") or ""),
                    "status": str(result_arg.get("status") or ""),
                    "result": str(result_arg.get("result") or ""),
                }
            )
            return original_projection(run_arg, result_arg)

        monkeypatch.setattr(
            service.run_transition_projection,
            "project_cancelled_workflow_group_if_root",
            spy_project_cancelled_workflow_group_if_root,
        )

        cancelled = service.cancel_run(run["run_id"])

        assert cancelled["status"] == "cancelled"
        assert cancelled["pending_approval"] == {}
        assert cancelled["result"] == "Workflow 已取消：人工确认"
        assert projection_calls == [
            {
                "run_id": run["run_id"],
                "result_run_id": run["run_id"],
                "status": "cancelled",
                "result": "Workflow 已取消：人工确认",
            }
        ]
        assert len(calls) == 1
        cancelled_event = next(event for event in cancelled["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["detail"] == "人工确认 cancelled"
        assert cancelled_event["workflow_node_id"] == "gate"
        assert cancelled_event["workflow_node_kind"] == "approval"
        assert cancelled_event["workflow_node_label"] == "人工确认"
        assert cancelled_event["status"] == "cancelled"
        run_events = service.list_run_events(run["run_id"])["events"]
        assert any(event["event_type"] == "workflow.run.started" for event in run_events)
        cancelled_fact = next(event for event in run_events if event["event_type"] == "workflow.run.cancelled")
        assert cancelled_fact["payload"]["kind"] == "workflow_run"
        assert cancelled_fact["payload"]["status"] == "cancelled"
        group = service.get_run_group(run["run_group_id"])
        assert group["status"] == "cancelled"
        assert group["summary"] == "Workflow 已取消：人工确认"
    finally:
        service.close()


def test_workflow_approval_resume_uses_runtime_snapshot_after_workflow_edit(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        context = str(messages[1]["content"])
        assert "Name: Original After Approval" in context
        assert "Name: Edited After Approval" not in context
        return {"content": "Original agent complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        original_agent = service.create_agent({"name": "Original After Approval", "model_mode": "custom_api", "model_config": model_config})
        edited_agent = service.create_agent({"name": "Edited After Approval", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Editable Paused Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {"id": "after", "type": "agent", "data": {"label": "Original After Approval", "agent_id": original_agent["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "after"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Wait then run"})
        assert run["status"] == "approval_required"
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_snapshot"]["nodes"][2]["data"]["agent_id"] == original_agent["agent_id"]

        service.update_workflow(
            workflow["workflow_id"],
            {
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {"id": "after", "type": "agent", "data": {"label": "Edited After Approval", "agent_id": edited_agent["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "after"},
                ],
            },
        )

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "completed"
        assert resumed["result"] == "Original agent complete"
        assert len(calls) == 1
        agent_event = next(event for event in resumed["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "after"
        assert agent_event["workflow_node_label"] == "Original After Approval"
    finally:
        service.close()


def test_workflow_approval_resume_rejects_out_of_range_next_index(tmp_path):
    service = make_service(tmp_path)
    try:
        workflow = service.create_workflow(
            {
                "name": "Bad Resume Index Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "summary"},
                ],
            }
        )
        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Wait then write"}
        )
        assert run["status"] == "approval_required"
        pending = service.runs.pending_approval_private(run["run_id"])
        pending["workflow_next_index"] = 99
        service.runs.update(run["run_id"], pending_approval=pending)

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "failed"
        assert resumed["pending_approval"] == {}
        assert "Workflow Run 待审批恢复位置无效" in resumed["result"]
        assert resumed["artifacts"] == []
        assert any(event["event"] == "workflow.node.approval_approved" for event in resumed["timeline"])
        assert any(
            event["event"] == "workflow.run.failed"
            and event["detail"] == "Workflow Run 待审批恢复位置无效"
            for event in resumed["timeline"]
        )
        assert not any(event["event"] == "workflow.run.completed" for event in resumed["timeline"])
        run_events = service.list_run_events(run["run_id"])["events"]
        assert any(event["event_type"] == "workflow.node.approval_approved" for event in run_events)
        assert any(
            event["event_type"] == "workflow.run.failed"
            and event["payload"]["error"] == "Workflow Run 待审批恢复位置无效"
            for event in run_events
        )
        assert not any(event["event_type"] == "workflow.run.completed" for event in run_events)
        assert service.get_run_group(run["run_group_id"])["status"] == "failed"
    finally:
        service.close()


def test_workflow_approval_node_reject_cancels_run(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": "First step complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent({"name": "Before Approval", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Reject Gate Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Before Approval", "agent_id": agent["agent_id"]}},
                    {
                        "id": "gate",
                        "type": "approval",
                        "data": {
                            "label": "人工确认",
                            "criteria": "确认设计输出已经覆盖验收点，再继续编码。",
                        },
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "gate"},
                    {"source": "gate", "target": "summary"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})

        rejected = service.reject_run_approval(run["run_id"], "先暂停")

        assert rejected["status"] == "cancelled"
        assert rejected["pending_approval"] == {}
        assert rejected["result"] == "Workflow 审批已拒绝：先暂停"
        assert len(calls) == 1
        rejected_event = next(event for event in rejected["timeline"] if event["event"] == "workflow.node.approval_rejected")
        assert rejected_event["detail"] == "先暂停"
        assert rejected_event["workflow_node_id"] == "gate"
        assert rejected_event["workflow_node_kind"] == "approval"
        assert rejected_event["workflow_node_label"] == "人工确认"
        assert rejected_event["workflow_node_approval_criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert rejected_event["input_preview"]["checkpoint"] == "人工确认"
        assert rejected_event["input_preview"]["criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert rejected_event["input_preview"]["context"] == "First step complete"
        assert rejected_event["status"] == "cancelled"
        cancelled_event = next(event for event in rejected["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["detail"] == "先暂停"
        assert cancelled_event["workflow_node_id"] == "gate"
        assert cancelled_event["workflow_node_kind"] == "approval"
        assert cancelled_event["workflow_node_label"] == "人工确认"
        assert cancelled_event["workflow_node_approval_criteria"] == "确认设计输出已经覆盖验收点，再继续编码。"
        assert cancelled_event["input_preview"]["checkpoint"] == "人工确认"
        assert cancelled_event["status"] == "cancelled"
        group = service.get_run_group(run["run_group_id"])
        assert group["status"] == "cancelled"
        assert group["summary"] == "Workflow 审批已拒绝：先暂停"
    finally:
        service.close()


def test_workflow_duplicate_artifact_labels_write_unique_paths(tmp_path):
    service = make_service(tmp_path)
    try:
        workflow = service.create_workflow(
            {
                "name": "Duplicate Artifact Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "summary-a", "type": "artifact", "data": {"label": "Summary"}},
                    {"id": "summary-b", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "summary-a"},
                    {"source": "summary-a", "target": "summary-b"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Write duplicate artifacts"}
        )

        assert run["status"] == "completed"
        artifacts = [artifact for artifact in run["artifacts"] if artifact.get("kind") == "workflow_artifact"]
        assert [artifact["path"] for artifact in artifacts] == ["summary.md", "summary-2.md"]
        assert [artifact["workflow_node_id"] for artifact in artifacts] == ["summary-a", "summary-b"]
        artifact_rows = service._conn.execute(
            "SELECT kind, path, sequence, payload_json FROM run_artifacts WHERE run_id=? ORDER BY sequence",
            (run["run_id"],),
        ).fetchall()
        assert [(row["kind"], row["path"], row["sequence"]) for row in artifact_rows] == [
            ("workflow_artifact", "summary.md", 0),
            ("workflow_artifact", "summary-2.md", 1),
        ]
        assert json.loads(artifact_rows[0]["payload_json"])["workflow_node_id"] == "summary-a"
        assert service.read_run_artifact(run["run_id"], "summary.md")["content"] == "Write duplicate artifacts"
        assert service.read_run_artifact(run["run_id"], "summary-2.md")["content"] == "Write duplicate artifacts"
        artifact_events = [event for event in run["timeline"] if event["event"] == "workflow.node.artifact"]
        assert [event["artifact"]["path"] for event in artifact_events] == ["summary.md", "summary-2.md"]
        assert [event["workflow_node_id"] for event in artifact_events] == ["summary-a", "summary-b"]
        run_events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        assert "workflow.run.started" in event_types
        assert "workflow.run.completed" in event_types
    finally:
        service.close()


def test_workflow_artifact_nodes_can_use_configured_paths(tmp_path):
    service = make_service(tmp_path)
    try:
        workflow = service.create_workflow(
            {
                "name": "Configured Artifact Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "report-a",
                        "type": "artifact",
                        "data": {"label": "Report A", "artifact_path": "reports/final-report.md"},
                    },
                    {
                        "id": "report-b",
                        "type": "artifact",
                        "data": {"label": "Report B", "artifact_path": "reports/final-report.md"},
                    },
                    {
                        "id": "notes",
                        "type": "artifact",
                        "data": {"label": "Notes", "artifact_path": "reports/notes"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "report-a"},
                    {"source": "report-a", "target": "report-b"},
                    {"source": "report-b", "target": "notes"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Configured artifact content"}
        )

        assert run["status"] == "completed"
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert [item.get("artifact_path") for item in started_event["workflow_path"] if item.get("kind") == "artifact"] == [
            "reports/final-report.md",
            "reports/final-report-2.md",
            "reports/notes.md",
        ]
        artifacts = [artifact for artifact in run["artifacts"] if artifact.get("kind") == "workflow_artifact"]
        assert [artifact["path"] for artifact in artifacts] == [
            "reports/final-report.md",
            "reports/final-report-2.md",
            "reports/notes.md",
        ]
        assert service.read_run_artifact(run["run_id"], "reports/final-report.md")["content"] == "Configured artifact content"
        assert service.read_run_artifact(run["run_id"], "reports/final-report-2.md")["content"] == "Configured artifact content"
        assert service.read_run_artifact(run["run_id"], "reports/notes.md")["content"] == "Configured artifact content"
    finally:
        service.close()


def test_workflow_rejects_invalid_artifact_path(tmp_path):
    service = make_service(tmp_path)
    try:
        with pytest.raises(AgentRuntimeError, match="Artifact 节点 Report 的产物路径无效"):
            service.create_workflow(
                {
                    "name": "Bad Artifact Path",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {"label": "Start"}},
                        {
                            "id": "report",
                            "type": "artifact",
                            "data": {"label": "Report", "artifact_path": "../escape.md"},
                        },
                    ],
                    "edges": [{"source": "start", "target": "report"}],
                }
            )
    finally:
        service.close()


def test_workflow_approval_resume_fails_if_next_agent_was_disabled(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": "Should not run"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Next Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Approval Then Agent",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Next Agent", "agent_id": agent["agent_id"]},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "agent"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Wait first"})
        service.update_agent(agent["agent_id"], {"enabled": False})

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "failed"
        assert "已停用" in resumed["result"]
        assert calls == []
        failed_event = next(event for event in resumed["timeline"] if event["event"] == "workflow.run.failed")
        assert failed_event["workflow_node_id"] == "agent"
        assert failed_event["workflow_node_kind"] == "agent"
        assert failed_event["workflow_node_label"] == "Next Agent"
        assert service.get_run_group(run["run_group_id"])["status"] == "failed"
    finally:
        service.close()


def test_workflow_canvas_spec_exposes_participants_and_executes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    responses = iter(["Design brief", "Code patch"])

    def fake_chat(_base_url, _model, _api_key, _messages, **_kwargs):
        return {"content": next(responses)}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design = service.create_agent({
            "name": "Design Agent",
            "nickname": "Design",
            "avatar_url": "https://example.test/design.png",
            "model_mode": "custom_api",
            "model_config": model_config,
        })
        coding = service.create_agent({
            "name": "Coding Agent",
            "nickname": "Code",
            "avatar_url": "https://example.test/code.png",
            "model_mode": "custom_api",
            "model_config": model_config,
        })
        workflow = service.create_workflow(
            {
                "name": "Web Design Chain",
                "nodes": [
                    {"id": "start", "type": "start", "position": {"x": 40, "y": 120}, "data": {"label": "Start", "kind": "start"}},
                    {"id": "design", "type": "agent", "position": {"x": 260, "y": 120}, "data": {"label": "Design", "kind": "agent", "agent_id": design["agent_id"]}},
                    {"id": "coding", "type": "agent", "position": {"x": 480, "y": 120}, "data": {"label": "Coding", "kind": "agent", "agent_id": coding["agent_id"]}},
                ],
                "edges": [
                    {"id": "edge-start-design", "source": "start", "target": "design"},
                    {"id": "edge-design-coding", "source": "design", "target": "coding"},
                ],
            }
        )

        runnable = next(item for item in service.list_runnables()["runnables"] if item["id"] == workflow["workflow_id"])
        run = service.create_run_for_runnable(runnable_id=workflow["workflow_id"], user_goal="Build a landing page")

        assert runnable["kind"] == "workflow"
        assert [participant["name"] for participant in runnable["participants"]] == ["Design Agent", "Coding Agent"]
        assert [participant["avatar_url"] for participant in runnable["participants"]] == [
            "https://example.test/design.png",
            "https://example.test/code.png",
        ]
        assert all("tool_policy" in participant for participant in runnable["participants"])
        assert all("artifact.write" in participant["tool_policy"]["allowed_tools"] for participant in runnable["participants"])
        assert run["status"] == "completed"
        assert run["result"] == "Code patch"
        assert [event["event"] for event in run["timeline"]].count("workflow.node.agent") == 2
        assert service.get_run_group(run["run_group_id"])["source"] == "workflow"
    finally:
        service.close()


def test_list_runs_returns_roots_and_standalone_agents_only(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "OK"})
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent({"name": "Workflow Agent A", "model_mode": "custom_api", "model_config": model_config})
        agent_b = service.create_agent({"name": "Workflow Agent B", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "List Runs Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Agent A", "agent_id": agent_a["agent_id"]}},
                    {"id": "b", "type": "agent", "data": {"label": "Agent B", "agent_id": agent_b["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                ],
            }
        )

        workflow_run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})
        standalone_agent_run = service.create_agent_run({"agent_id": agent_a["agent_id"], "user_goal": "Run alone"})

        listed = service.list_runs(limit=20)["runs"]
        listed_ids = {run["run_id"] for run in listed}
        group = service.get_run_group(workflow_run["run_group_id"])
        workflow_child_run_ids = [
            run_id
            for run_id in group["child_run_ids"]
            if run_id != workflow_run["run_id"]
        ]

        assert workflow_run["run_id"] in listed_ids
        assert standalone_agent_run["run_id"] in listed_ids
        assert not any(run_id in listed_ids for run_id in workflow_child_run_ids)
        assert service.get_run(workflow_child_run_ids[0])["run_group_source"] == "workflow"
        assert service.get_run(standalone_agent_run["run_id"])["run_group_source"] == "agent"
    finally:
        service.close()


def test_list_runs_hides_workflow_child_agents_for_delegated_workflows(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "OK"})
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent({"name": "Delegated Workflow Agent", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Delegated Workflow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Agent", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )

        delegated = service.delegate_runnable(
            kind="workflow",
            runnable_id=workflow["workflow_id"],
            user_goal="Run delegated workflow",
        )
        group = service.get_run_group(delegated["run_group_id"])
        child_agent_run_ids = [
            run_id
            for run_id in group["child_run_ids"]
            if service.get_run(run_id)["kind"] == "agent_run"
        ]
        listed_ids = {run["run_id"] for run in service.list_runs(limit=20)["runs"]}

        assert service.get_run_group(delegated["run_group_id"])["source"] == "delegation"
        assert delegated["run_id"] in listed_ids
        assert child_agent_run_ids
        assert not any(run_id in listed_ids for run_id in child_agent_run_ids)
    finally:
        service.close()


def test_list_runs_hides_workflow_child_agents_for_custom_workflow_sources(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "OK"})
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent({"name": "Custom Source Workflow Agent", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Custom Source Workflow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Agent", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )

        workflow_run = service.create_workflow_run(
            {
                "workflow_id": workflow["workflow_id"],
                "user_goal": "Run workflow from a specific smoke source",
                "source": "workflow_child_artifact_smoke",
            }
        )
        group = service.get_run_group(workflow_run["run_group_id"])
        child_agent_run_ids = [
            run_id
            for run_id in group["child_run_ids"]
            if service.get_run(run_id)["kind"] == "agent_run"
        ]
        listed_ids = {run["run_id"] for run in service.list_runs(limit=20)["runs"]}

        assert group["source"] == "workflow_child_artifact_smoke"
        assert workflow_run["run_id"] in listed_ids
        assert child_agent_run_ids
        assert not any(run_id in listed_ids for run_id in child_agent_run_ids)
    finally:
        service.close()


def test_workflow_stops_when_child_agent_fails(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(*_args, **_kwargs):
        calls.append("called")
        raise RuntimeError("model exploded")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent({"name": "Failing Agent", "model_mode": "custom_api", "model_config": model_config})
        agent_b = service.create_agent({"name": "Skipped Agent", "model_mode": "custom_api", "model_config": model_config})
        workflow = service.create_workflow(
            {
                "name": "Fail Fast Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Failing Agent", "agent_id": agent_a["agent_id"]}},
                    {"id": "b", "type": "agent", "data": {"label": "Skipped Agent", "agent_id": agent_b["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship it"})

        assert run["status"] == "failed"
        assert run["result"] == "model exploded"
        assert calls == ["called"]
        assert [event["event"] for event in run["timeline"]].count("workflow.node.agent") == 1
        failed_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.failed")
        assert failed_event["workflow_node_id"] == "a"
        assert failed_event["workflow_node_kind"] == "agent"
        assert failed_event["workflow_node_label"] == "Failing Agent"
        assert service.get_run_group(run["run_group_id"])["status"] == "failed"
    finally:
        service.close()


def test_workflow_child_agent_provider_exception_is_redacted_from_parent_events_and_storage(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []
    leaked_secret = "sk-workflow-child-provider-secret123456"

    def fake_chat(*_args, **_kwargs):
        calls.append("called")
        raise RuntimeError(f"provider rejected api_key={leaked_secret}")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Secret Failing Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "workflow-child-placeholder-key",
                },
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Secret Safe Child Failure",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "agent", "type": "agent", "data": {"label": "Failing Agent", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "agent"}],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Run failing child"})
        parent_events = service.list_run_events(run["run_id"])["events"]
        node_fact = next(event for event in parent_events if event["event_type"] == "workflow.node.agent")
        failed_fact = next(event for event in parent_events if event["event_type"] == "workflow.run.failed")
        child_run_id = node_fact["payload"]["child_run_id"]
        child_run = service.get_run(child_run_id)
        child_events = service.list_run_events(child_run_id)["events"]
        projection = json.dumps(
            {
                "workflow_run": run,
                "workflow_events": parent_events,
                "run_group": service.get_run_group(run["run_group_id"]),
                "child_run": child_run,
                "child_events": child_events,
            },
            ensure_ascii=False,
        )

        assert calls == ["called"]
        assert run["status"] == "failed"
        assert child_run["status"] == "failed"
        assert node_fact["payload"]["status"] == "failed"
        assert "[redacted]" in node_fact["payload"]["result"]
        assert "[redacted]" in failed_fact["payload"]["result"]
        assert "[redacted]" in projection
        assert leaked_secret not in projection
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []


def test_agent_execution_backend_legacy_values_normalize_to_native(tmp_path):
    service = make_service(tmp_path)
    try:
        native_agent = service.create_agent({"name": "Native Agent"})
        assert native_agent["execution_backend"] == "native_profile"
        run = service.create_agent_run({"agent_id": native_agent["agent_id"], "user_goal": "Plan"})
        assert run["status"] == "failed"
        assert "Chat Profile" in run["result"]

        external = service.create_agent({"name": "CLI Agent", "execution_backend": "external_cli"})
        assert external["execution_backend"] == "native_profile"
        external_run = service.create_agent_run({"agent_id": external["agent_id"], "user_goal": "Review"})
        assert external_run["status"] == "failed"
        assert "Chat Profile" in external_run["result"]

        with pytest.raises(AgentRuntimeError, match="不再支持 legacy"):
            service.create_agent({"name": "Legacy Agent", "execution_backend": "hermes_profile"})
    finally:
        service.close()


def test_delegation_targets_and_delegate_run(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", lambda *_args, **_kwargs: {"content": "Delegated result"})
    try:
        agent = service.create_agent(
            {
                "name": "Delegated Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        targets = service.list_delegation_targets()
        assert any(item["name"] == "Delegated Agent" for item in targets["agents"])

        result = service.delegate_runnable(kind="agent", name="Delegated Agent", user_goal="Do the work")
        assert result["ok"] is True
        assert result["runnable"]["id"] == agent["agent_id"]
        assert result["result"] == "Delegated result"
        run = service.get_run(result["run_id"])
        assert run["status"] == "completed"
        assert run["run_group_id"]
    finally:
        service.close()


def test_agent_run_executes_native_tool_call_and_continues(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("hello native tools", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "README.md"})},
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_read"
        assert "hello native tools" in messages[-1]["content"]
        return {"content": "Read complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})

        assert run["status"] == "completed"
        assert run["result"] == "Read complete"
        tool_event = next(event for event in run["timeline"] if event["event"] == "agent.tool.call" and event["detail"] == "workspace.read")
        assert tool_event["input_preview"]["path"] == "README.md"
        assert tool_event["result"]["ok"] is True
        run_events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        assert "agent.run.started" in event_types
        assert "agent.run.completed" in event_types
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
    finally:
        service.close()


def test_agent_run_executes_provider_message_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent provider message tool content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None, stream=False):
        calls.append({"messages": messages, "tools": tools, "stream": stream})
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_agent_provider_read",
                        "type": "function",
                        "function": {
                            "name": "workspace_read",
                            "arguments": {"path": "README.md"},
                        },
                    }
                ],
            }

        messages = calls[-1]["messages"]
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_provider_read"
        arguments = assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"path": "README.md"}
        assert tool_messages[-1]["tool_call_id"] == "call_agent_provider_read"
        assert "agent provider message tool content" in tool_messages[-1]["content"]
        return {"role": "assistant", "content": "Agent provider message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Provider Message Tool Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent provider message tool call complete"
        assert len(calls) == 2
        assert calls[0]["stream"] is True
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_openai_sdk_object_message_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent sdk object message tool content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None, stream=False):
        calls.append({"messages": messages, "tools": tools, "stream": stream})
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])
            return SimpleNamespace(
                role="assistant",
                content="",
                tool_calls=[
                    SimpleNamespace(
                        id="call_agent_sdk_object_read",
                        type="function",
                        function=SimpleNamespace(
                            name="workspace_read",
                            arguments={"path": "README.md"},
                        ),
                    )
                ],
            )

        messages = calls[-1]["messages"]
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_sdk_object_read"
        arguments = assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"]
        assert isinstance(arguments, str)
        assert json.loads(arguments) == {"path": "README.md"}
        assert tool_messages[-1]["tool_call_id"] == "call_agent_sdk_object_read"
        assert "agent sdk object message tool content" in tool_messages[-1]["content"]
        return {"role": "assistant", "content": "Agent SDK object message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "SDK Object Message Tool Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent SDK object message tool call complete"
        assert len(calls) == 2
        assert calls[0]["stream"] is True
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_streaming_tool_call_and_continues(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent streaming tool content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_agent_stream_read",
                                        "type": "function",
                                        "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                    }
                                ]
                            }
                        }
                    ]
                }
                yield {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"name": "read", "arguments": 'ME.md"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_stream_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_agent_stream_read"
        assert "agent streaming tool content" in messages[-1]["content"]
        return {"content": "Agent streaming tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Streaming Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent streaming tool call complete"
        assert len(calls) == 2
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_top_level_delta_message_streaming_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent top-level delta message tool content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "delta": {
                        "tool_calls": [
                            {
                                "id": "call_agent_top_level_delta_message_read",
                                "type": "function",
                                "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                            }
                        ]
                    }
                }
                yield {
                    "message": {
                        "tool_calls": [
                            {
                                "id": "call_agent_top_level_delta_message_read",
                                "type": "function",
                                "function": {"name": "read", "arguments": 'ME.md"}'},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_top_level_delta_message_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_agent_top_level_delta_message_read"
        assert "agent top-level delta message tool content" in messages[-1]["content"]
        return {"content": "Agent top-level delta message tool call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Top Level Delta Message Reader",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent top-level delta message tool call complete"
        assert len(calls) == 2
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_consumes_split_utf8_http_sse_content_chunks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent 跨块 HTTP SSE 完成"
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": expected}}]}, ensure_ascii=False)
            frame = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
            split_at = frame.index("跨".encode("utf-8")) + 1
            yield frame[:split_at]
            yield frame[split_at : split_at + 2]
            yield frame[split_at + 2 :]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Split UTF8 HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")
        payload_json = json.dumps(completed_fact["payload"], ensure_ascii=False)

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert len(requests) == 1
        assert "\ufffd" not in run["result"]
        assert "\ufffd" not in payload_json
        assert completed_fact["payload"]["result"] == expected
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_consumes_split_http_sse_content_frame_chunks(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            payload = json.dumps({"choices": [{"delta": {"content": "agent split frame"}}]})
            frame = f"data: {payload}\n\ndata: [DONE]\n\n".encode("utf-8")
            yield frame[:8]
            yield frame[8:29]
            yield frame[29:53]
            yield frame[53:]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Split HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == "agent split frame"
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == "agent split frame"
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_consumes_coalesced_http_sse_content_frames(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            first = json.dumps({"choices": [{"delta": {"content": "agent coalesced "}}]})
            second = json.dumps({"choices": [{"delta": {"content": "frames"}}]})
            yield f": keepalive\n\ndata: {first}\n\ndata: {second}\n\ndata: [DONE]\n\n".encode("utf-8")

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Coalesced HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == "agent coalesced frames"
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == "agent coalesced frames"
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_consumes_multiline_http_sse_content_data_event(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield (
                b"id: agent-runtime-chunk-1\r\n"
                b"event: completion.chunk\r\n"
                b'data: {"choices":[{"delta":{"content":"agent multiline"}\r\n'
                b'data: ,"finish_reason":"stop"}]}\r\n\r\n'
                b"data: [DONE]\r\n\r\n"
            )

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Multiline HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == "agent multiline"
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == "agent multiline"
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_consumes_http_sse_content_parts(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    requests = []
    private_reasoning_parts = ["agent hidden content-part reasoning", "agent hidden content-part thinking"]

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
            yield (
                b'data: {"choices":[{"delta":{"content":'
                b'[{"type":"reasoning","text":{"value":"agent hidden content-part reasoning"}},'
                b'{"type":"text","text":{"value":"agent content-part "}}]}}]}\n\n'
            )
            yield (
                b'data: {"choices":[{"message":{"role":"assistant","content":'
                b'[{"type":"thinking","text":{"value":"agent hidden content-part thinking"}},'
                b'{"type":"text","text":{"value":"stream output"}}]},"finish_reason":"stop"}]}\n\n'
            )
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Content Parts HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")
        projection = json.dumps({"run": run, "events": run_events}, ensure_ascii=False)

        assert run["status"] == "completed"
        assert run["result"] == "agent content-part stream output"
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == "agent content-part stream output"
        for private_reasoning in private_reasoning_parts:
            assert private_reasoning not in projection
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_persists_streaming_refusal_delta(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent refuses this request."
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"refusal":"Agent refuses "}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"refusal":"this request."},"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Refusal HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == expected
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_accepts_refusal_message_field(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent refusal from message field."
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None, stream=False):
        calls.append({"messages": messages, "tools": tools, "stream": stream})
        return {"role": "assistant", "content": None, "refusal": expected}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Refusal Message Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert calls and calls[0]["stream"] is True
        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_preserves_stream_stop_reason_as_finish_reason_in_run_events(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent provider stop metadata"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {"choices": [{"delta": {"content": "Agent provider stop "}, "stop_reason": None}]}
            yield {"choices": [{"delta": {"content": "metadata"}, "stop_reason": "stop"}]}

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Stop Reason Metadata Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        output_fact = next(event for event in run_events if event["event_type"] == "model.output.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert event_types.index("model.output.completed") < event_types.index("agent.run.completed")
        assert output_fact["payload"]["content"] == expected
        assert output_fact["payload"]["finish_reason"] == "stop"
        assert output_fact["payload"]["truncated"] is False
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_uses_responses_refusal_done_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent final Responses refusal"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.refusal.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "draft refusal",
            }
            yield {
                "type": "response.refusal.done",
                "output_index": 0,
                "content_index": 0,
                "refusal": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Refusal Done Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert "draft refusal" not in json.dumps({"run": run, "events": run_events}, ensure_ascii=False)
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_hides_streaming_reasoning_delta(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    private_reasoning = "agent provider hidden reasoning"
    expected = "Agent visible final answer"
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield (
                f'data: {{"choices":[{{"delta":{{"reasoning_content":"{private_reasoning}"}}}}]}}\n\n'.encode(
                    "utf-8"
                )
            )
            yield f'data: {{"choices":[{{"delta":{{"content":"{expected}"}}}}]}}\n\n'.encode("utf-8")
            yield b"data: [DONE]\n\n"

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Reasoning Delta HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")
        projection = json.dumps({"run": run, "events": run_events}, ensure_ascii=False)

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert len(requests) == 1
        assert completed_fact["payload"]["result"] == expected
        assert private_reasoning not in projection
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_rejects_reasoning_only_output_without_leaking(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    private_reasoning = "agent provider private reasoning only"
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None, stream=False):
        calls.append({"messages": messages, "tools": tools, "stream": stream})
        return {"role": "assistant", "content": None, "reasoning_content": private_reasoning}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Reasoning Only Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        projection = json.dumps({"run": run, "events": run_events}, ensure_ascii=False)

        assert calls and calls[0]["stream"] is True
        assert run["status"] == "failed"
        assert "空回复" in run["result"]
        event_types = [event["event_type"] for event in run_events]
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types
        assert "agent.tool.call" not in event_types
        assert private_reasoning not in projection
    finally:
        service.close()



def test_agent_run_redacts_http_sse_provider_error(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-agent-http-sse-provider-error123456"
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial agent output"}}]}\n\n'
            yield (
                "data: "
                + json.dumps(
                    {
                        "error": {
                            "message": f"agent provider stream rejected token={leaked_secret}",
                            "type": "rate_limit_error",
                            "code": "quota_exceeded",
                        }
                    }
                )
                + "\n\n"
            ).encode("utf-8")

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Provider Error HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        projection = json.dumps(
            {
                "run": run,
                "events": run_events,
                "run_group": service.get_run_group(run["run_group_id"]),
            },
            ensure_ascii=False,
        )
        event_types = [event["event_type"] for event in run_events]

        assert run["status"] == "failed"
        assert "rate_limit_error" in run["result"]
        assert "quota_exceeded" in run["result"]
        assert "[redacted]" in projection
        assert leaked_secret not in projection
        assert len(requests) == 1
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []



def test_agent_run_redacts_multiline_http_sse_provider_error(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    leaked_secret = "sk-agent-http-sse-multiline-error123456"
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial multiline agent output"}}]}\r\n\r\n'
            yield (
                b"id: agent-multiline-error-1\r\n"
                b"event: error\r\n"
                b'data: {"error":{\r\n'
                + f'data: "message":"agent provider stream rejected token={leaked_secret}",\r\n'.encode("utf-8")
                + b'data: "type":"rate_limit_error","code":"quota_exceeded"}}\r\n\r\n'
            )

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return FakeResponse()

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Multiline Provider Error HTTP SSE Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        projection = json.dumps(
            {
                "run": run,
                "events": run_events,
                "run_group": service.get_run_group(run["run_group_id"]),
            },
            ensure_ascii=False,
        )
        event_types = [event["event_type"] for event in run_events]

        assert run["status"] == "failed"
        assert "rate_limit_error" in run["result"]
        assert "quota_exceeded" in run["result"]
        assert "[redacted]" in projection
        assert leaked_secret not in projection
        assert len(requests) == 1
        assert "agent.run.failed" in event_types
        assert "agent.run.completed" not in event_types
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []



def test_agent_run_executes_http_sse_tool_call_and_continues(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent http sse tool content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            b": provider keepalive\n\n",
            b'event: ping\ndata: {"type":"ping"}\n\n',
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_agent_http_sse_read",
                                        "type": "function",
                                        "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            b'data: {"type":"heartbeat","created":123}\n\n',
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"name": "read", "arguments": 'ME.md"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            b"event: heartbeat\n"
            b'data: {"type":"heartbeat"}\n\n',
            event({"choices": [{"delta": {"content": "Agent HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "HTTP SSE Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_http_sse_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_agent_http_sse_read"
        assert "agent http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_split_http_sse_tool_call_chunks_and_continues(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent split http sse tool content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            b'"id":"call_agent_http_sse_split_chunk_read",',
            b'"type":"function","function":{"name":"workspace_read",',
            b'"arguments":"{\\"path\\": \\"README.md\\"}"}}]}}]}\n\n',
            event({"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Agent split HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Split HTTP SSE Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent split HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_http_sse_split_chunk_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_agent_http_sse_split_chunk_read"
        assert "agent split http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_singular_http_sse_tool_call_and_continues(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent singular http sse tool content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_call": {
                                    "index": 0,
                                    "id": "call_agent_http_sse_singular_read",
                                    "type": "function",
                                    "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                }
                            }
                        }
                    ]
                }
            ),
            event(
                {
                    "choices": [
                        {
                            "message": {
                                "tool_call": {
                                    "index": 0,
                                    "function": {"name": "read", "arguments": 'ME.md"}'},
                                }
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Agent singular HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Singular HTTP SSE Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent singular HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_http_sse_singular_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_agent_http_sse_singular_read"
        assert "agent singular http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_coalesces_indexless_interleaved_http_sse_tool_call_deltas_by_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent indexless readme content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("agent indexless notes content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_agent_indexless_readme",
                                        "type": "function",
                                        "function": {"name": "workspace_", "arguments": '{"path": "READ'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_agent_indexless_notes",
                                        "type": "function",
                                        "function": {"name": "workspace_", "arguments": '{"path": "NOT'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_agent_indexless_readme",
                                        "function": {"name": "read", "arguments": 'ME.md"}'},
                                    }
                                ]
                            }
                        }
                    ]
                }
            ),
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "call_agent_indexless_notes",
                                        "function": {"name": "read", "arguments": 'ES.md"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Agent indexless interleaved HTTP SSE complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Indexless Interleaved HTTP SSE Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README and NOTES"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_facts = [event for event in run_events if event["event_type"] == "agent.tool.call"]
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in requests[1]["body"]["messages"] if message.get("role") == "tool"]

        assert run["status"] == "completed"
        assert run["result"] == "Agent indexless interleaved HTTP SSE complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert [call["id"] for call in assistant_tool_messages[-1]["tool_calls"]] == [
            "call_agent_indexless_readme",
            "call_agent_indexless_notes",
        ]
        assert [call["function"]["arguments"] for call in assistant_tool_messages[-1]["tool_calls"]] == [
            '{"path": "README.md"}',
            '{"path": "NOTES.md"}',
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_agent_indexless_readme",
            "call_agent_indexless_notes",
        ]
        assert "agent indexless readme content" in tool_messages[0]["content"]
        assert "agent indexless notes content" in tool_messages[1]["content"]
        assert [event["payload"]["input_preview"]["path"] for event in tool_facts] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("agent.tool.call") == 2
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_http_sse_object_tool_call_arguments(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent http sse object arguments content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_agent_http_sse_object_args_read",
                                        "type": "function",
                                        "function": {
                                            "name": "workspace_read",
                                            "arguments": {"path": "README.md"},
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Agent HTTP SSE object arguments complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "HTTP SSE Object Arguments Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent HTTP SSE object arguments complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_http_sse_object_args_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_agent_http_sse_object_args_read"
        assert "agent http sse object arguments content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_message_level_http_sse_tool_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent message-level http sse tool content", encoding="utf-8")
    requests = []

    class FakeResponse:
        def __init__(self, lines):
            self._lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            for line in self._lines:
                yield line

    def event(payload: dict) -> bytes:
        return f"data: {json.dumps(payload)}\n\n".encode("utf-8")

    first_response = FakeResponse(
        [
            event(
                {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {
                                        "id": "call_agent_http_sse_message_read",
                                        "type": "function",
                                        "function": {
                                            "name": "workspace_read",
                                            "arguments": '{"path": "README.md"}',
                                        },
                                    }
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ),
            b"data: [DONE]\n\n",
        ]
    )
    second_response = FakeResponse(
        [
            event({"choices": [{"delta": {"content": "Agent message-level HTTP SSE tool call complete"}}]}),
            b"data: [DONE]\n\n",
        ]
    )
    responses = [first_response, second_response]

    def fake_urlopen(request, **kwargs):
        body = json.loads(request.data.decode("utf-8"))
        requests.append({"request": request, "body": body, "kwargs": kwargs})
        assert request.full_url == "https://api.example.test/v1/chat/completions"
        assert request.get_header("Accept") == "text/event-stream"
        assert body["stream"] is True
        return responses.pop(0)

    monkeypatch.setattr("apps.core.tls.urlrequest.urlopen", fake_urlopen)
    try:
        agent = service.create_agent(
            {
                "name": "Message HTTP SSE Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")
        assistant_tool_messages = [
            message
            for message in requests[1]["body"]["messages"]
            if message.get("role") == "assistant" and message.get("tool_calls")
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent message-level HTTP SSE tool call complete"
        assert len(requests) == 2
        assert requests[0]["body"]["tools"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_http_sse_message_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert requests[1]["body"]["messages"][-1]["role"] == "tool"
        assert requests[1]["body"]["messages"][-1]["tool_call_id"] == "call_agent_http_sse_message_read"
        assert "agent message-level http sse tool content" in requests[1]["body"]["messages"][-1]["content"]
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_executes_legacy_streaming_function_call(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent legacy function call content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                function_call=SimpleNamespace(
                                    name="workspace_",
                                    arguments='{"path": "READ',
                                )
                            )
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(
                                function_call=SimpleNamespace(
                                    name="read",
                                    arguments='ME.md"}',
                                )
                            ),
                            finish_reason="function_call",
                        )
                    ]
                )

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["name"] == "workspace_read"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == assistant_tool_messages[-1]["tool_calls"][0]["id"]
        assert "agent legacy function call content" in messages[-1]["content"]
        return {"content": "Agent legacy streaming function call complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Legacy Function Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_fact = next(event for event in run_events if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent legacy streaming function call complete"
        assert len(calls) == 2
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert tool_fact["payload"]["result"]["ok"] is True
        assert event_types.count("agent.tool.call") == 1
        assert event_types.count("agent.run.completed") == 1
        assert not any(str(event_type).endswith(".delta") for event_type in event_types)
    finally:
        service.close()



def test_agent_run_uses_responses_call_id_without_item_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent responses call id content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": "call_agent_response_only",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_response_only"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_agent_response_only"
        assert "agent responses call id content" in messages[-1]["content"]
        return {"content": "Agent Responses call id complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Call ID Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        tool_fact = next(event for event in provider_transport_events(service, run["run_id"])["events"] if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent Responses call id complete"
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert len(calls) == 2
    finally:
        service.close()



def test_agent_run_prefers_responses_call_id_over_item_id(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent responses item id content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "type": "response.output_item.added",
                    "item": {
                        "id": "fc_agent_response_item",
                        "type": "function_call",
                        "call_id": "call_agent_response_item",
                        "name": "workspace_read",
                        "arguments": "",
                    },
                }
                yield {
                    "type": "response.function_call_arguments.delta",
                    "item_id": "fc_agent_response_item",
                    "delta": '{"path": "READ',
                }
                yield {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_agent_response_item",
                    "arguments": '{"path": "README.md"}',
                }
                yield {
                    "type": "response.output_item.done",
                    "item": {
                        "id": "fc_agent_response_item",
                        "type": "function_call",
                        "call_id": "call_agent_response_item",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        assert assistant_tool_messages[-1]["content"] is None
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] == "call_agent_response_item"
        assert assistant_tool_messages[-1]["tool_calls"][0]["id"] != "fc_agent_response_item"
        assert assistant_tool_messages[-1]["tool_calls"][0]["function"]["arguments"] == '{"path": "README.md"}'
        assert messages[-1]["role"] == "tool"
        assert messages[-1]["tool_call_id"] == "call_agent_response_item"
        assert messages[-1]["tool_call_id"] != "fc_agent_response_item"
        assert "agent responses item id content" in messages[-1]["content"]
        return {"content": "Agent Responses item id complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Item ID Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README"})
        tool_fact = next(event for event in provider_transport_events(service, run["run_id"])["events"] if event["event_type"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Agent Responses item id complete"
        assert tool_fact["payload"]["tool"] == "workspace.read"
        assert tool_fact["payload"]["input_preview"]["path"] == "README.md"
        assert len(calls) == 2
    finally:
        service.close()



def test_agent_run_executes_multiple_responses_tool_calls(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent responses README content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("agent responses NOTES content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_agent_response_readme",
                        "type": "function_call",
                        "call_id": "call_agent_response_readme",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "id": "fc_agent_response_notes",
                        "type": "function_call",
                        "call_id": "call_agent_response_notes",
                        "name": "workspace_read",
                        "arguments": '{"path": "NOTES.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert assistant_tool_messages[-1]["content"] is None
        assert [tool_call["id"] for tool_call in assistant_tool_messages[-1]["tool_calls"]] == [
            "call_agent_response_readme",
            "call_agent_response_notes",
        ]
        assert [tool_call["function"]["arguments"] for tool_call in assistant_tool_messages[-1]["tool_calls"]] == [
            '{"path": "README.md"}',
            '{"path": "NOTES.md"}',
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_agent_response_readme",
            "call_agent_response_notes",
        ]
        assert "agent responses README content" in tool_messages[0]["content"]
        assert "agent responses NOTES content" in tool_messages[1]["content"]
        return {"content": "Agent Responses multiple tool calls complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Multi Tool Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README and NOTES"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        tool_facts = [event for event in run_events if event["event_type"] == "agent.tool.call"]

        assert run["status"] == "completed"
        assert run["result"] == "Agent Responses multiple tool calls complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_facts] == [
            "README.md",
            "NOTES.md",
        ]
        assert event_types.count("agent.tool.call") == 2
        assert event_types.count("agent.run.completed") == 1
        assert len(calls) == 2
    finally:
        service.close()



def test_agent_run_preserves_responses_zero_output_index(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("agent zero output index README content", encoding="utf-8")
    (workdir / "NOTES.md").write_text("agent zero output index NOTES content", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_read" for tool in tools or [])

            def stream():
                yield {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "fc_agent_response_zero",
                        "index": 1,
                        "type": "function_call",
                        "call_id": "call_agent_response_zero",
                        "name": "workspace_read",
                        "arguments": '{"path": "README.md"}',
                    },
                }
                yield {
                    "type": "response.output_item.done",
                    "output_index": 1,
                    "item": {
                        "id": "fc_agent_response_one",
                        "index": 0,
                        "type": "function_call",
                        "call_id": "call_agent_response_one",
                        "name": "workspace_read",
                        "arguments": '{"path": "NOTES.md"}',
                    },
                }
                yield {
                    "type": "response.completed",
                    "response": {
                        "status": "completed",
                        "output": [{"type": "function_call", "finish_reason": "tool_calls"}],
                    },
                }

            return stream()
        assistant_tool_messages = [
            message for message in messages if message.get("role") == "assistant" and message.get("tool_calls")
        ]
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert [tool_call["id"] for tool_call in assistant_tool_messages[-1]["tool_calls"]] == [
            "call_agent_response_zero",
            "call_agent_response_one",
        ]
        assert [message["tool_call_id"] for message in tool_messages] == [
            "call_agent_response_zero",
            "call_agent_response_one",
        ]
        assert "agent zero output index README content" in tool_messages[0]["content"]
        assert "agent zero output index NOTES content" in tool_messages[1]["content"]
        return {"content": "Agent Responses zero output index complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Zero Output Index Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read README and NOTES"})
        tool_facts = [
            event for event in provider_transport_events(service, run["run_id"])["events"] if event["event_type"] == "agent.tool.call"
        ]

        assert run["status"] == "completed"
        assert run["result"] == "Agent Responses zero output index complete"
        assert [event["payload"]["input_preview"]["path"] for event in tool_facts] == [
            "README.md",
            "NOTES.md",
        ]
        assert len(calls) == 2
    finally:
        service.close()



def test_agent_run_uses_responses_output_text_done_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent final Responses snapshot"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "draft ",
            }
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "partial",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Output Text Snapshot Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert "draft partial" not in json.dumps({"run": run, "events": run_events}, ensure_ascii=False)
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_uses_responses_output_text_done_list_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent final Responses list\nsnapshot"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": "agent draft value that should be replaced",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": [
                    {"type": "output_text", "text": "Agent final Responses list"},
                    {"type": "output_text", "text": {"value": "snapshot"}},
                ],
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Output Text List Snapshot Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert "agent draft value" not in json.dumps({"run": run, "events": run_events}, ensure_ascii=False)
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_uses_responses_output_item_message_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent final message item snapshot"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "msg_agent_response",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": expected}],
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Output Item Snapshot Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_uses_responses_content_part_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent final content part snapshot"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.content_part.done",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": expected},
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Content Part Snapshot Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        completed_fact = next(event for event in run_events if event["event_type"] == "agent.run.completed")

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert completed_fact["payload"]["result"] == expected
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_discards_responses_reasoning_summary_stream(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent visible Responses answer"
    private_reasoning = "agent private Responses reasoning summary"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.reasoning_summary_text.delta",
                "output_index": 0,
                "summary_index": 0,
                "delta": f"{private_reasoning} draft",
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 0,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.reasoning_summary_text.done",
                "output_index": 0,
                "summary_index": 0,
                "text": private_reasoning,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Reasoning Summary Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        run_events_json = json.dumps(run_events, ensure_ascii=False)
        timeline_json = json.dumps(run["timeline"], ensure_ascii=False)

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert private_reasoning not in run["result"]
        assert private_reasoning not in timeline_json
        assert private_reasoning not in run_events_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_run_discards_responses_reasoning_list_snapshot(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    expected = "Agent visible answer after private reasoning list"
    private_reasoning = "agent private list reasoning summary"

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None, stream=False):
        assert tools is not None
        assert stream is True

        def stream():
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "rs_agent_private_reasoning",
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "agent private list reasoning"},
                        {"type": "summary_text", "text": {"value": "summary"}},
                    ],
                },
            }
            yield {
                "type": "response.output_text.done",
                "output_index": 1,
                "content_index": 0,
                "text": expected,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "status": "completed",
                    "output": [{"type": "message", "finish_reason": "stop"}],
                },
            }

        return stream()

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Responses Reasoning List Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Summarize the current conversation"})
        run_events = provider_transport_events(service, run["run_id"])["events"]
        run_events_json = json.dumps(run_events, ensure_ascii=False)
        timeline_json = json.dumps(run["timeline"], ensure_ascii=False)

        assert run["status"] == "completed"
        assert run["result"] == expected
        assert private_reasoning not in run["result"]
        assert private_reasoning not in timeline_json
        assert private_reasoning not in run_events_json
        assert not any(str(event["event_type"]).endswith(".delta") for event in run_events)
    finally:
        service.close()



def test_agent_tool_output_is_truncated_by_runtime_budget(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_tool_output_chars=30)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "long.txt").write_text("x" * 120, encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "long.txt"})},
                    }
                ],
            }
        assert "[truncated]" in messages[-1]["content"]
        assert "x" * 60 not in messages[-1]["content"]
        return {"content": "Read truncated"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Truncating Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read long file"})
        tool_event = next(event for event in run["timeline"] if event["event"] == "agent.tool.call")

        assert run["status"] == "completed"
        assert run["result"] == "Read truncated"
        assert tool_event["result"]["truncated"] is True
        assert len(tool_event["result"]["content"]) <= 30
    finally:
        service.close()


def test_agent_run_fails_when_tool_call_budget_is_exceeded(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_tool_calls=1)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_list_1",
                    "type": "function",
                    "function": {"name": "workspace_list", "arguments": json.dumps({"path": "."})},
                },
                {
                    "id": "call_list_2",
                    "type": "function",
                    "function": {"name": "workspace_list", "arguments": json.dumps({"path": "."})},
                },
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Budgeted Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.list"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "List twice"})

        assert run["status"] == "failed"
        assert "max_tool_calls=1" in run["result"]
        tool_events = [event for event in run["timeline"] if event["event"] == "agent.tool.call"]
        assert len(tool_events) == 1
    finally:
        service.close()


def test_agent_run_fails_when_model_call_budget_is_exceeded(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_model_calls=1)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_list",
                    "type": "function",
                    "function": {"name": "workspace_list", "arguments": json.dumps({"path": "."})},
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Model Budget Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.list"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Loop once"})

        assert run["status"] == "failed"
        assert "max_model_calls=1" in run["result"]
        assert len(calls) == 1
    finally:
        service.close()


def test_agent_run_fails_when_run_duration_budget_is_exceeded(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_run_duration_seconds=1)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    clock = {"now": 1000.0}
    calls = []

    def fake_time():
        return clock["now"]

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        clock["now"] = 1002.0
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_list",
                    "type": "function",
                    "function": {"name": "workspace_list", "arguments": json.dumps({"path": "."})},
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.time.time", fake_time)
    monkeypatch.setattr("apps.shell.agent.runtime.budget.time.time", fake_time)
    monkeypatch.setattr("apps.shell.agent_runtime._iso_epoch", lambda _value: 1000.0)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    from apps.shell.agent.runtime.budget import run_budget_from_timeline

    def fake_run_budget(_run_id, timeline):
        return run_budget_from_timeline(
            service.runtime_limits,
            started_at_epoch=1000.0,
            timeline=timeline,
        )

    service.runtime_run_budget = fake_run_budget
    service.custom_api_agent_loop._run_budget = fake_run_budget
    try:
        agent = service.create_agent(
            {
                "name": "Duration Budget Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.list"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "List until timeout"})

        assert run["status"] == "failed"
        assert "max_run_duration_seconds=1" in run["result"]
        assert len(calls) == 1
        assert [event["event"] for event in run["timeline"]].count("agent.model.response") == 1
        assert [event["event"] for event in run["timeline"]].count("agent.tool.call") == 0
    finally:
        service.close()


def test_agent_run_fails_when_terminal_budget_is_exceeded_after_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_terminal_calls=0)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {"name": "terminal_run", "arguments": json.dumps({"command": "printf blocked"})},
                }
            ],
        },
    )
    try:
        agent = service.create_agent(
            {
                "name": "Terminal Budget Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run command"})
        resumed = service.approve_run_approval(run["run_id"])

        assert run["status"] == "approval_required"
        assert resumed["status"] == "failed"
        assert "max_terminal_calls=0" in resumed["result"]
    finally:
        service.close()


def test_agent_run_can_recover_from_workspace_tool_shape_error(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("hello", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_bad_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": "."})},
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "user"
            assert "Tool result for workspace.list" in messages[-1]["content"]
            assert "README.md" in messages[-1]["content"]
            return {"content": "Recovered and listed files"}
        raise AssertionError("workspace shape recovery should complete in one recovery turn")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Recovering Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read", "workspace.list"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Inspect repo"})

        assert run["status"] == "completed"
        assert run["result"] == "Recovered and listed files"
        tool_results = [
            event["result"]
            for event in run["timeline"]
            if event["event"] == "agent.tool.call" and isinstance(event.get("result"), dict)
        ]
        assert tool_results[0]["ok"] is False
        assert tool_results[0]["suggested_tool"] == "workspace.list"
        assert tool_results[1]["ok"] is True
    finally:
        service.close()


def test_agent_run_recovers_from_absolute_workspace_path_with_terminal(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    external_file = tmp_path / "external.txt"
    external_file.write_text("outside workspace", encoding="utf-8")
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert "Never pass absolute paths to workspace tools" in messages[0]["content"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_bad_read",
                        "type": "function",
                        "function": {"name": "workspace_read", "arguments": json.dumps({"path": str(external_file)})},
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "tool"
            assert "suggested_tool" in messages[-1]["content"]
            assert "terminal.run" in messages[-1]["content"]
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {"name": "terminal_run", "arguments": json.dumps({"command": f"cat {external_file}"})},
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "outside workspace" in messages[-1]["content"]
        return {"content": "Recovered with terminal"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "External Path Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read", "terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Read the external file"})

        assert run["status"] == "approval_required"
        workspace_event = next(
            event
            for event in run["timeline"]
            if event["event"] == "agent.tool.call" and event["detail"] == "workspace.read"
        )
        assert workspace_event["result"]["ok"] is False
        assert workspace_event["result"]["suggested_tool"] == "terminal.run"

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "completed"
        assert resumed["result"] == "Recovered with terminal"
    finally:
        service.close()


def test_agent_tool_loop_limit_includes_last_tool_detail(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": f"call_bad_read_{len(calls)}",
                    "type": "function",
                    "function": {"name": "workspace_read", "arguments": json.dumps({"path": "."})},
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Looping Reader",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read", "workspace.list"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Inspect repo"})

        assert run["status"] == "failed"
        assert "工具循环超过上限" in run["result"]
        assert "最后一次工具调用：workspace.read" in run["result"]
        assert "建议工具：workspace.list" in run["result"]
    finally:
        service.close()


def test_custom_api_agent_normalizes_invalid_start_iteration(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls: list[list[dict[str, object]]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {"content": "normalized start iteration"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        created = service.create_agent(
            {
                "name": "Direct Resume Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": []},
                "workspace_policy": {"default_workdir": str(tmp_path), "readable_scopes": ["."]},
            }
        )
        agent = service._get_agent_private(created["agent_id"])
        broker = ToolBroker(
            {"default_workdir": str(tmp_path), "readable_scopes": ["."]},
            service.agent_artifacts_dir / "direct-resume",
        )

        result = service._run_custom_api_agent(
            agent,
            "Summarize the current conversation",
            broker,
            [],
            [],
            start_iteration="not-an-int",
            run_id=service.start_main_chat_run(
                task_id="direct-resume-task", session_id="direct-resume-session",
                user_goal="Summarize the current conversation",
            )["run_id"],
            original_goal="Summarize the current conversation",
        )

        assert result == "normalized start iteration"
        assert len(calls) == 1
    finally:
        service.close()


def test_agent_tool_loop_limit_after_artifact_write_completes_with_artifact(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {
            "content": json.dumps(
                {
                    "action": "tool",
                    "tool": "artifact.write",
                    "input": {"path": "done.md", "content": "done"},
                }
            )
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Looping Writer",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["artifact.write"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Write artifact"})

        assert run["status"] == "completed"
        assert "模型在工具循环上限前没有返回最终总结" in run["result"]
        assert "done.md" in run["result"]
        assert any(artifact.get("path") == "done.md" for artifact in run["artifacts"])
        assert service.read_run_artifact(run["run_id"], "done.md")["content"] == "done"
        assert any(event["event"] == "agent.tool.loop_limit_completed" for event in run["timeline"])
        assert len(calls) == 50
    finally:
        service.close()


def test_artifact_write_redacts_file_content_and_passes_secret_scan(tmp_path):
    artifact_root = tmp_path / "artifacts"
    broker = ToolBroker({}, artifact_root)

    result = broker.artifact_write("reports/secret-report.md", "api_key=sk-artifact-secret123456\nsafe")
    artifact_path = artifact_root / "reports" / "secret-report.md"
    content = artifact_path.read_text(encoding="utf-8")

    assert result["ok"] is True
    assert "sk-artifact-secret123456" not in content
    assert "api_key=[redacted]" in content
    assert verify_secret_redaction(paths=[artifact_root]) == []


def test_agent_run_json_fallback_writes_artifact(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": json.dumps({"action": "tool", "tool": "artifact.write", "input": {"path": "notes.md", "content": "hello"}})}
        assert "Tool result for artifact.write" in messages[-1]["content"]
        return {"content": "Artifact done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Artifact Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["artifact.write"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Write artifact"})

        assert run["status"] == "completed"
        assert any(artifact.get("path") == "notes.md" for artifact in run["artifacts"])
        assert service.read_run_artifact(run["run_id"], "notes.md")["content"] == "hello"
    finally:
        service.close()


def test_agent_output_contract_expands_diff_rules_in_runtime_context(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        return {"content": "Inline code response"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Diff Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.write_patch", "artifact.write"]},
                "output_contract": "diff",
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Show a tiny function without changing files"})

        assert run["status"] == "completed"
        assert calls
        system_prompt = calls[0]["messages"][0]["content"]
        context = calls[0]["messages"][1]["content"]
        assert "Market-grade Agent operating doctrine" in system_prompt
        assert "Treat Skills as task manuals and tools as external actions" in system_prompt
        assert "Do not request a tool solely because of the output contract" in system_prompt
        assert "If the user asks not to create, save, write, or modify files" in system_prompt
        assert "If the user asks not to run or execute commands" in system_prompt
        assert "Contract: diff" in context
        assert "Do not call workspace.write_patch merely because the output contract is diff" in context
        assert "If no file change is requested, provide code inline." in context
    finally:
        service.close()


def test_agent_run_skips_write_tool_when_user_goal_forbids_file_changes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append({"messages": messages, "tools": tools})
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_write",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "scripts/demo.py",
                                    "patch": "--- scripts/demo.py\n+++ scripts/demo.py\n@@ -1 +1 @@\n-print('old')\n+print('demo')\n",
                                }
                            ),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        tool_result = json.loads(messages[-1]["content"])
        assert tool_result["blocked_by_user_goal"] is True
        assert tool_result["tool"] == "workspace.write_patch"
        assert "inline" in tool_result["hint"]
        return {"content": "Here is the code inline."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "No File Writer",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.write_patch", "artifact.write"]},
                "output_contract": "diff",
            }
        )
        run = service.create_agent_run({
            "agent_id": agent["agent_id"],
            "user_goal": "Show a tiny function without changing files",
        })

        assert run["status"] == "completed"
        assert run["result"] == "Here is the code inline."
        assert run["pending_approval"] == {}
        skipped_event = next(event for event in run["timeline"] if event["event"] == "agent.tool.skipped" and event["detail"] == "workspace.write_patch")
        assert skipped_event["input_preview"]["path"] == "scripts/demo.py"
        assert skipped_event["result"]["blocked_by_user_goal"] is True
        assert not any(event["event"] == "agent.tool.approval_required" for event in run["timeline"])
    finally:
        service.close()


def test_agent_run_skips_artifact_tool_when_chinese_goal_says_inline_only(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": json.dumps({"action": "tool", "tool": "artifact.write", "input": {"path": "card.html", "content": "<div>card</div>"}})}
        assert "blocked_by_user_goal" in messages[-1]["content"]
        assert "inline" in messages[-1]["content"]
        return {"content": "完整代码如下：<div>card</div>"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Inline Design Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["artifact.write"]},
                "output_contract": "artifacts",
            }
        )
        run = service.create_agent_run({
            "agent_id": agent["agent_id"],
            "user_goal": "用纯 HTML + CSS 制作一个简单卡片组件，代码完整展示即可。",
        })

        assert run["status"] == "completed"
        assert run["result"] == "完整代码如下：<div>card</div>"
        assert not any(artifact.get("path") == "card.html" for artifact in run["artifacts"])
        assert not any(artifact.get("kind") == "tool_artifact" for artifact in run["artifacts"])
        assert run["pending_approval"] == {}
        assert any(event["event"] == "agent.tool.skipped" and event["detail"] == "artifact.write" for event in run["timeline"])
    finally:
        service.close()


def test_workflow_child_agent_no_run_goal_does_not_request_terminal_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {"name": "terminal_run", "arguments": json.dumps({"command": "python3 demo.py"})},
                    }
                ],
            }
        assert "blocked_by_user_goal" in messages[-1]["content"]
        assert "不要运行命令或脚本" in messages[-1]["content"]
        return {"content": "代码示例已经 inline 展示。"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "No Run Coding Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = service.create_workflow(
            {
                "name": "No Run Workflow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "code", "type": "agent", "data": {"label": "Code", "agent_id": agent["agent_id"]}},
                ],
                "edges": [{"source": "start", "target": "code"}],
            }
        )
        run = service.create_workflow_run({
            "workflow_id": workflow["workflow_id"],
            "user_goal": "写一个 Python 示例即可，不需要运行命令或脚本。",
        })

        assert run["status"] == "completed"
        assert run["result"] == "代码示例已经 inline 展示。"
        assert run["pending_approval"] == {}
        assert service.get_run_group(run["run_group_id"])["status"] == "completed"
        child_run_id = next(run_id for run_id in service.get_run_group(run["run_group_id"])["child_run_ids"] if run_id != run["run_id"])
        child = service.get_run(child_run_id)
        assert child["status"] == "completed"
        assert child["pending_approval"] == {}
        assert any(event["event"] == "agent.tool.skipped" and event["detail"] == "terminal.run" for event in child["timeline"])
        assert not any(event["event"] == "agent.tool.approval_required" for event in child["timeline"])
    finally:
        service.close()


def test_agent_run_explicit_terminal_goal_not_blocked_by_downstream_no_execute_text(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf terminal-explicit-smoke; exit 7", "shell": True}),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Explicit Terminal Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({
            "agent_id": agent["agent_id"],
            "user_goal": "必须请求 terminal.run 执行命令。不要执行后续 artifact 节点，只使用 terminal.run。",
        })

        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == "terminal.run"
        assert not any(event["event"] == "agent.tool.skipped" and event["detail"] == "terminal.run" for event in run["timeline"])
    finally:
        service.close()


def test_agent_run_denies_unallowed_tool(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {"name": "terminal_run", "arguments": json.dumps({"command": "echo no"})},
                }
            ],
        },
    )
    try:
        agent = service.create_agent(
            {
                "name": "Denied Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.read"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run command"})

        assert run["status"] == "failed"
        assert "未授权工具" in run["result"]
        assert any(event["event"] == "agent.tool.denied" for event in run["timeline"])
    finally:
        service.close()


def test_workflow_parent_records_child_agent_artifact_refs(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": json.dumps({"action": "tool", "tool": "artifact.write", "input": {"path": "design.md", "content": "design artifact"}})}
        if len(calls) == 2:
            assert "Tool result for artifact.write" in messages[-1]["content"]
            return {"content": "Design done"}
        return {"content": "Code done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {
                "name": "Design Artifact Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["artifact.write"]},
            }
        )
        coding_agent = service.create_agent(
            {
                "name": "Coding Summary Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Artifact Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "design", "type": "agent", "data": {"label": "Design", "agent_id": design_agent["agent_id"]}},
                    {"id": "code", "type": "agent", "data": {"label": "Code", "agent_id": coding_agent["agent_id"]}},
                ],
                "edges": [
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "code"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Ship artifacts"})

        assert run["status"] == "completed"
        child_artifact_refs = [
            artifact
            for artifact in run["artifacts"]
            if artifact.get("kind") == "workflow_child_artifact"
        ]
        assert all(artifact.get("artifact_kind") != "context" for artifact in child_artifact_refs)
        design_ref = next(artifact for artifact in child_artifact_refs if artifact.get("path") == "design.md")
        assert design_ref["workflow_step_label"] == "Design"
        assert design_ref["source_runnable_name"] == "Design Artifact Agent"
        assert service.read_run_artifact(design_ref["source_run_id"], "design.md")["content"] == "design artifact"
        design_event = next(
            event
            for event in run["timeline"]
            if event["event"] == "workflow.node.agent" and event["detail"] == "Design"
        )
        assert design_event["status"] == "completed"
        assert design_event["result"] == "Design done"
        assert design_event["artifact_count"] >= 1
    finally:
        service.close()


def test_agent_run_pauses_for_terminal_approval_and_resumes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf approved"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "approved" in messages[-1]["content"]
        return {"content": "Command complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        resume_contexts: list[ToolApprovalResumeContext] = []
        original_resume = service.approval_resume.execute_approved_tool

        def spy_resume(context: ToolApprovalResumeContext) -> None:
            resume_contexts.append(context)
            original_resume(context)

        monkeypatch.setattr(service.approval_resume, "execute_approved_tool", spy_resume)
        agent = service.create_agent(
            {
                "name": "Terminal Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run command"})

        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == "terminal.run"
        assert "messages" not in run["pending_approval"]
        approval_row = service._conn.execute(
            "SELECT status, tool, input_preview_json, payload_json FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert approval_row is not None
        assert approval_row["status"] == "pending"
        assert approval_row["tool"] == "terminal.run"
        assert json.loads(approval_row["input_preview_json"])["command"] == "printf approved"
        assert "messages" not in json.loads(approval_row["payload_json"])
        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "completed"
        assert resumed["result"] == "Command complete"
        assert len(resume_contexts) == 1
        assert resume_contexts[0].run_id == run["run_id"]
        assert resume_contexts[0].tool_name == "terminal.run"
        assert resume_contexts[0].input_preview["command"] == "printf approved"
        approval_after = service._conn.execute(
            "SELECT status, resolved_at FROM run_approvals WHERE run_id=?",
            (run["run_id"],),
        ).fetchone()
        assert approval_after is not None
        assert approval_after["status"] == "approved"
        assert approval_after["resolved_at"]
        approved_event = next(event for event in resumed["timeline"] if event["event"] == "agent.tool.approval_approved")
        assert approved_event["detail"] == "terminal.run"
        assert approved_event["input_preview"]["command"] == "printf approved"
        assert approved_event["status"] == "completed"
        run_events = service.list_run_events(run["run_id"])["events"]
        event_types = [event["event_type"] for event in run_events]
        assert "agent.tool.approval_required" in event_types
        assert "agent.tool.approval_approved" in event_types
        approved_fact = next(event for event in run_events if event["event_type"] == "agent.tool.approval_approved")
        assert approved_fact["payload"]["tool"] == "terminal.run"
        assert approved_fact["payload"]["input_preview"]["command"] == "printf approved"
        tool_facts = [event for event in run_events if event["event_type"] == "agent.tool.call"]
        assert tool_facts[-1]["payload"]["tool"] == "terminal.run"
        assert tool_facts[-1]["payload"]["approved"] is True
        assert service.get_run_group(resumed["run_group_id"])["status"] == "completed"
    finally:
        service.close()


def test_agent_run_approval_uses_resume_coordinator_claim_boundary(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    fixture_actions = [('terminal_run', {'command': 'printf approved'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf approved"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        return {"content": "Claim boundary complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        claim_calls: list[dict[str, object]] = []
        resume_step_calls: list[dict[str, object]] = []
        running_projection_calls: list[dict[str, object]] = []
        completed_projection_calls: list[dict[str, object]] = []
        projection_calls: list[dict[str, object]] = []
        original_resume_approved_tool_run = service._resume_approved_tool_run
        original_claim = service.approval_resume.claim_and_project_approved_tool
        original_running_projection = service._project_agent_approval_resume_running
        original_completed_projection = service._project_agent_approval_resume_completed
        original_projection = service._project_child_run_transition

        def spy_resume_approved_tool_run(**kwargs):
            resume_step_calls.append(
                {
                    "run_id": kwargs.get("run_id"),
                    "tool": kwargs.get("pending", {}).get("tool"),
                    "context_run_id": kwargs.get("resume_context").run_id,
                    "context_tool_name": kwargs.get("resume_context").tool_name,
                    "resumed_detail": kwargs.get("resumed_detail"),
                    "running_result": kwargs.get("running_result"),
                    "has_running_projection": kwargs.get("project_running") is not None,
                    "has_required_projection": kwargs.get("project_required") is not None,
                    "has_result_projection": kwargs.get("project_result") is not None,
                }
            )
            return original_resume_approved_tool_run(**kwargs)

        def spy_claim(run_id, pending, context, **kwargs):
            claim_calls.append(
                {
                    "run_id": run_id,
                    "tool": pending.get("tool"),
                    "context_run_id": context.run_id,
                    "context_tool_name": context.tool_name,
                    "resumed_detail": kwargs.get("resumed_detail"),
                    "running_result": kwargs.get("running_result"),
                }
            )
            return original_claim(run_id, pending, context, **kwargs)

        def spy_project_agent_approval_resume_running(running):
            running_projection_calls.append(
                {
                    "run_id": running.get("run_id"),
                    "kind": running.get("kind"),
                    "status": running.get("status"),
                    "result": running.get("result"),
                }
            )
            return original_running_projection(running)

        def spy_project_agent_approval_resume_completed(context, result_text):
            completed_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": context.tool_name,
                    "result_text": result_text,
                }
            )
            return original_completed_projection(context, result_text)

        def spy_project_child_run_transition(result):
            projection_calls.append(
                {
                    "run_id": result.get("run_id"),
                    "kind": result.get("kind"),
                    "status": result.get("status"),
                }
            )
            return original_projection(result)

        monkeypatch.setattr(service, "_resume_approved_tool_run", spy_resume_approved_tool_run)
        monkeypatch.setattr(service.approval_resume, "claim_and_project_approved_tool", spy_claim)
        monkeypatch.setattr(
            service,
            "_project_agent_approval_resume_running",
            spy_project_agent_approval_resume_running,
        )
        monkeypatch.setattr(
            service,
            "_project_agent_approval_resume_completed",
            spy_project_agent_approval_resume_completed,
        )
        monkeypatch.setattr(service, "_project_child_run_transition", spy_project_child_run_transition)
        agent = service.create_agent(
            {
                "name": "Claim Boundary Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": fixture_goal,
                                        "runtime_execution_envelope": envelope})

        assert run["status"] == "approval_required"

        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "completed"
        assert "printf approved" in resumed["result"]
        assert "输出：approved" in resumed["result"]
        assert resume_step_calls == [
            {
                "run_id": run["run_id"],
                "tool": "terminal.run",
                "context_run_id": run["run_id"],
                "context_tool_name": "terminal.run",
                "resumed_detail": "Agent resumed after approval",
                "running_result": "已批准，Agent 正在继续执行",
                "has_running_projection": True,
                "has_required_projection": False,
                "has_result_projection": True,
            }
        ]
        assert claim_calls == [
            {
                "run_id": run["run_id"],
                "tool": "terminal.run",
                "context_run_id": run["run_id"],
                "context_tool_name": "terminal.run",
                "resumed_detail": "Agent resumed after approval",
                "running_result": "已批准，Agent 正在继续执行",
            }
        ]
        assert running_projection_calls == [
            {
                "run_id": run["run_id"],
                "kind": "agent_run",
                "status": "running",
                "result": "已批准，Agent 正在继续执行",
            }
        ]
        assert completed_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "terminal.run",
                "result_text": resumed["result"],
            }
        ]
        assert projection_calls == [
            {"run_id": run["run_id"], "kind": "agent_run", "status": "completed"}
        ]
    finally:
        service.close()



def test_agent_run_consecutive_terminal_approvals_update_pending_request(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    fixture_actions = [('terminal_run', {'command': 'printf first-approved'}), ('terminal_run', {'command': 'printf second-approved'})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_first_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf first-approved"}),
                        },
                    },
                    {
                        "id": "call_second_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf second-approved"}),
                        },
                    },
                ],
            }
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert any("first-approved" in message.get("content", "") for message in tool_messages)
        assert any("second-approved" in message.get("content", "") for message in tool_messages)
        return {"content": "Both terminal approvals completed"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        required_projection_calls: list[dict[str, object]] = []
        original_required_projection = service._project_approval_resume_required

        def spy_project_approval_resume_required(context, pending_approval):
            required_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": pending_approval.get("tool"),
                    "command": (pending_approval.get("input_preview") or {}).get("command"),
                }
            )
            return original_required_projection(context, pending_approval)

        monkeypatch.setattr(
            service,
            "_project_approval_resume_required",
            spy_project_approval_resume_required,
        )
        agent = service.create_agent(
            {
                "name": "Consecutive Terminal Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": fixture_goal,
                                        "runtime_execution_envelope": envelope})

        assert run["status"] == "approval_required"
        assert run["pending_approval"]["tool"] == "terminal.run"
        assert run["pending_approval"]["input_preview"]["command"] == "printf first-approved"

        after_first = service.approve_run_approval(run["run_id"])
        assert after_first["status"] == "approval_required"
        assert after_first["result"] == "等待审批：terminal.run"
        assert after_first["pending_approval"]["tool"] == "terminal.run"
        assert after_first["pending_approval"]["input_preview"]["command"] == "printf second-approved"
        assert required_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "terminal.run",
                "command": "printf second-approved",
            }
        ]
        assert len(calls) == 1

        after_second = service.approve_run_approval(run["run_id"])
        assert after_second["status"] == "completed"
        assert "first-approved" in after_second["result"]
        assert "second-approved" in after_second["result"]
        assert after_second["pending_approval"] == {}
        assert len(calls) == 1

        approved_events = [event for event in after_second["timeline"] if event["event"] == "agent.tool.approval_approved"]
        assert [event["input_preview"]["command"] for event in approved_events] == [
            "printf first-approved",
            "printf second-approved",
        ]
        assert service.get_run_group(after_second["run_group_id"])["status"] == "completed"
    finally:
        service.close()



def test_agent_run_supports_more_than_six_terminal_turns(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []
    terminal_turns = 8

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        turn = len(calls)
        if turn <= terminal_turns:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": f"call_terminal_{turn}",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": f"printf terminal-turn-{turn}"}),
                        },
                    }
                ],
            }
        return {"content": "All terminal turns completed"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Long Terminal Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run all terminal checks"})

        for turn in range(terminal_turns):
            assert run["status"] == "approval_required"
            assert run["pending_approval"]["input_preview"]["command"] == f"printf terminal-turn-{turn + 1}"
            run = service.approve_run_approval(
                run["run_id"],
                expected_approval_id=run["pending_approval"]["approval_id"],
            )

        assert run["status"] == "completed"
        assert run["result"] == "All terminal turns completed"
        assert len(calls) == terminal_turns + 1
    finally:
        service.close()


def test_agent_run_fails_when_approved_terminal_returns_nonzero(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.custom_api_agent_loop._max_tool_iterations = 3
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    fixture_actions = [('terminal_run', {'command': 'printf terminal-failure-smoke; exit 7', 'shell': True})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        return {"content": "The exact requested process failed; no repair is authorized."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        failed_projection_calls: list[dict[str, object]] = []
        original_failed_projection = service._project_approval_resume_failed

        def spy_project_approval_resume_failed(context, safe_error):
            failed_projection_calls.append(
                {
                    "run_id": context.run_id,
                    "tool_name": context.tool_name,
                    "safe_error": safe_error,
                }
            )
            return original_failed_projection(context, safe_error)

        monkeypatch.setattr(
            service,
            "_project_approval_resume_failed",
            spy_project_approval_resume_failed,
        )
        agent = service.create_agent(
            {
                "name": "Failing Terminal Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": fixture_goal,
                                        "runtime_execution_envelope": envelope})

        assert run["status"] == "approval_required"
        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "failed"
        assert "最后一次工具调用：terminal.run" in resumed["result"]
        assert "退出码：7" in resumed["result"]
        assert failed_projection_calls == [
            {
                "run_id": run["run_id"],
                "tool_name": "terminal.run",
                "safe_error": resumed["result"],
            }
        ]
        assert len(calls) == 3
        failed_event = next(event for event in resumed["timeline"] if event["event"] == "agent.tool.failed")
        assert failed_event["status"] == "failed"
        assert failed_event["result"]["returncode"] == 7
        assert failed_event["result"]["stdout"] == "terminal-failure-smoke"
        assert service.get_run_group(resumed["run_group_id"])["status"] == "failed"
    finally:
        service.close()



def test_agent_run_redacts_approved_terminal_failure_output_from_projection_and_storage(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.custom_api_agent_loop._max_tool_iterations = 3
    workdir = tmp_path / "repo"
    workdir.mkdir()
    stdout_secret = "sk-stdout-secret123456789"
    stderr_secret = "stderr-token-secret123456789"
    code = (
        'import sys; '
        'print("OPENAI_" + "API_KEY=" + "sk-" + "stdout-" + "secret123456789"); '
        'print("Author" + "ization: Bearer " + "stderr-token-" + "secret123456789", file=sys.stderr); '
        'sys.exit(7)'
    )
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    calls = []

    fixture_actions = [('terminal_run', {'command': command})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        return {"content": "The exact requested process failed; no repair is authorized."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Failing Terminal Redaction Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": fixture_goal,
                                        "runtime_execution_envelope": envelope})

        assert run["status"] == "approval_required"
        resumed = service.approve_run_approval(run["run_id"])

        assert resumed["status"] == "failed"
        assert "最后一次工具调用：terminal.run" in resumed["result"]
        assert "退出码：7" in resumed["result"]
        assert len(calls) == 3
        failed_event = next(event for event in resumed["timeline"] if event["event"] == "agent.tool.failed")
        assert failed_event["status"] == "failed"
        assert failed_event["result"]["returncode"] == 7
        assert "[redacted]" in failed_event["result"]["stdout"]
        assert "[redacted]" in failed_event["result"]["stderr"]

        run_events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        projection = json.dumps({"run": resumed, "events": run_events}, ensure_ascii=False)
        assert stdout_secret not in projection
        assert stderr_secret not in projection
        assert "[redacted]" in projection
        tool_call_fact = next(
            event
            for event in run_events
            if event["event_type"] == "agent.tool.call" and event["payload"].get("approved") is True
        )
        assert tool_call_fact["payload"]["result"]["returncode"] == 7
        assert stdout_secret not in json.dumps(tool_call_fact, ensure_ascii=False)
        assert stderr_secret not in json.dumps(tool_call_fact, ensure_ascii=False)
    finally:
        service.close()

    assert verify_secret_redaction(paths=[tmp_path]) == []



def test_workflow_resumes_after_child_agent_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []
    resuming_statuses = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {"name": "terminal_run", "arguments": json.dumps({"command": "printf approved"})},
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "tool"
            assert "approved" in messages[-1]["content"]
            parent_during_resume = service.get_run(run["run_id"])
            child_during_resume = service.get_run(child_run_ids[0])
            group_during_resume = service.get_run_group(run["run_group_id"])
            resuming_statuses.append(
                (
                    child_during_resume["status"],
                    parent_during_resume["status"],
                    group_during_resume["status"],
                    parent_during_resume["result"],
                )
            )
            return {"content": "Agent A complete"}
        return {"content": "Agent B complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent_a = service.create_agent(
            {
                "name": "Needs Approval",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        agent_b = service.create_agent(
            {
                "name": "After Approval",
                "model_mode": "custom_api",
                "model_config": model_config,
            }
        )
        edited_agent = service.create_agent(
            {
                "name": "Edited After Approval",
                "model_mode": "custom_api",
                "model_config": model_config,
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Approval Resume Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "a",
                        "type": "agent",
                        "data": {
                            "label": "Needs Approval",
                            "agent_id": agent_a["agent_id"],
                        },
                    },
                    {
                        "id": "b",
                        "type": "agent",
                        "data": {
                            "label": "After Approval",
                            "agent_id": agent_b["agent_id"],
                        },
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                    {"source": "b", "target": "summary"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run approval flow"}
        )

        assert run["status"] == "approval_required"
        group = service.get_run_group(run["run_group_id"])
        child_run_ids = [run_id for run_id in group["child_run_ids"] if run_id != run["run_id"]]
        assert len(child_run_ids) == 1
        child = service.get_run(child_run_ids[0])
        assert child["status"] == "approval_required"

        child_running_calls: list[str] = []
        parent_resume_calls: list[str] = []
        original_mark_child_running = service.workflow_parent_resume.mark_child_running
        original_resume_after_child_update = service.workflow_parent_resume.resume_after_child_update

        def spy_mark_child_running(child_run: dict) -> None:
            child_running_calls.append(str(child_run.get("run_id") or ""))
            original_mark_child_running(child_run)

        def spy_resume_after_child_update(child_run: dict) -> None:
            parent_resume_calls.append(str(child_run.get("run_id") or ""))
            original_resume_after_child_update(child_run)

        monkeypatch.setattr(service.workflow_parent_resume, "mark_child_running", spy_mark_child_running)
        monkeypatch.setattr(service.workflow_parent_resume, "resume_after_child_update", spy_resume_after_child_update)

        service.update_workflow(
            workflow["workflow_id"],
            {
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "a",
                        "type": "agent",
                        "data": {
                            "label": "Needs Approval",
                            "agent_id": agent_a["agent_id"],
                        },
                    },
                    {
                        "id": "b",
                        "type": "agent",
                        "data": {
                            "label": "Edited After Approval",
                            "agent_id": edited_agent["agent_id"],
                        },
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "b"},
                    {"source": "b", "target": "summary"},
                ],
            },
        )
        approved_child = service.approve_run_approval(child["run_id"])

        assert resuming_statuses == [
            ("running", "running", "running", "Needs Approval 已批准，正在继续执行")
        ]
        assert child_running_calls == [child["run_id"]]
        assert parent_resume_calls == [child["run_id"]]
        assert approved_child["status"] == "completed"
        assert any(event["event"] == "agent.run.resumed" for event in approved_child["timeline"])
        resumed_parent = service.get_run(run["run_id"])
        assert resumed_parent["status"] == "completed"
        assert resumed_parent["result"] == "Agent B complete"
        agent_events = [
            event
            for event in resumed_parent["timeline"]
            if event["event"] == "workflow.node.agent"
        ]
        assert len(agent_events) == 2
        assert agent_events[0]["status"] == "completed"
        assert agent_events[0]["result"] == "Agent A complete"
        assert agent_events[1]["workflow_node_label"] == "After Approval"
        assert any(event["event"] == "workflow.run.child_resumed" for event in resumed_parent["timeline"])
        assert any(event["event"] == "workflow.run.resumed" for event in resumed_parent["timeline"])
        assert any(
            artifact.get("kind") == "workflow_artifact"
            for artifact in resumed_parent["artifacts"]
        )
        assert service.get_run_group(run["run_group_id"])["status"] == "completed"
    finally:
        service.close()


def test_workflow_fails_when_child_terminal_returns_nonzero_after_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf workflow-child-failure-smoke; exit 7", "shell": True}),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent(
            {
                "name": "Failing Child",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Child Terminal Failure Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Failing Child", "agent_id": agent["agent_id"]}},
                    {
                        "id": "artifact",
                        "type": "artifact",
                        "data": {"label": "Should Not Run", "artifact_path": "reports/should-not-run.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "artifact"},
                ],
            }
        )
        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Run failing child"})

        assert run["status"] == "approval_required"
        group = service.get_run_group(run["run_group_id"])
        child_run_ids = [run_id for run_id in group["child_run_ids"] if run_id != run["run_id"]]
        assert len(child_run_ids) == 1
        child = service.get_run(child_run_ids[0])

        resumed_child = service.approve_run_approval(child["run_id"])
        resumed_parent = service.get_run(run["run_id"])

        assert resumed_child["status"] == "failed"
        assert resumed_parent["status"] == "failed"
        assert "workflow-child-failure-smoke" in resumed_parent["result"]
        assert not any(artifact.get("path") == "reports/should-not-run.md" for artifact in resumed_parent["artifacts"])
        failed_event = next(event for event in resumed_parent["timeline"] if event["event"] == "workflow.run.failed")
        assert failed_event["workflow_node_id"] == "a"
        assert failed_event["workflow_node_kind"] == "agent"
        assert failed_event["workflow_node_label"] == "Failing Child"
        assert failed_event["child_run_id"] == child["run_id"]
        assert service.get_run_group(run["run_group_id"])["status"] == "failed"
        assert len(calls) == 1
    finally:
        service.close()


def test_workflow_resume_failure_keeps_child_node_context(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    responses = iter(["approval", "Agent A complete"])

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        response = next(responses)
        if response == "approval":
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {"name": "terminal_run", "arguments": json.dumps({"command": "printf approved"})},
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        return {"content": response}

    def fail_resume(_run):
        raise AgentRuntimeError("workflow snapshot unavailable")

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Needs Approval",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Approval Resume Failure Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Needs Approval", "agent_id": agent["agent_id"]}},
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "summary"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Run approval flow"})
        group = service.get_run_group(run["run_group_id"])
        child_run_id = next(run_id for run_id in group["child_run_ids"] if run_id != run["run_id"])
        monkeypatch.setattr(service, "_workflow_for_run_resume", fail_resume)

        approved_child = service.approve_run_approval(child_run_id)
        parent = service.get_run(run["run_id"])

        assert approved_child["status"] == "completed"
        assert parent["status"] == "failed"
        assert parent["result"] == "workflow snapshot unavailable"
        failed_event = next(event for event in parent["timeline"] if event["event"] == "workflow.run.failed")
        assert failed_event["workflow_node_id"] == "a"
        assert failed_event["workflow_node_kind"] == "agent"
        assert failed_event["workflow_node_label"] == "Needs Approval"
        assert failed_event["child_run_id"] == child_run_id
        assert failed_event["child_run_status"] == "completed"
        assert service.get_run_group(run["run_group_id"])["status"] == "failed"
    finally:
        service.close()


def test_workflow_parent_records_child_agent_rejection_node_info(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {"name": "terminal_run", "arguments": json.dumps({"command": "printf blocked"})},
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Needs Approval",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Rejected Child Approval Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Needs Approval", "agent_id": agent["agent_id"]}},
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "summary"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Run approval flow"})
        group = service.get_run_group(run["run_group_id"])
        child_run_ids = [run_id for run_id in group["child_run_ids"] if run_id != run["run_id"]]
        child = service.get_run(child_run_ids[0])

        rejected_child = service.reject_run_approval(child["run_id"], "not now")
        parent = service.get_run(run["run_id"])

        assert rejected_child["status"] == "cancelled"
        assert parent["status"] == "cancelled"
        assert service.get_run_group(run["run_group_id"])["status"] == "cancelled"
        agent_event = next(event for event in parent["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "a"
        assert agent_event["workflow_node_kind"] == "agent"
        assert agent_event["workflow_node_label"] == "Needs Approval"
        assert agent_event["status"] == "cancelled"
        cancelled_event = next(event for event in parent["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["workflow_node_id"] == "a"
        assert cancelled_event["workflow_node_kind"] == "agent"
        assert cancelled_event["workflow_node_label"] == "Needs Approval"
        assert cancelled_event["child_run_id"] == child["run_id"]
    finally:
        service.close()


def test_cancel_workflow_waiting_for_child_approval_cancels_child_run(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {"name": "terminal_run", "arguments": json.dumps({"command": "printf blocked"})},
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Needs Approval",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = service.create_workflow(
            {
                "name": "Cancelable Child Approval Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "a", "type": "agent", "data": {"label": "Needs Approval", "agent_id": agent["agent_id"]}},
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                "edges": [
                    {"source": "start", "target": "a"},
                    {"source": "a", "target": "summary"},
                ],
            }
        )

        run = service.create_workflow_run({"workflow_id": workflow["workflow_id"], "user_goal": "Run approval flow"})
        group = service.get_run_group(run["run_group_id"])
        child_run_ids = [run_id for run_id in group["child_run_ids"] if run_id != run["run_id"]]
        child = service.get_run(child_run_ids[0])

        assert run["status"] == "approval_required"
        assert child["status"] == "approval_required"

        cancelled_parent = service.cancel_run(run["run_id"])
        cancelled_child = service.get_run(child["run_id"])

        assert cancelled_parent["status"] == "cancelled"
        assert cancelled_parent["result"] == "Workflow 已取消：Needs Approval"
        assert cancelled_child["status"] == "cancelled"
        assert cancelled_child["pending_approval"] == {}
        assert cancelled_child["result"] == "父 Workflow 已取消"
        assert service.get_run_group(run["run_group_id"])["status"] == "cancelled"
        agent_event = next(event for event in cancelled_parent["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "a"
        assert agent_event["workflow_node_kind"] == "agent"
        assert agent_event["workflow_node_label"] == "Needs Approval"
        assert agent_event["status"] == "cancelled"
        cancelled_event = next(event for event in cancelled_parent["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["workflow_node_id"] == "a"
        assert cancelled_event["workflow_node_kind"] == "agent"
        assert cancelled_event["workflow_node_label"] == "Needs Approval"
        assert cancelled_event["child_run_id"] == child["run_id"]
    finally:
        service.close()


def test_agent_run_rejects_pending_tool(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {
            "content": json.dumps({"action": "tool", "tool": "terminal.run", "input": {"command": "echo blocked"}})
        },
    )
    try:
        agent = service.create_agent(
            {
                "name": "Reject Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run command"})
        leaked_secret = "sk-approval-reject-secret123456"
        rejected = service.reject_run_approval(run["run_id"], f"not now api_key={leaked_secret}")

        assert rejected["status"] == "cancelled"
        assert rejected["pending_approval"] == {}
        assert "not now" in rejected["result"]
        assert leaked_secret not in json.dumps(rejected, ensure_ascii=False)
        rejected_event = next(event for event in rejected["timeline"] if event["event"] == "agent.tool.approval_rejected")
        assert rejected_event["tool"] == "terminal.run"
        assert rejected_event["input_preview"]["command"] == "echo blocked"
        assert rejected_event["status"] == "cancelled"
        stored_run = service.get_run(run["run_id"])
        assert stored_run["status"] == "cancelled"
        assert "not now" in stored_run["result"]
        assert leaked_secret not in json.dumps(stored_run, ensure_ascii=False)
        run_events = service.list_run_events(run["run_id"])["events"]
        rejected_fact = next(event for event in run_events if event["event_type"] == "agent.tool.approval_rejected")
        assert rejected_fact["payload"]["tool"] == "terminal.run"
        assert rejected_fact["payload"]["input_preview"]["command"] == "echo blocked"
        assert rejected_fact["payload"]["status"] == "cancelled"
        cancelled_fact = next(event for event in run_events if event["event_type"] == "agent.run.cancelled")
        assert "not now" in cancelled_fact["payload"]["reason"]
        assert "not now" in cancelled_fact["payload"]["result"]
        assert leaked_secret not in json.dumps(run_events, ensure_ascii=False)
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        service.close()


def test_tool_descriptor_schema_and_validation_share_patch_contract():
    schema = NativeRunEngine._tool_schemas(["workspace.write_patch"])[0]
    properties = schema["function"]["parameters"]["properties"]

    assert "patch" in properties
    assert "content" not in properties
    assert schema["function"]["parameters"]["required"] == ["path"]
    properties["approved"] = {"type": "boolean"}
    properties["path"]["description"] = "mutated"
    schema["function"]["parameters"]["required"].append("patch")
    fresh_schema = NativeRunEngine._tool_schemas(["workspace.write_patch"])[0]
    fresh_properties = fresh_schema["function"]["parameters"]["properties"]
    assert "approved" not in fresh_properties
    assert fresh_properties["path"]["description"] == "Relative file path inside writable scopes."
    assert fresh_schema["function"]["parameters"]["required"] == ["path"]

    NativeRunEngine._validate_tool_payload("workspace.write_patch", {"path": "src/out.txt", "patch": "*** patch"})
    with pytest.raises(AgentRuntimeError, match="未声明字段：approved"):
        NativeRunEngine._validate_tool_payload("workspace.write_patch", {"path": "src/out.txt", "patch": "*** patch", "approved": True})
    with pytest.raises(AgentRuntimeError, match="未声明字段：content"):
        NativeRunEngine._validate_tool_payload("workspace.write_patch", {"path": "src/out.txt", "content": "bad"})
    with pytest.raises(AgentRuntimeError, match="patch 必须是非空字符串"):
        NativeRunEngine._validate_tool_payload("workspace.write_patch", {"path": "src/out.txt"})
    with pytest.raises(AgentRuntimeError, match="敏感凭据"):
        NativeRunEngine._validate_tool_payload("artifact.write", {"path": "notes.md", "content": "sk-secret-token"})


def test_model_payload_approved_flag_is_rejected_by_tool_schema(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_write",
                    "type": "function",
                    "function": {
                        "name": "workspace_write_patch",
                        "arguments": json.dumps(
                            {
                                "path": "src/out.txt",
                                "patch": "--- src/out.txt\n+++ src/out.txt\n@@ -1 +1 @@\n-before\n+bad\n",
                                "approved": True,
                            }
                        ),
                    },
                }
            ],
        },
    )
    try:
        agent = service.create_agent(
            {
                "name": "Writer",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.write_patch"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["src"]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Write file"})

        assert run["status"] == "failed"
        assert "未声明字段：approved" in run["result"]
        assert not (workdir / "src" / "out.txt").exists()
    finally:
        service.close()


def test_tool_broker_blocks_out_of_scope_and_unapproved_terminal(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    (workdir / "README.md").write_text("hello", encoding="utf-8")
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["src"],
        },
        tmp_path / "artifacts",
    )

    assert broker.workspace_read("README.md")["content"] == "hello"
    directory_read = broker.workspace_read(".")
    assert directory_read["ok"] is False
    assert directory_read["suggested_tool"] == "workspace.list"
    file_list = broker.workspace_list("README.md")
    assert file_list["ok"] is False
    assert file_list["suggested_tool"] == "workspace.read"
    with pytest.raises(AgentRuntimeError):
        broker.workspace_write_patch(
            "../escape.txt",
            patch="--- ../escape.txt\n+++ ../escape.txt\n@@ -1 +1 @@\n-old\n+bad\n",
            approved=True,
        )
    assert broker.terminal_run("echo should-not-run")["approval_required"] is True
    assert broker.call("terminal.run", {"command": "echo should-not-run", "approved": True})["approval_required"] is True


def test_agent_run_validates_write_patch_workspace_boundary_before_approval(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    calls = []
    service.custom_api_agent_loop._max_tool_iterations = 3
    fixture_actions = [("workspace_write_patch", {"path": "../outside.txt", "patch": "--- ../outside.txt\n+++ ../outside.txt\n@@ -1 +1 @@\n-outside\n+modified\n"})]
    fixture_goal = semantic_fixture_goal(fixture_actions)

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        planning_response = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning_response is not None:
            return planning_response
        if len(calls) == 1:
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_escape_write",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "../outside.txt",
                                    "patch": "--- ../outside.txt\n+++ ../outside.txt\n@@ -1 +1 @@\n-outside\n+modified\n",
                                }
                            ),
                        },
                    }
                ],
            }
        return {"content": "The requested path was refused by the workspace boundary."}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Boundary Writer",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["workspace.write_patch", "terminal.run"]},
                "workspace_policy": {
                    "default_workdir": str(workdir),
                    "readable_scopes": ["."],
                    "writable_scopes": ["."],
                },
            }
        )

        envelope = semantic_fixture_execution_envelope(fake_chat, fixture_actions, fixture_goal)
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": fixture_goal, "runtime_execution_envelope": envelope})
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]

        assert run["status"] == "failed"
        assert run["pending_approval"] == {}
        assert outside.read_text(encoding="utf-8") == "outside\n"
        assert not any(event["event"] == "agent.tool.approval_required" for event in run["timeline"])
        assert not any(event["event_type"] == "agent.tool.approval_required" for event in events)
        tool_event = next(event for event in events if event["event_type"] == "agent.tool.call")
        assert tool_event["payload"]["tool"] == "workspace.write_patch"
        assert tool_event["payload"]["result"]["ok"] is False
        assert "越界" in tool_event["payload"]["result"]["error"]
        assert "Workspace tools only accept relative paths" in tool_event["payload"]["result"]["hint"]
        assert tool_event["payload"]["result"]["suggested_tool"] == "terminal.run"
    finally:
        service.close()


def test_tool_broker_rejects_symlink_workspace_escape(tmp_path):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret_file = outside / "secret.txt"
    secret_file.write_text("secret", encoding="utf-8")
    outside_dir = outside / "nested"
    outside_dir.mkdir()
    try:
        (workdir / "secret-link.txt").symlink_to(secret_file)
        (workdir / "dir-link").symlink_to(outside_dir, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable on this filesystem: {exc}")
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    with pytest.raises(AgentRuntimeError, match="工作区范围"):
        broker.workspace_read("secret-link.txt")
    with pytest.raises(AgentRuntimeError, match="工作区范围"):
        broker.workspace_list("dir-link")
    with pytest.raises(AgentRuntimeError, match="工作区范围"):
        broker.workspace_write_patch(
            "secret-link.txt",
            patch="--- secret-link.txt\n+++ secret-link.txt\n@@ -1 +1 @@\n-secret\n+modified\n",
            approved=True,
        )
    assert secret_file.read_text(encoding="utf-8") == "secret"


def test_terminal_run_uses_workspace_argv_and_scrubbed_environment(tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setenv("SAFE_ENV", "kept")
    monkeypatch.setenv("SSH_AUTH_SOCK", "ssh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secretsecretsecret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "google-secret")
    monkeypatch.setenv("AZURE_TOKEN", "azure-secret")
    monkeypatch.setenv("CUSTOM_API_KEY", "custom-secret")
    monkeypatch.setenv("CUSTOM_PASSWORD", "password-secret")

    class FakeProcess:
        pid = 4242
        returncode = 0

        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured.update(kwargs)

        def communicate(self, *, timeout):
            captured["timeout"] = timeout
            return (
                "OPENAI_API_KEY=sk-output-secret123456789",
                "Authorization: Bearer stderr-secret-123456",
            )

    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.Popen", FakeProcess)
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    result = broker.terminal_run("python -c 'print(123)'", approved=True, timeout_seconds=999)

    assert captured["argv"] == ["python", "-c", "print(123)"]
    assert captured["cwd"] == workdir
    assert captured["shell"] is False
    assert captured["start_new_session"] is True
    assert captured["timeout"] == 120
    env = captured["env"]
    assert env["SAFE_ENV"] == "kept"
    for key in (
        "SSH_AUTH_SOCK",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "AZURE_TOKEN",
        "CUSTOM_API_KEY",
        "CUSTOM_PASSWORD",
    ):
        assert key not in env
    assert result["ok"] is True
    assert result["shell"] is False
    assert "sk-output-secret123456789" not in result["stdout"]
    assert "stderr-secret-123456" not in result["stderr"]


def test_terminal_run_startup_failure_returns_structured_sanitized_error(tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    captured: dict[str, object] = {}

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_should_not_leak_to_child")

    def fail_popen(argv, **kwargs):
        captured["argv"] = argv
        captured.update(kwargs)
        raise FileNotFoundError("missing binary token=sk-startup-secret123456789")

    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.Popen", fail_popen)
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    result = broker.terminal_run("missing-native-tool --flag", approved=True)

    assert captured["argv"] == ["missing-native-tool", "--flag"]
    assert captured["cwd"] == workdir
    assert captured["shell"] is False
    assert "GITHUB_TOKEN" not in captured["env"]
    assert result["ok"] is False
    assert result["returncode"] is None
    assert result["timed_out"] is False
    assert result["stdout"] == ""
    assert "sk-startup-secret123456789" not in result["stderr"]
    assert "[redacted]" in result["stderr"]


def test_terminal_run_truncates_and_sanitizes_outputs(tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    workdir.mkdir()

    class FakeProcess:
        pid = 4243
        returncode = 0

        def __init__(self, *_args, **_kwargs):
            pass

        def communicate(self, *, timeout):
            stdout = f"{'x' * 9000}OPENAI_API_KEY=sk-stdout-secret123456789\nstdout-tail"
            stderr = f"{'y' * 9000}Authorization: Bearer stderr-secret-123456\nstderr-tail"
            return stdout, stderr

    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.Popen", FakeProcess)
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    result = broker.terminal_run("printf long-output", approved=True)

    assert result["ok"] is True
    assert len(result["stdout"]) <= 8000
    assert len(result["stderr"]) <= 8000
    assert "sk-stdout-secret123456789" not in result["stdout"]
    assert "stderr-secret-123456" not in result["stderr"]
    assert result["stdout"].endswith("stdout-tail")
    assert result["stderr"].endswith("stderr-tail")


def test_terminal_run_timeout_kills_process_group(tmp_path, monkeypatch):
    workdir = tmp_path / "repo"
    workdir.mkdir()
    killed: list[tuple[int, int]] = []

    class FakeProcess:
        pid = 4343
        returncode = -9

        def __init__(self, argv, **_kwargs):
            self.argv = argv
            self.calls = 0

        def communicate(self, *, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise subprocess.TimeoutExpired(self.argv, timeout)
            return ("late stdout", "late stderr")

    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.Popen", FakeProcess)
    monkeypatch.setattr("apps.shell.agent_runtime.os.killpg", lambda pid, sig: killed.append((pid, sig)))
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        tmp_path / "artifacts",
    )

    result = broker.terminal_run("sleep 30", approved=True, timeout_seconds=1)

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["returncode"] == -9
    assert killed == [(4343, 9)]


def test_workspace_write_patch_applies_single_file_unified_diff_with_hash(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    target = workdir / "src" / "app.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    before_sha = hashlib.sha256(target.read_bytes()).hexdigest()
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["src"],
        },
        tmp_path / "artifacts",
    )
    patch = """--- a/src/app.txt
+++ b/src/app.txt
@@ -1,3 +1,3 @@
 one
-two
+TWO
 three
"""

    result = broker.call(
        "workspace.write_patch",
        {"path": "src/app.txt", "patch": patch, "expected_sha256": before_sha},
        approved=True,
    )

    assert result["ok"] is True
    assert result["mode"] == "patch"
    assert result["sha256_before"] == before_sha
    assert result["sha256_after"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert target.read_text(encoding="utf-8") == "one\nTWO\nthree\n"


def test_workspace_write_patch_rejects_hash_or_context_mismatch_without_writing(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    target = workdir / "src" / "app.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8")
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["src"],
        },
        tmp_path / "artifacts",
    )
    context_mismatch_patch = """--- a/src/app.txt
+++ b/src/app.txt
@@ -1,3 +1,3 @@
 one
-missing
+TWO
 three
"""

    hash_result = broker.call(
        "workspace.write_patch",
        {"path": "src/app.txt", "patch": context_mismatch_patch, "expected_sha256": "0" * 64},
        approved=True,
    )

    assert hash_result["ok"] is False
    assert "hash" in hash_result["error"]
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"
    with pytest.raises(AgentRuntimeError, match="hunk context"):
        broker.call(
            "workspace.write_patch",
            {"path": "src/app.txt", "patch": context_mismatch_patch},
            approved=True,
        )
    assert target.read_text(encoding="utf-8") == "one\ntwo\nthree\n"


def test_workspace_write_patch_rejects_multifile_or_binary_patch(tmp_path):
    workdir = tmp_path / "repo"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "app.txt").write_text("one\n", encoding="utf-8")
    broker = ToolBroker(
        {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["src"],
        },
        tmp_path / "artifacts",
    )
    multifile_patch = """--- a/src/app.txt
+++ b/src/app.txt
@@ -1 +1 @@
-one
+two
--- a/src/other.txt
+++ b/src/other.txt
@@ -1 +1 @@
-x
+y
"""

    with pytest.raises(AgentRuntimeError, match="单文件"):
        broker.call("workspace.write_patch", {"path": "src/app.txt", "patch": multifile_patch}, approved=True)
    with pytest.raises(AgentRuntimeError, match="二进制"):
        broker.call("workspace.write_patch", {"path": "src/app.txt", "patch": "GIT binary patch\n"}, approved=True)


def test_explicit_empty_tool_policy_disables_model_tools(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    captured = {}

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        captured["messages"] = messages
        captured["tools"] = tools
        return {"content": "No tools used"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "No Tools Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": []},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Answer only"})

        assert agent["tool_policy"]["allowed_tools"] == []
        assert run["status"] == "completed"
        assert captured["tools"] == []
        prompt = captured["messages"][0]["content"]
        assert "artifact.write" not in prompt
        assert "Oha-Yachiyo Agent Runtime" in prompt
        assert "Runtime: Yachiyo Agent Runtime" not in prompt
        context_artifact = service.read_run_artifact(run["run_id"], "agent-context.md")
        assert "Runtime: Oha Agent Runtime" in context_artifact["content"]
        assert "Runtime: Yachiyo Agent Runtime" not in context_artifact["content"]
        started = next(event for event in run["timeline"] if event["event"] == "agent.run.started")
        assert started["runtime"] == "oha_agent"
        compiled = next(event for event in run["timeline"] if event["event"] == "agent.runtime.compiled")
        assert compiled["detail"] == "Oha Agent Runtime compiled tools and workspace policy"
        assert compiled["allowed_tools"] == []
        run_events = service.list_run_events(run["run_id"])["events"]
        started_event = next(event for event in run_events if event["event_type"] == "agent.run.started")
        assert started_event["payload"]["runtime"] == "oha_agent"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_run_approval_routes_return_404_and_are_idempotent(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    model_calls = 0

    def fake_chat(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        return {"content": "Done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        with pytest.raises(HTTPException) as missing:
            await agent_routes.approve_run_approval(
                "run_missing",
                agent_routes.ApprovalRejectRequest(
                    approval_id="approval-missing-run",
                ),
            )
        assert missing.value.status_code == 404

        agent = service.create_agent(
            {
                "name": "Done Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Reply with exactly 'Done'"})
        repeated = await agent_routes.approve_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id="approval-terminal-run",
            ),
        )
        assert repeated["run_id"] == run["run_id"]
        assert repeated["status"] == "completed"
        assert model_calls == 1
    finally:
        service.close()


@pytest.mark.asyncio
async def test_run_approval_reject_route_is_idempotent(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)

    def fake_chat(*_args, **_kwargs):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal_reject",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf should-not-run"}),
                    },
                }
            ],
        }

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Reject Route Agent",
                "model_mode": "custom_api",
                "model_config": {"base_url": "https://api.example.test/v1", "model": "demo-model", "api_key": "sk-secret"},
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        run = service.create_agent_run({"agent_id": agent["agent_id"], "user_goal": "Run printf should-not-run"})
        assert run["status"] == "approval_required"
        approval_id = run["pending_approval"]["approval_id"]

        first = await agent_routes.reject_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=approval_id,
                reason="not allowed",
            ),
        )
        second = await agent_routes.reject_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=approval_id,
                reason="not allowed again",
            ),
        )
        events = service.list_run_events(run["run_id"])["events"]
        rejection_facts = [
            event
            for event in events
            if event["event_type"] == "agent.tool.approval_rejected"
        ]

        assert first["status"] == "cancelled"
        assert second["status"] == "cancelled"
        assert len(rejection_facts) == 1
        assert rejection_facts[0]["payload"]["reason"] == "not allowed"
        assert "should-not-run" in json.dumps(rejection_facts[0]["payload"], ensure_ascii=False)
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_routes_update_then_run_latest_graph(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    responses = iter(["Route design", "Route code"])

    def fake_chat(_base_url, _model, _api_key, _messages, **_kwargs):
        return {"content": next(responses)}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        old_agent = service.create_agent({"name": "Route Old", "model_mode": "custom_api", "model_config": model_config})
        design_agent = service.create_agent({"name": "Route Design", "model_mode": "custom_api", "model_config": model_config})
        coding_agent = service.create_agent({"name": "Route Coding", "model_mode": "custom_api", "model_config": model_config})

        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Save And Run",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "old", "type": "agent", "data": {"label": "Route Old", "agent_id": old_agent["agent_id"]}},
                ],
                edges=[{"source": "start", "target": "old"}],
            )
        )
        updated = await agent_routes.update_workflow(
            workflow["workflow_id"],
            agent_routes.WorkflowRequest(
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "design", "type": "agent", "data": {"label": "Route Design", "agent_id": design_agent["agent_id"]}},
                    {"id": "coding", "type": "agent", "data": {"label": "Route Coding", "agent_id": coding_agent["agent_id"]}},
                ],
                edges=[
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "coding"},
                ],
            ),
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(
                workflow_id=updated["workflow_id"],
                user_goal="Run latest route graph",
            )
        )

        assert run["status"] == "completed"
        assert run["result"] == "Route code"
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"] == [
            {"id": "start", "kind": "start", "label": "Start"},
            {"id": "design", "kind": "agent", "label": "Route Design"},
            {"id": "coding", "kind": "agent", "label": "Route Coding"},
        ]
        group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        assert [child["runnable_id"] for child in child_runs] == [design_agent["agent_id"], coding_agent["agent_id"]]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_routes_save_and_run_latest_canvas_with_step_approval_and_artifact(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    contexts: list[str] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        contexts.append(messages[-1]["content"])
        return {"content": "Mobile acceptance risks ready"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        old_agent = service.create_agent({"name": "Canvas Old", "model_mode": "custom_api", "model_config": model_config})
        design_agent = service.create_agent({"name": "Canvas Design", "model_mode": "custom_api", "model_config": model_config})

        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Canvas Save And Run",
                nodes=[
                    {"id": "start", "type": "input", "data": {"label": "Start", "kind": "start"}},
                    {
                        "id": "old",
                        "type": "default",
                        "data": {"label": "Old Agent", "kind": "agent", "agent_id": old_agent["agent_id"]},
                    },
                ],
                edges=[{"source": "start", "target": "old"}],
            )
        )
        updated = await agent_routes.update_workflow(
            workflow["workflow_id"],
            agent_routes.WorkflowRequest(
                nodes=[
                    {"id": "start", "type": "input", "data": {"label": "Start", "kind": "start"}},
                    {
                        "id": "design",
                        "type": "default",
                        "data": {
                            "label": "Mobile Design",
                            "kind": "agent",
                            "agent_id": design_agent["agent_id"],
                            "step_task": "Explain mobile acceptance risks and the checks to verify them.",
                        },
                    },
                    {
                        "id": "gate",
                        "type": "default",
                        "data": {
                            "label": "Review Gate",
                            "kind": "approval",
                            "approval_criteria": "Confirm the mobile risks are specific enough before writing the report.",
                        },
                    },
                    {
                        "id": "report",
                        "type": "output",
                        "data": {
                            "label": "Risk Report",
                            "kind": "artifact",
                            "artifact_path": "reports/mobile-risk.md",
                        },
                    },
                ],
                edges=[
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "gate"},
                    {"source": "gate", "target": "report"},
                ],
            ),
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(
                workflow_id=updated["workflow_id"],
                user_goal="Prepare mobile release acceptance",
            )
        )

        assert run["status"] == "approval_required"
        assert len(contexts) == 1
        assert "# User Goal\nExplain mobile acceptance risks and the checks to verify them." in contexts[0]
        assert "Workflow Goal:\nPrepare mobile release acceptance" in contexts[0]
        assert "# Upstream Context\nNone" in contexts[0]
        assert "Old Agent" not in contexts[0]
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"] == [
            {"id": "start", "kind": "start", "label": "Start"},
            {
                "id": "design",
                "kind": "agent",
                "label": "Mobile Design",
                "task": "Explain mobile acceptance risks and the checks to verify them.",
            },
            {
                "id": "gate",
                "kind": "approval",
                "label": "Review Gate",
                "criteria": "Confirm the mobile risks are specific enough before writing the report.",
            },
            {
                "id": "report",
                "kind": "artifact",
                "label": "Risk Report",
                "artifact_path": "reports/mobile-risk.md",
            },
        ]
        assert run["pending_approval"]["input_preview"]["criteria"] == (
            "Confirm the mobile risks are specific enough before writing the report."
        )
        agent_event = next(event for event in run["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "design"
        assert agent_event["workflow_node_task"] == "Explain mobile acceptance risks and the checks to verify them."
        approval_event = next(event for event in run["timeline"] if event["event"] == "workflow.node.approval_required")
        assert approval_event["workflow_node_id"] == "gate"
        assert approval_event["workflow_node_approval_criteria"] == (
            "Confirm the mobile risks are specific enough before writing the report."
        )
        group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        assert [child["runnable_id"] for child in child_runs] == [design_agent["agent_id"]]
        assert child_runs[0]["user_goal"] == (
            "Explain mobile acceptance risks and the checks to verify them."
        )

        resumed = await agent_routes.approve_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=run["pending_approval"]["approval_id"],
            ),
        )

        assert resumed["status"] == "completed"
        artifact_event = next(event for event in resumed["timeline"] if event["event"] == "workflow.node.artifact")
        assert artifact_event["workflow_node_id"] == "report"
        assert artifact_event["artifact"]["path"] == "reports/mobile-risk.md"
        assert service.read_run_artifact(resumed["run_id"], "reports/mobile-risk.md")["content"] == "Mobile acceptance risks ready"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_routes_accept_reactflow_node_types(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)

    def fake_chat(_base_url, _model, _api_key, _messages, **_kwargs):
        return {"content": "ReactFlow route done"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        agent = service.create_agent({"name": "Route Design", "model_mode": "custom_api", "model_config": model_config})

        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="ReactFlow Raw Types",
                nodes=[
                    {"id": "start", "type": "input", "data": {"label": "Start", "kind": "start"}},
                    {"id": "design", "type": "default", "data": {"label": "Route Design", "kind": "agent", "agent_id": agent["agent_id"]}},
                    {"id": "summary", "type": "output", "data": {"label": "Summary", "kind": "artifact"}},
                ],
                edges=[
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "summary"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(
                workflow_id=workflow["workflow_id"],
                user_goal="Run raw ReactFlow graph",
            )
        )

        assert run["status"] == "completed"
        started_event = next(event for event in run["timeline"] if event["event"] == "workflow.run.started")
        assert started_event["workflow_path"] == [
            {"id": "start", "kind": "start", "label": "Start"},
            {"id": "design", "kind": "agent", "label": "Route Design"},
            {"id": "summary", "kind": "artifact", "label": "Summary", "artifact_path": "summary.md"},
        ]
        assert service.read_run_artifact(run["run_id"], "summary.md")["content"] == "ReactFlow route done"
    finally:
        service.close()


def test_workflow_condition_node_routes_true_and_false_branches(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {"content": "decision: ship"}
        if len(calls) == 2:
            return {"content": "ship branch done"}
        if len(calls) == 3:
            return {"content": "decision: skip"}
        return {"content": "skip branch done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        classifier = service.create_agent(
            {"name": "Condition Classifier", "model_mode": "custom_api", "model_config": model_config}
        )
        ship_agent = service.create_agent(
            {"name": "Ship Branch Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        skip_agent = service.create_agent(
            {"name": "Skip Branch Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Condition Branch Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "classify",
                        "type": "agent",
                        "data": {
                            "label": "Classify",
                            "agent_id": classifier["agent_id"],
                            "task": "Return decision: ship or decision: skip.",
                        },
                    },
                    {
                        "id": "route",
                        "type": "condition",
                        "data": {"label": "Route", "condition": "ship", "operator": "contains"},
                    },
                    {
                        "id": "ship",
                        "type": "agent",
                        "data": {"label": "Ship", "agent_id": ship_agent["agent_id"]},
                    },
                    {
                        "id": "skip",
                        "type": "agent",
                        "data": {"label": "Skip", "agent_id": skip_agent["agent_id"]},
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Branch Report", "artifact_path": "reports/branch.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "classify"},
                    {"source": "classify", "target": "route"},
                    {"source": "route", "target": "ship", "data": {"branch": "true"}},
                    {"source": "route", "target": "skip", "data": {"branch": "false"}},
                    {"source": "ship", "target": "report"},
                    {"source": "skip", "target": "report"},
                ],
            }
        )

        ship_run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Use ship branch"}
        )
        skip_run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Use skip branch"}
        )

        ship_steps = [
            event for event in ship_run["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        skip_steps = [
            event for event in skip_run["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        ship_condition = next(event for event in ship_steps if event["event"] == "workflow.node.condition")
        skip_condition = next(event for event in skip_steps if event["event"] == "workflow.node.condition")

        assert ship_run["status"] == "completed"
        assert ship_run["result"] == "ship branch done"
        assert service.read_run_artifact(ship_run["run_id"], "reports/branch.md")["content"] == "ship branch done"
        assert [event.get("workflow_node_id") for event in ship_steps] == [
            "start",
            "classify",
            "route",
            "ship",
            "report",
        ]
        assert ship_condition["workflow_node_condition_matched"] is True
        assert ship_condition["workflow_node_selected_branch"] == "true"
        assert ship_condition["workflow_node_selected_target"] == "ship"

        assert skip_run["status"] == "completed"
        assert skip_run["result"] == "skip branch done"
        assert service.read_run_artifact(skip_run["run_id"], "reports/branch.md")["content"] == "skip branch done"
        assert [event.get("workflow_node_id") for event in skip_steps] == [
            "start",
            "classify",
            "route",
            "skip",
            "report",
        ]
        assert skip_condition["workflow_node_condition_matched"] is False
        assert skip_condition["workflow_node_selected_branch"] == "false"
        assert skip_condition["workflow_node_selected_target"] == "skip"
        assert len(calls) == 4
    finally:
        service.close()


def test_workflow_parallel_node_runs_branches_and_merges_into_artifact(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls: list[str] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        context = str(messages[1]["content"])
        calls.append(context)
        if "Design branch" in context:
            return {"content": "Design branch complete"}
        if "Code branch" in context:
            return {"content": "Code branch complete"}
        return {"content": "unexpected"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {"name": "Parallel Design Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        code_agent = service.create_agent(
            {"name": "Parallel Code Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Parallel Branch Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "fanout", "type": "parallel", "data": {"label": "Parallel Work"}},
                    {
                        "id": "design",
                        "type": "agent",
                        "data": {
                            "label": "Design",
                            "agent_id": design_agent["agent_id"],
                            "task": "Design branch",
                        },
                    },
                    {
                        "id": "code",
                        "type": "agent",
                        "data": {
                            "label": "Code",
                            "agent_id": code_agent["agent_id"],
                            "task": "Code branch",
                        },
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Parallel Report", "artifact_path": "reports/parallel.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "fanout"},
                    {"source": "fanout", "target": "design"},
                    {"source": "fanout", "target": "code"},
                    {"source": "design", "target": "report"},
                    {"source": "code", "target": "report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run parallel branches"}
        )
        steps = [
            event for event in run["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        parallel_event = next(event for event in steps if event["event"] == "workflow.node.parallel")
        artifact = service.read_run_artifact(run["run_id"], "reports/parallel.md")

        assert run["status"] == "completed"
        assert [event.get("workflow_node_id") for event in steps] == [
            "start",
            "design",
            "code",
            "fanout",
            "report",
        ]
        assert parallel_event["workflow_node_branch_count"] == 2
        assert parallel_event["workflow_node_completed_branch_count"] == 2
        assert parallel_event["workflow_node_join_target"] == "report"
        assert [item["label"] for item in parallel_event["workflow_node_branch_results"]] == ["Design", "Code"]
        assert "Parallel Parallel Work results:" in run["result"]
        assert "- Design: Design branch complete" in run["result"]
        assert "- Code: Code branch complete" in run["result"]
        assert artifact["content"] == run["result"]
        assert len(calls) == 2
    finally:
        service.close()


def test_workflow_parallel_branch_approval_resumes_remaining_branches_and_fans_in(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        prompt = str(messages[1]["content"]) if len(messages) > 1 else ""
        if len(calls) == 1:
            assert "Design branch" in prompt
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf parallel-approved"}),
                        },
                    }
                ],
            }
        if len(calls) == 2:
            assert messages[-1]["role"] == "tool"
            assert "parallel-approved" in messages[-1]["content"]
            return {"content": "Design branch approved"}
        assert "Code branch" in prompt
        assert "Design branch approved" not in prompt
        return {"content": "Code branch complete"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {
                "name": "Parallel Approval Design Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        code_agent = service.create_agent(
            {"name": "Parallel Approval Code Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Parallel Approval Branch Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "fanout", "type": "parallel", "data": {"label": "Parallel Work"}},
                    {
                        "id": "design",
                        "type": "agent",
                        "data": {
                            "label": "Design",
                            "agent_id": design_agent["agent_id"],
                            "task": "Design branch",
                        },
                    },
                    {
                        "id": "code",
                        "type": "agent",
                        "data": {
                            "label": "Code",
                            "agent_id": code_agent["agent_id"],
                            "task": "Code branch",
                        },
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {
                            "label": "Parallel Approval Report",
                            "artifact_path": "reports/parallel-approval.md",
                        },
                    },
                ],
                "edges": [
                    {"source": "start", "target": "fanout"},
                    {"source": "fanout", "target": "design"},
                    {"source": "fanout", "target": "code"},
                    {"source": "design", "target": "report"},
                    {"source": "code", "target": "report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run parallel approval branches"}
        )
        group = service.get_run_group(run["run_group_id"])
        design_run_id = next(
            run_id
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"] and service.get_run(run_id)["kind"] == "agent_run"
        )
        design_run = service.get_run(design_run_id)
        initial_agent_event = next(
            event for event in run["timeline"]
            if event["event"] == "workflow.node.agent" and event["workflow_node_id"] == "design"
        )

        assert run["status"] == "approval_required"
        assert design_run["status"] == "approval_required"
        assert initial_agent_event["workflow_parent_node_id"] == "fanout"
        assert initial_agent_event["workflow_parent_node_kind"] == "parallel"
        assert initial_agent_event["workflow_parallel_branch_label"] == "Design"
        assert len(calls) == 1

        approved_design = service.approve_run_approval(design_run_id)
        completed = service.get_run(run["run_id"])
        completed_group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in completed_group["child_run_ids"]
            if run_id != run["run_id"]
        ]
        steps = [
            event for event in completed["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        parallel_event = next(event for event in steps if event["event"] == "workflow.node.parallel")
        design_event = next(
            event for event in steps
            if event["event"] == "workflow.node.agent" and event["workflow_node_id"] == "design"
        )
        parent_events = service.run_events.list(run["run_id"], after_sequence=0, limit=200)["events"]
        parent_agent_events = [
            event for event in parent_events
            if event["event_type"] == "workflow.node.agent"
            and event["payload"].get("workflow_node_id") == "design"
        ]

        assert approved_design["status"] == "completed"
        assert completed["status"] == "completed"
        assert completed["result"] == (
            "Parallel Parallel Work results:\n"
            "- Design: Design branch approved\n"
            "- Code: Code branch complete"
        )
        assert [event.get("workflow_node_id") for event in steps] == [
            "start",
            "design",
            "code",
            "fanout",
            "report",
        ]
        assert parallel_event["workflow_node_branch_count"] == 2
        assert parallel_event["workflow_node_completed_branch_count"] == 2
        assert parallel_event["workflow_node_join_target"] == "report"
        assert [item["label"] for item in parallel_event["workflow_node_branch_results"]] == ["Design", "Code"]
        assert design_event["status"] == "completed"
        assert design_event["workflow_node_context"] == "Design branch approved"
        assert design_event["workflow_parent_node_id"] == "fanout"
        assert [event["payload"].get("status") for event in parent_agent_events] == [
            "approval_required",
            "running",
            "completed",
        ]
        assert service.read_run_artifact(run["run_id"], "reports/parallel-approval.md")["content"] == completed["result"]
        assert [child["runnable_id"] for child in child_runs] == [design_agent["agent_id"], code_agent["agent_id"]]
        assert len(calls) == 3
    finally:
        service.close()


def test_workflow_subworkflow_node_runs_child_workflow_and_projects_artifacts(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls: list[str] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        context = str(messages[1]["content"])
        calls.append(context)
        return {"content": "child workflow agent done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        child_agent = service.create_agent(
            {"name": "Child Workflow Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        child_workflow = service.create_workflow(
            {
                "name": "Child Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "child-agent",
                        "type": "agent",
                        "data": {
                            "label": "Child Agent",
                            "agent_id": child_agent["agent_id"],
                            "task": "Do the child work",
                        },
                    },
                    {
                        "id": "child-report",
                        "type": "artifact",
                        "data": {"label": "Child Report", "artifact_path": "reports/child.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "child-agent"},
                    {"source": "child-agent", "target": "child-report"},
                ],
            }
        )
        parent_workflow = service.create_workflow(
            {
                "name": "Parent Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "child-flow",
                        "type": "workflow",
                        "data": {
                            "label": "Run Child Flow",
                            "workflow_id": child_workflow["workflow_id"],
                            "task": "Run child flow first",
                        },
                    },
                    {
                        "id": "parent-report",
                        "type": "artifact",
                        "data": {"label": "Parent Report", "artifact_path": "reports/parent.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "child-flow"},
                    {"source": "child-flow", "target": "parent-report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": parent_workflow["workflow_id"], "user_goal": "Run parent flow"}
        )
        steps = [
            event for event in run["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        workflow_event = next(event for event in steps if event["event"] == "workflow.node.workflow")
        child_run_id = workflow_event["child_run_id"]
        child_run = service.get_run(child_run_id)
        group = service.get_run_group(run["run_group_id"])
        child_artifact_refs = [
            artifact for artifact in run["artifacts"]
            if artifact.get("kind") == "workflow_child_artifact"
        ]

        assert WorkflowSubworkflowNodeExecution.__name__ == "WorkflowSubworkflowNodeExecution"
        assert run["status"] == "completed"
        assert run["result"] == "child workflow agent done"
        assert [event.get("workflow_node_id") for event in steps] == [
            "start",
            "child-flow",
            "parent-report",
        ]
        assert workflow_event["child_workflow_id"] == child_workflow["workflow_id"]
        assert workflow_event["child_workflow_name"] == "Child Flow"
        assert workflow_event["status"] == "completed"
        assert workflow_event["artifact_count"] == 1
        assert child_run["kind"] == "workflow_run"
        assert child_run["runnable_id"] == child_workflow["workflow_id"]
        assert child_run["status"] == "completed"
        assert child_run["run_group_id"] == run["run_group_id"]
        assert child_run_id in group["child_run_ids"]
        assert child_artifact_refs == [
            {
                "kind": "workflow_child_artifact",
                "path": "reports/child.md",
                "source_run_id": child_run_id,
                "source_run_kind": "workflow_run",
                "source_runnable_id": child_workflow["workflow_id"],
                "source_runnable_name": "Child Flow",
                "workflow_step_label": "Run Child Flow",
                "artifact_kind": "workflow_artifact",
            }
        ]
        assert service.read_run_artifact(child_run_id, "reports/child.md")["content"] == "child workflow agent done"
        assert service.read_run_artifact(run["run_id"], "reports/parent.md")["content"] == "child workflow agent done"
        assert len(calls) == 1
    finally:
        service.close()


def test_workflow_subworkflow_child_approval_resumes_parent_workflow(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf subworkflow-approved"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "subworkflow-approved" in messages[-1]["content"]
        return {"content": "Subworkflow child approved result"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        child_agent = service.create_agent(
            {
                "name": "Subworkflow Approval Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        child_workflow = service.create_workflow(
            {
                "name": "Approval Child Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "child-agent",
                        "type": "agent",
                        "data": {
                            "label": "Subworkflow Approval Agent",
                            "agent_id": child_agent["agent_id"],
                        },
                    },
                    {
                        "id": "child-report",
                        "type": "artifact",
                        "data": {"label": "Child Report", "artifact_path": "reports/child-approval.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "child-agent"},
                    {"source": "child-agent", "target": "child-report"},
                ],
            }
        )
        parent_workflow = service.create_workflow(
            {
                "name": "Parent Approval Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "child-flow",
                        "type": "workflow",
                        "data": {
                            "label": "Run Approval Child Flow",
                            "workflow_id": child_workflow["workflow_id"],
                        },
                    },
                    {
                        "id": "parent-report",
                        "type": "artifact",
                        "data": {"label": "Parent Report", "artifact_path": "reports/parent-approval.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "child-flow"},
                    {"source": "child-flow", "target": "parent-report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": parent_workflow["workflow_id"], "user_goal": "Run nested approval flow"}
        )
        group = service.get_run_group(run["run_group_id"])
        child_workflow_run_id = next(
            run_id
            for run_id in group["child_run_ids"]
            if service.get_run(run_id)["kind"] == "workflow_run" and run_id != run["run_id"]
        )
        child_workflow_run = service.get_run(child_workflow_run_id)
        grandchild_agent_run_id = next(
            run_id
            for run_id in group["child_run_ids"]
            if service.get_run(run_id)["kind"] == "agent_run"
        )
        grandchild_agent_run = service.get_run(grandchild_agent_run_id)
        parent_wait_event = next(
            event for event in run["timeline"]
            if event["event"] == "workflow.run.approval_required"
        )
        child_wait_event = next(
            event for event in child_workflow_run["timeline"]
            if event["event"] == "workflow.run.approval_required"
        )

        assert run["status"] == "approval_required"
        assert run["pending_approval"] == {}
        assert child_workflow_run["status"] == "approval_required"
        assert child_workflow_run["pending_approval"] == {}
        assert grandchild_agent_run["status"] == "approval_required"
        assert grandchild_agent_run["user_goal"] == DELEGATED_WORKFLOW_RESPONSE_ONLY_GOAL
        assert grandchild_agent_run["pending_approval"]["tool"] == "terminal.run"
        assert any(
            "Workflow Goal:\nRun nested approval flow"
            in str(message.get("content") or "")
            for message in calls[0]
        )
        assert parent_wait_event["child_run_id"] == child_workflow_run_id
        assert parent_wait_event["workflow_node_kind"] == "workflow"
        assert child_wait_event["child_run_id"] == grandchild_agent_run_id
        assert child_wait_event["workflow_node_kind"] == "agent"

        approved_grandchild = service.approve_run_approval(grandchild_agent_run_id)
        completed_child_workflow = service.get_run(child_workflow_run_id)
        completed_parent = service.get_run(run["run_id"])
        completed_group = service.get_run_group(run["run_group_id"])
        parent_events = service.run_events.list(run["run_id"], after_sequence=0, limit=200)["events"]
        parent_workflow_node_events = [
            event for event in parent_events
            if event["event_type"] == "workflow.node.workflow"
        ]

        assert approved_grandchild["status"] == "completed"
        assert approved_grandchild["pending_approval"] == {}
        assert approved_grandchild["result"] == "Subworkflow child approved result"
        assert completed_child_workflow["status"] == "completed"
        assert completed_child_workflow["pending_approval"] == {}
        assert completed_child_workflow["result"] == "Subworkflow child approved result"
        assert completed_parent["status"] == "completed"
        assert completed_parent["pending_approval"] == {}
        assert completed_parent["result"] == "Subworkflow child approved result"
        assert completed_group["status"] == "completed"
        assert service.read_run_artifact(child_workflow_run_id, "reports/child-approval.md")["content"] == (
            "Subworkflow child approved result"
        )
        assert service.read_run_artifact(run["run_id"], "reports/parent-approval.md")["content"] == (
            "Subworkflow child approved result"
        )
        workflow_node = next(
            event for event in completed_parent["timeline"]
            if event["event"] == "workflow.node.workflow"
        )
        assert workflow_node["workflow_node_id"] == "child-flow"
        assert workflow_node["status"] == "completed"
        assert workflow_node["child_run_id"] == child_workflow_run_id
        assert [event["payload"].get("status") for event in parent_workflow_node_events] == [
            "approval_required",
            "completed",
        ]
        assert parent_workflow_node_events[-1]["payload"]["workflow_node_kind"] == "workflow"
        assert parent_workflow_node_events[-1]["payload"]["result"] == "Subworkflow child approved result"
        assert len(calls) == 2
    finally:
        service.close()


def test_workflow_loop_node_repeats_until_condition_exits_to_artifact(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    calls: list[str] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        context = str(messages[1]["content"])
        calls.append(context)
        if len(calls) < 3:
            return {"content": "again"}
        return {"content": "done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        worker = service.create_agent(
            {"name": "Loop Worker", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Loop Flow",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "worker",
                        "type": "agent",
                        "data": {
                            "label": "Worker",
                            "agent_id": worker["agent_id"],
                            "task": "Return again until the work is done.",
                        },
                    },
                    {
                        "id": "repeat",
                        "type": "loop",
                        "data": {"label": "Repeat", "condition": "again", "max_iterations": 5},
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Loop Report", "artifact_path": "reports/loop.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "worker"},
                    {"source": "worker", "target": "repeat"},
                    {"source": "repeat", "target": "worker", "data": {"branch": "continue"}},
                    {"source": "repeat", "target": "report", "data": {"branch": "exit"}},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run loop workflow"}
        )
        steps = [
            event for event in run["timeline"]
            if str(event.get("event") or "").startswith("workflow.node.")
        ]
        loop_events = [event for event in steps if event["event"] == "workflow.node.loop"]
        artifact = service.read_run_artifact(run["run_id"], "reports/loop.md")

        assert run["status"] == "completed"
        assert run["result"] == "done"
        assert artifact["content"] == "done"
        assert [event.get("workflow_node_id") for event in steps] == [
            "start",
            "worker",
            "repeat",
            "worker",
            "repeat",
            "worker",
            "repeat",
            "report",
        ]
        assert [event["workflow_node_selected_branch"] for event in loop_events] == [
            "continue",
            "continue",
            "exit",
        ]
        assert [event["workflow_node_loop_iteration"] for event in loop_events] == [1, 2, 2]
        assert [event["workflow_node_selected_target"] for event in loop_events] == [
            "worker",
            "worker",
            "report",
        ]
        assert loop_events[-1]["workflow_node_condition_matched"] is False
        assert loop_events[-1]["workflow_node_loop_limit_reached"] is False
        assert len(calls) == 3
    finally:
        service.close()


def test_workflow_run_fails_when_context_budget_is_exceeded(tmp_path):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_context_chars=20)
    try:
        workflow = service.create_workflow(
            {
                "name": "Workflow Context Budget",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Context Report", "artifact_path": "reports/context-budget.md"},
                    },
                ],
                "edges": [{"source": "start", "target": "report"}],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "large workflow context"}
        )

        assert run["status"] == "failed"
        assert "max_context_chars=20" in run["result"]
        assert not any(artifact.get("path") == "reports/context-budget.md" for artifact in run["artifacts"])
        failure = next(event for event in run["timeline"] if event["event"] == "workflow.run.failed")
        assert failure["workflow_node_id"] == "start"
    finally:
        service.close()


def test_workflow_step_budget_survives_child_approval_resume(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_workflow_steps=2)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf workflow-budget"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "workflow-budget" in messages[-1]["content"]
        return {"content": "Design approved"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {
                "name": "Workflow Budget Design Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        code_agent = service.create_agent(
            {"name": "Workflow Budget Code Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Workflow Step Budget",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "design",
                        "type": "agent",
                        "data": {"label": "Design", "agent_id": design_agent["agent_id"]},
                    },
                    {
                        "id": "code",
                        "type": "agent",
                        "data": {"label": "Code", "agent_id": code_agent["agent_id"]},
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Budget Report", "artifact_path": "reports/workflow-budget.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "code"},
                    {"source": "code", "target": "report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run workflow step budget"}
        )
        group = service.get_run_group(run["run_group_id"])
        design_run_id = next(
            run_id
            for run_id in group["child_run_ids"]
            if run_id != run["run_id"] and service.get_run(run_id)["kind"] == "agent_run"
        )

        assert run["status"] == "approval_required"

        approved_design = service.approve_run_approval(design_run_id)
        failed_parent = service.get_run(run["run_id"])
        failed_group = service.get_run_group(run["run_group_id"])
        child_runs = [
            service.get_run(run_id)
            for run_id in failed_group["child_run_ids"]
            if run_id != run["run_id"]
        ]

        assert approved_design["status"] == "completed"
        assert failed_parent["status"] == "failed"
        assert "max_workflow_steps=2" in failed_parent["result"]
        assert [child["runnable_id"] for child in child_runs] == [design_agent["agent_id"]]
        assert len(calls) == 2
        failure = next(event for event in failed_parent["timeline"] if event["event"] == "workflow.run.failed")
        assert failure["workflow_node_id"] == "code"
    finally:
        service.close()


def test_workflow_run_fails_when_duration_budget_is_exceeded_between_nodes(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    service.runtime_limits = service.runtime_limits.__class__(max_run_duration_seconds=1)
    clock = {"now": 1000.0}
    calls: list[list[dict]] = []

    def fake_time():
        return clock["now"]

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        calls.append(messages)
        clock["now"] = 1002.0
        return {"content": "Agent finished after timeout"}

    monkeypatch.setattr("apps.shell.agent_runtime.time.time", fake_time)
    monkeypatch.setattr("apps.shell.agent.runtime.budget.time.time", fake_time)
    monkeypatch.setattr("apps.shell.agent_runtime._iso_epoch", lambda _value: 1000.0)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    service.workflow_continuation._iso_epoch = lambda _value: 1000.0
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        worker = service.create_agent(
            {"name": "Workflow Duration Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = service.create_workflow(
            {
                "name": "Workflow Duration Budget",
                "nodes": [
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "worker",
                        "type": "agent",
                        "data": {"label": "Worker", "agent_id": worker["agent_id"]},
                    },
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Duration Report", "artifact_path": "reports/duration-budget.md"},
                    },
                ],
                "edges": [
                    {"source": "start", "target": "worker"},
                    {"source": "worker", "target": "report"},
                ],
            }
        )

        run = service.create_workflow_run(
            {"workflow_id": workflow["workflow_id"], "user_goal": "Run workflow duration budget"}
        )

        assert run["status"] == "failed"
        assert "max_run_duration_seconds=1" in run["result"]
        assert not any(artifact.get("path") == "reports/duration-budget.md" for artifact in run["artifacts"])
        assert len(calls) == 1
        failure = next(event for event in run["timeline"] if event["event"] == "workflow.run.failed")
        assert failure["workflow_node_id"] == "report"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_run_route_rejects_start_only_saved_draft(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    try:
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Draft Only",
                nodes=[{"id": "start", "type": "start", "data": {"label": "Start"}}],
                edges=[],
            )
        )

        with pytest.raises(HTTPException) as invalid:
            await agent_routes.create_workflow_run(
                agent_routes.WorkflowRunRequest(
                    workflow_id=workflow["workflow_id"],
                    user_goal="Run empty draft",
                )
            )

        assert invalid.value.status_code == 400
        assert "至少需要一个可执行节点" in str(invalid.value.detail)
        assert service.list_runs()["runs"] == []
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_approval_route_resumes_runtime_snapshot_after_edit(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        calls.append(messages)
        context = str(messages[1]["content"])
        assert "Name: Original After Approval" in context
        assert "Name: Edited After Approval" not in context
        return {"content": "Original route agent complete"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        original_agent = service.create_agent(
            {"name": "Original After Approval", "model_mode": "custom_api", "model_config": model_config}
        )
        edited_agent = service.create_agent(
            {"name": "Edited After Approval", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Editable Paused Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {
                        "id": "after",
                        "type": "agent",
                        "data": {"label": "Original After Approval", "agent_id": original_agent["agent_id"]},
                    },
                ],
                edges=[
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "after"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Wait then run")
        )
        assert run["status"] == "approval_required"

        await agent_routes.update_workflow(
            workflow["workflow_id"],
            agent_routes.WorkflowRequest(
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "gate", "type": "approval", "data": {"label": "Manual Gate"}},
                    {
                        "id": "after",
                        "type": "agent",
                        "data": {"label": "Edited After Approval", "agent_id": edited_agent["agent_id"]},
                    },
                ],
                edges=[
                    {"source": "start", "target": "gate"},
                    {"source": "gate", "target": "after"},
                ],
            ),
        )

        resumed = await agent_routes.approve_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=run["pending_approval"]["approval_id"],
            ),
        )

        assert resumed["status"] == "completed"
        assert resumed["result"] == "Original route agent complete"
        assert len(calls) == 1
        agent_event = next(event for event in resumed["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "after"
        assert agent_event["workflow_node_label"] == "Original After Approval"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_cancel_route_cancels_child_agent_approval(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, _messages, **_kwargs):
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf blocked"}),
                    },
                }
            ],
        }

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Route Needs Approval",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Cancel Child Approval Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Route Needs Approval", "agent_id": agent["agent_id"]},
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                edges=[
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "summary"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Run approval flow")
        )
        group = await agent_routes.get_run_group(run["run_group_id"])
        child_run_id = next(run_id for run_id in group["child_run_ids"] if run_id != run["run_id"])
        child = await agent_routes.get_any_run(child_run_id)
        assert run["status"] == "approval_required"
        assert child["status"] == "approval_required"

        cancelled_parent = await agent_routes.cancel_run(run["run_id"])
        cancelled_child = await agent_routes.get_any_run(child_run_id)

        assert cancelled_parent["status"] == "cancelled"
        assert cancelled_parent["result"] == "Workflow 已取消：Route Needs Approval"
        assert cancelled_child["status"] == "cancelled"
        assert cancelled_child["pending_approval"] == {}
        assert cancelled_child["result"] == "父 Workflow 已取消"
        cancelled_group = await agent_routes.get_run_group(run["run_group_id"])
        assert cancelled_group["status"] == "cancelled"
        cancelled_event = next(event for event in cancelled_parent["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["workflow_node_id"] == "agent"
        assert cancelled_event["workflow_node_label"] == "Route Needs Approval"
        assert cancelled_event["child_run_id"] == child_run_id
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_child_approval_route_approve_resumes_parent_workflow(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes
    from apps.bridge.routes import runs as run_routes

    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf route-approved"}),
                        },
                    }
                ],
            }
        assert messages[-1]["role"] == "tool"
        assert "route-approved" in messages[-1]["content"]
        return {"content": "Route child approved result"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Route Approval Child",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Child Approval Resume Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Route Approval Child", "agent_id": agent["agent_id"]},
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                edges=[
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "summary"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Run route approval flow")
        )
        group = await agent_routes.get_run_group(run["run_group_id"])
        child_run_id = next(run_id for run_id in group["child_run_ids"] if run_id != run["run_id"])
        child = await agent_routes.get_any_run(child_run_id)

        assert run["status"] == "approval_required"
        assert run["pending_approval"] == {}
        assert child["status"] == "approval_required"
        assert child["pending_approval"]["tool"] == "terminal.run"
        assert child["pending_approval"]["input_preview"]["command"] == "printf route-approved"

        listed = await agent_routes.list_runs(limit=20)
        parent_detail = await agent_routes.get_any_run(run["run_id"])
        child_detail = await agent_routes.get_any_run(child_run_id)
        parent_replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
        child_replay = await run_routes.list_run_events(child_run_id, after_sequence=0, limit=200)
        parent_replay_types = [event["event_type"] for event in parent_replay["events"]]
        child_replay_types = [event["event_type"] for event in child_replay["events"]]

        assert any(item["run_id"] == run["run_id"] for item in listed["runs"])
        assert not any(item["run_id"] == child_run_id for item in listed["runs"])
        assert parent_detail["status"] == "approval_required"
        assert parent_detail["pending_approval"] == {}
        parent_wait_event = next(
            event for event in parent_detail["timeline"]
            if event["event"] == "workflow.run.approval_required"
        )
        assert parent_wait_event["child_run_id"] == child_run_id
        assert parent_wait_event["workflow_node_id"] == "agent"
        assert child_detail["status"] == "approval_required"
        assert child_detail["pending_approval"]["tool"] == "terminal.run"
        replay_agent_before = [
            event for event in parent_replay["events"]
            if event["event_type"] == "workflow.node.agent"
            and event["payload"].get("child_run_id") == child_run_id
        ]
        assert [event["payload"].get("status") for event in replay_agent_before] == ["approval_required"]
        assert replay_agent_before[0]["payload"]["workflow_node_id"] == "agent"
        assert "workflow.run.approval_required" in parent_replay_types
        replay_wait_event = next(
            event for event in parent_replay["events"]
            if event["event_type"] == "workflow.run.approval_required"
        )
        assert replay_wait_event["payload"]["child_run_id"] == child_run_id
        assert replay_wait_event["payload"]["workflow_node_id"] == "agent"
        assert "agent.tool.approval_required" in child_replay_types

        approved_child = await agent_routes.approve_run_approval(
            child_run_id,
            agent_routes.ApprovalRejectRequest(
                approval_id=child_detail["pending_approval"]["approval_id"],
            ),
        )
        parent = await agent_routes.get_workflow_run(run["run_id"])
        completed_group = await agent_routes.get_run_group(run["run_group_id"])
        child_detail_after = await agent_routes.get_any_run(child_run_id)
        child_replay_after = await run_routes.list_run_events(child_run_id, after_sequence=0, limit=200)
        parent_replay_after = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
        child_replay_after_types = [event["event_type"] for event in child_replay_after["events"]]
        parent_replay_after_types = [event["event_type"] for event in parent_replay_after["events"]]

        assert approved_child["status"] == "completed"
        assert approved_child["pending_approval"] == {}
        assert approved_child["result"] == "Route child approved result"
        assert child_detail_after["status"] == "completed"
        assert child_detail_after["pending_approval"] == {}
        assert child_detail_after["result"] == "Route child approved result"
        assert any(event["event"] == "agent.tool.approval_approved" for event in child_detail_after["timeline"])
        assert any(event["event"] == "agent.tool.call" and event["detail"] == "terminal.run" for event in child_detail_after["timeline"])
        assert child_replay_after_types.count("agent.tool.approval_required") == 1
        assert child_replay_after_types.count("agent.tool.approval_approved") == 1
        assert "agent.tool.call" in child_replay_after_types
        assert "agent.run.completed" in child_replay_after_types
        approved_fact = next(
            event for event in child_replay_after["events"]
            if event["event_type"] == "agent.tool.approval_approved"
        )
        tool_fact = next(
            event for event in child_replay_after["events"]
            if event["event_type"] == "agent.tool.call"
            and event["payload"].get("tool") == "terminal.run"
        )
        assert approved_fact["payload"]["tool"] == "terminal.run"
        assert "route-approved" in json.dumps(tool_fact["payload"].get("result", {}), ensure_ascii=False)
        assert parent["status"] == "completed"
        assert parent["result"] == "Route child approved result"
        assert completed_group["status"] == "completed"
        assert any(event["event"] == "workflow.run.child_resumed" for event in parent["timeline"])
        assert any(event["event"] == "workflow.run.resumed" for event in parent["timeline"])
        agent_event = next(event for event in parent["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "agent"
        assert agent_event["workflow_node_label"] == "Route Approval Child"
        assert agent_event["child_run_id"] == child_run_id
        assert agent_event["status"] == "completed"
        artifact_event = next(event for event in parent["timeline"] if event["event"] == "workflow.node.artifact")
        assert artifact_event["workflow_node_id"] == "summary"
        assert artifact_event["status"] == "completed"
        assert artifact_event["artifact"]["path"] == "summary.md"
        assert parent_replay_after_types.count("workflow.run.approval_required") == 1
        assert parent_replay_after_types.count("workflow.run.child_resumed") == 1
        assert parent_replay_after_types.count("workflow.run.resumed") == 1
        assert "workflow.run.completed" in parent_replay_after_types
        replay_agent_after = [
            event for event in parent_replay_after["events"]
            if event["event_type"] == "workflow.node.agent"
            and event["payload"].get("child_run_id") == child_run_id
        ]
        assert [event["payload"].get("status") for event in replay_agent_after] == [
            "approval_required",
            "running",
            "completed",
        ]
        assert replay_agent_after[-1]["payload"]["workflow_node_id"] == "agent"
        assert replay_agent_after[-1]["payload"]["artifact_count"] == 0
        assert replay_agent_after[-1]["payload"]["result"] == "Route child approved result"
        replay_resumed_event = next(
            event for event in parent_replay_after["events"]
            if event["event_type"] == "workflow.run.resumed"
        )
        assert replay_resumed_event["payload"]["child_run_id"] == child_run_id
        assert replay_resumed_event["payload"]["workflow_node_id"] == "agent"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_child_consecutive_approvals_keep_parent_waiting(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        calls.append(messages)
        if len(calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_first_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf workflow-first-approved"}),
                        },
                    },
                    {
                        "id": "call_second_terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf workflow-second-approved"}),
                        },
                    },
                ],
            }
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        assert any("workflow-first-approved" in message.get("content", "") for message in tool_messages)
        assert any("workflow-second-approved" in message.get("content", "") for message in tool_messages)
        return {"content": "Workflow child consecutive approvals completed"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Route Consecutive Approval Child",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Child Consecutive Approval Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Route Consecutive Approval Child", "agent_id": agent["agent_id"]},
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                edges=[
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "summary"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Run two child approvals")
        )
        group = await agent_routes.get_run_group(run["run_group_id"])
        child_run_id = next(run_id for run_id in group["child_run_ids"] if run_id != run["run_id"])
        child = await agent_routes.get_any_run(child_run_id)

        assert run["status"] == "approval_required"
        assert child["status"] == "approval_required"
        assert child["pending_approval"]["input_preview"]["command"] == "printf workflow-first-approved"

        after_first = await agent_routes.approve_run_approval(
            child_run_id,
            agent_routes.ApprovalRejectRequest(
                approval_id=child["pending_approval"]["approval_id"],
            ),
        )
        parent_after_first = await agent_routes.get_workflow_run(run["run_id"])
        group_after_first = await agent_routes.get_run_group(run["run_group_id"])

        assert after_first["status"] == "approval_required", after_first.get("result")
        assert after_first["pending_approval"]["input_preview"]["command"] == "printf workflow-second-approved"
        assert parent_after_first["status"] == "approval_required"
        assert parent_after_first["pending_approval"] == {}
        assert parent_after_first["result"] == "等待审批：terminal.run"
        assert group_after_first["status"] == "approval_required"
        assert group_after_first["summary"] == "等待审批：terminal.run"
        approval_events = [
            event for event in parent_after_first["timeline"]
            if event["event"] == "workflow.run.approval_required"
        ]
        assert len(approval_events) == 2
        assert approval_events[-1]["child_run_id"] == child_run_id
        assert approval_events[-1]["workflow_node_id"] == "agent"
        assert approval_events[-1]["workflow_node_label"] == "Route Consecutive Approval Child"
        agent_event = next(event for event in parent_after_first["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["status"] == "approval_required"
        assert agent_event["child_run_id"] == child_run_id

        after_second = await agent_routes.approve_run_approval(
            child_run_id,
            agent_routes.ApprovalRejectRequest(
                approval_id=after_first["pending_approval"]["approval_id"],
            ),
        )
        parent_after_second = await agent_routes.get_workflow_run(run["run_id"])
        group_after_second = await agent_routes.get_run_group(run["run_group_id"])

        assert after_second["status"] == "completed"
        assert after_second["pending_approval"] == {}
        assert after_second["result"] == "Workflow child consecutive approvals completed"
        assert parent_after_second["status"] == "completed"
        assert parent_after_second["result"] == "Workflow child consecutive approvals completed"
        assert group_after_second["status"] == "completed"
        completed_agent_event = next(
            event for event in parent_after_second["timeline"] if event["event"] == "workflow.node.agent"
        )
        assert completed_agent_event["status"] == "completed"
        approved_events = [
            event for event in after_second["timeline"] if event["event"] == "agent.tool.approval_approved"
        ]
        assert [event["input_preview"]["command"] for event in approved_events] == [
            "printf workflow-first-approved",
            "printf workflow-second-approved",
        ]
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_child_approval_route_reject_cancels_parent_workflow(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes
    from apps.bridge.routes import runs as run_routes

    service = make_service(tmp_path)
    workdir = tmp_path / "repo"
    workdir.mkdir()

    def fake_chat(_base_url, _model, _api_key, _messages, *, tools=None):
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": "call_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps({"command": "printf route-blocked"}),
                    },
                }
            ],
        }

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        agent = service.create_agent(
            {
                "name": "Route Reject Child",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
                "tool_policy": {"allowed_tools": ["terminal.run"]},
                "workspace_policy": {"default_workdir": str(workdir), "readable_scopes": ["."]},
            }
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Child Approval Reject Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {
                        "id": "agent",
                        "type": "agent",
                        "data": {"label": "Route Reject Child", "agent_id": agent["agent_id"]},
                    },
                    {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                ],
                edges=[
                    {"source": "start", "target": "agent"},
                    {"source": "agent", "target": "summary"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Run route rejection flow")
        )
        group = await agent_routes.get_run_group(run["run_group_id"])
        child_run_id = next(run_id for run_id in group["child_run_ids"] if run_id != run["run_id"])
        child = await agent_routes.get_any_run(child_run_id)

        assert run["status"] == "approval_required"
        assert child["status"] == "approval_required"
        assert child["pending_approval"]["tool"] == "terminal.run"

        rejected_child = await agent_routes.reject_run_approval(
            child_run_id,
            agent_routes.ApprovalRejectRequest(
                approval_id=child["pending_approval"]["approval_id"],
                reason="not now",
            ),
        )
        parent = await agent_routes.get_workflow_run(run["run_id"])
        cancelled_group = await agent_routes.get_run_group(run["run_group_id"])
        child_detail_after = await agent_routes.get_any_run(child_run_id)
        child_replay_after = await run_routes.list_run_events(child_run_id, after_sequence=0, limit=200)
        parent_replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
        child_replay_after_types = [event["event_type"] for event in child_replay_after["events"]]

        assert rejected_child["status"] == "cancelled"
        assert rejected_child["pending_approval"] == {}
        assert "not now" in rejected_child["result"]
        assert child_detail_after["status"] == "cancelled"
        assert child_detail_after["pending_approval"] == {}
        assert "not now" in child_detail_after["result"]
        assert any(event["event"] == "agent.tool.approval_rejected" for event in child_detail_after["timeline"])
        assert child_replay_after_types.count("agent.tool.approval_required") == 1
        assert child_replay_after_types.count("agent.tool.approval_rejected") == 1
        assert "agent.run.cancelled" in child_replay_after_types
        rejected_fact = next(
            event for event in child_replay_after["events"]
            if event["event_type"] == "agent.tool.approval_rejected"
        )
        cancelled_fact = next(
            event for event in child_replay_after["events"]
            if event["event_type"] == "agent.run.cancelled"
        )
        assert rejected_fact["payload"]["tool"] == "terminal.run"
        assert rejected_fact["payload"]["reason"] == "not now"
        assert "not now" in cancelled_fact["payload"]["result"]
        assert parent["status"] == "cancelled"
        assert cancelled_group["status"] == "cancelled"
        agent_event = next(event for event in parent["timeline"] if event["event"] == "workflow.node.agent")
        assert agent_event["workflow_node_id"] == "agent"
        assert agent_event["workflow_node_kind"] == "agent"
        assert agent_event["workflow_node_label"] == "Route Reject Child"
        assert agent_event["status"] == "cancelled"
        cancelled_event = next(event for event in parent["timeline"] if event["event"] == "workflow.run.cancelled")
        assert cancelled_event["workflow_node_id"] == "agent"
        assert cancelled_event["workflow_node_kind"] == "agent"
        assert cancelled_event["workflow_node_label"] == "Route Reject Child"
        assert cancelled_event["child_run_id"] == child_run_id
        replay_agent_events = [
            event for event in parent_replay["events"]
            if event["event_type"] == "workflow.node.agent"
            and event["payload"].get("child_run_id") == child_run_id
        ]
        assert [event["payload"].get("status") for event in replay_agent_events] == [
            "approval_required",
            "cancelled",
        ]
        replay_cancelled = next(
            event for event in parent_replay["events"]
            if event["event_type"] == "workflow.run.cancelled"
        )
        assert replay_cancelled["payload"]["child_run_id"] == child_run_id
        assert replay_cancelled["payload"]["workflow_node_id"] == "agent"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_child_artifact_route_reads_source_run_artifact(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        calls.append(messages)
        if len(calls) == 1:
            return {
                "content": json.dumps(
                    {
                        "action": "tool",
                        "tool": "artifact.write",
                        "input": {"path": "design.md", "content": "route design artifact"},
                    }
                )
            }
        if len(calls) == 2:
            assert "Tool result for artifact.write" in messages[-1]["content"]
            return {"content": "Design done"}
        return {"content": "Code done"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {
                "name": "Route Design Artifact Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["artifact.write"]},
            }
        )
        coding_agent = service.create_agent(
            {"name": "Route Coding Summary Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Artifact Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "design", "type": "agent", "data": {"label": "Design", "agent_id": design_agent["agent_id"]}},
                    {"id": "code", "type": "agent", "data": {"label": "Code", "agent_id": coding_agent["agent_id"]}},
                ],
                edges=[
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "code"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Ship artifacts")
        )
        parent = await agent_routes.get_workflow_run(run["run_id"])
        design_ref = next(
            artifact
            for artifact in parent["artifacts"]
            if artifact.get("kind") == "workflow_child_artifact" and artifact.get("path") == "design.md"
        )

        artifact = await agent_routes.get_run_artifact(design_ref["source_run_id"], design_ref["path"])

        assert artifact["ok"] is True
        assert artifact["path"] == "design.md"
        assert artifact["content"] == "route design artifact"
        assert design_ref["source_runnable_name"] == "Route Design Artifact Agent"
        assert design_ref["workflow_step_label"] == "Design"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_workflow_artifact_review_route_exposes_outputs_and_reruns(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes
    from apps.bridge.routes import runs as run_routes

    service = make_service(tmp_path)
    calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, **_kwargs):
        calls.append(messages)
        if len(calls) in {1, 4}:
            return {
                "content": json.dumps(
                    {
                        "action": "tool",
                        "tool": "artifact.write",
                        "input": {
                            "path": "design.md",
                            "content": f"design artifact run {1 if len(calls) == 1 else 2}",
                        },
                    }
                )
            }
        if len(calls) in {2, 5}:
            assert "Tool result for artifact.write" in messages[-1]["content"]
            return {"content": f"Design done run {1 if len(calls) == 2 else 2}"}
        return {"content": f"Code final result run {1 if len(calls) == 3 else 2}"}

    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        model_config = {
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
        }
        design_agent = service.create_agent(
            {
                "name": "Route Design Artifact Agent",
                "model_mode": "custom_api",
                "model_config": model_config,
                "tool_policy": {"allowed_tools": ["artifact.write"]},
            }
        )
        coding_agent = service.create_agent(
            {"name": "Route Coding Final Agent", "model_mode": "custom_api", "model_config": model_config}
        )
        workflow = await agent_routes.create_workflow(
            agent_routes.WorkflowRequest(
                name="Route Artifact Review Flow",
                nodes=[
                    {"id": "start", "type": "start", "data": {"label": "Start"}},
                    {"id": "design", "type": "agent", "data": {"label": "Design", "agent_id": design_agent["agent_id"]}},
                    {"id": "code", "type": "agent", "data": {"label": "Code", "agent_id": coding_agent["agent_id"]}},
                    {
                        "id": "report",
                        "type": "artifact",
                        "data": {"label": "Final Report", "artifact_path": "reports/final.md"},
                    },
                ],
                edges=[
                    {"source": "start", "target": "design"},
                    {"source": "design", "target": "code"},
                    {"source": "code", "target": "report"},
                ],
            )
        )
        run = await agent_routes.create_workflow_run(
            agent_routes.WorkflowRunRequest(workflow_id=workflow["workflow_id"], user_goal="Ship artifact review")
        )
        parent = await agent_routes.get_workflow_run(run["run_id"])

        assert parent["status"] == "completed"
        assert parent["result"] == "Code final result run 1"
        assert parent["runnable_id"] == workflow["workflow_id"]
        assert parent["user_goal"] == "Ship artifact review"
        child_ref = next(
            artifact
            for artifact in parent["artifacts"]
            if artifact.get("kind") == "workflow_child_artifact" and artifact.get("path") == "design.md"
        )
        workflow_ref = next(
            artifact
            for artifact in parent["artifacts"]
            if artifact.get("kind") == "workflow_artifact" and artifact.get("path") == "reports/final.md"
        )
        assert child_ref["workflow_step_label"] == "Design"
        assert child_ref["source_runnable_name"] == "Route Design Artifact Agent"
        assert workflow_ref["workflow_node_id"] == "report"
        assert workflow_ref["workflow_node_label"] == "Final Report"

        child_artifact = await agent_routes.get_run_artifact(child_ref["source_run_id"], child_ref["path"])
        workflow_artifact = await agent_routes.get_run_artifact(parent["run_id"], workflow_ref["path"])

        assert child_artifact["content"] == "design artifact run 1"
        assert workflow_artifact["content"] == "Code final result run 1"
        steps = [event for event in parent["timeline"] if str(event.get("event") or "").startswith("workflow.node.")]
        assert [(event["event"], event.get("workflow_node_id"), event.get("status")) for event in steps] == [
            ("workflow.node.start", "start", "completed"),
            ("workflow.node.agent", "design", "completed"),
            ("workflow.node.agent", "code", "completed"),
            ("workflow.node.artifact", "report", "completed"),
        ]
        replay = await run_routes.list_run_events(parent["run_id"], after_sequence=0, limit=200)
        workflow_detail_events = {
            "workflow.node.start",
            "workflow.node.agent",
            "workflow.node.workflow",
            "workflow.node.artifact",
            "workflow.node.condition",
            "workflow.node.parallel",
            "workflow.node.loop",
        }
        replay_steps = [
            event for event in replay["events"]
            if str(event.get("event_type") or "") in workflow_detail_events
        ]
        assert [
            (
                event["event_type"],
                event["payload"].get("workflow_node_id"),
                event["payload"].get("status"),
            )
            for event in replay_steps
        ] == [
            ("workflow.node.start", "start", "completed"),
            ("workflow.node.agent", "design", "completed"),
            ("workflow.node.agent", "code", "completed"),
            ("workflow.node.artifact", "report", "completed"),
        ]
        replay_design = next(
            event for event in replay_steps
            if event["event_type"] == "workflow.node.agent"
            and event["payload"].get("workflow_node_id") == "design"
        )
        replay_artifact = next(
            event for event in replay_steps
            if event["event_type"] == "workflow.node.artifact"
        )
        assert replay_design["payload"]["child_run_id"] == child_ref["source_run_id"]
        assert replay_design["payload"]["artifact_count"] == 1
        assert replay_artifact["payload"]["artifact"]["path"] == "reports/final.md"

        rerun = await agent_routes.rerun_run(parent["run_id"])
        rerun_detail = await agent_routes.get_any_run(rerun["run_id"])
        rerun_replay = await run_routes.list_run_events(rerun["run_id"], after_sequence=0, limit=200)
        rerun_artifact = await agent_routes.get_run_artifact(rerun["run_id"], "reports/final.md")
        rerun_group = service.get_run_group(rerun["run_group_id"])
        rerun_event = rerun["timeline"][0]
        rerun_replay_types = [event["event_type"] for event in rerun_replay["events"]]
        rerun_replay_event = next(
            event for event in rerun_replay["events"]
            if event["event_type"] == "run.rerun.started"
        )

        assert rerun["run_id"] != parent["run_id"]
        assert rerun["status"] == "completed"
        assert rerun["result"] == "Code final result run 2"
        assert rerun["workflow_run_id"] == rerun["run_id"]
        assert rerun_detail["run_id"] == rerun["run_id"]
        assert rerun_detail["status"] == "completed"
        assert rerun_detail["run_group_source"] == "rerun"
        assert rerun_detail["timeline"][0]["event"] == "run.rerun.started"
        assert rerun_group["source"] == "rerun"
        assert rerun_event["event"] == "run.rerun.started"
        assert rerun_event["rerun_of_run_id"] == parent["run_id"]
        assert rerun_event["input_preview"]["original_status"] == "completed"
        assert rerun_event["input_preview"]["original_goal"] == parent["user_goal"]
        assert rerun_replay_types.count("run.rerun.started") == 1
        assert "workflow.node.artifact" in rerun_replay_types
        assert "workflow.run.completed" in rerun_replay_types
        assert rerun_replay_event["payload"]["rerun_of_run_id"] == parent["run_id"]
        assert rerun_replay_event["payload"]["input_preview"]["original_status"] == "completed"
        assert rerun_replay_event["payload"]["input_preview"]["original_goal"] == parent["user_goal"]
        assert rerun_artifact["content"] == "Code final result run 2"
    finally:
        service.close()


@pytest.mark.asyncio
async def test_skill_update_route_toggles_enabled_and_returns_404(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    source = tmp_path / "route-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# Route Skill\n\nRoute import.", encoding="utf-8")
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    try:
        skill = service.import_skill(str(source))
        updated = await agent_routes.update_skill(
            skill["skill_id"],
            agent_routes.SkillUpdateRequest(enabled=False),
        )
        assert updated["enabled"] is False

        with pytest.raises(HTTPException) as missing:
            await agent_routes.update_skill("missing", agent_routes.SkillUpdateRequest(enabled=True))
        assert missing.value.status_code == 404
    finally:
        service.close()


@pytest.mark.asyncio
async def test_skill_folder_routes_rename_delete_and_validate(tmp_path, monkeypatch):
    from fastapi import HTTPException

    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    source = tmp_path / "route-folder-skill"
    source.mkdir()
    (source / "SKILL.md").write_text("# Route Folder Skill\n\nRoute folder.", encoding="utf-8")
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    try:
        folder = await agent_routes.create_skill_folder(agent_routes.SkillFolderRequest(name="Writing"))
        skill = service.import_skill(str(source), folder["folder_id"])

        renamed = await agent_routes.update_skill_folder(
            folder["folder_id"],
            agent_routes.SkillFolderRequest(name="Docs"),
        )
        assert renamed["name"] == "Docs"
        assert service.get_skill(skill["skill_id"])["folder_name"] == "Docs"

        duplicate = await agent_routes.create_skill_folder(agent_routes.SkillFolderRequest(name="Research"))
        with pytest.raises(HTTPException) as duplicate_name:
            await agent_routes.update_skill_folder(
                duplicate["folder_id"],
                agent_routes.SkillFolderRequest(name="docs"),
            )
        assert duplicate_name.value.status_code == 400

        deleted = await agent_routes.delete_skill_folder(folder["folder_id"])
        assert deleted["ok"] is True
        assert service.get_skill(skill["skill_id"])["folder_id"] == ""

        destructive_folder = await agent_routes.create_skill_folder(agent_routes.SkillFolderRequest(name="Temporary"))
        destructive_skill = service.import_skill(str(source), destructive_folder["folder_id"])
        deleted_with_skills = await agent_routes.delete_skill_folder(destructive_folder["folder_id"], delete_skills=True)
        assert deleted_with_skills["ok"] is True
        assert deleted_with_skills["deleted_skill_count"] == 1
        with pytest.raises(KeyError):
            service.get_skill(destructive_skill["skill_id"])

        with pytest.raises(HTTPException) as missing:
            await agent_routes.delete_skill_folder("folder_missing")
        assert missing.value.status_code == 404
    finally:
        service.close()


@pytest.mark.asyncio
async def test_skill_sync_and_install_routes(tmp_path, monkeypatch):
    from apps.bridge.routes import agents as agent_routes

    service = make_service(tmp_path)
    native_home = tmp_path / ".oha-yachiyo" / "skill-library"

    def fake_run(argv, **_kwargs):
        skill_root = Path(_kwargs["cwd"]) / ".skills" / "skills" / "office" / "route-installed"
        skill_root.mkdir(parents=True)
        (skill_root / "SKILL.md").write_text("# Route Installed\n\nRoute install.", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0, stdout="installed", stderr="")

    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / ".oha-yachiyo"))
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr("apps.shell.agent_runtime.subprocess.run", fake_run)
    service.skill_install_service._run_command = fake_run
    try:
        sources = await agent_routes.list_skill_sources()
        assert sources["roots"][0]["path"] == str(native_home / "skills")

        folder = await agent_routes.create_skill_folder(agent_routes.SkillFolderRequest(name="Office"))
        installed = await agent_routes.install_skill(
            agent_routes.SkillInstallRequest(command="skills@latest add owner/repo", folder_id=folder["folder_id"])
        )
        assert installed["ok"] is True
        assert installed["sync"]["summary"]["imported"] == 1
        skills = service.list_skills()["skills"]
        assert skills[0]["folder_id"] == folder["folder_id"]
        assert skills[0]["folder_name"] == "Office"

        synced = await agent_routes.sync_native_skills()
        assert synced["summary"]["skipped"] >= 1
    finally:
        service.close()
