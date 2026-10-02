"""Public Yachiyo Agent contract tests."""

from __future__ import annotations

import json
import os
from typing import Any, get_args

import pytest
from pydantic import ValidationError

import apps.shell.yachiyo_agent.isolated_provider_session as isolated_session_module
from apps.shell.agent.tools.plugins import (
    RestrictedPluginTool,
    RestrictedToolPluginManager,
    RestrictedToolPlugin,
    clear_restricted_tool_plugins,
    register_restricted_tool_plugin,
    unregister_restricted_tool_plugin,
)
from apps.shell.agent.runtime.main_chat_config import MAIN_CHAT_DESKTOP_AGENT_INSTRUCTIONS
from apps.shell.agent.runtime.approval_snapshots import public_pending_approval
from apps.shell.agent.runtime.desktop_execution_providers import (
    LOCAL_DESKTOP_PROVIDER_ID,
    LOCAL_DESKTOP_PROVIDER_KIND,
    desktop_execution_provider_registry_from_env,
    desktop_execution_provider_status_from_env,
    local_desktop_execution_provider_status,
)
from apps.shell.yachiyo_agent.runtime_debug_snapshots import (
    runtime_debug_summary_from_runtime_objects,
)
import apps.shell.yachiyo_agent.desktop_execution_policy as desktop_policy_module
from apps.shell.yachiyo_agent import (
    AgentDefinitionSnapshot,
    AgentDeskFileEventRequest,
    AgentDeskItemSnapshot,
    AgentDeskSnapshot,
    AgentStudioService,
    AgentGroupMemberSnapshot,
    AgentGroupSnapshot,
    AgentTaskLightSnapshot,
    AgentTaskSnapshot,
    ApprovalCardSnapshot,
    ArtifactContentSnapshot,
    ArtifactSnapshot,
    CapabilityCategory,
    CapabilitySnapshot,
    ChatRunnableCatalogSnapshot,
    ChatRunnableParticipantSnapshot,
    ChatRunnableSnapshot,
    ControlledDesktopProviderDiagnosticSnapshot,
    DesktopExecutionLoopSnapshot,
    DesktopActionRiskSnapshot,
    DesktopExecutionCapabilitySnapshot,
    DesktopExecutionRouteSnapshot,
    DesktopExecutionModeSnapshot,
    DesktopExecutionPolicySnapshot,
    DesktopProviderConformanceSnapshot,
    DesktopProviderHealthSnapshot,
    DesktopRecoveryActionMetadataSnapshot,
    FutureTaskSnapshot,
    FutureTaskTriggerResultSnapshot,
    GroupRunSnapshot,
    InstallRestrictedToolPluginRequest,
    CapabilityPlanItemSnapshot,
    CapabilityPlanSnapshot,
    MemorySnapshot,
    MemoryTraceSnapshot,
    PlannerDecisionSnapshot,
    PlannerOrchestrationStartSnapshot,
    PlannerTraceSummarySnapshot,
    PublicRunEvent,
    RecoveryRunProvenanceSnapshot,
    ReplanContinuationSnapshot,
    RunEventPageSnapshot,
    RunTimelineChildSnapshot,
    RunTimelineSnapshot,
    RuntimeCheckpointPolicySnapshot,
    RuntimeDebugSummarySnapshot,
    RuntimeExecutionEnvelopeSnapshot,
    RuntimeExecutionStrategySnapshot,
    RuntimeExecutionRequestSnapshot,
    SandboxDesktopProviderSnapshot,
    RuntimePlanSnapshot,
    RestrictedPluginToolSnapshot,
    RestrictedToolPluginSnapshot,
    SaveAgentDeskFileRequest,
    SaveAgentDeskNoteRequest,
    SaveAgentGroupMemberRequest,
    SaveAgentGroupRequest,
    SaveAgentRequest,
    SaveWorkflowRequest,
    SkillFolderSnapshot,
    SkillSnapshot,
    SkillSourceRootSnapshot,
    SkillTraceSnapshot,
    StartChatTaskRequest,
    StartPlannerOrchestrationRequest,
    ReplanRecoveryActionSnapshot,
    ReplanRecoverySnapshot,
    ReplanSignalSnapshot,
    TaskCheckpointSnapshot,
    TaskCoreSnapshot,
    TaskProgressSummarySnapshot,
    TaskIntentSnapshot,
    TaskIntentKind,
    TaskReplanRequestSnapshot,
    TaskTodoItemSnapshot,
    TaskWorkspaceItemSnapshot,
    TaskWorkspaceSnapshot,
    ToolCatalogItemSnapshot,
    ToolCatalogSnapshot,
    ToolCallSnapshot,
    ToolPlanSnapshot,
    ToolPlanStepSnapshot,
    UpdateRestrictedToolPluginRequest,
    WorkflowRunSnapshot,
    WorkflowSnapshot,
    approval_is_pending,
    daily_entrypoint_desktop_execution_policy,
    desktop_provider_session_auto_start_default,
    desktop_provider_session_auto_start_recommended_for_requests,
    desktop_provider_session_strict_foreground_default,
    desktop_action_risk_level,
    desktop_action_risk_snapshots,
    desktop_tool_execution_mode,
    desktop_tool_execution_mode_for_input,
    desktop_execution_capability_snapshots,
    desktop_execution_route_decision,
    runtime_execution_envelope_with_desktop_execution_policy,
    desktop_tool_risk_level,
    sandbox_desktop_provider_status,
    with_agent_studio_desktop_execution_policy,
    with_daily_entrypoint_desktop_execution_policy,
    is_high_risk_desktop_action,
    task_requires_user_action,
)
from apps.shell.yachiyo_agent.controlled_provider_diagnostics import (
    controlled_desktop_provider_diagnostics_snapshot,
)
from apps.shell.yachiyo_agent.desktop_provider_contract import (
    OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS,
)
from apps.shell.yachiyo_agent.daily_desktop import (
    daily_desktop_direct_metadata_request,
    daily_desktop_runtime_execution_envelope,
)
from apps.shell.yachiyo_agent.approvals import approval_card_from_payload
from apps.shell.yachiyo_agent.capability_registry import runtime_execution_tool_names
from apps.shell.yachiyo_agent.events import public_run_event_from_payload
from apps.shell.yachiyo_agent.group_run_snapshots import group_run_snapshot_from_payload
from apps.shell.yachiyo_agent.planner_execution import planner_direct_tool_requests
from apps.shell.yachiyo_agent.planner_projection import planner_run_event_payloads
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_from_decision,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.replan_recovery_snapshots import (
    replan_recovery_snapshots_from_runtime_execution_envelope,
)
from apps.shell.yachiyo_agent.run_snapshots import run_timeline_snapshot_from_payload
from apps.shell.yachiyo_agent.runtime_execution_status import (
    runtime_execution_envelope_with_status_overlay,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner
from apps.shell.yachiyo_agent.task_cards import (
    agent_task_light_snapshot_from_task,
    agent_task_snapshot_from_payload,
)
from apps.shell.yachiyo_agent.tool_catalog import runtime_tool_catalog_snapshot
from apps.shell.yachiyo_agent.workflow_run_snapshots import workflow_run_snapshot_from_payload


def _json(model) -> dict:
    return json.loads(model.model_dump_json())


class _FakeStudioOrchestrationPort:
    def list_workflows(self) -> dict[str, Any]:
        return {
            "workflows": [
                {
                    "workflow_id": "workflow-1",
                    "name": "Review workflow",
                    "nodes": [],
                    "edges": [],
                }
            ]
        }

    def start_workflow_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "run_id": "workflow-run-1",
            "workflow_run_id": "workflow-run-1",
            "workflow_id": payload.get("workflow_id") or "workflow-1",
            "kind": "workflow_run",
            "status": "running",
            "title": payload.get("title") or "Review workflow",
            "objective": payload.get("objective") or "Build report",
            "timeline": [{"event": "workflow.run.started"}],
        }

    def list_groups(self) -> dict[str, Any]:
        return {
            "groups": [
                {
                    "group_id": "group-1",
                    "name": "Research squad",
                    "members": [],
                }
            ]
        }

    def start_group_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "group_run_id": "group-run-1",
            "run_group_id": "group-run-1",
            "group_id": payload.get("group_id") or "group-1",
            "title": payload.get("title") or "Research squad",
            "objective": payload.get("objective") or "Research Hanako",
            "status": "running",
            "events": [{"event": "group.run.started"}],
            "runs": [],
            "child_run_ids": [],
        }


def test_task_intent_kind_contract_covers_runtime_planner_routes() -> None:
    assert {
        "system_control",
        "file_access",
        "file_organization",
        "clipboard_operation",
        "information_capture",
    }.issubset(set(get_args(TaskIntentKind)))


def test_capability_category_contract_covers_runtime_registry_categories() -> None:
    assert {
        "capture",
        "clipboard",
        "memory",
        "skill",
        "system",
    }.issubset(set(get_args(CapabilityCategory)))


def test_tool_plan_step_snapshot_exposes_runtime_action() -> None:
    snapshot = ToolPlanStepSnapshot(
        step_id="discover-desktop-state",
        title="Discover desktop state",
        capability_id="desktop.app_discovery",
        action="list_apps",
        tool_name="desktop.running_apps",
        reason="Inspect before acting.",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "step_id",
        "title",
        "capability_id",
        "action",
        "tool_name",
        "input_preview",
        "risk_level",
        "execution_mode",
        "approval_required",
        "depends_on",
        "input_bindings",
        "reason",
        "fallback_tools",
        "status",
    ]
    assert payload["action"] == "list_apps"
    assert payload["input_bindings"] == []


def test_planner_public_snapshots_explain_intent_capabilities_and_tool_plan() -> None:
    intent = TaskIntentSnapshot(
        intent_id="intent-1",
        kind="data_analysis",
        title="Analyze sales CSV",
        user_goal="分析 sales.csv 并输出报告",
        confidence=0.92,
        description="Read a local dataset and write a report artifact.",
        inputs={"path": "sales.csv"},
        expected_outputs=["markdown_report", "chart"],
        required_capabilities=["workspace.file_read", "data.analysis", "artifact.output"],
        preferred_capabilities=["desktop.app_discovery"],
        risk_level="medium",
    )
    capability = CapabilitySnapshot(
        capability_id="data.analysis",
        title="Data analysis",
        category="data_analysis",
        description="Analyze structured data with Python.",
        tools=["data.analyze", "terminal.run"],
        available_tools=["data.analyze"],
        missing_tools=["terminal.run"],
        risk_level="medium",
        approval_required=True,
        discovery_actions=["inspect_file"],
        execution_actions=["run_analysis"],
        output_kinds=["markdown", "chart"],
    )
    capability_plan = CapabilityPlanSnapshot(
        plan_id="capability-plan-1",
        title="Analyze Capability Plan",
        intent_kind="data_analysis",
        items=[
            CapabilityPlanItemSnapshot(
                capability_id="data.analysis",
                title="Analyze Data",
                category="data",
                status="degraded",
                required=True,
                reason="Selected because the tool plan has concrete steps for this capability.",
                selected_tools=["data.analyze"],
                available_tools=["data.analyze"],
                missing_tools=["terminal.run"],
                planned_step_ids=["run-analysis"],
                execution_actions=["run_analysis"],
                output_kinds=["markdown", "chart"],
                risk_level="medium",
                approval_required=True,
            )
        ],
        required_capabilities=["workspace.file_read", "data.analysis", "artifact.output"],
        preferred_capabilities=["desktop.app_discovery"],
        available_capabilities=["data.analysis"],
        missing_capabilities=[],
        approvals_required=["data.analysis"],
    )
    tool_plan = ToolPlanSnapshot(
        plan_id="tool-plan-1",
        title="Analyze and write report",
        steps=[
            ToolPlanStepSnapshot(
                step_id="inspect-data-source",
                title="Inspect data source",
                capability_id="workspace.file_read",
                action="read_file",
                tool_name="workspace.read",
                input_preview={"path": "sales.csv"},
                reason="Confirm the dataset shape before analysis.",
            ),
            ToolPlanStepSnapshot(
                step_id="run-analysis",
                title="Run analysis",
                capability_id="data.analysis",
                action="analyze",
                tool_name="data.analyze",
                input_preview={"path": "sales.csv"},
                risk_level="medium",
                approval_required=True,
                depends_on=["inspect-data-source"],
                reason="Compute summary statistics and charts.",
                fallback_tools=["terminal.run"],
            ),
        ],
        required_capabilities=["workspace.file_read", "data.analysis", "artifact.output"],
        approvals_required=["run-analysis"],
        artifacts_expected=["markdown_report", "chart"],
    )
    execution_strategy = RuntimeExecutionStrategySnapshot(
        strategy_id="execution-strategy-1",
        preferred_environment="isolated_desktop",
        interaction_mode="foreground",
        policy_mode="preview_input",
        isolated_desktop_preferred=True,
        foreground_takeover_allowed=False,
        sandbox_required=True,
        foreground_control_step_count=1,
        keyboard_mouse_step_count=1,
        sandbox_recommended_step_count=1,
        approval_step_count=1,
        reasons=["keyboard_mouse_capture_planned"],
        mitigations=["do_not_take_over_user_foreground_session"],
    )
    runtime_plan = RuntimePlanSnapshot(
        plan_id="runtime-plan-1",
        intent=intent,
        capabilities=[capability],
        capability_plan=capability_plan,
        tool_plan=tool_plan,
        execution_strategy=execution_strategy,
        timeline_preview=[{"event_type": "agent.plan.created"}],
    )
    decision = PlannerDecisionSnapshot(
        decision_id="decision-1",
        prompt="分析 sales.csv 并输出报告",
        selected_intent=intent,
        candidate_intents=[intent],
        plan=runtime_plan,
        created_at="2026-06-27T00:00:00Z",
    )

    payload = _json(decision)

    assert list(payload) == [
        "decision_id",
        "prompt",
        "selected_intent",
        "candidate_intents",
        "plan",
        "created_at",
        "source",
    ]
    assert payload["selected_intent"]["required_capabilities"] == [
        "workspace.file_read",
        "data.analysis",
        "artifact.output",
    ]
    assert payload["plan"]["capabilities"][0]["available_tools"] == ["data.analyze"]
    assert payload["plan"]["capabilities"][0]["missing_tools"] == ["terminal.run"]
    assert payload["plan"]["capability_plan"]["items"][0]["capability_id"] == "data.analysis"
    assert payload["plan"]["capability_plan"]["approvals_required"] == ["data.analysis"]
    assert payload["plan"]["tool_plan"]["approvals_required"] == ["run-analysis"]
    assert payload["plan"]["tool_plan"]["artifacts_expected"] == ["markdown_report", "chart"]
    assert payload["plan"]["tool_plan"]["steps"][1]["reason"] == (
        "Compute summary statistics and charts."
    )
    assert payload["plan"]["tool_plan"]["steps"][1]["fallback_tools"] == ["terminal.run"]
    assert payload["plan"]["execution_strategy"]["preferred_environment"] == (
        "isolated_desktop"
    )
    assert payload["plan"]["execution_strategy"]["sandbox_required"] is True
    assert payload["plan"]["execution_strategy"]["keyboard_mouse_step_count"] == 1
    assert payload["plan"]["timeline_preview"] == [{"event_type": "agent.plan.created"}]


def test_runtime_execution_envelope_snapshot_is_public_contract() -> None:
    request = RuntimeExecutionRequestSnapshot(
        request_id="runtime-plan-1:request:1:desktop.list_apps",
        step_id="discover-desktop-state",
        capability_id="desktop.app_discovery",
        capability_title="Discover Desktop Apps",
        capability_status="available",
        capability_reason="Selected because the tool plan has concrete steps for this capability.",
        capability_selected_tools=["desktop.list_apps"],
        capability_planned_step_ids=["discover-desktop-state"],
        tool_name="desktop.list_apps",
        input={"query": "PixelForge", "limit": 20},
        planning_reason="planner_desktop_app_discovery",
        risk_level="low",
        execution_mode=DesktopExecutionModeSnapshot(mode="read_only_observation"),
        desktop_execution_policy=DesktopExecutionPolicySnapshot(mode="preview_input"),
        sandbox_provider=SandboxDesktopProviderSnapshot(
            status="provider_required",
            blocking_conditions=["sandbox_desktop_provider_required"],
        ),
        desktop_execution_route=DesktopExecutionRouteSnapshot(
            tool_name="desktop.list_apps",
            requested_mode="preview_input",
            selected_provider_kind="none",
        ),
        policy_reason="Desktop app discovery is read-only.",
        runtime_doctrine="discover_operate_verify",
        runtime_stage="discover",
        runtime_role="find_target_app",
        requires_observation=True,
        replan_triggers=["verification_failed"],
        replan_signal_ids=["replan-1"],
        followup_target={"kind": "desktop_observed_action", "target_action": "click"},
        action_target={"action": "click", "label": "Open result"},
        observation_evidence={"source_tool": "desktop.ui_elements"},
        observation_retry={
            "from_tool": "desktop.ui_elements",
            "reason": "target_not_found",
        },
        checkpoint_policy=RuntimeCheckpointPolicySnapshot(
            checkpoint_ids=["checkpoint-open-app"],
            checkpoint_titles=["Verify app opened"],
            verifies=["discover-desktop-state", "desktop.app_discovery"],
            replan_on_failure=True,
            replan_triggers=["verification_failed"],
            replan_signal_ids=["replan-1"],
            fallback_tools=["desktop.ui_elements"],
            verification_target_step_ids=["open-app"],
            requires_observation=True,
            requires_post_action_verification=True,
        ),
        desktop_loop=DesktopExecutionLoopSnapshot(
            stage="discover",
            role="find_target_app",
            action="open_app",
            target_kind="desktop_app",
            selection_source="desktop.list_apps",
            app_name="PixelForge",
            query="PixelForge",
            source_tool="desktop.list_apps",
            retry_tool="desktop.list_apps",
            retry_reason="resolve_desktop_app",
            retry_input={"query": "PixelForge", "limit": 20},
            verification_target_step_ids=["open-app"],
            requires_observation=True,
            requires_post_action_verification=True,
            can_auto_retry=True,
        ),
    )
    envelope = RuntimeExecutionEnvelopeSnapshot(
        envelope_id="execution-envelope-runtime-plan-1",
        decision_id="decision-1",
        plan_id="runtime-plan-1",
        intent_kind="desktop_operation",
        capability_plan=CapabilityPlanSnapshot(
            plan_id="capability-plan-1",
            title="Desktop Capability Plan",
            intent_kind="desktop_operation",
            items=[
                CapabilityPlanItemSnapshot(
                    capability_id="desktop.app_discovery",
                    title="Discover Desktop Apps",
                    status="available",
                    required=True,
                    reason="Selected because the tool plan has concrete steps for this capability.",
                    selected_tools=["desktop.list_apps"],
                    planned_step_ids=["discover-desktop-state"],
                )
            ],
            required_capabilities=["desktop.app_discovery"],
            available_capabilities=["desktop.app_discovery"],
        ),
        execution_strategy=RuntimeExecutionStrategySnapshot(
            strategy_id="execution-strategy-runtime-plan-1",
            preferred_environment="isolated_desktop",
            interaction_mode="read_only",
            policy_mode="preview_input",
            isolated_desktop_preferred=True,
            foreground_takeover_allowed=False,
            sandbox_required=False,
            reasons=["policy_prefers_isolated_desktop"],
            mitigations=["observe_before_operate"],
        ),
        requests=[request],
        approvals_required=["operate-foreground-ui"],
        route_to_studio=True,
        desktop_execution_policy=DesktopExecutionPolicySnapshot(mode="preview_input"),
        sandbox_provider=SandboxDesktopProviderSnapshot(
            status="provider_required",
            blocking_conditions=["sandbox_desktop_provider_required"],
        ),
        desktop_execution_route=DesktopExecutionRouteSnapshot(
            tool_name="desktop.list_apps",
            requested_mode="preview_input",
            selected_provider_kind="none",
        ),
        runtime_doctrine="discover_operate_verify",
        runtime_stage_counts={"discover": 1},
        replan_signal_count=1,
    )

    payload = _json(envelope)

    assert list(payload) == [
        "envelope_id",
        "decision_id",
        "plan_id",
        "intent_kind",
        "capability_plan",
        "execution_strategy",
        "requests",
        "task_core",
        "task_progress",
        "approvals_required",
        "artifacts_expected",
        "open_questions",
        "route_to_studio",
        "desktop_execution_policy",
        "sandbox_provider",
        "desktop_execution_route",
        "desktop_provider_session",
        "runtime_doctrine",
        "runtime_stage_counts",
        "replan_signal_count",
        "source",
    ]
    assert payload["requests"][0]["tool_name"] == "desktop.list_apps"
    assert payload["capability_plan"]["items"][0]["capability_id"] == "desktop.app_discovery"
    assert payload["execution_strategy"]["preferred_environment"] == "isolated_desktop"
    assert payload["execution_strategy"]["interaction_mode"] == "read_only"
    assert payload["requests"][0]["capability_title"] == "Discover Desktop Apps"
    assert payload["requests"][0]["capability_selected_tools"] == ["desktop.list_apps"]
    assert payload["requests"][0]["input"] == {"query": "PixelForge", "limit": 20}
    assert payload["requests"][0]["risk_level"] == "low"
    assert payload["requests"][0]["execution_mode"]["mode"] == "read_only_observation"
    assert payload["requests"][0]["desktop_execution_policy"]["mode"] == "preview_input"
    assert payload["requests"][0]["sandbox_provider"]["status"] == "provider_required"
    assert payload["requests"][0]["sandbox_provider"]["blocking_conditions"] == [
        "sandbox_desktop_provider_required"
    ]
    assert payload["requests"][0]["desktop_execution_route"]["status"] == "ready"
    assert payload["requests"][0]["desktop_provider_session"] == {}
    assert payload["desktop_provider_session"] == {}
    assert payload["requests"][0]["policy_reason"] == "Desktop app discovery is read-only."
    assert payload["requests"][0]["runtime_stage"] == "discover"
    assert payload["requests"][0]["replan_triggers"] == ["verification_failed"]
    assert payload["requests"][0]["followup_target"] == {
        "kind": "desktop_observed_action",
        "target_action": "click",
    }
    assert payload["requests"][0]["action_target"] == {
        "action": "click",
        "label": "Open result",
    }
    assert payload["requests"][0]["observation_evidence"] == {
        "source_tool": "desktop.ui_elements",
    }
    assert payload["requests"][0]["observation_retry"] == {
        "from_tool": "desktop.ui_elements",
        "reason": "target_not_found",
    }
    assert payload["requests"][0]["checkpoint_policy"] == {
        "checkpoint_ids": ["checkpoint-open-app"],
        "checkpoint_titles": ["Verify app opened"],
        "verifies": ["discover-desktop-state", "desktop.app_discovery"],
        "replan_on_failure": True,
        "replan_triggers": ["verification_failed"],
        "replan_signal_ids": ["replan-1"],
        "fallback_tools": ["desktop.ui_elements"],
        "verification_target_step_ids": ["open-app"],
        "requires_approval": False,
        "requires_observation": True,
        "requires_post_action_verification": True,
        "source": "runtime_checkpoint_policy",
    }
    assert payload["requests"][0]["desktop_loop"] == {
        "stage": "discover",
        "role": "find_target_app",
        "action": "open_app",
        "target_kind": "desktop_app",
        "selection_source": "desktop.list_apps",
        "app_name": "PixelForge",
        "query": "PixelForge",
        "source_tool": "desktop.list_apps",
        "retry_tool": "desktop.list_apps",
        "retry_reason": "resolve_desktop_app",
        "retry_input": {"query": "PixelForge", "limit": 20},
        "verification_target_step_ids": ["open-app"],
        "requires_observation": True,
        "requires_post_action_verification": True,
        "can_auto_retry": True,
        "source": "desktop_execution_loop",
    }
    assert payload["runtime_stage_counts"] == {"discover": 1}
    assert payload["desktop_execution_policy"]["mode"] == "preview_input"
    assert payload["sandbox_provider"]["status"] == "provider_required"
    assert payload["desktop_execution_route"]["status"] == "ready"
    assert payload["replan_signal_count"] == 1
    projected_requests = runtime_execution_requests_from_envelope_payload(
        payload,
        allowed_tools=["desktop.list_apps"],
    )
    assert projected_requests[0]["desktop_execution_policy"]["mode"] == "preview_input"
    assert projected_requests[0]["sandbox_provider"]["status"] == "provider_required"
    assert projected_requests[0]["desktop_execution_route"]["status"] == "ready"


def test_runtime_execution_projection_skips_requests_with_blocked_dependencies() -> None:
    envelope = {
        "envelope_id": "execution-envelope-dependent-block",
        "requests": [
            {
                "tool_name": "desktop.search_submit",
                "input": {},
                "step_id": "submit-query",
                "depends_on": ["type-query"],
                "status": "planned",
            },
            {
                "tool_name": "desktop.safe_type_text",
                "input": {"text": "Kaguya"},
                "step_id": "type-query",
                "depends_on": ["focus-search"],
                "status": "planned",
            },
            {
                "tool_name": "desktop.safe_shortcut",
                "input": {"action": "find"},
                "step_id": "focus-search",
                "status": "blocked",
            },
            {
                "tool_name": "desktop.list_apps",
                "input": {"query": "Music"},
                "step_id": "independent-discovery",
                "status": "planned",
            },
        ],
    }

    projected = runtime_execution_requests_from_envelope_payload(envelope)

    assert [request["tool"] for request in projected] == ["desktop.list_apps"]


def test_provider_session_recovery_preserves_real_virtual_backend_requirement() -> None:
    envelope = RuntimeExecutionEnvelopeSnapshot(
        envelope_id="execution-envelope-provider-1",
        decision_id="decision-provider-1",
        plan_id="runtime-plan-provider-1",
        intent_kind="desktop_operation",
        requests=[
            RuntimeExecutionRequestSnapshot(
                request_id="request-type",
                step_id="type-foreground-text",
                capability_id="desktop.ui_operation",
                tool_name="desktop.safe_type_text",
                input={"text": "hello"},
                planning_reason="planner_desktop_ui_operation",
                execution_mode=DesktopExecutionModeSnapshot(
                    mode="supervised_live",
                    foreground_control=True,
                    keyboard_mouse_capture=True,
                ),
                desktop_execution_policy=DesktopExecutionPolicySnapshot(
                    mode="preview_input",
                    prefer_isolated_desktop=True,
                    avoid_user_foreground_takeover=True,
                    require_sandbox_for_keyboard_mouse=True,
                ),
                desktop_execution_route=DesktopExecutionRouteSnapshot(
                    tool_name="desktop.safe_type_text",
                    selected_provider_kind="sandbox_desktop",
                    status="real_virtual_desktop_provider_required",
                    can_execute=False,
                    sandbox_required=True,
                    blocking_conditions=[
                        "loopback_desktop_backend",
                        "real_virtual_desktop_backend_required",
                    ],
                    requires_real_virtual_desktop_backend=True,
                ),
                runtime_stage="operate",
                runtime_role="mutate_foreground_ui",
            )
        ],
        desktop_provider_session={
            "ok": False,
            "status": "real_virtual_desktop_provider_required",
            "needed": True,
            "running": False,
            "started": False,
            "provider_id": "real-virtual-desktop",
            "reason": "real_virtual_desktop_provider_required",
            "request_ids": ["request-type"],
            "tool_names": ["desktop.safe_type_text"],
            "desktop_session_kind": "virtual_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "requires_real_virtual_desktop_backend": True,
        },
    )

    recoveries = replan_recovery_snapshots_from_runtime_execution_envelope(
        envelope,
        run_id="run-1",
        task_id="task-1",
    )

    recovery = recoveries[0]
    action = recovery.recovery_actions[0]
    assert recovery.selected_tool_name == "desktop.provider_session.start"
    assert recovery.trigger == "real_virtual_desktop_provider_required"
    assert action.input["requires_real_virtual_desktop_backend"] is True
    assert action.metadata["requires_real_virtual_desktop_backend"] is True
    assert action.metadata["desktop_provider_session"][
        "requires_real_virtual_desktop_backend"
    ] is True
    assert action.deferred_continuation[0]["tool"] == "desktop.safe_type_text"
    assert action.deferred_continuation[0]["desktop_execution_policy"][
        "require_sandbox_for_keyboard_mouse"
    ] is True


def test_runtime_execution_request_projects_verification_evidence() -> None:
    envelope = RuntimeExecutionEnvelopeSnapshot(
        envelope_id="execution-envelope-verify",
        decision_id="decision-verify",
        plan_id="runtime-plan-verify",
        intent_kind="data_analysis",
        requests=[
            RuntimeExecutionRequestSnapshot(
                request_id="request-read-source",
                step_id="read-source",
                capability_id="workspace.file_read",
                tool_name="workspace.read",
                task_workspace_items=[
                    {
                        "item_id": "artifact-read-source",
                        "kind": "artifact",
                        "path": "reports/read-source.md",
                        "source_step_id": "read-source",
                    }
                ],
                task_verification_targets=[
                    {
                        "step_id": "read-source",
                        "artifact_path": "reports/read-source.md",
                    }
                ],
                verification_targets=[
                    {
                        "step_id": "verify-read-source",
                        "artifact_path": "reports/read-source-verified.md",
                    }
                ],
            )
        ],
    )
    tool_call = ToolCallSnapshot(
        tool_call_id="tool-call-read-source",
        run_id="run-verify",
        step_id="read-source",
        capability_id="workspace.file_read",
        tool_name="workspace.read",
        status="completed",
        approval_id="approval-read-source",
        output_preview={
            "ok": True,
            "artifact_path": "reports/read-source.md",
        },
    )
    events = [
        public_run_event_from_payload(
            {
                "event_id": "event-verify-read-source",
                "run_id": "run-verify",
                "sequence": 1,
                "event_type": "workflow.run.task.checkpoint.updated",
                "payload": {
                    "checkpoint_id": "checkpoint-read-source",
                    "step_id": "read-source",
                    "status": "completed",
                    "verification_status": "verified",
                    "verified_by_step_id": "verify-read-source",
                    "artifact_path": "reports/read-source-verified.md",
                    "checkpoint": {
                        "checkpoint_id": "checkpoint-read-source",
                        "after_step_id": "read-source",
                        "status": "completed",
                        "payload": {
                            "verification_status": "verified",
                        },
                    },
                },
            }
        ),
        public_run_event_from_payload(
            {
                "event_id": "event-artifact-read-source",
                "run_id": "run-verify",
                "sequence": 2,
                "event_type": "agent.artifact.write",
                "payload": {
                    "step_id": "read-source",
                    "tool_name": "workspace.read",
                    "artifact": {
                        "artifact_id": "artifact-read-source",
                        "path": "reports/read-source.md",
                    },
                },
            }
        ),
    ]

    projected = runtime_execution_envelope_with_status_overlay(
        envelope,
        tool_calls=[tool_call],
        events=events,
    )

    assert projected is not None
    request = projected.requests[0]
    assert request.status == "completed"
    assert request.event_ids == ["event-verify-read-source", "event-artifact-read-source"]
    assert request.tool_call_ids == ["tool-call-read-source"]
    assert request.approval_ids == ["approval-read-source"]
    assert request.verification_targets == [
        {
            "step_id": "verify-read-source",
            "artifact_path": "reports/read-source-verified.md",
        }
    ]
    assert request.artifact_ids == ["artifact-read-source"]
    assert request.artifact_paths == [
        "reports/read-source-verified.md",
        "reports/read-source.md",
    ]
    assert request.verification_status == "verified"
    assert request.verification_step_id == "verify-read-source"
    assert request.verification_event_ids == ["event-verify-read-source"]
    assert request.verification_artifact_paths == [
        "reports/read-source-verified.md",
        "reports/read-source.md",
    ]


def test_runtime_execution_request_blocks_completed_tool_when_verification_fails() -> None:
    envelope = RuntimeExecutionEnvelopeSnapshot(
        envelope_id="execution-envelope-verify-failed",
        decision_id="decision-verify-failed",
        plan_id="runtime-plan-verify-failed",
        intent_kind="desktop_operation",
        requests=[
            RuntimeExecutionRequestSnapshot(
                request_id="request-open-app",
                step_id="open-app",
                capability_id="desktop.app_control",
                tool_name="app.open",
                verification_targets=[
                    {
                        "step_id": "verify-open-app",
                        "target": "Music",
                    }
                ],
            )
        ],
    )
    tool_call = ToolCallSnapshot(
        tool_call_id="tool-call-open-app",
        run_id="run-verify-failed",
        step_id="open-app",
        capability_id="desktop.app_control",
        tool_name="app.open",
        status="completed",
        output_preview={"ok": True},
    )
    events = [
        public_run_event_from_payload(
            {
                "event_id": "event-verify-open-app-failed",
                "run_id": "run-verify-failed",
                "sequence": 1,
                "event_type": "workflow.run.task.checkpoint.updated",
                "payload": {
                    "checkpoint_id": "checkpoint-open-app",
                    "step_id": "open-app",
                    "status": "blocked",
                    "verification_status": "verification_failed",
                    "verified_by_step_id": "verify-open-app",
                    "checkpoint": {
                        "checkpoint_id": "checkpoint-open-app",
                        "after_step_id": "open-app",
                        "status": "blocked",
                        "payload": {
                            "verification_failed": True,
                        },
                    },
                },
            }
        )
    ]

    projected = runtime_execution_envelope_with_status_overlay(
        envelope,
        tool_calls=[tool_call],
        events=events,
    )

    assert projected is not None
    request = projected.requests[0]
    assert request.status == "blocked"
    assert request.tool_call_ids == ["tool-call-open-app"]
    assert request.verification_status == "verification_failed"
    assert request.verification_step_id == "verify-open-app"
    assert request.verification_event_ids == ["event-verify-open-app-failed"]


def test_runtime_execution_request_blocks_completed_tool_result_verification_failure() -> None:
    envelope = RuntimeExecutionEnvelopeSnapshot(
        envelope_id="execution-envelope-tool-result-verify-failed",
        decision_id="decision-tool-result-verify-failed",
        plan_id="runtime-plan-tool-result-verify-failed",
        intent_kind="desktop_operation",
        requests=[
            RuntimeExecutionRequestSnapshot(
                request_id="request-verify-app",
                step_id="verify-app",
                capability_id="desktop.ui_observe",
                tool_name="desktop.verify",
            )
        ],
    )
    tool_call = ToolCallSnapshot(
        tool_call_id="tool-call-verify-app",
        run_id="run-tool-result-verify-failed",
        step_id="verify-app",
        capability_id="desktop.ui_observe",
        tool_name="desktop.verify",
        status="completed",
        output_preview={
            "ok": False,
            "verification_failed": True,
            "reason": "target_window_not_found",
        },
    )

    projected = runtime_execution_envelope_with_status_overlay(
        envelope,
        tool_calls=[tool_call],
    )

    assert projected is not None
    request = projected.requests[0]
    assert request.status == "blocked"
    assert request.tool_call_ids == ["tool-call-verify-app"]
    assert request.verification_status == "verification_failed"


def test_replan_continuation_snapshot_is_public_contract() -> None:
    continuation = ReplanContinuationSnapshot(
        continuation_id="replan-continuation:replan-1:action-1",
        request_id="replan-1",
        action_id="action-1",
        tool_name="desktop.list_apps",
        prompt="执行恢复动作：Find target app",
        title="Find target app",
        source_run_id="run-1",
        source_task_id="task-1",
        source_group_run_id="group-run-1",
        source_workflow_run_id="workflow-run-1",
        agent_id="agent-1",
        conversation_id="chat-1",
        client_run_id="client-run-1",
        direct_tool_requests=[
            {
                "tool": "desktop.list_apps",
                "input": {"query": "Music"},
                "approval_required": True,
            }
        ],
        metadata={"replan_continuation_id": "replan-continuation:replan-1:action-1"},
        task_context={"workspace_id": "task-workspace-1"},
        daily_desktop_planning_context="执行恢复动作：Find target app",
        approval_required=True,
        auto_start_eligible=False,
        auto_start_reason="manual_replan_continuation_required",
        auto_start_blockers=["approval_required"],
        risk_level="medium",
    )

    payload = _json(continuation)

    assert list(payload) == [
        "continuation_id",
        "request_id",
        "action_id",
        "tool_name",
        "prompt",
        "title",
        "source_run_id",
        "source_task_id",
        "source_group_run_id",
        "source_workflow_run_id",
        "agent_id",
        "conversation_id",
        "client_run_id",
        "direct_tool_requests",
        "metadata",
        "task_context",
        "daily_desktop_planning_context",
        "approval_required",
        "auto_start_eligible",
        "auto_start_reason",
        "auto_start_blockers",
        "risk_level",
        "source",
    ]
    assert payload["direct_tool_requests"][0]["approval_required"] is True
    assert payload["auto_start_eligible"] is False
    assert payload["auto_start_blockers"] == ["approval_required"]


def test_task_core_public_snapshot_exposes_workspace_todo_checkpoint_and_replan() -> None:
    workspace = TaskWorkspaceSnapshot(
        workspace_id="workspace-1",
        title="Analyze workspace",
        items=[
            TaskWorkspaceItemSnapshot(
                item_id="input-1",
                title="sales.csv",
                kind="input",
                path="sales.csv",
            )
        ],
    )
    task_core = TaskCoreSnapshot(
        core_id="task-core-1",
        workspace=workspace,
        todos=[
            TaskTodoItemSnapshot(
                todo_id="todo-1",
                title="Run analysis",
                capability_id="data.analysis",
                step_id="run-analysis",
                tool_name="data.analyze",
                approval_required=True,
            )
        ],
        checkpoints=[
            TaskCheckpointSnapshot(
                checkpoint_id="checkpoint-1",
                title="Verify report",
                after_step_id="write-report",
                verifies=["analysis-report.md"],
            )
        ],
        replan_signals=[
            ReplanSignalSnapshot(
                signal_id="replan-1",
                trigger="tool_failure",
                source_step_id="run-analysis",
                target="data.analysis",
                fallback_tools=["terminal.run"],
            )
        ],
    )

    payload = _json(task_core)

    assert list(payload) == [
        "core_id",
        "workspace",
        "todos",
        "checkpoints",
        "replan_signals",
        "goal_contract",
        "source",
    ]
    assert payload["workspace"]["items"][0]["kind"] == "input"
    assert payload["todos"][0]["approval_required"] is True
    assert payload["checkpoints"][0]["replan_on_failure"] is True
    assert payload["replan_signals"][0]["fallback_tools"] == ["terminal.run"]
    assert payload["goal_contract"] is None


def test_task_progress_summary_public_snapshot_exposes_replay_state() -> None:
    summary = TaskProgressSummarySnapshot(
        core_id="task-core-1",
        workspace_id="task-workspace-1",
        status="replan_requested",
        current_step_id="run-analysis",
        current_step_title="Run analysis",
        current_tool_name="data.analyze",
        total_todos=3,
        completed_todos=1,
        blocked_todos=1,
        total_checkpoints=3,
        waiting_approval_checkpoints=1,
        pending_verification_count=1,
        failed_verification_count=1,
        verified_verification_count=2,
        latest_verification_status="verification_failed",
        latest_verification_step_id="run-analysis",
        replan_request_count=1,
        latest_replan_request_id="replan-1",
        latest_replan_trigger="tool_failure",
        latest_replan_step_id="run-analysis",
        needs_replan=True,
        blocked_step_ids=["run-analysis"],
        progress_text="1/3 todos completed | 1 blocked | replan requested",
    )

    payload = _json(summary)

    assert list(payload) == [
        "core_id",
        "workspace_id",
        "status",
        "current_step_id",
        "current_step_title",
        "current_tool_name",
        "total_todos",
        "completed_todos",
        "active_todos",
        "blocked_todos",
        "skipped_todos",
        "total_checkpoints",
        "completed_checkpoints",
        "blocked_checkpoints",
        "waiting_approval_checkpoints",
        "total_workspace_items",
        "completed_workspace_items",
        "blocked_workspace_items",
        "pending_verification_count",
        "failed_verification_count",
        "verified_verification_count",
        "latest_verification_status",
        "latest_verification_step_id",
        "replan_request_count",
        "latest_replan_request_id",
        "latest_replan_trigger",
        "latest_replan_step_id",
        "needs_replan",
        "needs_user_action",
        "desktop_provider_session_status",
        "desktop_provider_session_needed",
        "desktop_provider_session_running",
        "desktop_provider_session_started",
        "desktop_provider_session_provider_id",
        "desktop_provider_session_tool_names",
        "desktop_provider_session_needs_user_action",
        "desktop_provider_session_needs_replan",
        "blocked_step_ids",
        "approval_step_ids",
        "progress_text",
        "source",
    ]
    assert payload["status"] == "replan_requested"
    assert payload["needs_replan"] is True
    assert payload["blocked_step_ids"] == ["run-analysis"]
    assert payload["pending_verification_count"] == 1
    assert payload["failed_verification_count"] == 1
    assert payload["latest_verification_status"] == "verification_failed"


def test_task_replan_request_contract_links_failure_to_planner_state() -> None:
    request = TaskReplanRequestSnapshot(
        request_id="replan-request-1",
        trigger="tool_failure",
        run_id="run-1",
        task_id="task-1",
        decision_id="decision-1",
        plan_id="plan-1",
        core_id="core-1",
        source_step_id="run-analysis",
        source_tool_name="data.analyze",
        target_capability_id="data.analysis",
        failure_event_type="agent.tool.call",
        failure_detail="data.analyze failed",
        fallback_tools=["terminal.run"],
        replan_prompt="replan prompt",
    )

    payload = _json(request)

    assert list(payload) == [
        "request_id",
        "trigger",
        "status",
        "run_id",
        "task_id",
        "decision_id",
        "plan_id",
        "core_id",
        "source_step_id",
        "source_tool_name",
        "target_capability_id",
        "condition",
        "reason",
        "failure_event_type",
        "failure_detail",
        "fallback_tools",
        "recovery_actions",
        "action_target",
        "observation_evidence",
        "observation_retry",
        "verification_targets",
        "task_verification_targets",
        "replan_prompt",
        "route_to_studio",
        "metadata",
        "created_at",
        "source",
    ]
    assert payload["status"] == "requested"
    assert payload["route_to_studio"] is True
    assert payload["fallback_tools"] == ["terminal.run"]


def test_replan_recovery_contract_links_request_fallback_and_checkpoint() -> None:
    recovery = ReplanRecoverySnapshot(
        request_id="replan-request-1",
        trigger="tool_failure",
        status="completed",
        run_id="run-1",
        task_id="task-1",
        group_run_id="group-run-1",
        workflow_run_id="workflow-run-1",
        decision_id="decision-1",
        plan_id="plan-1",
        core_id="core-1",
        source_step_id="run-analysis",
        source_tool_name="data.analyze",
        target_capability_id="data.analysis",
        fallback_tools=["terminal.run"],
        verification_targets=[
            {
                "step_id": "run-analysis",
                "todo_id": "todo-run-analysis",
            }
        ],
        selected_tool_name="terminal.run",
        selected_step_id="run-analysis",
        planning_reason="planner_replan_fallback_recovery",
        recovery_action_label="Run terminal fallback",
        recovery_actions=[
            ReplanRecoveryActionSnapshot(
                action_id="replan-request-1:action:1:terminal.run",
                label="Run terminal fallback",
                tool="terminal.run",
                input={"command": "python analyze.py sales.csv"},
                planning_reason="planner_replan_fallback_recovery",
                permission_target="terminal_execution",
                risk_level="medium",
                approval_required=True,
                approval_id="approval-1",
                approval_status="approved",
                selected=True,
                deferred_tool="terminal.run",
                deferred_input={"command": "python analyze.py sales.csv"},
                deferred_context={"step_id": "run-analysis"},
                deferred_continuation=[{"tool": "desktop.active_window"}],
                verification_targets=[
                    {
                        "step_id": "run-analysis",
                        "todo_id": "todo-run-analysis",
                    }
                ],
            )
        ],
        permission_target="terminal_execution",
        risk_level="medium",
        approval_id="approval-1",
        approval_status="approved",
        approval_ids=["approval-1"],
        deferred_tool="terminal.run",
        deferred_input={"command": "python analyze.py sales.csv"},
        deferred_context={"step_id": "run-analysis"},
        deferred_continuation=[{"tool": "desktop.active_window"}],
        action_target={
            "action": "click",
            "label": "Apple Music result",
            "app_name": "Music",
        },
        observation_evidence={
            "strategy": "observed_center_fallback",
            "observed_center": {"x": 512, "y": 220},
        },
        observation_retry={
            "from_tool": "desktop.ui_elements",
            "reason": "target_not_found",
        },
        tool_call_id="tool-call-1",
        tool_call_ids=["tool-call-1"],
        artifact_ids=["artifact-1"],
        artifact_paths=["reports/analysis.md"],
        tool_status="completed",
        todo_status="completed",
        checkpoint_status="completed",
        failure_detail="data.analyze failed",
        result_preview={"ok": True},
        metadata={
            "replan_recovery_failed": True,
            "failed_recovery_tool": "terminal.run",
        },
        recovery_event_ids=["event-1", "event-2"],
        created_at="2026-06-27T00:00:00Z",
        updated_at="2026-06-27T00:00:01Z",
    )

    payload = _json(recovery)

    assert list(payload) == [
        "request_id",
        "trigger",
        "status",
        "run_id",
        "task_id",
        "group_run_id",
        "workflow_run_id",
        "decision_id",
        "plan_id",
        "core_id",
        "source_step_id",
        "source_tool_name",
        "target_capability_id",
        "fallback_tools",
        "verification_targets",
        "selected_tool_name",
        "selected_step_id",
        "planning_reason",
        "recovery_action_label",
        "recovery_actions",
        "permission_target",
        "risk_level",
        "approval_id",
        "approval_status",
        "approval_ids",
        "deferred_tool",
        "deferred_input",
        "deferred_context",
        "deferred_continuation",
        "action_target",
        "observation_evidence",
        "observation_retry",
        "desktop_execution_policy",
        "desktop_execution_route",
        "sandbox_provider",
        "desktop_provider_session",
        "desktop_loop",
        "tool_call_id",
        "tool_call_ids",
        "artifact_ids",
        "artifact_paths",
        "tool_status",
        "todo_status",
        "checkpoint_status",
        "failure_detail",
        "result_preview",
        "metadata",
        "recovery_event_ids",
        "created_at",
        "updated_at",
        "source",
    ]
    assert payload["selected_tool_name"] == "terminal.run"
    assert payload["recovery_action_label"] == "Run terminal fallback"
    assert payload["recovery_actions"][0]["tool"] == "terminal.run"
    assert payload["recovery_actions"][0]["selected"] is True
    assert payload["recovery_actions"][0]["approval_required"] is True
    assert payload["recovery_actions"][0]["approval_id"] == "approval-1"
    assert payload["recovery_actions"][0]["approval_status"] == "approved"
    assert payload["recovery_actions"][0]["deferred_tool"] == "terminal.run"
    assert payload["recovery_actions"][0]["deferred_context"] == {"step_id": "run-analysis"}
    assert payload["recovery_actions"][0]["input"] == {
        "command": "python analyze.py sales.csv",
    }
    assert payload["permission_target"] == "terminal_execution"
    assert payload["risk_level"] == "medium"
    assert payload["approval_id"] == "approval-1"
    assert payload["approval_status"] == "approved"
    assert payload["approval_ids"] == ["approval-1"]
    assert payload["deferred_tool"] == "terminal.run"
    assert payload["deferred_input"] == {"command": "python analyze.py sales.csv"}
    assert payload["metadata"]["replan_recovery_failed"] is True
    assert payload["metadata"]["failed_recovery_tool"] == "terminal.run"
    assert payload["deferred_continuation"] == [{"tool": "desktop.active_window"}]
    assert payload["verification_targets"] == [
        {
            "step_id": "run-analysis",
            "todo_id": "todo-run-analysis",
        }
    ]
    assert payload["action_target"] == {
        "action": "click",
        "label": "Apple Music result",
        "app_name": "Music",
    }
    assert payload["observation_evidence"] == {
        "strategy": "observed_center_fallback",
        "observed_center": {"x": 512, "y": 220},
    }
    assert payload["observation_retry"] == {
        "from_tool": "desktop.ui_elements",
        "reason": "target_not_found",
    }
    assert payload["tool_call_ids"] == ["tool-call-1"]
    assert payload["artifact_ids"] == ["artifact-1"]
    assert payload["artifact_paths"] == ["reports/analysis.md"]
    assert payload["checkpoint_status"] == "completed"


def test_planner_orchestration_start_contract_links_decision_and_started_run() -> None:
    intent = TaskIntentSnapshot(
        intent_id="intent-workflow-1",
        kind="workflow_orchestration",
        title="Run workflow",
        user_goal="运行 Review workflow",
        inputs={"target_name_hint": "Review workflow"},
        required_capabilities=["workflow.orchestration"],
    )
    tool_plan = ToolPlanSnapshot(
        plan_id="tool-plan-workflow-1",
        title="Workflow orchestration",
        steps=[
            ToolPlanStepSnapshot(
                step_id="workflow-orchestration",
                title="Start workflow",
                capability_id="workflow.orchestration",
                action="start_workflow",
                tool_name="workflow.run",
            )
        ],
    )
    decision = PlannerDecisionSnapshot(
        decision_id="decision-workflow-1",
        prompt="运行 Review workflow",
        selected_intent=intent,
        plan=RuntimePlanSnapshot(
            plan_id="runtime-plan-workflow-1",
            intent=intent,
            tool_plan=tool_plan,
            route_to_studio=True,
        ),
    )
    workflow_run = WorkflowRunSnapshot(
        run_id="workflow-run-1",
        workflow_run_id="workflow-run-1",
        workflow_id="workflow-1",
        status="running",
        title="Review workflow",
        objective="Build report",
    )
    snapshot = PlannerOrchestrationStartSnapshot(
        kind="workflow",
        status="started",
        decision=decision,
        run_id="workflow-run-1",
        workflow_run_id="workflow-run-1",
        target_id="workflow-1",
        target_name="Review workflow",
        objective="Build report",
        title="Review workflow",
        workflow_run=workflow_run,
    )
    request = StartPlannerOrchestrationRequest(
        prompt="运行 Review workflow",
        target_name="Review workflow",
        allowed_tools=["workflow.run"],
        metadata={"surface": "agent_studio"},
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "kind",
        "status",
        "decision",
        "run_id",
        "workflow_run_id",
        "group_run_id",
        "run_group_id",
        "target_id",
        "target_name",
        "objective",
        "title",
        "route_to_studio",
        "message",
        "workflow_run",
        "group_run",
    ]
    assert payload["decision"]["selected_intent"]["kind"] == "workflow_orchestration"
    assert payload["run_id"] == "workflow-run-1"
    assert payload["workflow_run_id"] == "workflow-run-1"
    assert payload["group_run_id"] is None
    assert payload["target_id"] == "workflow-1"
    assert payload["route_to_studio"] is True
    assert payload["workflow_run"]["workflow_id"] == "workflow-1"
    assert payload["group_run"] is None
    assert request.model_dump(exclude_none=True) == {
        "prompt": "运行 Review workflow",
        "target_name": "Review workflow",
        "allowed_tools": ["workflow.run"],
        "metadata": {"surface": "agent_studio"},
    }


def test_agent_studio_planner_orchestration_start_surfaces_run_correlation() -> None:
    service = AgentStudioService(_FakeStudioOrchestrationPort())

    workflow = service.start_planner_orchestration(
        StartPlannerOrchestrationRequest(
            prompt="运行 Review workflow",
            target_name="Review workflow",
            allowed_tools=["workflow.run"],
        )
    )
    group = service.start_planner_orchestration(
        StartPlannerOrchestrationRequest(
            prompt="启动 Research squad group 调研 Hanako",
            target_name="Research squad",
            allowed_tools=["group.run", "agent.group_run"],
        )
    )

    assert workflow.status == "started"
    assert workflow.run_id == "workflow-run-1"
    assert workflow.workflow_run_id == "workflow-run-1"
    assert workflow.workflow_run is not None
    assert workflow.workflow_run.workflow_id == "workflow-1"
    assert group.status == "started"
    assert group.run_id == "group-run-1"
    assert group.group_run_id == "group-run-1"
    assert group.run_group_id == "group-run-1"
    assert group.group_run is not None
    assert group.group_run.group_id == "group-1"


def test_run_timeline_child_snapshot_projects_planner_summary_from_child_events() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "workflow-run-1",
            "kind": "workflow_run",
            "status": "running",
            "children": [
                {
                    "run_id": "child-run-1",
                    "status": "completed",
                    "kind": "agent_run",
                    "workflow_node_id": "analyze",
                    "events": [
                        {
                            "event_type": "agent.intent.selected",
                            "payload": {
                                "source": "runtime_planner",
                                "decision_id": "decision-1",
                                "plan_id": "plan-1",
                                "route_to_studio": True,
                                "intent": {
                                    "intent_id": "intent-1",
                                    "kind": "data_analysis",
                                    "title": "Analyze CSV",
                                    "required_capabilities": [
                                        "workspace.file_read",
                                        "data.analysis",
                                    ],
                                },
                            },
                        },
                        {
                            "event_type": "agent.plan.created",
                            "payload": {
                                "source": "runtime_planner",
                                "decision_id": "decision-1",
                                "plan": {
                                    "plan_id": "plan-1",
                                    "route_to_studio": True,
                                    "intent": {
                                        "intent_id": "intent-1",
                                        "kind": "data_analysis",
                                        "title": "Analyze CSV",
                                        "required_capabilities": ["data.analysis"],
                                    },
                                    "capabilities": [
                                        {"capability_id": "workspace.file_read"},
                                        {"capability_id": "data.analysis"},
                                    ],
                                    "tool_plan": {
                                        "steps": [
                                            {
                                                "step_id": "inspect-data",
                                                "capability_id": "workspace.file_read",
                                                "tool_name": "workspace.read",
                                            },
                                            {
                                                "step_id": "run-analysis",
                                                "capability_id": "data.analysis",
                                                "tool_name": "data.analyze",
                                                "approval_required": True,
                                            },
                                        ],
                                        "required_capabilities": [
                                            "workspace.file_read",
                                            "data.analysis",
                                        ],
                                        "approvals_required": ["run-analysis"],
                                        "artifacts_expected": ["markdown_report"],
                                        "open_questions": ["confirm date range"],
                                    },
                                },
                            },
                        },
                        {
                            "event_type": "agent.plan.selection",
                            "payload": {
                                "source": "runtime_planner",
                                "decision_id": "decision-1",
                                "plan_id": "plan-1",
                                "selection_source": "runtime_planner",
                                "selection_role": "runtime_planner_primary",
                                "selection_reason": "runtime_planner_direct",
                                "planner_entrypoint": "chat_default",
                                "entrypoint_source": "main_chat",
                                "launcher_mode": "chat",
                                "launcher_surface": "main_window",
                                "runnable_kind": "agent",
                                "followup_target": {
                                    "kind": "app_write",
                                    "app_name": "Numbers",
                                    "target_action": "app_paste",
                                    "body_source": "model_generated_content",
                                },
                                "selected_tools": ["data.analyze"],
                                "plan_step_count": 2,
                            },
                        },
                    ],
                }
            ],
        }
    )

    child = snapshot.children[0]
    assert child.planner_summary == PlannerTraceSummarySnapshot(
        source="runtime_planner",
        decision_id="decision-1",
        plan_id="plan-1",
        intent_kind="data_analysis",
        intent_title="Analyze CSV",
        route_to_studio=True,
        selection_source="runtime_planner",
        selection_role="runtime_planner_primary",
        selection_reason="runtime_planner_direct",
        planner_entrypoint="chat_default",
        entrypoint_source="main_chat",
        launcher_mode="chat",
        launcher_surface="main_window",
        runnable_kind="agent",
        followup_target={
            "kind": "app_write",
            "app_name": "Numbers",
            "target_action": "app_paste",
            "body_source": "model_generated_content",
        },
        plan_tools=["workspace.read", "data.analyze"],
        selected_tools=["data.analyze"],
        plan_capabilities=["workspace.file_read", "data.analysis"],
        required_capabilities=["workspace.file_read", "data.analysis"],
        approvals_required=["run-analysis"],
        artifacts_expected=["markdown_report"],
        open_questions=["confirm date range"],
        step_count=2,
        event_count=3,
    )
    payload = _json(snapshot)
    assert payload["children"][0]["planner_summary"]["intent_kind"] == "data_analysis"
    assert payload["children"][0]["planner_summary"]["plan_tools"] == [
        "workspace.read",
        "data.analyze",
    ]
    assert payload["children"][0]["planner_summary"]["followup_target"] == {
        "kind": "app_write",
        "app_name": "Numbers",
        "target_action": "app_paste",
        "body_source": "model_generated_content",
    }


def test_run_timeline_child_snapshot_projects_task_progress_from_payload() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "workflow-run-1",
            "kind": "workflow_run",
            "status": "running",
            "children": [
                {
                    "run_id": "child-run-1",
                    "status": "running",
                    "kind": "agent_run",
                    "workflow_node_id": "analyze",
                    "task_progress": {
                        "core_id": "task-core-1",
                        "workspace_id": "workspace-1",
                        "status": "running",
                        "completed_todos": 1,
                        "total_todos": 2,
                        "pending_verification_count": 1,
                        "progress_text": "1/2 todos completed",
                    },
                }
            ],
        }
    )

    child = snapshot.children[0]
    assert child.task_progress is not None
    assert child.task_progress.core_id == "task-core-1"
    assert child.task_progress.status == "running"
    assert child.task_progress.completed_todos == 1
    payload = _json(snapshot)
    assert payload["children"][0]["task_progress"]["progress_text"] == "1/2 todos completed"


def test_run_timeline_child_snapshot_projects_task_progress_from_task_core() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "workflow-run-1",
            "kind": "workflow_run",
            "status": "running",
            "children": [
                {
                    "run_id": "child-run-1",
                    "status": "running",
                    "kind": "agent_run",
                    "workflow_node_id": "analyze",
                    "task_core": {
                        "core_id": "task-core-1",
                        "workspace": {"workspace_id": "workspace-1", "title": "Analyze workspace"},
                        "todos": [
                            {
                                "todo_id": "todo-inspect-data",
                                "step_id": "inspect-data",
                                "title": "Inspect data",
                                "status": "completed",
                            },
                            {
                                "todo_id": "todo-run-analysis",
                                "step_id": "run-analysis",
                                "title": "Run analysis",
                                "status": "in_progress",
                            },
                        ],
                        "checkpoints": [
                            {
                                "checkpoint_id": "verify-analysis",
                                "after_step_id": "run-analysis",
                                "title": "Check analysis output",
                                "status": "pending",
                                "payload": {"verification_status": "pending_verification"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    child = snapshot.children[0]
    assert child.task_progress is not None
    assert child.task_progress.core_id == "task-core-1"
    assert child.task_progress.workspace_id == "workspace-1"
    assert child.task_progress.status == "running"
    assert child.task_progress.completed_todos == 1
    assert child.task_progress.total_todos == 2
    assert child.task_progress.pending_verification_count == 1


def test_run_timeline_snapshot_projects_planner_summary_from_events() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "completed",
            "events": [
                {
                    "event_type": "agent.plan.created",
                    "payload": {
                        "source": "runtime_planner",
                        "decision_id": "decision-1",
                        "plan": {
                            "plan_id": "plan-1",
                            "intent": {
                                "intent_id": "intent-1",
                                "kind": "report_generation",
                                "title": "Write report",
                            },
                            "capabilities": [{"capability_id": "artifact.output"}],
                            "tool_plan": {
                                "steps": [
                                    {
                                        "step_id": "write-report",
                                        "capability_id": "artifact.output",
                                        "tool_name": "artifact.write",
                                    }
                                ],
                                "required_capabilities": ["artifact.output"],
                                "artifacts_expected": ["markdown_report"],
                            },
                        },
                    },
                },
                {
                    "event_type": "agent.plan.selection",
                    "payload": {
                        "source": "runtime_planner",
                        "selection_source": "runtime_planner",
                        "selection_role": "runtime_planner_primary",
                        "selection_reason": "runtime_planner_direct",
                        "planner_entrypoint": "studio_default",
                        "selected_tools": ["artifact.write"],
                    },
                },
            ],
        }
    )

    assert snapshot.planner_summary == PlannerTraceSummarySnapshot(
        source="runtime_planner",
        decision_id="decision-1",
        plan_id="plan-1",
        intent_kind="report_generation",
        intent_title="Write report",
        selection_source="runtime_planner",
        selection_role="runtime_planner_primary",
        selection_reason="runtime_planner_direct",
        planner_entrypoint="studio_default",
        plan_tools=["artifact.write"],
        selected_tools=["artifact.write"],
        plan_capabilities=["artifact.output"],
        required_capabilities=["artifact.output"],
        artifacts_expected=["markdown_report"],
        step_count=1,
        event_count=2,
    )
    assert _json(snapshot)["planner_summary"]["artifacts_expected"] == ["markdown_report"]


def test_agent_task_snapshot_projects_task_core_from_planner_metadata() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Analysis Workspace",
            "items": [
                {
                    "item_id": "input-1",
                    "title": "sales.csv",
                    "kind": "input",
                    "path": "sales.csv",
                }
            ],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Run analysis",
                "step_id": "run-analysis",
                "tool_name": "data.analyze",
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "title": "Verify analysis",
                "after_step_id": "run-analysis",
            }
        ],
        "replan_signals": [
            {
                "signal_id": "replan-1",
                "trigger": "tool_failure",
                "source_step_id": "run-analysis",
                "target": "data.analysis",
            }
        ],
    }

    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "title": "Analyze data",
            "status": "running",
            "metadata": {"yachiyo_task_core": core_payload},
        }
    )

    assert snapshot.task_core is not None
    assert snapshot.task_core.core_id == "task-core-1"
    assert snapshot.task_core.workspace.items[0].path == "sales.csv"
    assert snapshot.task_core.todos[0].step_id == "run-analysis"
    assert snapshot.task_core.checkpoints[0].after_step_id == "run-analysis"
    assert snapshot.task_core.replan_signals[0].target == "data.analysis"


def test_agent_task_snapshot_updates_task_core_progress_from_runtime_events() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Report Workspace",
            "items": [
                {
                    "item_id": "input-1",
                    "title": "sales.csv",
                    "kind": "input",
                    "path": "sales.csv",
                    "source_step_id": "read-source",
                },
                {
                    "item_id": "artifact-1",
                    "title": "report.md",
                    "kind": "artifact",
                    "path": "report.md",
                    "source_step_id": "write-report",
                },
                {
                    "item_id": "todo-file-1",
                    "title": "tool-plan.todo.md",
                    "kind": "todo",
                    "path": "tool-plan.todo.md",
                    "source_step_id": "format-report",
                },
            ],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Read source",
                "step_id": "read-source",
                "tool_name": "workspace.read",
            },
            {
                "todo_id": "todo-2",
                "title": "Write report",
                "step_id": "write-report",
                "tool_name": "artifact.write",
            },
            {
                "todo_id": "todo-3",
                "title": "Format report",
                "step_id": "format-report",
                "tool_name": "terminal.run",
            },
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "title": "Verify read",
                "after_step_id": "read-source",
            },
            {
                "checkpoint_id": "checkpoint-2",
                "title": "Verify write",
                "after_step_id": "write-report",
            },
            {
                "checkpoint_id": "checkpoint-3",
                "title": "Verify format",
                "after_step_id": "format-report",
            },
        ],
        "replan_signals": [],
    }

    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "title": "Write report",
            "status": "approval_required",
            "metadata": {"yachiyo_task_core": core_payload},
            "events": [
                {
                    "event_type": "agent.tool.call",
                    "payload": {
                        "step_id": "read-source",
                        "tool": "workspace.read",
                        "status": "completed",
                        "result": {"ok": True},
                    },
                },
                {
                    "event_type": "agent.tool.approval_required",
                    "payload": {
                        "step_id": "write-report",
                        "tool": "artifact.write",
                        "status": "approval_required",
                    },
                },
                {
                    "event_type": "agent.tool.started",
                    "payload": {
                        "step_id": "format-report",
                        "tool": "terminal.run",
                        "status": "running",
                    },
                },
            ],
        }
    )

    assert snapshot.task_core is not None
    assert [item.status for item in snapshot.task_core.workspace.items] == [
        "completed",
        "blocked",
        "in_progress",
    ]
    assert snapshot.task_core.workspace.items[1].metadata["runtime_event_type"] == (
        "agent.tool.approval_required"
    )
    assert [todo.status for todo in snapshot.task_core.todos] == [
        "completed",
        "blocked",
        "in_progress",
    ]
    assert snapshot.task_core.todos[0].metadata["runtime_status"] == "completed"
    assert snapshot.task_core.todos[2].metadata["runtime_event_type"] == (
        "agent.tool.started"
    )
    assert [checkpoint.status for checkpoint in snapshot.task_core.checkpoints] == [
        "completed",
        "waiting_approval",
        "ready",
    ]
    assert snapshot.task_core.checkpoints[1].payload["runtime_event_type"] == (
        "agent.tool.approval_required"
    )


def test_agent_task_snapshot_updates_task_core_from_desktop_approval_event() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Desktop Workspace",
            "items": [
                {
                    "item_id": "ui-action-input",
                    "title": "operate-foreground-ui.input.json",
                    "kind": "scratch",
                    "source_step_id": "operate-foreground-ui",
                }
            ],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Click export",
                "step_id": "operate-foreground-ui",
                "tool_name": "app.open_and_click_ui_element",
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "title": "Verify click",
                "after_step_id": "operate-foreground-ui",
            }
        ],
        "replan_signals": [],
    }

    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "title": "Click export",
            "status": "approval_required",
            "metadata": {"yachiyo_task_core": core_payload},
            "events": [
                {
                    "event_type": "agent.desktop.intent_approval_required",
                    "payload": {
                        "step_id": "operate-foreground-ui",
                        "tool": "app.open_and_click_ui_element",
                        "status": "approval_required",
                    },
                }
            ],
        }
    )

    assert snapshot.task_core is not None
    assert snapshot.task_core.workspace.items[0].status == "blocked"
    assert snapshot.task_core.todos[0].status == "blocked"
    assert snapshot.task_core.checkpoints[0].status == "waiting_approval"
    assert snapshot.task_core.todos[0].metadata["runtime_event_type"] == (
        "agent.desktop.intent_approval_required"
    )


def test_agent_task_snapshot_replays_explicit_task_core_update_events_by_id() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Report Workspace",
            "items": [
                {
                    "item_id": "input-1",
                    "title": "sales.csv",
                    "kind": "input",
                    "path": "sales.csv",
                }
            ],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Read source",
                "step_id": "read-source",
                "tool_name": "workspace.read",
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "title": "Verify read",
                "after_step_id": "read-source",
            }
        ],
        "replan_signals": [],
    }

    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "title": "Write report",
            "status": "running",
            "metadata": {"yachiyo_task_core": core_payload},
            "events": [
                {
                    "event_type": "agent.task.workspace_item.updated",
                    "payload": {
                        "workspace_item_id": "input-1",
                        "status": "completed",
                        "source_event": {"event": "agent.tool.call"},
                        "workspace_item": {
                            "item_id": "input-1",
                            "title": "sales.csv",
                            "kind": "input",
                            "path": "sales.csv",
                            "status": "completed",
                            "metadata": {"observed_path": "sales.csv"},
                        },
                    },
                },
                {
                    "event_type": "group.run.task.todo.updated",
                    "payload": {
                        "todo_id": "todo-1",
                        "status": "completed",
                        "source_event": {"event": "agent.tool.call"},
                        "todo": {
                            "todo_id": "todo-1",
                            "title": "Read source",
                            "step_id": "read-source",
                            "tool_name": "workspace.read",
                            "status": "completed",
                            "metadata": {"runtime_note": "read ok"},
                        },
                    },
                },
                {
                    "event_type": "workflow.run.task.checkpoint.updated",
                    "payload": {
                        "checkpoint_id": "checkpoint-1",
                        "step_id": "read-source",
                        "status": "completed",
                        "verification_status": "verified",
                        "verified_by_step_id": "verify-read-source",
                        "source_event": {"event": "agent.tool.call"},
                        "checkpoint": {
                            "checkpoint_id": "checkpoint-1",
                            "title": "Verify read",
                            "after_step_id": "read-source",
                            "status": "completed",
                            "payload": {
                                "verified": True,
                                "verification_status": "verified",
                                "verified_by_step_id": "verify-read-source",
                            },
                        },
                    },
                },
            ],
        }
    )

    assert snapshot.task_core is not None
    item = snapshot.task_core.workspace.items[0]
    todo = snapshot.task_core.todos[0]
    checkpoint = snapshot.task_core.checkpoints[0]
    assert item.status == "completed"
    assert item.metadata["observed_path"] == "sales.csv"
    assert item.metadata["runtime_event_type"] == "agent.tool.call"
    assert item.metadata["runtime_update_event_type"] == "agent.task.workspace_item.updated"
    assert todo.status == "completed"
    assert todo.metadata["runtime_note"] == "read ok"
    assert todo.metadata["runtime_update_event_type"] == "group.run.task.todo.updated"
    assert checkpoint.status == "completed"
    assert checkpoint.payload["verified"] is True
    assert checkpoint.payload["runtime_update_event_type"] == (
        "workflow.run.task.checkpoint.updated"
    )
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.core_id == "task-core-1"
    assert snapshot.task_progress.status == "completed"
    assert snapshot.task_progress.completed_todos == 1
    assert snapshot.task_progress.completed_checkpoints == 1
    assert snapshot.task_progress.completed_workspace_items == 1
    assert snapshot.task_progress.verified_verification_count == 1
    assert snapshot.task_progress.latest_verification_status == "verified"
    assert snapshot.task_progress.latest_verification_step_id == "read-source"
    assert snapshot.task_progress.progress_text == "1/1 todos completed"


def test_run_timeline_snapshot_projects_task_core_from_plan_event() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Report Workspace",
            "items": [
                {
                    "item_id": "artifact-1",
                    "title": "report.md",
                    "kind": "artifact",
                    "path": "report.md",
                }
            ],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Write report",
                "step_id": "write-report",
                "tool_name": "artifact.write",
            }
        ],
        "checkpoints": [],
        "replan_signals": [],
    }
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "events": [
                {
                    "event_type": "agent.plan.created",
                    "payload": {
                        "source": "runtime_planner",
                        "decision_id": "decision-1",
                        "plan": {
                            "plan_id": "plan-1",
                            "intent": {
                                "intent_id": "intent-1",
                                "kind": "report_generation",
                                "title": "Write report",
                            },
                            "tool_plan": {"steps": []},
                            "task_core": core_payload,
                        },
                    },
                }
            ],
        }
    )

    assert snapshot.task_core is not None
    assert snapshot.task_core.core_id == "task-core-1"
    assert snapshot.task_core.workspace.items[0].kind == "artifact"
    assert snapshot.task_core.todos[0].tool_name == "artifact.write"


def test_run_timeline_snapshot_adds_runtime_artifacts_to_task_workspace() -> None:
    core_payload = {
        "core_id": "task-core-1",
        "workspace": {
            "workspace_id": "task-workspace-1",
            "title": "Report Workspace",
            "items": [],
        },
        "todos": [
            {
                "todo_id": "todo-1",
                "title": "Write report",
                "step_id": "write-report",
                "tool_name": "artifact.write",
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-1",
                "title": "Verify report",
                "after_step_id": "write-report",
            }
        ],
        "replan_signals": [],
    }
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "metadata": {"yachiyo_task_core": core_payload},
            "events": [
                {
                    "event_id": "artifact-event-1",
                    "sequence": 3,
                    "event_type": "agent.artifact.write",
                    "detail": "report.md",
                    "payload": {
                        "step_id": "write-report",
                        "tool": "artifact.write",
                        "artifact": {
                            "title": "Analysis report",
                            "kind": "markdown",
                            "path": "report.md",
                        },
                    },
                }
            ],
        }
    )

    assert snapshot.task_core is not None
    assert [item.path for item in snapshot.task_core.workspace.items] == ["report.md"]
    item = snapshot.task_core.workspace.items[0]
    assert item.item_id == "artifact:report.md"
    assert item.title == "Analysis report"
    assert item.kind == "artifact"
    assert item.status == "completed"
    assert item.source_step_id == "write-report"
    assert item.metadata["source"] == "runtime_artifact_event"
    assert item.metadata["artifact_kind"] == "markdown"
    assert item.metadata["runtime_event_type"] == "agent.artifact.write"
    assert snapshot.task_core.todos[0].status == "completed"
    assert snapshot.task_core.checkpoints[0].status == "completed"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.total_workspace_items == 1
    assert snapshot.task_progress.completed_workspace_items == 1


def test_agent_task_snapshot_adds_runtime_artifacts_to_task_workspace() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "title": "Write report",
            "status": "running",
            "metadata": {
                "yachiyo_task_core": {
                    "core_id": "task-core-1",
                    "workspace": {
                        "workspace_id": "task-workspace-1",
                        "title": "Report Workspace",
                        "items": [],
                    },
                    "todos": [],
                    "checkpoints": [],
                    "replan_signals": [],
                }
            },
            "events": [
                {
                    "event_type": "tool.completed",
                    "payload": {
                        "tool": "artifact.write",
                        "result": {
                            "ok": True,
                            "artifact": {
                                "title": "Report",
                                "kind": "markdown",
                                "path": "report.md",
                            },
                        },
                    },
                }
            ],
        }
    )

    assert snapshot.task_core is not None
    assert len(snapshot.task_core.workspace.items) == 1
    assert snapshot.task_core.workspace.items[0].path == "report.md"
    assert snapshot.task_core.workspace.items[0].status == "completed"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.completed_workspace_items == 1


def test_planner_plan_created_event_includes_execution_envelope_for_studio_debugging() -> None:
    decision = RuntimePlanner().decision(
        "分析当前窗口里的表格并输出报告",
        allowed_tools=["desktop.ui_elements", "data.analyze", "artifact.write"],
    )
    events = [
        {"event_type": event_type, "payload": payload}
        for event_type, payload in planner_run_event_payloads(decision)
    ]
    plan_event = next(
        event for event in events if event["event_type"] == "agent.plan.created"
    )
    task_core_event = next(
        event for event in events if event["event_type"] == "agent.task_core.created"
    )
    workspace_events = [
        event
        for event in events
        if event["event_type"] == "agent.task.workspace_item.updated"
    ]

    envelope = plan_event["payload"]["runtime_execution_envelope"]
    assert plan_event["payload"]["capability_plan"]["plan_id"] == (
        decision.plan.capability_plan.plan_id
    )
    assert task_core_event["payload"]["workspace_item_count"] == len(
        decision.plan.task_core.workspace.items
    )
    assert workspace_events
    assert workspace_events[0]["payload"]["workspace_item_id"]
    assert envelope["capability_plan"]["plan_id"] == decision.plan.capability_plan.plan_id
    assert envelope["decision_id"] == decision.decision_id
    assert envelope["plan_id"] == decision.plan.plan_id
    assert envelope["intent_kind"] == "data_analysis"
    assert envelope["task_core"]["core_id"] == decision.plan.task_core.core_id
    assert plan_event["payload"]["execution_request_count"] == len(envelope["requests"])
    assert envelope["requests"][0]["tool_name"] == "desktop.ui_elements"
    assert envelope["requests"][1]["tool_name"] == "data.analyze"
    assert envelope["requests"][1]["step_id"] == "analyze-data-context"
    assert envelope["requests"][1]["replan_signal_ids"]

    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "events": events,
        }
    )

    assert snapshot.events[1].payload["runtime_execution_envelope"]["requests"][1][
        "tool_name"
    ] == "data.analyze"
    assert snapshot.planner_summary is not None
    assert snapshot.planner_summary.plan_tools == [
        "desktop.ui_elements",
        "data.analyze",
    ]


def test_run_timeline_snapshot_synthesizes_replan_event_from_failed_tool() -> None:
    decision = RuntimePlanner().decision(
        "请分析 sales.csv 并输出一份数据分析报告",
        allowed_tools=["workspace.read", "data.analyze", "terminal.run", "artifact.write"],
    )
    events = [
        {"event_type": event_type, "payload": payload}
        for event_type, payload in planner_run_event_payloads(decision)
    ]
    events.append(
        {
            "event_type": "agent.tool.call",
            "payload": {
                "step_id": "run-analysis",
                "tool_name": "data.analyze",
                "status": "failed",
                "result": {"ok": False, "error": "unsupported chart type"},
            },
        }
    )

    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "failed",
            "events": events,
        }
    )
    task = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "failed",
            "events": events,
        }
    )

    replan_event = snapshot.events[-1]
    assert replan_event.event_type == "agent.replan.requested"
    assert replan_event.payload["trigger"] == "tool_failure"
    assert replan_event.payload["run_id"] == "run-1"
    assert replan_event.payload["task_id"] == "task-1"
    assert replan_event.payload["decision_id"] == decision.decision_id
    assert replan_event.payload["plan_id"] == decision.plan.plan_id
    assert replan_event.payload["core_id"] == decision.plan.task_core.core_id
    assert replan_event.payload["source_step_id"] == "run-analysis"
    assert replan_event.payload["source_tool_name"] == "data.analyze"
    assert replan_event.payload["target_capability_id"] == "data.analysis"
    assert replan_event.payload["fallback_tools"] == ["terminal.run"]
    assert "unsupported chart type" in replan_event.payload["failure_detail"]
    assert task.recent_events[-1].event_type == "agent.replan.requested"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.status == "replan_requested"
    assert snapshot.task_progress.needs_replan is True
    assert snapshot.task_progress.latest_replan_step_id == "run-analysis"
    assert snapshot.task_progress.latest_replan_trigger == "tool_failure"
    assert snapshot.task_progress.blocked_step_ids == ["analyze-data-file"]
    assert snapshot.replan_recoveries
    assert snapshot.replan_recoveries[0].request_id == replan_event.payload["request_id"]
    assert snapshot.replan_recoveries[0].status == "requested"
    assert snapshot.replan_recoveries[0].source_step_id == "run-analysis"
    assert snapshot.replan_recoveries[0].source_tool_name == "data.analyze"
    assert snapshot.replan_recoveries[0].fallback_tools == ["terminal.run"]
    assert task.task_core is not None
    assert task.task_progress is not None
    assert task.task_progress.status == snapshot.task_progress.status
    assert task.task_progress.latest_replan_step_id == "run-analysis"
    assert task.replan_recoveries[0].request_id == snapshot.replan_recoveries[0].request_id
    failed_todo = next(
        todo for todo in task.task_core.todos if todo.tool_name == "data.analyze"
    )
    assert failed_todo.status == "blocked"


def test_run_timeline_snapshot_projects_completed_replan_recovery() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "completed",
            "events": [
                {
                    "event_id": "event-0",
                    "sequence": 0,
                    "event_type": "agent.task_core.created",
                    "payload": {
                        "core_id": "task-core-1",
                        "task_core": {
                            "core_id": "task-core-1",
                            "workspace": {
                                "workspace_id": "task-workspace-1",
                                "title": "Analysis Workspace",
                            },
                            "todos": [
                                {
                                    "todo_id": "todo-analyze-data-file",
                                    "title": "Analyze data file",
                                    "step_id": "analyze-data-file",
                                    "tool_name": "data.analyze",
                                    "status": "pending",
                                }
                            ],
                            "checkpoints": [
                                {
                                    "checkpoint_id": "checkpoint:analyze-data-file",
                                    "title": "Verify analysis",
                                    "after_step_id": "analyze-data-file",
                                    "status": "planned",
                                }
                            ],
                            "replan_signals": [],
                        },
                    },
                },
                {
                    "event_id": "event-1",
                    "sequence": 1,
                    "event_type": "agent.replan.requested",
                    "payload": {
                        "request_id": "replan-1",
                        "trigger": "tool_failure",
                        "run_id": "run-1",
                        "task_id": "task-1",
                        "decision_id": "decision-1",
                        "plan_id": "plan-1",
                        "core_id": "task-core-1",
                        "source_step_id": "analyze-data-file",
                        "source_tool_name": "data.analyze",
                        "target_capability_id": "data.analysis",
                        "fallback_tools": ["terminal.run"],
                        "verification_targets": [
                            {
                                "step_id": "analyze-data-file",
                                "todo_id": "todo-analyze-data-file",
                                "todo_title": "Analyze data file",
                                "checkpoint_ids": ["checkpoint:analyze-data-file"],
                            }
                        ],
                        "task_verification_targets": [
                            {
                                "step_id": "analyze-data-file",
                                "workspace_items": [
                                    {
                                        "item_id": "workspace-analyze-input",
                                        "title": "analyze-data-file.input.json",
                                        "kind": "scratch",
                                        "source_step_id": "analyze-data-file",
                                    }
                                ],
                            }
                        ],
                        "failure_detail": "data.analyze failed",
                        "metadata": {
                            "recovery_actions": [
                                {
                                    "label": "Run terminal fallback",
                                    "tool": "terminal.run",
                                    "input": {"cmd": "python analyze.py"},
                                    "permission_target": "terminal_execution",
                                    "risk_level": "medium",
                                    "task_verification_targets": [
                                        {
                                            "step_id": "terminal-fallback",
                                            "workspace_items": [
                                                {
                                                    "item_id": "workspace-terminal-input",
                                                    "title": "terminal-fallback.input.json",
                                                    "kind": "scratch",
                                                    "source_step_id": "terminal-fallback",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ]
                        },
                    },
                },
                {
                    "event_id": "event-2",
                    "sequence": 2,
                    "event_type": "agent.desktop.intent_planned",
                    "payload": {
                        "replan_request_id": "replan-1",
                        "replan_trigger": "tool_failure",
                        "step_id": "analyze-data-file",
                        "capability_id": "data.analysis",
                        "tool_name": "terminal.run",
                        "planning_reason": "planner_replan_fallback_recovery",
                    },
                },
                {
                    "event_id": "event-3",
                    "sequence": 3,
                    "event_type": "agent.tool.call",
                    "payload": {
                        "replan_request_id": "replan-1",
                        "replan_trigger": "tool_failure",
                        "tool_call_id": "tool-call-terminal-recovery",
                        "step_id": "analyze-data-file",
                        "capability_id": "data.analysis",
                        "tool_name": "terminal.run",
                        "status": "completed",
                        "planning_reason": "planner_replan_runtime_recovery_action",
                        "recovery_action_label": "Run terminal fallback",
                        "permission_target": "terminal_execution",
                        "risk_level": "medium",
                        "action_target": {
                            "action": "click",
                            "label": "Apple Music result",
                            "app_name": "Music",
                        },
                        "observation_evidence": {
                            "strategy": "observed_center_fallback",
                            "observed_center": {"x": 512, "y": 220},
                        },
                        "result": {
                            "ok": True,
                            "stdout": "report.md",
                            "artifact_path": "reports/report.md",
                            "artifact_id": "artifact-report",
                        },
                    },
                },
                {
                    "event_id": "event-4",
                    "sequence": 4,
                    "event_type": "agent.task.todo.updated",
                    "payload": {
                        "replan_request_id": "replan-1",
                        "todo_id": "todo-analyze-data-file",
                        "status": "completed",
                        "todo": {
                            "todo_id": "todo-analyze-data-file",
                            "title": "Analyze data file",
                            "step_id": "analyze-data-file",
                            "tool_name": "data.analyze",
                            "status": "completed",
                        },
                    },
                },
                {
                    "event_id": "event-5",
                    "sequence": 5,
                    "event_type": "agent.task.checkpoint.updated",
                    "payload": {
                        "replan_request_id": "replan-1",
                        "checkpoint_id": "checkpoint:analyze-data-file",
                        "status": "completed",
                        "checkpoint": {
                            "checkpoint_id": "checkpoint:analyze-data-file",
                            "title": "Verify analysis",
                            "after_step_id": "analyze-data-file",
                            "status": "completed",
                        },
                    },
                },
            ],
        }
    )

    recovery = snapshot.replan_recoveries[0]
    assert recovery.request_id == "replan-1"
    assert recovery.status == "completed"
    assert recovery.selected_tool_name == "terminal.run"
    assert recovery.planning_reason == "planner_replan_runtime_recovery_action"
    assert recovery.recovery_action_label == "Run terminal fallback"
    assert len(recovery.recovery_actions) == 1
    assert recovery.recovery_actions[0].label == "Run terminal fallback"
    assert recovery.recovery_actions[0].tool == "terminal.run"
    assert recovery.recovery_actions[0].input == {"cmd": "python analyze.py"}
    assert recovery.recovery_actions[0].selected is True
    assert recovery.recovery_actions[0].planning_reason == (
        "planner_replan_runtime_recovery_action"
    )
    assert recovery.recovery_actions[0].permission_target == "terminal_execution"
    assert recovery.recovery_actions[0].risk_level == "medium"
    assert recovery.permission_target == "terminal_execution"
    assert recovery.risk_level == "medium"
    assert {
        "step_id": "analyze-data-file",
        "todo_id": "todo-analyze-data-file",
        "todo_title": "Analyze data file",
        "checkpoint_ids": ["checkpoint:analyze-data-file"],
    } in recovery.verification_targets
    assert {
        "step_id": "analyze-data-file",
        "workspace_items": [
            {
                "item_id": "workspace-analyze-input",
                "title": "analyze-data-file.input.json",
                "kind": "scratch",
                "source_step_id": "analyze-data-file",
            }
        ],
    } in recovery.verification_targets
    assert recovery.action_target == {
        "action": "click",
        "label": "Apple Music result",
        "app_name": "Music",
    }
    assert recovery.recovery_actions[0].action_target == recovery.action_target
    assert {
        "step_id": "terminal-fallback",
        "workspace_items": [
            {
                "item_id": "workspace-terminal-input",
                "title": "terminal-fallback.input.json",
                "kind": "scratch",
                "source_step_id": "terminal-fallback",
            }
        ],
    } in recovery.recovery_actions[0].verification_targets
    assert all(
        target in recovery.recovery_actions[0].verification_targets
        for target in recovery.verification_targets
    )
    assert recovery.observation_evidence == {
        "strategy": "observed_center_fallback",
        "observed_center": {"x": 512, "y": 220},
    }
    assert recovery.tool_call_id == "tool-call-terminal-recovery"
    assert recovery.tool_call_ids == ["tool-call-terminal-recovery"]
    assert recovery.artifact_paths == ["reports/report.md"]
    assert recovery.artifact_ids == ["artifact-report"]
    assert recovery.tool_status == "completed"
    assert recovery.checkpoint_status == "completed"
    assert recovery.result_preview == {
        "ok": True,
        "stdout": "report.md",
        "artifact_path": "reports/report.md",
        "artifact_id": "artifact-report",
    }
    assert recovery.recovery_event_ids == ["event-1", "event-2", "event-3", "event-4", "event-5"]
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.status == "completed"
    assert snapshot.task_progress.needs_replan is False
    assert snapshot.task_progress.replan_request_count == 1
    assert snapshot.task_progress.latest_replan_request_id is None
    assert snapshot.task_progress.latest_replan_step_id is None
    assert snapshot.task_progress.completed_todos == 1
    assert snapshot.task_progress.completed_checkpoints == 1
    assert snapshot.task_progress.progress_text == "1/1 todos completed"


def test_run_timeline_snapshot_projects_explicit_replan_recovery_update() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "completed",
            "events": [
                {
                    "event_id": "event-1",
                    "sequence": 1,
                    "event_type": "agent.replan.requested",
                    "payload": {
                        "request_id": "replan-1",
                        "trigger": "tool_failure",
                        "run_id": "run-1",
                        "task_id": "task-1",
                        "decision_id": "decision-1",
                        "plan_id": "plan-1",
                        "core_id": "task-core-1",
                        "source_step_id": "open-selected-discovered-app",
                        "source_tool_name": "app.open",
                        "target_capability_id": "desktop.app_control",
                        "fallback_tools": ["desktop.list_apps"],
                        "failure_detail": "app_resolution_failed",
                    },
                },
                {
                    "event_id": "event-2",
                    "sequence": 2,
                    "event_type": "agent.replan.recovery.updated",
                    "payload": {
                        "request_id": "replan-1",
                        "trigger": "tool_failure",
                        "run_id": "run-1",
                        "task_id": "task-1",
                        "decision_id": "decision-1",
                        "plan_id": "plan-1",
                        "core_id": "task-core-1",
                        "source_step_id": "open-selected-discovered-app",
                        "source_tool_name": "app.open",
                        "selected_step_id": "open-selected-discovered-app",
                        "selected_tool_name": "desktop.list_apps",
                        "target_capability_id": "desktop.app_control",
                        "fallback_tools": ["desktop.list_apps"],
                        "planning_reason": "planner_replan_runtime_recovery_action",
                        "recovery_action_label": "Rediscover app",
                        "permission_target": "app_discovery",
                        "risk_level": "low",
                        "approval_id": "approval-replan-1",
                        "status": "completed",
                        "tool_status": "completed",
                        "todo_status": "completed",
                        "checkpoint_status": "completed",
                        "artifact_path": "reports/preview.md",
                        "artifact_id": "artifact-preview",
                        "action_target": {
                            "action": "open_app",
                            "app_name": "Preview",
                        },
                        "observation_evidence": {
                            "source_tool": "desktop.list_apps",
                            "matched_app": "Preview",
                        },
                        "result_preview": {
                            "ok": True,
                            "summary": "Found Preview",
                        },
                    },
                },
            ],
        }
    )

    recovery = snapshot.replan_recoveries[0]
    assert recovery.request_id == "replan-1"
    assert recovery.status == "completed"
    assert recovery.source_tool_name == "app.open"
    assert recovery.selected_tool_name == "desktop.list_apps"
    assert recovery.recovery_action_label == "Rediscover app"
    assert recovery.permission_target == "app_discovery"
    assert recovery.risk_level == "low"
    assert recovery.approval_id == "approval-replan-1"
    assert recovery.approval_ids == ["approval-replan-1"]
    assert recovery.artifact_paths == ["reports/preview.md"]
    assert recovery.artifact_ids == ["artifact-preview"]
    assert recovery.action_target == {"action": "open_app", "app_name": "Preview"}
    assert recovery.observation_evidence == {
        "source_tool": "desktop.list_apps",
        "matched_app": "Preview",
    }
    assert recovery.tool_status == "completed"
    assert recovery.todo_status == "completed"
    assert recovery.checkpoint_status == "completed"
    assert recovery.result_preview == {"ok": True, "summary": "Found Preview"}
    assert recovery.recovery_event_ids == ["event-1", "event-2"]


def test_run_timeline_snapshot_synthesizes_scoped_workflow_replan_event() -> None:
    decision = RuntimePlanner().decision(
        "请分析 sales.csv 并输出一份数据分析报告",
        allowed_tools=["workspace.read", "data.analyze", "terminal.run", "artifact.write"],
    )
    analysis_step = next(
        step for step in decision.plan.tool_plan.steps if step.tool_name == "data.analyze"
    )
    scoped_events = []
    for event_type, payload in planner_run_event_payloads(decision):
        scoped_type = {
            "agent.intent.selected": "workflow.run.intent.selected",
            "agent.plan.created": "workflow.run.plan.created",
            "agent.task_core.created": "workflow.run.task_core.created",
            "agent.plan.step": "workflow.run.plan.step",
        }.get(event_type, event_type)
        scoped_events.append(
            {
                "event_type": scoped_type,
                "payload": {
                    **payload,
                    "planner_event_type": event_type,
                    "planner_scope": "workflow_run",
                },
            }
        )
    scoped_events.append(
        {
            "event_type": "agent.tool.call",
            "payload": {
                "step_id": analysis_step.step_id,
                "tool_name": "data.analyze",
                "status": "failed",
                "result": {"ok": False, "error": "empty result"},
            },
        }
    )

    snapshot = run_timeline_snapshot_from_payload(
        {
            "workflow_run_id": "workflow-run-1",
            "status": "failed",
            "events": scoped_events,
        }
    )

    replan_event = snapshot.events[-1]
    assert replan_event.event_type == "workflow.run.replan.requested"
    assert replan_event.payload["planner_event_type"] == "agent.replan.requested"
    assert replan_event.payload["planner_scope"] == "workflow_run"
    assert replan_event.payload["source_step_id"] == analysis_step.step_id
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.status == "replan_requested"
    assert snapshot.task_progress.latest_replan_step_id == analysis_step.step_id


def test_run_timeline_snapshot_synthesizes_replan_from_native_scoped_workflow_events() -> None:
    decision = RuntimePlanner().decision(
        "请分析 sales.csv 并输出一份数据分析报告",
        allowed_tools=["workspace.read", "data.analyze", "terminal.run", "artifact.write"],
    )
    analysis_step = next(
        step for step in decision.plan.tool_plan.steps if step.tool_name == "data.analyze"
    )
    scoped_events = []
    for event_type, payload in planner_run_event_payloads(decision):
        scoped_type = {
            "agent.intent.selected": "workflow.run.intent.selected",
            "agent.plan.created": "workflow.run.plan.created",
            "agent.task_core.created": "workflow.run.task_core.created",
            "agent.plan.step": "workflow.run.plan.step",
        }.get(event_type, event_type)
        scoped_events.append({"event_type": scoped_type, "payload": payload})
    scoped_events.append(
        {
            "event_type": "agent.tool.call",
            "payload": {
                "step_id": analysis_step.step_id,
                "tool_name": "data.analyze",
                "status": "failed",
                "result": {"ok": False, "error": "empty result"},
            },
        }
    )

    snapshot = run_timeline_snapshot_from_payload(
        {
            "workflow_run_id": "workflow-run-1",
            "status": "failed",
            "events": scoped_events,
        }
    )

    replan_event = snapshot.events[-1]
    assert replan_event.event_type == "workflow.run.replan.requested"
    assert replan_event.payload["planner_event_type"] == "agent.replan.requested"
    assert replan_event.payload["planner_scope"] == "workflow_run"
    assert replan_event.payload["source_step_id"] == analysis_step.step_id


def test_group_run_snapshot_synthesizes_scoped_replan_event() -> None:
    decision = RuntimePlanner().decision(
        "请分析 sales.csv 并输出一份数据分析报告",
        allowed_tools=["workspace.read", "data.analyze", "terminal.run", "artifact.write"],
    )
    analysis_step = next(
        step for step in decision.plan.tool_plan.steps if step.tool_name == "data.analyze"
    )
    events = [
        {"event_type": event_type, "payload": payload}
        for event_type, payload in planner_run_event_payloads(decision)
    ]
    events.append(
        {
            "event_type": "agent.desktop.intent_planned",
            "payload": {
                "tool": "terminal.run",
                "planning_reason": "planner_replan_fallback_recovery",
                "replan_request_id": "replan-terminal-1",
                "replan_trigger": "tool_failure",
                "step_id": analysis_step.step_id,
            },
        }
    )
    events.append(
        {
            "event_type": "agent.tool.call",
            "payload": {
                "step_id": analysis_step.step_id,
                "tool_name": "data.analyze",
                "status": "failed",
                "result": {"ok": False, "error": "empty result"},
            },
        }
    )

    snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-1",
            "group_id": "group-1",
            "status": "failed",
            "objective": "Analyze sales as a group",
            "events": events,
        }
    )

    event_types = [event.event_type for event in snapshot.events]
    assert "group.run.intent.selected" in event_types
    assert "group.run.plan.created" in event_types
    assert "group.run.task_core.created" in event_types
    assert "group.run.plan.step" in event_types
    planned_event = next(
        event for event in snapshot.events if event.event_type == "group.run.desktop.intent_planned"
    )
    assert planned_event.payload["planner_event_type"] == "agent.desktop.intent_planned"
    assert planned_event.payload["planner_scope"] == "group_run"
    assert planned_event.payload["replan_request_id"] == "replan-terminal-1"
    replan_event = next(
        event for event in snapshot.events if event.event_type == "group.run.replan.requested"
    )
    assert replan_event.event_type == "group.run.replan.requested"
    assert replan_event.payload["planner_event_type"] == "agent.replan.requested"
    assert replan_event.payload["planner_scope"] == "group_run"
    assert replan_event.payload["source_step_id"] == analysis_step.step_id
    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.intent_kind == decision.selected_intent.kind
    assert snapshot.runtime_execution_envelope.requests
    assert snapshot.runtime_execution_envelope.requests[0].group_run_id == "group-run-1"
    assert snapshot.runtime_execution_envelope.requests[0].run_group_id == "group-run-1"
    assert snapshot.planner_summary is not None
    assert snapshot.planner_summary.intent_kind == decision.selected_intent.kind
    assert "data.analyze" in snapshot.planner_summary.plan_tools
    assert "data.analysis" in snapshot.planner_summary.plan_capabilities
    assert snapshot.runtime_debug is not None
    assert "data.analyze" in snapshot.runtime_debug.plan_tools
    assert "data.analysis" in snapshot.runtime_debug.plan_capabilities
    assert snapshot.task_core is not None
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.status == "replan_requested"
    assert snapshot.task_progress.latest_replan_step_id == analysis_step.step_id
    assert snapshot.replan_recoveries
    assert snapshot.replan_recoveries[0].status == "requested"
    assert snapshot.replan_recoveries[0].group_run_id == "group-run-1"
    assert snapshot.replan_recoveries[0].source_step_id == analysis_step.step_id
    failed_todo = next(
        todo for todo in snapshot.task_core.todos if todo.step_id == analysis_step.step_id
    )
    assert failed_todo.status == "blocked"
    assert "group.run.failed" in event_types


def test_workflow_run_snapshot_scopes_agent_planner_events_to_workflow_run() -> None:
    snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-1",
            "workflow_run_id": "workflow-run-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "user_goal": "Build report",
            "events": [
                {
                    "event_type": "agent.intent.selected",
                    "payload": {
                        "source": "runtime_planner",
                        "intent": {
                            "intent_id": "intent-1",
                            "kind": "workflow_orchestration",
                            "title": "Workflow Orchestration",
                        },
                    },
                },
                {
                    "event_type": "agent.task_core.created",
                    "payload": {
                        "source": "runtime_planner",
                        "core_id": "task-core-1",
                        "task_core": {
                            "core_id": "task-core-1",
                            "workspace": {
                                "workspace_id": "task-workspace-1",
                                "title": "Workflow Workspace",
                            },
                            "todos": [],
                            "checkpoints": [],
                            "replan_signals": [],
                        },
                    },
                },
                {
                    "event_type": "agent.desktop.intent_planned",
                    "payload": {
                        "source": "runtime_planner",
                        "tool": "workflow.run",
                        "planning_reason": "planner_replan_fallback_recovery",
                        "replan_request_id": "replan-workflow-1",
                    },
                },
            ],
        }
    )

    event_types = [event.event_type for event in snapshot.events]
    assert event_types[:4] == [
        "workflow.run.started",
        "workflow.run.intent.selected",
        "workflow.run.task_core.created",
        "workflow.run.desktop.intent_planned",
    ]
    planner_event = snapshot.events[1]
    assert planner_event.payload["planner_scope"] == "workflow_run"
    assert planner_event.payload["planner_event_type"] == "agent.intent.selected"
    task_core_event = snapshot.events[2]
    assert task_core_event.payload["planner_event_type"] == "agent.task_core.created"
    assert task_core_event.payload["task_core"]["workspace"]["title"] == "Workflow Workspace"
    planned_event = snapshot.events[3]
    assert planned_event.payload["planner_event_type"] == "agent.desktop.intent_planned"
    assert planned_event.payload["planner_scope"] == "workflow_run"
    assert planned_event.payload["replan_request_id"] == "replan-workflow-1"


def test_workflow_run_snapshot_rolls_child_debug_state_into_timeline() -> None:
    snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-rollup-1",
            "workflow_run_id": "workflow-run-rollup-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "objective": "Analyze a CSV and approve export",
            "runs": [
                {
                    "run_id": "workflow-node-run-1",
                    "status": "approval_required",
                    "kind": "agent_run",
                    "agent_id": "agent-1",
                    "workflow_node_id": "analyze",
                    "workflow_node_label": "Analyze data",
                    "events": [
                        {
                            "event_type": "agent.tool.call",
                            "payload": {
                                "tool_call_id": "tool-call-1",
                                "tool_name": "data.analyze",
                                "status": "completed",
                                "result": {"ok": True, "rows": 42},
                            },
                        },
                        {
                            "event_type": "artifact.created",
                            "payload": {
                                "artifact_id": "artifact-1",
                                "title": "Analysis report",
                                "kind": "markdown_report",
                                "path": "artifacts/report.md",
                            },
                        },
                        {
                            "event_type": "agent.tool.approval_required",
                            "payload": {
                                "approval_id": "approval-1",
                                "tool_name": "desktop.click_ui_element",
                                "title": "Approve export",
                                "risk_level": "medium",
                                "input_preview": {"label": "Export"},
                            },
                        },
                    ],
                }
            ],
        }
    )

    child = snapshot.children[0]
    assert child.run_id == "workflow-node-run-1"
    assert child.parent_run_id == "workflow-run-rollup-1"
    assert child.workflow_run_id == "workflow-run-rollup-1"
    assert child.workflow_node_id == "analyze"
    assert child.agent_id == "agent-1"

    assert snapshot.tool_calls[0].tool_name == "data.analyze"
    assert snapshot.tool_calls[0].run_id == "workflow-node-run-1"
    assert snapshot.tool_calls[0].workflow_run_id == "workflow-run-rollup-1"
    assert snapshot.tool_calls[0].workflow_node_id == "analyze"
    assert snapshot.artifacts[0].artifact_id == "artifact-1"
    assert snapshot.artifacts[0].source_run_id == "workflow-node-run-1"
    assert snapshot.artifacts[0].workflow_run_id == "workflow-run-rollup-1"
    assert snapshot.pending_approval is not None
    assert snapshot.pending_approval.approval_id == "approval-1"
    assert snapshot.pending_approval.run_id == "workflow-node-run-1"
    assert snapshot.pending_approval.workflow_node_label == "Analyze data"
    assert snapshot.approvals[0].workflow_run_id == "workflow-run-rollup-1"
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.workflow_id == "workflow-1"
    assert snapshot.runtime_debug.workflow_run_id == "workflow-run-rollup-1"
    assert snapshot.runtime_debug.tool_call_count == 2
    assert snapshot.runtime_debug.waiting_tool_call_count == 1
    assert snapshot.runtime_debug.pending_approval_count == 1
    assert snapshot.runtime_debug.artifact_count == 1
    assert snapshot.runtime_debug.child_run_count == 1


def test_run_timeline_snapshot_projects_desktop_approval_event() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-approval-1",
            "status": "approval_required",
            "events": [
                {
                    "event_type": "agent.task_core.created",
                    "payload": {"task_core": _desktop_approval_task_core_payload()},
                },
                {
                    "event_type": "agent.desktop.intent_approval_required",
                    "payload": _desktop_approval_event_payload("approval-1"),
                },
            ],
        }
    )

    assert snapshot.pending_approval is not None
    assert snapshot.pending_approval.approval_id == "approval-1"
    assert snapshot.pending_approval.tool_name == "desktop.click_ui_element"
    assert snapshot.pending_approval.step_id == "operate-foreground-ui"
    assert snapshot.pending_approval.capability_id == "desktop.ui_operation"
    assert snapshot.pending_approval.plan_id == "runtime-plan-1"
    assert snapshot.task_core is not None
    assert snapshot.task_core.todos[0].status == "blocked"
    assert snapshot.task_core.checkpoints[0].status == "waiting_approval"


def test_group_and_workflow_snapshots_scope_desktop_approval_events() -> None:
    group_snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-approval-1",
            "group_id": "group-1",
            "status": "approval_required",
            "objective": "Operate desktop as a group",
            "events": [
                {
                    "event_type": "agent.task_core.created",
                    "payload": {"task_core": _desktop_approval_task_core_payload()},
                },
                {
                    "event_type": "agent.desktop.intent_approval_required",
                    "payload": _desktop_approval_event_payload("approval-group-1"),
                },
            ],
        }
    )
    workflow_snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-approval-1",
            "workflow_run_id": "workflow-run-approval-1",
            "workflow_id": "workflow-1",
            "status": "approval_required",
            "objective": "Operate desktop in workflow",
            "events": [
                {
                    "event_type": "agent.task_core.created",
                    "payload": {"task_core": _desktop_approval_task_core_payload()},
                },
                {
                    "event_type": "agent.desktop.intent_approval_required",
                    "payload": {
                        **_desktop_approval_event_payload("approval-workflow-1"),
                        "workflow_id": "workflow-1",
                        "workflow_run_id": "workflow-run-approval-1",
                    },
                },
            ],
        }
    )

    group_event_types = [event.event_type for event in group_snapshot.events]
    assert "group.run.desktop.intent_approval_required" in group_event_types
    assert group_snapshot.pending_approvals[0].group_run_id == "group-run-approval-1"
    assert group_snapshot.pending_approvals[0].step_id == "operate-foreground-ui"
    assert group_snapshot.task_core is not None
    assert group_snapshot.task_progress is not None
    assert group_snapshot.task_progress.status == "waiting_approval"
    assert group_snapshot.task_progress.needs_user_action is True
    assert group_snapshot.task_core.todos[0].metadata["runtime_event_type"] == (
        "group.run.desktop.intent_approval_required"
    )

    workflow_event_types = [event.event_type for event in workflow_snapshot.events]
    assert "workflow.run.desktop.intent_approval_required" in workflow_event_types
    assert workflow_snapshot.pending_approval is not None
    assert workflow_snapshot.pending_approval.workflow_run_id == "workflow-run-approval-1"
    assert workflow_snapshot.task_progress is not None
    assert workflow_snapshot.task_progress.status == "waiting_approval"
    assert workflow_snapshot.pending_approval.step_id == "operate-foreground-ui"
    assert workflow_snapshot.task_core is not None
    assert workflow_snapshot.task_core.todos[0].metadata["runtime_event_type"] == (
        "workflow.run.desktop.intent_approval_required"
    )


def test_group_and_workflow_snapshots_scope_desktop_result_events() -> None:
    group_snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-desktop-result-1",
            "group_id": "group-1",
            "status": "completed",
            "objective": "Operate desktop as a group",
            "events": [
                {
                    "event_type": "agent.task_core.created",
                    "payload": {"task_core": _desktop_approval_task_core_payload()},
                },
                {
                    "event_type": "agent.desktop.intent_completed",
                    "payload": {
                        "tool": "desktop.click_ui_element",
                        "status": "completed",
                        "source": "runtime_planner",
                        "step_id": "operate-foreground-ui",
                        "capability_id": "desktop.ui_operation",
                        "input_preview": {"label": "Play"},
                        "result": {"ok": True},
                    },
                },
            ],
        }
    )
    workflow_snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-desktop-result-1",
            "workflow_run_id": "workflow-run-desktop-result-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "objective": "Operate desktop in workflow",
            "events": [
                {
                    "event_type": "agent.task_core.created",
                    "payload": {"task_core": _desktop_approval_task_core_payload()},
                },
                {
                    "event_type": "agent.desktop.intent_unavailable",
                    "payload": {
                        "tool": "desktop.click_ui_element",
                        "source": "runtime_planner",
                        "step_id": "operate-foreground-ui",
                        "capability_id": "desktop.ui_operation",
                        "reason": "tool_not_allowed",
                        "blocked_by": "agent_tool_policy",
                    },
                },
            ],
        }
    )

    group_event_types = [event.event_type for event in group_snapshot.events]
    assert "group.run.desktop.intent_completed" in group_event_types
    assert group_snapshot.tool_calls[0].status == "completed"
    assert group_snapshot.tool_calls[0].group_run_id == "group-run-desktop-result-1"
    assert group_snapshot.task_core is not None
    assert group_snapshot.task_core.todos[0].status == "completed"
    assert group_snapshot.task_core.todos[0].metadata["runtime_event_type"] == (
        "group.run.desktop.intent_completed"
    )
    assert group_snapshot.task_core.checkpoints[0].status == "completed"

    workflow_event_types = [event.event_type for event in workflow_snapshot.events]
    assert "workflow.run.desktop.intent_unavailable" in workflow_event_types
    assert workflow_snapshot.tool_calls[0].status == "blocked"
    assert workflow_snapshot.tool_calls[0].workflow_run_id == "workflow-run-desktop-result-1"
    assert workflow_snapshot.task_core is not None
    assert workflow_snapshot.task_core.todos[0].status == "blocked"
    assert workflow_snapshot.task_core.todos[0].metadata["runtime_event_type"] == (
        "workflow.run.desktop.intent_unavailable"
    )
    assert workflow_snapshot.task_core.checkpoints[0].status == "blocked"


def test_group_and_workflow_snapshots_scope_desktop_recovery_events() -> None:
    group_snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-desktop-recovery-1",
            "group_id": "group-1",
            "status": "running",
            "objective": "Recover desktop operation as a group",
            "events": [
                {
                    "event_type": "agent.desktop.permission_recovery",
                    "payload": {
                        "tool": "desktop.safe_type_text",
                        "permission_targets": ["accessibility"],
                        "affected_tools": ["desktop.safe_type_text"],
                        "recovery_actions": [
                            {
                                "label": "打开辅助功能权限",
                                "tool": "system.settings_open",
                                "input": {"target": "accessibility"},
                            }
                        ],
                    },
                }
            ],
        }
    )
    workflow_snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-desktop-recovery-1",
            "workflow_run_id": "workflow-run-desktop-recovery-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "objective": "Recover desktop operation in workflow",
            "events": [
                {
                    "event_type": "agent.desktop.readiness_recovered",
                    "sequence": 2,
                    "payload": {
                        "tool": "desktop.list_apps",
                        "recovery_tool": "desktop.list_apps",
                        "status": "recovered",
                        "app_name": "PixelForge",
                        "blocking_conditions": ["app_not_found"],
                    },
                }
            ],
        }
    )

    group_event_types = [event.event_type for event in group_snapshot.events]
    group_recovery_event = next(
        event
        for event in group_snapshot.events
        if event.event_type == "group.run.desktop.permission_recovery"
    )
    assert "group.run.desktop.permission_recovery" in group_event_types
    assert group_recovery_event.payload["planner_event_type"] == (
        "agent.desktop.permission_recovery"
    )
    assert group_snapshot.tool_calls[0].tool_name == "desktop.safe_type_text"
    assert group_snapshot.tool_calls[0].status == "failed"
    assert group_snapshot.tool_calls[0].output_preview["permission_targets"] == [
        "accessibility"
    ]
    workflow_event_types = [event.event_type for event in workflow_snapshot.events]
    workflow_recovered_event = next(
        event
        for event in workflow_snapshot.events
        if event.event_type == "workflow.run.desktop.readiness_recovered"
    )
    assert "workflow.run.desktop.readiness_recovered" in workflow_event_types
    assert workflow_recovered_event.payload["planner_event_type"] == (
        "agent.desktop.readiness_recovered"
    )


def test_group_and_workflow_snapshots_scope_replan_recovery_updates() -> None:
    task_core_created = {
        "event_type": "agent.task_core.created",
        "payload": {
            "core_id": "task-core-1",
            "task_core": {
                "core_id": "task-core-1",
                "workspace": {
                    "workspace_id": "task-workspace-1",
                    "title": "Desktop Recovery Workspace",
                },
                "todos": [
                    {
                        "todo_id": "todo-open-selected-discovered-app",
                        "title": "Open selected app",
                        "step_id": "open-selected-discovered-app",
                        "tool_name": "app.open",
                        "status": "pending",
                    }
                ],
                "checkpoints": [
                    {
                        "checkpoint_id": "checkpoint-open-selected-discovered-app",
                        "title": "Verify selected app opened",
                        "after_step_id": "open-selected-discovered-app",
                        "status": "planned",
                    }
                ],
                "replan_signals": [],
            },
        },
    }
    replan_request = {
        "request_id": "replan-app-1",
        "trigger": "tool_failure",
        "decision_id": "decision-1",
        "plan_id": "plan-1",
        "core_id": "task-core-1",
        "source_step_id": "open-selected-discovered-app",
        "source_tool_name": "app.open",
        "target_capability_id": "desktop.app_control",
        "fallback_tools": ["desktop.list_apps"],
        "failure_detail": "app_resolution_failed",
    }
    todo_completed = {
        "event_type": "agent.task.todo.updated",
        "payload": {
            "replan_request_id": "replan-app-1",
            "todo_id": "todo-open-selected-discovered-app",
            "status": "completed",
            "todo": {
                "todo_id": "todo-open-selected-discovered-app",
                "title": "Open selected app",
                "step_id": "open-selected-discovered-app",
                "tool_name": "app.open",
                "status": "completed",
            },
        },
    }
    checkpoint_completed = {
        "event_type": "agent.task.checkpoint.updated",
        "payload": {
            "replan_request_id": "replan-app-1",
            "checkpoint_id": "checkpoint-open-selected-discovered-app",
            "status": "completed",
            "checkpoint": {
                "checkpoint_id": "checkpoint-open-selected-discovered-app",
                "title": "Verify selected app opened",
                "after_step_id": "open-selected-discovered-app",
                "status": "completed",
            },
        },
    }
    recovery_update = {
        "request_id": "replan-app-1",
        "trigger": "tool_failure",
        "decision_id": "decision-1",
        "plan_id": "plan-1",
        "core_id": "task-core-1",
        "source_step_id": "open-selected-discovered-app",
        "source_tool_name": "app.open",
        "selected_step_id": "open-selected-discovered-app",
        "selected_tool_name": "desktop.list_apps",
        "target_capability_id": "desktop.app_control",
        "planning_reason": "planner_replan_runtime_recovery_action",
        "status": "completed",
        "tool_status": "completed",
        "todo_status": "completed",
        "checkpoint_status": "completed",
        "recovery_action_label": "Rediscover app",
        "permission_target": "app_discovery",
        "risk_level": "low",
        "result_preview": {"ok": True, "summary": "Found Preview"},
    }
    group_snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-recovery-1",
            "group_id": "group-1",
            "status": "completed",
            "objective": "Recover app as a group",
            "events": [
                task_core_created,
                {
                    "event_type": "agent.replan.requested",
                    "payload": replan_request,
                },
                {
                    "event_type": "agent.replan.recovery.updated",
                    "payload": recovery_update,
                },
                todo_completed,
                checkpoint_completed,
            ],
        }
    )
    workflow_snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-recovery-1",
            "workflow_run_id": "workflow-run-recovery-1",
            "workflow_id": "workflow-1",
            "status": "completed",
            "objective": "Recover app in workflow",
            "events": [
                task_core_created,
                {
                    "event_type": "agent.replan.requested",
                    "payload": replan_request,
                },
                {
                    "event_type": "agent.replan.recovery.updated",
                    "payload": recovery_update,
                },
                todo_completed,
                checkpoint_completed,
            ],
        }
    )

    group_recovery_event = next(
        event
        for event in group_snapshot.events
        if event.event_type == "group.run.replan.recovery.updated"
    )
    assert group_recovery_event.payload["planner_event_type"] == (
        "agent.replan.recovery.updated"
    )
    assert group_recovery_event.payload["planner_scope"] == "group_run"
    assert group_snapshot.replan_recoveries[0].status == "completed"
    assert group_snapshot.replan_recoveries[0].group_run_id == "group-run-recovery-1"
    assert group_snapshot.replan_recoveries[0].selected_tool_name == "desktop.list_apps"
    assert group_snapshot.replan_recoveries[0].result_preview == {
        "ok": True,
        "summary": "Found Preview",
    }
    assert group_snapshot.task_progress is not None
    assert group_snapshot.task_progress.status == "completed"
    assert group_snapshot.task_progress.needs_replan is False
    assert group_snapshot.task_progress.replan_request_count == 1
    assert group_snapshot.task_progress.completed_todos == 1
    assert group_snapshot.task_progress.completed_checkpoints == 1

    workflow_recovery_event = next(
        event
        for event in workflow_snapshot.events
        if event.event_type == "workflow.run.replan.recovery.updated"
    )
    assert workflow_recovery_event.payload["planner_event_type"] == (
        "agent.replan.recovery.updated"
    )
    assert workflow_recovery_event.payload["planner_scope"] == "workflow_run"
    assert workflow_snapshot.replan_recoveries[0].status == "completed"
    assert workflow_snapshot.replan_recoveries[0].workflow_run_id == (
        "workflow-run-recovery-1"
    )
    assert workflow_snapshot.replan_recoveries[0].selected_tool_name == "desktop.list_apps"
    assert workflow_snapshot.replan_recoveries[0].permission_target == "app_discovery"
    assert workflow_snapshot.task_progress is not None
    assert workflow_snapshot.task_progress.status == "completed"
    assert workflow_snapshot.task_progress.needs_replan is False
    assert workflow_snapshot.task_progress.replan_request_count == 1
    assert workflow_snapshot.task_progress.completed_todos == 1
    assert workflow_snapshot.task_progress.completed_checkpoints == 1


def test_group_and_workflow_snapshots_select_single_replan_recovery_action() -> None:
    replan_request = {
        "request_id": "replan-app-discovery-1",
        "trigger": "tool_failure",
        "source_step_id": "open-selected-discovered-app",
        "source_tool_name": "app.open",
        "target_capability_id": "desktop.app_control",
        "fallback_tools": ["desktop.list_apps"],
        "recovery_actions": [
            {
                "label": "Rediscover app",
                "tool": "desktop.list_apps",
                "input": {"query": "Preview", "limit": 20},
                "planning_reason": "planner_replan_runtime_recovery_action",
                "permission_target": "app_discovery",
                "risk_level": "low",
                "approval_required": False,
            }
        ],
    }

    group_snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-request-recovery-1",
            "group_id": "group-1",
            "status": "running",
            "events": [
                {
                    "event_type": "agent.replan.requested",
                    "payload": replan_request,
                }
            ],
        }
    )
    workflow_snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "workflow-run-request-recovery-1",
            "workflow_run_id": "workflow-run-request-recovery-1",
            "workflow_id": "workflow-1",
            "status": "running",
            "events": [
                {
                    "event_type": "agent.replan.requested",
                    "payload": replan_request,
                }
            ],
        }
    )

    group_event = next(
        event
        for event in group_snapshot.events
        if event.event_type == "group.run.replan.requested"
    )
    workflow_event = next(
        event
        for event in workflow_snapshot.events
        if event.event_type == "workflow.run.replan.requested"
    )
    assert group_event.payload["recovery_actions"][0]["input"] == {
        "query": "Preview",
        "limit": 20,
    }
    assert workflow_event.payload["recovery_actions"][0]["tool"] == "desktop.list_apps"

    group_recovery = group_snapshot.replan_recoveries[0]
    workflow_recovery = workflow_snapshot.replan_recoveries[0]
    assert group_recovery.group_run_id == "group-run-request-recovery-1"
    assert workflow_recovery.workflow_run_id == "workflow-run-request-recovery-1"
    for recovery in (group_recovery, workflow_recovery):
        assert recovery.selected_tool_name == "desktop.list_apps"
        assert recovery.planning_reason == "planner_replan_runtime_recovery_action"
        assert recovery.recovery_action_label == "Rediscover app"
        assert recovery.recovery_actions[0].selected is True
        assert recovery.recovery_actions[0].input == {
            "query": "Preview",
            "limit": 20,
        }


def _desktop_approval_task_core_payload() -> dict:
    return {
        "core_id": "task-core-desktop-approval",
        "workspace": {
            "workspace_id": "task-workspace-desktop",
            "title": "Desktop Workspace",
            "items": [
                {
                    "item_id": "desktop-ui-target",
                    "title": "Foreground UI",
                    "kind": "app",
                    "source_step_id": "operate-foreground-ui",
                }
            ],
        },
        "todos": [
            {
                "todo_id": "todo-operate-foreground-ui",
                "title": "Operate foreground UI",
                "step_id": "operate-foreground-ui",
                "capability_id": "desktop.ui_operation",
                "tool_name": "desktop.click_ui_element",
                "approval_required": True,
            }
        ],
        "checkpoints": [
            {
                "checkpoint_id": "checkpoint-foreground-ui",
                "title": "Verify foreground UI",
                "after_step_id": "operate-foreground-ui",
            }
        ],
        "replan_signals": [],
    }


def _desktop_approval_event_payload(approval_id: str) -> dict:
    return {
        "tool": "desktop.click_ui_element",
        "status": "approval_required",
        "source": "runtime_planner",
        "planning_reason": "planner_policy_gate",
        "approval_id": approval_id,
        "risk_level": "medium",
        "input_preview": {"label": "Play"},
        "step_id": "operate-foreground-ui",
        "capability_id": "desktop.ui_operation",
        "decision_id": "decision-1",
        "plan_id": "runtime-plan-1",
        "tool_plan_id": "tool-plan-1",
        "intent_kind": "desktop_operation",
    }


def test_planner_summary_redacts_followup_target_secret_fields() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-secret",
            "status": "running",
            "events": [
                {
                    "event_type": "agent.plan.selection",
                    "payload": {
                        "source": "runtime_planner",
                        "selection_source": "runtime_planner",
                        "followup_target": {
                            "kind": "app_write",
                            "app_name": "Notes",
                            "api_key": "abc123",
                            "communication_compose": {
                                "recipient": "Alice",
                                "password": "pw",
                            },
                        },
                    },
                }
            ],
        }
    )

    assert snapshot.planner_summary is not None
    assert snapshot.planner_summary.followup_target == {
        "kind": "app_write",
        "app_name": "Notes",
        "api_key": "[redacted]",
        "communication_compose": {
            "recipient": "Alice",
            "password": "[redacted]",
        },
    }


def test_agent_task_snapshot_json_shape_is_stable() -> None:
    snapshot = AgentTaskSnapshot(
        task_id="task-1",
        conversation_id="chat-1",
        title="Review README",
        status="waiting_approval",
        summary="Waiting for write approval",
        current_step="Prepare patch",
        progress_text="1 approval pending",
        needs_user_action=True,
        pending_approvals=[
            ApprovalCardSnapshot(
                approval_id="approval-1",
                run_id="run-1",
                title="Approve workspace.write_patch",
                tool_name="workspace.write_patch",
                input_preview={"path": "README.md"},
            )
        ],
        recent_events=[
            PublicRunEvent(
                event_id="event-1",
                run_id="run-1",
                sequence=1,
                event_type="agent.tool.approval_required",
                detail="workspace.write_patch",
            )
        ],
        tool_calls=[
            ToolCallSnapshot(
                tool_call_id="call-1",
                run_id="run-1",
                tool_name="workspace.write_patch",
                status="waiting_approval",
                input_preview={"path": "README.md"},
            )
        ],
        artifacts=[
            ArtifactSnapshot(
                artifact_id="artifact-1",
                run_id="run-1",
                title="Report",
                kind="markdown",
                path="report.md",
            )
        ],
        metadata={
            "yachiyo_runtime_planner": True,
            "yachiyo_intent_kind": "code_task",
            "yachiyo_plan_tools": ["workspace.write_patch"],
        },
        planner_summary=PlannerTraceSummarySnapshot(
            source="runtime_planner",
            intent_kind="code_task",
            plan_tools=["workspace.write_patch"],
            plan_capabilities=["terminal.execute"],
            step_count=1,
            event_count=2,
        ),
        runtime_debug=RuntimeDebugSummarySnapshot(
            run_id="run-1",
            task_id="task-1",
            event_count=1,
            tool_call_count=1,
            pending_approval_count=1,
            artifact_count=1,
            latest_recovery_action_id="replan-1:action:1:desktop.list_apps",
            latest_recovery_tool="desktop.list_apps",
            latest_recovery_action_label="Discover installed apps",
            latest_recovery_action_count=1,
            debug_surfaces=["timeline", "tools", "approvals", "artifacts"],
        ),
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="execution-envelope-runtime-plan-1",
            decision_id="decision-1",
            plan_id="runtime-plan-1",
            intent_kind="code_task",
            runtime_stage_counts={"operate": 1},
        ),
        open_in_studio_url="#/agents?run_id=run-1",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "task_id",
        "conversation_id",
        "title",
        "status",
        "summary",
        "current_step",
        "progress_text",
        "needs_user_action",
        "pending_approvals",
        "recent_events",
        "tool_calls",
        "artifacts",
        "metadata",
        "planner_summary",
        "runtime_debug",
        "runtime_execution_envelope",
        "task_core",
        "task_progress",
        "replan_recoveries",
        "open_in_studio_url",
        "created_at",
        "updated_at",
    ]
    assert payload["pending_approvals"][0]["approval_id"] == "approval-1"
    assert payload["recent_events"][0]["event_type"] == "agent.tool.approval_required"
    assert payload["tool_calls"][0]["tool_name"] == "workspace.write_patch"
    assert payload["metadata"]["yachiyo_plan_tools"] == ["workspace.write_patch"]
    assert payload["planner_summary"]["intent_kind"] == "code_task"
    assert payload["planner_summary"]["plan_capabilities"] == ["terminal.execute"]
    assert payload["runtime_debug"]["tool_call_count"] == 1
    assert payload["runtime_debug"]["latest_recovery_tool"] == "desktop.list_apps"
    assert payload["runtime_debug"]["latest_recovery_action_count"] == 1
    assert payload["runtime_debug"]["debug_surfaces"] == [
        "timeline",
        "tools",
        "approvals",
        "artifacts",
    ]
    assert payload["runtime_execution_envelope"]["intent_kind"] == "code_task"
    assert "event" not in payload["recent_events"][0]


def test_agent_task_snapshot_projects_runtime_execution_envelope_from_metadata() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "running",
            "metadata": {
                "yachiyo_execution_envelope": {
                    "envelope_id": "execution-envelope-runtime-plan-1",
                    "decision_id": "decision-1",
                    "plan_id": "runtime-plan-1",
                    "intent_kind": "desktop_operation",
                    "requests": [
                        {
                            "request_id": "request-1",
                            "tool_name": "desktop.list_apps",
                            "runtime_stage": "discover",
                        },
                        {
                            "request_id": "request-2",
                            "tool_name": "app.open",
                            "runtime_stage": "operate",
                            "approval_required": True,
                        },
                    ],
                    "runtime_stage_counts": {"discover": 1, "operate": 1},
                    "approvals_required": ["open-or-focus-app"],
                }
            },
        }
    )

    envelope = snapshot.runtime_execution_envelope
    assert envelope is not None
    assert envelope.envelope_id == "execution-envelope-runtime-plan-1"
    assert envelope.intent_kind == "desktop_operation"
    assert [request.tool_name for request in envelope.requests] == [
        "desktop.list_apps",
        "app.open",
    ]
    assert envelope.runtime_stage_counts == {"discover": 1, "operate": 1}
    assert envelope.approvals_required == ["open-or-focus-app"]


def test_agent_task_snapshot_overlays_runtime_request_status_from_run_facts() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "waiting_approval",
            "metadata": {
                "yachiyo_execution_envelope": {
                    "envelope_id": "execution-envelope-runtime-plan-1",
                    "decision_id": "decision-1",
                    "plan_id": "runtime-plan-1",
                    "intent_kind": "data_analysis",
                    "requests": [
                        {
                            "request_id": "request-read",
                            "step_id": "inspect-data-source",
                            "tool_name": "workspace.read",
                            "runtime_stage": "discover",
                        },
                        {
                            "request_id": "request-write",
                            "step_id": "write-analysis-artifact",
                            "tool_name": "artifact.write",
                            "runtime_stage": "produce",
                        },
                    ],
                    "runtime_stage_counts": {"discover": 1, "produce": 1},
                }
            },
            "tool_calls": [
                {
                    "tool_call_id": "tool-call-read",
                    "tool_name": "workspace.read",
                    "step_id": "inspect-data-source",
                    "status": "completed",
                }
            ],
            "pending_approvals": [
                {
                    "approval_id": "approval-write",
                    "tool_name": "artifact.write",
                    "step_id": "write-analysis-artifact",
                    "status": "pending",
                    "title": "Write analysis artifact",
                }
            ],
        }
    )

    envelope = snapshot.runtime_execution_envelope
    assert envelope is not None
    assert [request.status for request in envelope.requests] == [
        "completed",
        "waiting_approval",
    ]
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.completed_runtime_request_count == 1
    assert snapshot.runtime_debug.waiting_runtime_request_count == 1
    assert snapshot.runtime_debug.pending_runtime_request_count == 0


def test_agent_task_snapshot_projects_failed_runtime_request_into_replan_recovery() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-runtime-replan",
            "task_id": "task-runtime-replan",
            "status": "running",
            "metadata": {
                "yachiyo_execution_envelope": {
                    "envelope_id": "execution-envelope-runtime-replan",
                    "decision_id": "decision-runtime-replan",
                    "plan_id": "runtime-plan-replan",
                    "intent_kind": "desktop_operation",
                    "task_core": {
                        "core_id": "task-core-runtime-replan",
                        "workspace": {
                            "workspace_id": "workspace-runtime-replan",
                            "title": "Runtime Replan Workspace",
                        },
                        "todos": [
                            {
                                "todo_id": "todo-inspect-ui",
                                "title": "Inspect app UI",
                                "step_id": "inspect-ui",
                                "tool_name": "desktop.ui_elements",
                            }
                        ],
                    },
                    "requests": [
                        {
                            "request_id": "request-inspect-ui",
                            "step_id": "inspect-ui",
                            "tool_name": "desktop.ui_elements",
                            "runtime_stage": "operate",
                            "runtime_role": "inspect_ui",
                            "replan_triggers": ["verification_failed"],
                            "observation_evidence": {
                                "verification_failed": True,
                                "message": "No actionable controls found.",
                            },
                            "observation_retry": {
                                "tool": "desktop.ui_elements",
                                "input": {"app_name": "PixelForge"},
                                "reason": "inspect_current_ui_again",
                            },
                        }
                    ],
                    "runtime_stage_counts": {"operate": 1},
                }
            },
            "tool_calls": [
                {
                    "tool_call_id": "tool-call-inspect-ui",
                    "tool_name": "desktop.ui_elements",
                    "step_id": "inspect-ui",
                    "status": "failed",
                }
            ],
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:01Z",
        }
    )

    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.requests[0].status == "failed"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.needs_replan is True
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.needs_replan is True
    assert snapshot.runtime_debug.failed_runtime_request_count == 1
    replan_event = next(
        event
        for event in snapshot.recent_events
        if event.event_type == "agent.replan.requested"
    )
    assert replan_event.payload["recovery_actions"][0]["tool"] == "desktop.ui_elements"
    assert replan_event.payload["recovery_actions"][0]["input"] == {
        "app_name": "PixelForge"
    }
    assert any(
        recovery.selected_tool_name == "desktop.ui_elements"
        for recovery in snapshot.replan_recoveries
    )


def test_agent_task_snapshot_projects_desktop_provider_session_recovery() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-provider-replan",
            "task_id": "task-provider-replan",
            "status": "running",
            "metadata": {
                "yachiyo_execution_envelope": {
                    "envelope_id": "execution-envelope-provider-replan",
                    "decision_id": "decision-provider-replan",
                    "plan_id": "runtime-plan-provider-replan",
                    "intent_kind": "desktop_operation",
                    "desktop_provider_session": {
                        "ok": False,
                        "status": "start_failed",
                        "needed": True,
                        "running": False,
                        "started": False,
                        "provider_id": "local-isolated-desktop",
                        "provider_manifest": "tmp/virtual-provider.manifest.json",
                        "reason": "isolated_provider_required",
                        "error": "provider launch failed",
                        "request_ids": ["request-click-export"],
                        "tool_names": ["app.focus_and_click_ui_element"],
                        "desktop_session_kind": "isolated_desktop",
                        "desktop_session_isolated": True,
                        "foreground_takeover_required": False,
                        "keyboard_mouse_capture_supported": True,
                        "requires_real_virtual_desktop_backend": True,
                        "supported_tools": [
                            "app.focus_and_click_ui_element",
                            "desktop.ui_elements",
                        ],
                    },
                    "requests": [
                        {
                            "request_id": "request-click-export",
                            "step_id": "operate-foreground-ui",
                            "tool_name": "app.focus_and_click_ui_element",
                            "capability_id": "desktop.ui_operation",
                            "input": {
                                "app_name": "Apple Music",
                                "target": "Play",
                                "role_filter": "button",
                            },
                            "runtime_stage": "operate",
                            "runtime_role": "desktop_ui_action",
                        }
                    ],
                    "runtime_stage_counts": {"operate": 1},
                }
            },
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:01Z",
        }
    )

    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.needs_replan is True
    assert snapshot.runtime_debug.desktop_provider_session_status == "start_failed"
    assert snapshot.runtime_debug.desktop_provider_session_needed is True
    assert snapshot.runtime_debug.desktop_provider_session_provider_id == (
        "local-isolated-desktop"
    )
    assert snapshot.runtime_debug.desktop_provider_session_kind == "isolated_desktop"
    assert snapshot.runtime_debug.desktop_provider_session_isolated is True
    assert (
        snapshot.runtime_debug.desktop_provider_session_foreground_takeover_required
        is False
    )
    assert (
        snapshot.runtime_debug.desktop_provider_session_keyboard_mouse_capture_supported
        is True
    )
    assert snapshot.runtime_debug.desktop_provider_session_supported_tools == [
        "app.focus_and_click_ui_element",
        "desktop.ui_elements",
    ]
    recovery = next(
        item
        for item in snapshot.replan_recoveries
        if item.selected_tool_name == "desktop.provider_session.start"
    )
    assert recovery.trigger == "isolated_provider_required"
    assert recovery.source_tool_name == "app.focus_and_click_ui_element"
    assert recovery.target_capability_id == "desktop.ui_operation"
    assert recovery.recovery_actions[0].approval_required is True
    assert recovery.recovery_actions[0].input["api_route"] == (
        "/yachiyo/studio/tools/desktop-provider/session/start"
    )
    assert recovery.recovery_actions[0].input["provider_manifest"] == (
        "tmp/virtual-provider.manifest.json"
    )
    assert recovery.recovery_actions[0].input[
        "requires_real_virtual_desktop_backend"
    ] is True
    assert recovery.recovery_actions[0].metadata["runtime_retry_source"] == (
        "desktop_provider_session"
    )
    assert recovery.deferred_tool == "app.focus_and_click_ui_element"
    assert recovery.deferred_input == {
        "app_name": "Apple Music",
        "target": "Play",
        "role_filter": "button",
    }
    assert recovery.recovery_actions[0].deferred_tool == (
        "app.focus_and_click_ui_element"
    )
    assert recovery.recovery_actions[0].deferred_input == recovery.deferred_input
    continuation = recovery.recovery_actions[0].deferred_continuation[0]
    assert continuation["tool"] == "app.focus_and_click_ui_element"
    assert continuation["input"] == recovery.deferred_input
    assert continuation["source_request_id"] == "request-click-export"
    assert "request_id" not in continuation
    assert continuation["desktop_execution_policy"]["prefer_isolated_desktop"] is True
    assert (
        continuation["desktop_execution_policy"]["avoid_user_foreground_takeover"]
        is True
    )
    assert (
        continuation["desktop_execution_policy"]["require_sandbox_for_keyboard_mouse"]
        is True
    )
    assert continuation["source"] == "desktop_provider_session_recovery"
    session_metadata = recovery.recovery_actions[0].metadata[
        "desktop_provider_session"
    ]
    assert session_metadata["desktop_session_isolated"] is True
    assert session_metadata["foreground_takeover_required"] is False
    assert session_metadata["provider_manifest"] == (
        "tmp/virtual-provider.manifest.json"
    )


def test_agent_task_snapshot_marks_recovered_runtime_request_after_recovery_success() -> None:
    replan_request_id = (
        "runtime-replan:request-inspect-ui:inspect-ui:desktop.ui_elements:"
        "verification_failed"
    )
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-runtime-recovered",
            "task_id": "task-runtime-recovered",
            "status": "completed",
            "metadata": {
                "yachiyo_execution_envelope": {
                    "envelope_id": "execution-envelope-runtime-recovered",
                    "decision_id": "decision-runtime-recovered",
                    "plan_id": "runtime-plan-recovered",
                    "intent_kind": "desktop_operation",
                    "task_core": {
                        "core_id": "task-core-runtime-recovered",
                        "workspace": {
                            "workspace_id": "workspace-runtime-recovered",
                            "title": "Runtime Recovered Workspace",
                        },
                        "todos": [
                            {
                                "todo_id": "todo-inspect-ui",
                                "title": "Inspect app UI",
                                "step_id": "inspect-ui",
                                "tool_name": "desktop.ui_elements",
                            }
                        ],
                    },
                    "requests": [
                        {
                            "request_id": "request-inspect-ui",
                            "step_id": "inspect-ui",
                            "capability_id": "desktop.ui_operation",
                            "tool_name": "desktop.ui_elements",
                            "runtime_stage": "operate",
                            "runtime_role": "inspect_ui",
                            "replan_triggers": ["verification_failed"],
                            "observation_evidence": {
                                "verification_failed": True,
                                "message": "No actionable controls found.",
                            },
                            "observation_retry": {
                                "tool": "desktop.ui_elements",
                                "input": {"app_name": "PixelForge"},
                                "reason": "inspect_current_ui_again",
                            },
                        }
                    ],
                    "runtime_stage_counts": {"operate": 1},
                }
            },
            "tool_calls": [
                {
                    "tool_call_id": "tool-call-inspect-ui",
                    "tool_name": "desktop.ui_elements",
                    "step_id": "inspect-ui",
                    "status": "failed",
                }
            ],
            "events": [
                {
                    "event_type": "agent.replan.requested",
                    "sequence": 1,
                    "run_id": "run-runtime-recovered",
                    "payload": {
                        "request_id": replan_request_id,
                        "trigger": "verification_failed",
                        "source_step_id": "inspect-ui",
                        "source_tool_name": "desktop.ui_elements",
                        "target_capability_id": "desktop.ui_operation",
                    },
                },
                {
                    "event_type": "agent.replan.recovery.updated",
                    "sequence": 2,
                    "run_id": "run-runtime-recovered",
                    "payload": {
                        "request_id": replan_request_id,
                        "replan_request_id": replan_request_id,
                        "trigger": "verification_failed",
                        "status": "completed",
                        "source_step_id": "inspect-ui",
                        "source_tool_name": "desktop.ui_elements",
                        "target_capability_id": "desktop.ui_operation",
                        "selected_tool_name": "desktop.ui_elements",
                        "tool_status": "completed",
                        "todo_status": "completed",
                        "checkpoint_status": "completed",
                    },
                },
            ],
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:02Z",
        }
    )

    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.requests[0].status == "recovered"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.needs_replan is False
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.needs_replan is False
    assert snapshot.runtime_debug.recovered_runtime_request_count == 1
    assert snapshot.runtime_debug.failed_runtime_request_count == 0
    assert snapshot.runtime_debug.pending_runtime_request_count == 0
    assert snapshot.replan_recoveries[0].status == "completed"


def test_agent_task_snapshot_exposes_failed_replan_recovery_metadata() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-recovery-failed",
            "task_id": "task-recovery-failed",
            "status": "running",
            "events": [
                {
                    "event_type": "agent.replan.requested",
                    "sequence": 1,
                    "run_id": "run-recovery-failed",
                    "payload": {
                        "request_id": "runtime-replan:failed-recovery",
                        "trigger": "verification_failed",
                        "source_step_id": "open-pixelforge",
                        "source_tool_name": "app.open",
                        "target_capability_id": "desktop.app",
                        "failure_detail": "app not found",
                        "metadata": {
                            "replan_recovery_failed": True,
                            "parent_replan_request_id": "runtime-replan:verify-pixelforge",
                            "parent_replan_trigger": "verification_failed",
                            "failed_recovery_action_id": (
                                "runtime-replan:verify-pixelforge:action:1:app.open"
                            ),
                            "failed_recovery_action_label": "Open target app",
                            "failed_recovery_tool": "app.open",
                            "failed_recovery_input": {"app_name": "PixelForge"},
                            "failed_recovery_result_preview": {
                                "ok": False,
                                "error": "app not found",
                            },
                            "original_source_step_id": "verify-pixelforge",
                            "original_source_tool_name": "desktop.verify",
                        },
                    },
                }
            ],
            "created_at": "2026-06-16T00:00:00Z",
            "updated_at": "2026-06-16T00:00:01Z",
        }
    )

    recovery = snapshot.replan_recoveries[0]
    assert recovery.metadata["replan_recovery_failed"] is True
    assert recovery.metadata["parent_replan_request_id"] == (
        "runtime-replan:verify-pixelforge"
    )
    assert recovery.metadata["failed_recovery_tool"] == "app.open"
    assert recovery.metadata["failed_recovery_input"] == {"app_name": "PixelForge"}
    assert recovery.result_preview["error"] == "app not found"
    assert recovery.failure_detail == "app not found"
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.needs_replan is True
    assert snapshot.runtime_debug.latest_recovery_failed is True
    assert snapshot.runtime_debug.latest_failed_recovery_tool == "app.open"
    assert snapshot.runtime_debug.latest_failed_recovery_action_id == (
        "runtime-replan:verify-pixelforge:action:1:app.open"
    )
    assert snapshot.runtime_debug.latest_failed_recovery_reason == "app not found"


def test_agent_task_snapshot_projects_runtime_execution_envelope_from_events() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.plan.created",
                    "payload": {
                        "runtime_execution_envelope": {
                            "envelope_id": "execution-envelope-runtime-plan-2",
                            "decision_id": "decision-2",
                            "plan_id": "runtime-plan-2",
                            "intent_kind": "data_analysis",
                            "requests": [
                                {
                                    "request_id": "request-1",
                                    "tool_name": "workspace.read",
                                    "runtime_stage": "discover",
                                }
                            ],
                            "runtime_stage_counts": {"discover": 1},
                        }
                    },
                }
            ],
        }
    )

    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.plan_id == "runtime-plan-2"
    assert snapshot.runtime_execution_envelope.requests[0].tool_name == "workspace.read"


def test_agent_task_snapshot_keeps_verify_events_but_shows_primary_desktop_tool() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "completed",
            "summary": "已打开 Microsoft Word。",
            "events": [
                {
                    "event_type": "agent.tool.call",
                    "payload": {
                        "tool": "desktop.list_apps",
                        "input_preview": {"limit": 20, "query": "Word"},
                        "result": {"ok": True},
                    },
                },
                {
                    "event_type": "agent.tool.call",
                    "payload": {
                        "tool": "app.open",
                        "input_preview": {"app_name": "Microsoft Word"},
                        "result": {"ok": True},
                    },
                },
                {
                    "event_type": "agent.tool.call",
                    "payload": {
                        "tool": "desktop.active_window",
                        "input_preview": {},
                        "result": {"ok": True},
                    },
                },
                {
                    "event_type": "agent.desktop.intent_completed",
                    "payload": {
                        "tool": "app.open",
                        "tools": ["desktop.list_apps", "app.open", "desktop.active_window"],
                        "input_preview": {"app_name": "Microsoft Word"},
                        "result": {"ok": True},
                        "steps": [
                            {
                                "tool": "desktop.list_apps",
                                "input_preview": {"limit": 20, "query": "Word"},
                                "result": {"ok": True},
                            },
                            {
                                "tool": "app.open",
                                "input_preview": {"app_name": "Microsoft Word"},
                                "result": {"ok": True},
                            },
                            {
                                "tool": "desktop.active_window",
                                "input_preview": {},
                                "result": {"ok": True},
                            },
                        ],
                        "summary": "已打开 Microsoft Word。",
                    },
                },
            ],
        }
    )

    assert [event.event_type for event in snapshot.recent_events] == [
        "agent.desktop.intent_completed"
    ]
    assert snapshot.recent_events[0].payload == {
        "tool": "app.open",
        "status": "completed",
        "summary": "已打开 Microsoft Word。",
        "input_preview": {"app_name": "Microsoft Word"},
    }
    assert all(
        event.event_type != "agent.tool.call" for event in snapshot.recent_events
    )
    assert [tool_call.tool_name for tool_call in snapshot.tool_calls] == ["app.open"]
    assert snapshot.tool_calls[0].input_preview == {"app_name": "Microsoft Word"}
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.event_count == 1
    assert snapshot.runtime_debug.tool_call_count == 1
    assert snapshot.runtime_debug.latest_tool_name == "app.open"
    assert snapshot.runtime_debug.debug_surfaces == ["timeline", "tools"]


def test_runtime_debug_summary_counts_deferred_continuation_events() -> None:
    summary = runtime_debug_summary_from_runtime_objects(
        run_id="run-1",
        events=[
            PublicRunEvent(
                run_id="run-1",
                sequence=1,
                event_type="agent.deferred_continuation.enqueued",
                payload={
                    "deferred_continuation_count": 2,
                    "deferred_tools": ["desktop.safe_type_text", "desktop.active_window"],
                },
            )
        ],
    )

    assert summary.event_count == 1
    assert summary.deferred_continuation_count == 2
    assert summary.latest_deferred_continuation_tool == "desktop.active_window"
    assert summary.latest_deferred_tool == "desktop.active_window"
    assert "deferred_continuation" in summary.debug_surfaces


def test_runtime_debug_summary_projects_metadata_blocked_direct_requests() -> None:
    summary = runtime_debug_summary_from_runtime_objects(
        runtime_execution_envelope={
            "requests": [
                {
                    "request_id": "request-open",
                    "tool_name": "app.open",
                    "status": "planned",
                }
            ]
        },
        runtime_metadata={
            "yachiyo_blocked_direct_tool_requests": [
                {
                    "request_id": "request-type",
                    "tool": "desktop.safe_type_text",
                    "status": "blocked",
                    "blocked_by": "real_virtual_desktop_provider_required",
                    "policy_reason": "Real virtual desktop provider required.",
                    "desktop_execution_route": {
                        "status": "real_virtual_desktop_provider_required",
                        "can_execute": False,
                    },
                }
            ]
        },
    )

    assert summary.blocked_runtime_request_count == 1
    assert summary.blocked_direct_request_count == 1
    assert summary.blocked_runtime_request_tools == ["desktop.safe_type_text"]
    assert summary.latest_blocked_request_tool_name == "desktop.safe_type_text"
    assert summary.latest_blocked_request_status == (
        "real_virtual_desktop_provider_required"
    )
    assert summary.latest_blocked_request_reason == (
        "Real virtual desktop provider required."
    )
    assert summary.needs_user_action is True
    assert summary.needs_replan is True
    assert "runtime_blockers" in summary.debug_surfaces


def test_agent_task_snapshot_projects_blocked_direct_requests_into_task_core_replan() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "task_id": "task-1",
            "run_id": "run-1",
            "status": "running",
            "task_core": {
                "core_id": "task-core-1",
                "workspace": {
                    "workspace_id": "workspace-1",
                    "title": "Desktop workspace",
                    "items": [
                        {
                            "item_id": "type-input",
                            "title": "type-text.input.json",
                            "kind": "scratch",
                            "source_step_id": "type-text",
                            "status": "planned",
                            "metadata": {"tool_name": "desktop.safe_type_text"},
                        }
                    ],
                },
                "todos": [
                    {
                        "todo_id": "todo-type",
                        "title": "Type search text",
                        "status": "pending",
                        "capability_id": "desktop.ui_operation",
                        "step_id": "type-text",
                        "tool_name": "desktop.safe_type_text",
                    }
                ],
                "checkpoints": [
                    {
                        "checkpoint_id": "checkpoint-type",
                        "title": "Verify typed text",
                        "status": "planned",
                        "after_step_id": "type-text",
                        "depends_on": ["type-text"],
                        "verifies": ["desktop.safe_type_text"],
                    }
                ],
                "replan_signals": [],
            },
            "metadata": {
                "yachiyo_blocked_direct_tool_requests": [
                    {
                        "request_id": "request-type",
                        "tool": "desktop.safe_type_text",
                        "step_id": "type-text",
                        "capability_id": "desktop.ui_operation",
                        "status": "blocked",
                        "blocked_by": "real_virtual_desktop_provider_required",
                        "policy_reason": "Real virtual desktop provider required.",
                        "desktop_execution_route": {
                            "status": "real_virtual_desktop_provider_required",
                            "can_execute": False,
                            "blocking_conditions": [
                                "real_virtual_desktop_provider_required"
                            ],
                        },
                    }
                ]
            },
        }
    )

    assert snapshot.task_core is not None
    assert snapshot.task_core.todos[0].status == "blocked"
    assert snapshot.task_core.todos[0].metadata["runtime_blocked"] is True
    assert snapshot.task_core.checkpoints[0].status == "blocked"
    assert snapshot.task_core.workspace.items[0].status == "blocked"
    assert snapshot.task_core.replan_signals[0].trigger == "runtime_blocked"
    assert snapshot.task_core.replan_signals[0].source_step_id == "type-text"
    assert snapshot.task_progress is not None
    assert snapshot.task_progress.status == "replan_requested"
    assert snapshot.task_progress.needs_replan is True
    assert snapshot.task_progress.blocked_step_ids == ["type-text"]
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.needs_replan is True
    assert snapshot.runtime_debug.blocked_runtime_request_count == 1


def test_run_timeline_projects_blocked_direct_requests_into_workflow_events() -> None:
    timeline = run_timeline_snapshot_from_payload(
        {
            **_blocked_direct_runtime_payload(),
            "run_id": "workflow-run-1",
            "workflow_run_id": "workflow-run-1",
            "status": "running",
        }
    )

    blocked_events = _blocked_direct_runtime_events(timeline.events)

    assert {
        "workflow.run.task.todo.updated",
        "workflow.run.task.checkpoint.updated",
        "workflow.run.replan.requested",
    }.issubset({event.event_type for event in blocked_events})
    assert timeline.task_progress is not None
    assert timeline.task_progress.needs_replan is True
    assert timeline.task_progress.blocked_step_ids == ["type-text"]
    assert timeline.runtime_debug is not None
    assert timeline.runtime_debug.needs_replan is True


def test_group_run_projects_blocked_direct_requests_into_group_events() -> None:
    group_run = group_run_snapshot_from_payload(
        {
            **_blocked_direct_runtime_payload(),
            "group_run_id": "group-run-1",
            "group_id": "group-1",
            "title": "Group desktop run",
            "objective": "Type text",
            "status": "running",
        }
    )

    blocked_events = _blocked_direct_runtime_events(group_run.events)

    assert {
        "group.run.task.todo.updated",
        "group.run.task.checkpoint.updated",
        "group.run.replan.requested",
    }.issubset({event.event_type for event in blocked_events})
    assert group_run.task_progress is not None
    assert group_run.task_progress.needs_replan is True
    assert group_run.task_progress.blocked_step_ids == ["type-text"]
    assert group_run.runtime_debug is not None
    assert group_run.runtime_debug.needs_replan is True


def _blocked_direct_runtime_payload() -> dict[str, Any]:
    return {
        "task_id": "task-1",
        "run_id": "run-1",
        "status": "running",
        "task_core": {
            "core_id": "task-core-1",
            "workspace": {
                "workspace_id": "workspace-1",
                "title": "Desktop workspace",
                "items": [
                    {
                        "item_id": "type-input",
                        "title": "type-text.input.json",
                        "kind": "scratch",
                        "source_step_id": "type-text",
                        "status": "planned",
                        "metadata": {"tool_name": "desktop.safe_type_text"},
                    }
                ],
            },
            "todos": [
                {
                    "todo_id": "todo-type",
                    "title": "Type search text",
                    "status": "pending",
                    "capability_id": "desktop.ui_operation",
                    "step_id": "type-text",
                    "tool_name": "desktop.safe_type_text",
                }
            ],
            "checkpoints": [
                {
                    "checkpoint_id": "checkpoint-type",
                    "title": "Verify typed text",
                    "status": "planned",
                    "after_step_id": "type-text",
                    "depends_on": ["type-text"],
                    "verifies": ["desktop.safe_type_text"],
                }
            ],
            "replan_signals": [],
        },
        "metadata": {
            "yachiyo_blocked_direct_tool_requests": [
                {
                    "request_id": "request-type",
                    "tool": "desktop.safe_type_text",
                    "step_id": "type-text",
                    "capability_id": "desktop.ui_operation",
                    "status": "blocked",
                    "blocked_by": "real_virtual_desktop_provider_required",
                    "policy_reason": "Real virtual desktop provider required.",
                    "desktop_execution_route": {
                        "status": "real_virtual_desktop_provider_required",
                        "can_execute": False,
                        "blocking_conditions": [
                            "real_virtual_desktop_provider_required"
                        ],
                    },
                }
            ]
        },
    }


def _blocked_direct_runtime_events(
    events: list[PublicRunEvent],
) -> list[PublicRunEvent]:
    return [
        event
        for event in events
        if event.payload.get("source") == "runtime_blocked_direct_request"
    ]


def test_runtime_debug_summary_projects_task_workspace_progress() -> None:
    task_core = TaskCoreSnapshot(
        core_id="task-core-1",
        workspace=TaskWorkspaceSnapshot(
            workspace_id="workspace-1",
            title="Analysis workspace",
            items=[
                TaskWorkspaceItemSnapshot(
                    item_id="dataset",
                    title="Dataset",
                    kind="input",
                    path="data/source.csv",
                    source_step_id="inspect-data",
                    status="completed",
                ),
                TaskWorkspaceItemSnapshot(
                    item_id="report-md",
                    title="Analysis report",
                    kind="artifact",
                    path="reports/analysis.md",
                    source_step_id="write-report",
                    status="planned",
                ),
            ],
        ),
        todos=[
            TaskTodoItemSnapshot(
                todo_id="write-report",
                title="Write report",
                status="in_progress",
                step_id="write-report",
                tool_name="artifact.write",
            )
        ],
    )
    task_progress = TaskProgressSummarySnapshot(
        core_id="task-core-1",
        workspace_id="workspace-1",
        status="running",
        current_step_id="write-report",
        current_step_title="Write report",
        current_tool_name="artifact.write",
        total_workspace_items=2,
        completed_workspace_items=1,
    )

    summary = runtime_debug_summary_from_runtime_objects(
        run_id="run-1",
        task_core=task_core,
        task_progress=task_progress,
    )

    assert summary.current_workspace_item_id == "report-md"
    assert summary.current_workspace_item_title == "Analysis report"
    assert summary.current_workspace_item_kind == "artifact"
    assert summary.current_workspace_item_path == "reports/analysis.md"
    assert summary.current_workspace_item_status == "planned"
    assert summary.total_workspace_items == 2
    assert summary.completed_workspace_items == 1
    assert summary.blocked_workspace_items == 0
    assert "task" in summary.debug_surfaces


def test_runtime_debug_summary_projects_provider_session_from_replay_events() -> None:
    summary = runtime_debug_summary_from_runtime_objects(
        run_id="run-1",
        events=[
            PublicRunEvent(
                run_id="run-1",
                sequence=1,
                event_type="desktop.provider_session.required",
                payload={
                    "desktop_provider_session": {
                        "ok": True,
                        "status": "required",
                        "needed": True,
                        "running": False,
                        "provider_id": "local-isolated-desktop",
                        "reason": "sandbox_desktop_provider_required",
                        "tool_names": ["app.open"],
                        "desktop_session_kind": "isolated_desktop",
                        "desktop_session_isolated": True,
                        "foreground_takeover_required": False,
                        "desktop_backend_kind": "loopback_session_harness",
                        "desktop_backend_is_loopback": True,
                        "desktop_backend_ready_for_public_release": False,
                        "requires_real_virtual_desktop_backend": True,
                        "provider_contract": {
                            "ok": False,
                            "contract_version": "oha-yachiyo.desktop-provider.v1",
                            "blocking_conditions": [
                                "loopback_desktop_backend",
                                "desktop_backend_not_release_ready",
                            ],
                        },
                        "provider_conformance": {
                            "ok": False,
                            "mode": "dev_loopback_provider_conformance",
                            "smoke_ok": True,
                            "public_release_ready": False,
                            "release_candidate": False,
                            "release_blocking_conditions": [
                                "loopback_desktop_backend",
                                "real_virtual_desktop_backend_required",
                            ],
                            "missing_required_tools": ["desktop.verify"],
                            "failed_tools": ["desktop.click_ui_element"],
                        },
                    }
                },
            )
        ],
    )

    assert summary.desktop_provider_session_status == "required"
    assert summary.desktop_provider_session_needed is True
    assert summary.desktop_provider_session_running is False
    assert summary.desktop_provider_session_provider_id == "local-isolated-desktop"
    assert summary.desktop_provider_session_reason == "sandbox_desktop_provider_required"
    assert summary.desktop_provider_session_tool_names == ["app.open"]
    assert summary.desktop_provider_session_kind == "isolated_desktop"
    assert summary.desktop_provider_session_isolated is True
    assert summary.desktop_provider_session_foreground_takeover_required is False
    assert summary.desktop_provider_backend_kind == "loopback_session_harness"
    assert summary.desktop_provider_backend_is_loopback is True
    assert summary.desktop_provider_backend_ready_for_public_release is False
    assert summary.desktop_provider_requires_real_virtual_backend is True
    assert summary.desktop_provider_contract_ok is False
    assert (
        summary.desktop_provider_contract_version
        == "oha-yachiyo.desktop-provider.v1"
    )
    assert summary.desktop_provider_contract_blocking_conditions == [
        "loopback_desktop_backend",
        "desktop_backend_not_release_ready",
    ]
    assert summary.desktop_provider_conformance_ok is False
    assert summary.desktop_provider_conformance_mode == "dev_loopback_provider_conformance"
    assert summary.desktop_provider_conformance_smoke_ok is True
    assert summary.desktop_provider_conformance_public_release_ready is False
    assert summary.desktop_provider_conformance_release_candidate is False
    assert summary.desktop_provider_conformance_release_blocking_conditions == [
        "loopback_desktop_backend",
        "real_virtual_desktop_backend_required",
    ]
    assert summary.desktop_provider_conformance_missing_required_tools == [
        "desktop.verify"
    ]
    assert summary.desktop_provider_conformance_failed_tools == [
        "desktop.click_ui_element"
    ]
    assert summary.needs_user_action is True
    assert summary.needs_replan is True
    assert "desktop_provider" in summary.debug_surfaces


def test_runtime_debug_summary_projects_provider_manifest_from_replay_events() -> None:
    summary = runtime_debug_summary_from_runtime_objects(
        run_id="run-provider-manifest",
        events=[
            PublicRunEvent(
                run_id="run-provider-manifest",
                sequence=1,
                event_type="desktop.provider_session.failed",
                payload={
                    "desktop_provider_session": {
                        "status": "start_failed",
                        "needed": True,
                        "provider_id": "local-isolated-desktop",
                        "provider_manifest_evidence": {
                            "ok": False,
                            "remote_endpoint_allowed": False,
                            "remote_endpoint_urls": [
                                "https://provider.example.com/tools/execute"
                            ],
                            "blocking_conditions": [
                                "desktop_provider_manifest_remote_endpoint_not_allowed"
                            ],
                        },
                        "provider_conformance": {
                            "ok": False,
                            "public_release_ready": False,
                            "release_blocking_conditions": [
                                "desktop_provider_manifest_remote_endpoint_not_allowed"
                            ],
                        },
                    }
                },
            )
        ],
    )

    assert summary.desktop_provider_manifest_ok is False
    assert summary.desktop_provider_manifest_remote_endpoint_allowed is False
    assert summary.desktop_provider_manifest_remote_endpoint_urls == [
        "https://provider.example.com/tools/execute"
    ]
    assert summary.desktop_provider_manifest_blocking_conditions == [
        "desktop_provider_manifest_remote_endpoint_not_allowed"
    ]
    assert summary.desktop_provider_conformance_public_release_ready is False
    assert "desktop_provider" in summary.debug_surfaces


def test_agent_task_snapshot_uses_first_planned_desktop_step_for_progress() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.desktop.intent_planned",
                    "payload": {
                        "tool": "desktop.list_apps",
                        "input_preview": {"limit": 20, "query": "Apple Music"},
                    },
                },
                {
                    "event_type": "agent.desktop.intent_planned",
                    "payload": {
                        "tool": "app.open",
                        "input_preview": {"app_name": "Music"},
                    },
                },
                {
                    "event_type": "agent.desktop.intent_planned",
                    "payload": {"tool": "desktop.active_window", "input_preview": {}},
                },
            ],
        }
    )

    assert snapshot.current_step == "准备执行 · 发现已安装应用"


def test_agent_task_snapshot_clears_recovered_foreground_readiness_action() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.tool.call",
                    "sequence": 1,
                    "payload": {
                        "tool": "desktop.inspect_app",
                        "input_preview": {"app_name": "PixelForge"},
                        "result": {
                            "ok": False,
                            "error": "app_not_found",
                            "recovery_actions": [
                                {
                                    "label": "重新发现应用",
                                    "tool": "desktop.list_apps",
                                    "input": {"query": "PixelForge", "limit": 20},
                                }
                            ],
                            "data": {
                                "app_name": "PixelForge",
                                "ready_for_foreground_action": False,
                            },
                        },
                    },
                },
                {
                    "event_type": "agent.desktop.readiness_recovered",
                    "sequence": 2,
                    "payload": {
                        "tool": "desktop.list_apps",
                        "recovery_tool": "desktop.list_apps",
                        "status": "recovered",
                        "app_name": "PixelForge",
                        "blocking_conditions": ["app_not_found"],
                    },
                },
            ],
        }
    )

    assert snapshot.needs_user_action is False
    assert snapshot.current_step == "桌面就绪已恢复 · 发现已安装应用"
    assert [event.event_type for event in snapshot.recent_events] == [
        "agent.desktop.readiness_recovered",
    ]
    assert snapshot.recent_events[0].payload == {
        "tool": "desktop.list_apps",
        "status": "recovered",
    }
    assert all(
        event.event_type != "agent.tool.call" for event in snapshot.recent_events
    )
    assert [tool_call.tool_name for tool_call in snapshot.tool_calls] == [
        "desktop.inspect_app"
    ]
    assert snapshot.tool_calls[0].status == "failed"


def test_agent_task_snapshot_handles_scoped_desktop_recovery_events() -> None:
    recovered_snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.tool.call",
                    "sequence": 1,
                    "payload": {
                        "tool": "desktop.inspect_app",
                        "input_preview": {"app_name": "PixelForge"},
                        "result": {
                            "ok": False,
                            "error": "app_not_found",
                            "recovery_actions": [
                                {
                                    "label": "重新发现应用",
                                    "tool": "desktop.list_apps",
                                    "input": {"query": "PixelForge", "limit": 20},
                                }
                            ],
                            "data": {
                                "app_name": "PixelForge",
                                "ready_for_foreground_action": False,
                            },
                        },
                    },
                },
                {
                    "event_type": "workflow.run.desktop.readiness_recovered",
                    "sequence": 2,
                    "payload": {
                        "tool": "desktop.list_apps",
                        "recovery_tool": "desktop.list_apps",
                        "status": "recovered",
                        "app_name": "PixelForge",
                        "blocking_conditions": ["app_not_found"],
                    },
                },
            ],
        }
    )
    pending_snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-2",
            "status": "running",
            "timeline": [
                {
                    "event_type": "group.run.desktop.permission_recovery",
                    "sequence": 1,
                    "payload": {
                        "tool": "desktop.safe_type_text",
                        "permission_targets": ["accessibility"],
                        "recovery_actions": [
                            {
                                "label": "打开辅助功能权限",
                                "tool": "system.settings_open",
                                "input": {"target": "accessibility"},
                            }
                        ],
                    },
                }
            ],
        }
    )

    assert recovered_snapshot.needs_user_action is False
    assert recovered_snapshot.current_step == "桌面就绪已恢复 · 发现已安装应用"
    assert pending_snapshot.needs_user_action is True


def test_agent_task_snapshot_keeps_permission_action_after_readiness_recovery() -> None:
    snapshot = agent_task_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "timeline": [
                {
                    "event_type": "agent.tool.call",
                    "sequence": 1,
                    "payload": {
                        "tool": "desktop.safe_type_text",
                        "input_preview": {"text": "hello"},
                        "result": {
                            "ok": False,
                            "permission_error": True,
                            "permission_targets": ["accessibility"],
                            "recovery_actions": [
                                {
                                    "label": "打开辅助功能权限",
                                    "tool": "system.settings_open",
                                    "input": {"target": "accessibility"},
                                }
                            ],
                        },
                    },
                },
                {
                    "event_type": "agent.desktop.readiness_recovered",
                    "sequence": 2,
                    "payload": {
                        "tool": "desktop.list_apps",
                        "recovery_tool": "desktop.list_apps",
                        "status": "recovered",
                        "app_name": "Notes",
                        "blocking_conditions": ["foreground_not_ready"],
                    },
                },
            ],
        }
    )

    assert snapshot.needs_user_action is True
    assert snapshot.current_step == "桌面就绪已恢复 · 发现已安装应用"


def test_agent_task_light_snapshot_json_shape_is_stable() -> None:
    pending = ApprovalCardSnapshot(
        approval_id="approval-1",
        run_id="run-1",
        title="Approve workspace.write_patch",
        tool_name="workspace.write_patch",
        input_preview={"path": "README.md"},
    )
    snapshot = AgentTaskLightSnapshot(
        task_id="task-1",
        conversation_id="chat-1",
        title="Review README",
        status="waiting_approval",
        detail="Prepare patch",
        needs_user_action=True,
        pending_approval=pending,
        task_progress=TaskProgressSummarySnapshot(
            core_id="core-1",
            workspace_id="workspace-1",
            status="waiting_approval",
            total_todos=2,
            completed_todos=1,
            needs_user_action=True,
            progress_text="1/2 todos completed",
        ),
        runtime_debug=RuntimeDebugSummarySnapshot(
            run_id="run-1",
            task_id="task-1",
            intent_kind="desktop_operation",
            runtime_stage="operate",
            runtime_role="desktop_ui_action",
            latest_tool_name="desktop.click_ui_element",
            debug_surfaces=["runtime"],
        ),
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="envelope-1",
            decision_id="decision-1",
            plan_id="plan-1",
            intent_kind="desktop_operation",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="request-1",
                    tool_name="desktop.click_ui_element",
                    risk_level="medium",
                    policy_reason="Foreground UI click needs approval.",
                )
            ],
            runtime_stage_counts={"operate": 1},
        ),
        open_in_studio_url="#/agents?run_id=run-1",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "task_id",
        "conversation_id",
        "title",
        "status",
        "detail",
        "needs_user_action",
        "pending_approval",
        "task_progress",
        "runtime_debug",
        "runtime_execution_envelope",
        "open_in_studio_url",
        "created_at",
        "updated_at",
    ]
    assert payload["pending_approval"]["approval_id"] == "approval-1"
    assert payload["task_progress"]["workspace_id"] == "workspace-1"
    assert payload["task_progress"]["progress_text"] == "1/2 todos completed"
    assert payload["runtime_debug"]["runtime_stage"] == "operate"
    assert payload["runtime_execution_envelope"]["requests"][0]["risk_level"] == "medium"
    assert payload["open_in_studio_url"] == "#/agents?run_id=run-1"


def test_agent_task_light_snapshot_projects_full_task_for_launcher_surfaces() -> None:
    approved = ApprovalCardSnapshot(
        approval_id="approval-approved",
        run_id="run-1",
        title="Approved read",
        tool_name="workspace.read",
        status="approved",
    )
    pending = ApprovalCardSnapshot(
        approval_id="approval-pending",
        run_id="run-1",
        title="Approve write",
        tool_name="workspace.write_patch",
    )
    task = AgentTaskSnapshot(
        task_id="task-1",
        conversation_id="chat-1",
        title="Review README",
        status="waiting_approval",
        summary="Waiting",
        current_step="Prepare patch",
        progress_text="1 approval pending",
        needs_user_action=False,
        pending_approvals=[approved, pending],
        task_progress=TaskProgressSummarySnapshot(
            core_id="core-1",
            workspace_id="workspace-1",
            status="waiting_approval",
            total_todos=2,
            completed_todos=1,
            total_checkpoints=1,
            latest_replan_request_id="replan-1",
            needs_replan=True,
            needs_user_action=True,
            progress_text="1/2 todos completed",
        ),
        runtime_debug=RuntimeDebugSummarySnapshot(
            run_id="run-1",
            task_id="task-1",
            intent_kind="desktop_operation",
            runtime_stage="operate",
            runtime_role="desktop_ui_action",
            latest_tool_name="desktop.click_ui_element",
        ),
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="envelope-1",
            decision_id="decision-1",
            plan_id="plan-1",
            intent_kind="desktop_operation",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="request-1",
                    tool_name="desktop.click_ui_element",
                    risk_level="medium",
                    policy_reason="Foreground UI click needs approval.",
                )
            ],
            runtime_stage_counts={"operate": 1},
        ),
        open_in_studio_url="#/agents?run_id=run-1",
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
    )

    light = agent_task_light_snapshot_from_task(task)

    assert light.task_id == "task-1"
    assert light.detail == "Prepare patch"
    assert light.needs_user_action is True
    assert light.pending_approval is not None
    assert light.pending_approval.approval_id == "approval-pending"
    assert light.task_progress is not None
    assert light.task_progress.workspace_id == "workspace-1"
    assert light.task_progress.needs_replan is True
    assert light.runtime_debug is not None
    assert light.runtime_debug.runtime_stage == "operate"
    assert light.runtime_execution_envelope is not None
    assert light.runtime_execution_envelope.requests[0].risk_level == "medium"
    assert light.open_in_studio_url == "#/agents?run_id=run-1"


def test_approval_card_snapshot_keeps_runtime_trace_fields() -> None:
    snapshot = ApprovalCardSnapshot(
        approval_id="approval-1",
        run_id="run-1",
        source_run_id="child-run-1",
        source_runnable_id="agent-1",
        source_runnable_name="Planner",
        workflow_id="workflow-1",
        workflow_run_id="workflow-run-1",
        workflow_node_id="review",
        workflow_node_label="Review Gate",
        group_id="group-1",
        group_run_id="group-run-1",
        core_id="core-1",
        workspace_id="workspace-1",
        task_id="task-1",
        step_id="review-step",
        capability_id="workflow.approval",
        decision_id="decision-1",
        plan_id="runtime-plan-1",
        tool_plan_id="tool-plan-1",
        intent_kind="workflow_orchestration",
        replan_request_id="replan-1",
        replan_trigger="approval_retry",
        replan_triggers=["approval_retry"],
        replan_signal_ids=["signal-1"],
        runtime_doctrine="discover_operate_verify",
        runtime_stage="approve",
        runtime_role="manual_checkpoint",
        requires_observation=True,
        requires_post_action_verification=True,
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="approval-envelope-1",
            decision_id="decision-1",
            plan_id="runtime-plan-1",
            intent_kind="workflow_orchestration",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="approval-request-1",
                    tool_name="desktop.click_ui_element",
                    risk_level="medium",
                )
            ],
        ),
        runtime_execution_metadata={"yachiyo_runtime_planner": True},
        deferred_tool="desktop.click_ui_element",
        deferred_input={"target": "Review"},
        deferred_context={"step_id": "review-step"},
        deferred_continuation=[{"tool": "screen.capture", "step_id": "verify"}],
        action_target={"action": "click", "label": "Review"},
        observation_evidence={"source_tool": "desktop.ui_elements", "strategy": "button"},
        observation_retry={"from_tool": "desktop.ui_elements", "reason": "target_not_found"},
        verification_targets=[{"step_id": "verify-review", "todo_id": "todo-review"}],
        title="Approve Review Gate",
        description="Needs review",
        status="pending",
        tool_name="workflow.approval",
        risk_level="medium",
        input_preview={"checkpoint": "Review Gate"},
        policy_reason="manual checkpoint",
        requested_at="2026-06-14T00:00:00Z",
        resolved_at=None,
        open_in_studio_url="#/agents?run_id=run-1&group_run=group-run-1",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "approval_id",
        "run_id",
        "source_run_id",
        "source_runnable_id",
        "source_runnable_name",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "group_id",
        "group_run_id",
        "core_id",
        "workspace_id",
        "task_id",
        "source",
        "planning_reason",
        "step_id",
        "planner_step_id",
        "capability_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "replan_request_id",
        "replan_trigger",
        "replan_triggers",
        "replan_signal_ids",
        "runtime_doctrine",
        "runtime_stage",
        "runtime_role",
        "requires_observation",
        "requires_post_action_verification",
        "runtime_execution_envelope",
        "runtime_execution_metadata",
        "deferred_tool",
        "deferred_input",
        "deferred_context",
        "deferred_continuation",
        "action_target",
        "observation_evidence",
        "observation_retry",
        "task_workspace_items",
        "verification_targets",
        "task_verification_targets",
        "title",
        "description",
        "status",
        "tool_name",
        "risk_level",
        "input_preview",
        "policy_reason",
        "requested_at",
        "resolved_at",
        "open_in_studio_url",
    ]
    assert payload["workflow_node_id"] == "review"
    assert payload["source_runnable_name"] == "Planner"
    assert payload["group_run_id"] == "group-run-1"
    assert payload["core_id"] == "core-1"
    assert payload["workspace_id"] == "workspace-1"
    assert payload["task_id"] == "task-1"
    assert payload["step_id"] == "review-step"
    assert payload["capability_id"] == "workflow.approval"
    assert payload["plan_id"] == "runtime-plan-1"
    assert payload["replan_triggers"] == ["approval_retry"]
    assert payload["runtime_stage"] == "approve"
    assert payload["requires_post_action_verification"] is True
    assert payload["runtime_execution_envelope"]["envelope_id"] == "approval-envelope-1"
    assert payload["runtime_execution_envelope"]["requests"][0]["tool_name"] == (
        "desktop.click_ui_element"
    )
    assert payload["runtime_execution_metadata"] == {"yachiyo_runtime_planner": True}
    assert payload["verification_targets"] == [
        {"step_id": "verify-review", "todo_id": "todo-review"}
    ]
    assert payload["action_target"] == {"action": "click", "label": "Review"}
    assert payload["observation_evidence"] == {
        "source_tool": "desktop.ui_elements",
        "strategy": "button",
    }
    assert payload["observation_retry"] == {
        "from_tool": "desktop.ui_elements",
        "reason": "target_not_found",
    }
    assert payload["deferred_tool"] == "desktop.click_ui_element"
    assert payload["deferred_context"] == {"step_id": "review-step"}


def test_public_pending_approval_projects_runtime_planner_trace_fields() -> None:
    snapshot = public_pending_approval(
        {
            "approval_id": "approval-1",
            "tool": "desktop.click_ui_element",
            "input": {"label": "Save"},
            "tool_request": {
                "step_id": "save-discovered-app-creative-result",
                "capability_id": "desktop.ui_operation",
                "decision_id": "decision-1",
                "plan_id": "runtime-plan-1",
                "tool_plan_id": "tool-plan-1",
                "intent_kind": "desktop_operation",
                "core_id": "core-1",
                "workspace_id": "workspace-1",
                "task_id": "task-1",
                "group_id": "group-1",
                "group_run_id": "group-run-1",
                "workflow_id": "workflow-1",
                "workflow_run_id": "workflow-run-1",
                "workflow_node_id": "review",
                "workflow_node_label": "Review Save",
                "runtime_stage": "operate",
                "runtime_role": "click_ui",
                "requires_post_action_verification": True,
                "runtime_execution_envelope": {
                    "envelope_id": "approval-envelope-1",
                    "decision_id": "decision-1",
                    "plan_id": "runtime-plan-1",
                    "intent_kind": "desktop_operation",
                    "requests": [
                        {
                            "request_id": "approval-request-1",
                            "tool_name": "desktop.click_ui_element",
                            "risk_level": "medium",
                        }
                    ],
                },
                "runtime_execution_metadata": {"yachiyo_runtime_planner": True},
                "replan_triggers": ["ui_not_found"],
                "replan_request_id": "replan-1",
                "replan_trigger": "ui_not_found",
                "action_target": {"action": "click", "label": "Save"},
                "observation_evidence": {
                    "source_tool": "desktop.ui_elements",
                    "strategy": "button",
                },
                "observation_retry": {
                    "from_tool": "desktop.ui_elements",
                    "reason": "target_not_found",
                },
                "task_workspace_items": [
                    {"item_id": "workspace-save", "title": "Saved draft", "path": "draft.md"}
                ],
                "verification_targets": [
                    {"step_id": "verify-save", "todo_id": "todo-save"}
                ],
                "task_verification_targets": [
                    {
                        "todo_id": "todo-save",
                        "todo_title": "Verify save",
                        "workspace_items": [
                            {"item_id": "workspace-save", "path": "draft.md"}
                        ],
                    }
                ],
            },
        }
    )

    assert snapshot["approval_id"] == "approval-1"
    assert snapshot["tool"] == "desktop.click_ui_element"
    # ``label`` is planner vocabulary, not a broker-declared executable
    # argument for this tool.  Public approval cards fail closed and omit it.
    assert snapshot["input_preview"] == {}
    assert snapshot["risk_level"] == "medium"
    assert snapshot["core_id"] == "core-1"
    assert snapshot["workspace_id"] == "workspace-1"
    assert snapshot["task_id"] == "task-1"
    assert snapshot["group_id"] == "group-1"
    assert snapshot["group_run_id"] == "group-run-1"
    assert snapshot["workflow_id"] == "workflow-1"
    assert snapshot["workflow_run_id"] == "workflow-run-1"
    assert snapshot["workflow_node_id"] == "review"
    assert snapshot["workflow_node_label"] == "Review Save"
    assert set(snapshot).isdisjoint(
        {
            "step_id",
            "capability_id",
            "decision_id",
            "plan_id",
            "tool_plan_id",
            "intent_kind",
            "runtime_stage",
            "runtime_role",
            "requires_post_action_verification",
            "runtime_execution_envelope",
            "runtime_execution_metadata",
            "replan_triggers",
            "replan_request_id",
            "replan_trigger",
            "action_target",
            "observation_evidence",
            "observation_retry",
            "task_workspace_items",
            "verification_targets",
            "task_verification_targets",
        }
    )


def test_approval_card_from_payload_maps_runtime_planner_trace_fields() -> None:
    snapshot = approval_card_from_payload(
        {
            "approval_id": "approval-1",
            "run_id": "run-1",
            "tool": "desktop.click_ui_element",
            "input_preview": {
                "label": "Save",
                "planner_step_id": "save-discovered-app-creative-result",
                "target_capability_id": "desktop.ui_operation",
                "runtime_plan_id": "runtime-plan-1",
                "runtime_doctrine": "discover_operate_verify",
                "runtime_stage": "operate",
                "runtime_role": "click_ui",
                "requires_observation": True,
                "requires_post_action_verification": True,
                "replan_triggers": ["ui_not_found"],
                "deferred_input": {"label": "Save"},
                "deferred_context": {"step_id": "save-discovered-app-creative-result"},
                "deferred_continuation": [
                    {"tool": "screen.capture", "step_id": "verify"}
                ],
                "action_target": {"action": "click", "label": "Save"},
                "observation_evidence": {
                    "source_tool": "desktop.ui_elements",
                    "strategy": "button",
                },
                "observation_retry": {
                    "from_tool": "desktop.ui_elements",
                    "reason": "target_not_found",
                },
                "task_workspace_items": [
                    {"item_id": "workspace-save", "title": "Saved draft", "path": "draft.md"}
                ],
                "verification_targets": [
                    {"step_id": "verify-save", "todo_id": "todo-save"}
                ],
                "task_verification_targets": [
                    {
                        "todo_id": "todo-save",
                        "todo_title": "Verify save",
                        "workspace_items": [
                            {"item_id": "workspace-save", "path": "draft.md"}
                        ],
                    }
                ],
            },
            "decision_id": "decision-1",
            "tool_plan_id": "tool-plan-1",
            "intent_kind": "desktop_operation",
            "runtime_execution_envelope": {
                "envelope_id": "approval-envelope-1",
                "decision_id": "decision-1",
                "plan_id": "runtime-plan-1",
                "intent_kind": "desktop_operation",
                "requests": [
                    {
                        "request_id": "approval-request-1",
                        "tool_name": "desktop.click_ui_element",
                        "risk_level": "medium",
                    }
                ],
            },
            "runtime_execution_metadata": {"yachiyo_runtime_planner": True},
            "replan_signal_ids": ["signal-1"],
            "deferred_tool": "desktop.click_ui_element",
        }
    )

    assert snapshot.step_id == "save-discovered-app-creative-result"
    assert snapshot.capability_id == "desktop.ui_operation"
    assert snapshot.decision_id == "decision-1"
    assert snapshot.plan_id == "runtime-plan-1"
    assert snapshot.tool_plan_id == "tool-plan-1"
    assert snapshot.intent_kind == "desktop_operation"
    assert snapshot.replan_triggers == ["ui_not_found"]
    assert snapshot.replan_signal_ids == ["signal-1"]
    assert snapshot.runtime_doctrine == "discover_operate_verify"
    assert snapshot.runtime_stage == "operate"
    assert snapshot.runtime_role == "click_ui"
    assert snapshot.requires_observation is True
    assert snapshot.requires_post_action_verification is True
    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.envelope_id == "approval-envelope-1"
    assert snapshot.runtime_execution_envelope.requests[0].request_id == "approval-request-1"
    assert snapshot.runtime_execution_metadata == {"yachiyo_runtime_planner": True}
    assert snapshot.deferred_tool == "desktop.click_ui_element"
    assert snapshot.deferred_input == {"label": "Save"}
    assert snapshot.deferred_context == {"step_id": "save-discovered-app-creative-result"}
    assert snapshot.deferred_continuation == [
        {"tool": "screen.capture", "step_id": "verify"}
    ]
    assert snapshot.action_target == {"action": "click", "label": "Save"}
    assert snapshot.observation_evidence == {
        "source_tool": "desktop.ui_elements",
        "strategy": "button",
    }
    assert snapshot.observation_retry == {
        "from_tool": "desktop.ui_elements",
        "reason": "target_not_found",
    }
    assert snapshot.task_workspace_items == [
        {"item_id": "workspace-save", "title": "Saved draft", "path": "draft.md"}
    ]
    assert snapshot.verification_targets == [
        {"step_id": "verify-save", "todo_id": "todo-save"}
    ]
    assert snapshot.task_verification_targets == [
        {
            "todo_id": "todo-save",
            "todo_title": "Verify save",
            "workspace_items": [
                {"item_id": "workspace-save", "path": "draft.md"}
            ],
        }
    ]


def test_product_policy_helpers_use_public_snapshots() -> None:
    pending = ApprovalCardSnapshot(
        approval_id="approval-pending",
        run_id="run-1",
        title="Approve write",
        tool_name="workspace.write_patch",
    )
    approved = ApprovalCardSnapshot(
        approval_id="approval-approved",
        run_id="run-1",
        title="Approved read",
        tool_name="workspace.read",
        status="approved",
    )
    task = AgentTaskSnapshot(
        task_id="task-1",
        title="Review README",
        status="running",
        pending_approvals=[approved, pending],
    )

    assert approval_is_pending(pending) is True
    assert approval_is_pending(approved) is False
    assert task_requires_user_action(task) is True

    cleared = task.model_copy(update={"pending_approvals": [approved]})
    assert task_requires_user_action(cleared) is False


def test_desktop_execution_capability_snapshot_json_shape_is_stable() -> None:
    snapshot = DesktopExecutionCapabilitySnapshot(
        available=True,
        platform="macos",
        missing_permissions=["accessibility"],
        blocking_conditions=["desktop_session_locked"],
        tools=["desktop.type_text"],
        risk_default="medium",
        diagnostic_route="/ui/native-agent/diagnostics/cache",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "available",
        "platform",
        "missing_permissions",
        "blocking_conditions",
        "tools",
        "available_tools",
        "degraded_tools",
        "unavailable_tools",
        "provider_supported_tools",
        "provider_ready_tools",
        "provider_blocked_tools",
        "risk_default",
        "diagnostic_route",
    ]
    assert payload["available"] is True
    assert payload["blocking_conditions"] == ["desktop_session_locked"]
    assert payload["provider_supported_tools"] == []
    assert payload["risk_default"] == "medium"
    with pytest.raises(ValidationError):
        DesktopExecutionCapabilitySnapshot(
            available=True,
            platform="macos",
            unknown=True,
        )


def test_desktop_execution_mode_snapshot_classifies_live_foreground_tools() -> None:
    snapshot = DesktopExecutionModeSnapshot(
        mode="supervised_live",
        isolation="none",
        foreground_control=True,
        keyboard_mouse_capture=True,
        sandbox_recommended=True,
        approval_recommended=True,
        reason="Sends input to the real foreground desktop.",
        mitigations=["Prefer sandbox execution."],
    )

    payload = _json(snapshot)
    safe_type = desktop_tool_execution_mode("desktop.safe_type_text").model_dump(mode="json")
    read_ui = desktop_tool_execution_mode("desktop.ui_elements").model_dump(mode="json")

    assert list(payload) == [
        "mode",
        "isolation",
        "foreground_control",
        "keyboard_mouse_capture",
        "sandbox_recommended",
        "user_handoff_recommended",
        "approval_recommended",
        "reason",
        "mitigations",
    ]
    assert payload["mode"] == "supervised_live"
    assert safe_type["mode"] == "supervised_live"
    assert safe_type["keyboard_mouse_capture"] is True
    assert safe_type["sandbox_recommended"] is True
    assert read_ui["mode"] == "read_only_observation"
    assert read_ui["keyboard_mouse_capture"] is False
    music_app = desktop_tool_execution_mode("media.music_app_open_and_play")
    assert music_app.mode == "supervised_live"
    assert music_app.foreground_control is True
    assert music_app.keyboard_mouse_capture is False
    assert music_app.sandbox_recommended is False
    assert desktop_tool_execution_mode("media.apple_music_play").mode == "tool_native"
    assert (
        desktop_tool_execution_mode("media.apple_music_status").mode
        == "read_only_observation"
    )
    with pytest.raises(ValidationError):
        DesktopExecutionModeSnapshot(mode="tool_native", unknown=True)


def test_desktop_execution_policy_snapshot_json_shape_is_stable() -> None:
    snapshot = DesktopExecutionPolicySnapshot(
        mode="preview",
        allow_live_foreground=False,
        source="chat",
        reason="Avoid stealing keyboard focus in daily chat.",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "mode",
        "allow_live_foreground",
        "prefer_background_desktop",
        "prefer_isolated_desktop",
        "avoid_user_foreground_takeover",
        "require_sandbox_for_keyboard_mouse",
        "allow_media_control",
        "source",
        "reason",
    ]
    assert payload["mode"] == "preview"
    assert payload["allow_live_foreground"] is False
    assert payload["prefer_isolated_desktop"] is False
    assert payload["avoid_user_foreground_takeover"] is False
    assert payload["require_sandbox_for_keyboard_mouse"] is False
    assert payload["allow_media_control"] is True
    with pytest.raises(ValidationError):
        DesktopExecutionPolicySnapshot(mode="allow", unknown=True)


def test_sandbox_desktop_provider_snapshot_json_shape_is_stable() -> None:
    snapshot = SandboxDesktopProviderSnapshot(
        available=False,
        provider_id="",
        provider_kind="sandbox_desktop",
        status="provider_required",
        adapter_ready=False,
        reason="No sandbox provider is configured.",
        blocking_conditions=["sandbox_desktop_provider_required"],
        supported_tools=["desktop.safe_type_text"],
        recommended_for=["keyboard_mouse_capture"],
        diagnostic_route="/yachiyo/studio/tools",
        source="runtime",
        health=DesktopProviderHealthSnapshot(
            ok=False,
            checked=False,
            status="not_configured",
            blocking_conditions=["sandbox_desktop_provider_required"],
        ),
        launch_hint={
            "command": ["python", "scripts/run_headless_desktop_provider.py"],
            "foreground_mutation_supported": False,
        },
    )

    payload = _json(snapshot)
    default_status = sandbox_desktop_provider_status()
    explicit_status = sandbox_desktop_provider_status(
        {
            "sandbox_provider": {
                "available": True,
                "provider_id": "sandbox-1",
                "provider_kind": "sandbox_desktop",
                "adapter_ready": True,
                "supported_tools": ["desktop.safe_type_text"],
            }
        }
    )

    assert list(payload) == [
        "available",
        "provider_id",
        "provider_kind",
        "status",
        "adapter_ready",
        "authentication_configured",
        "reason",
        "blocking_conditions",
        "supported_tools",
        "recommended_for",
        "diagnostic_route",
        "source",
        "health",
        "launch_hint",
        "foreground_mutation_supported",
        "keyboard_mouse_capture_supported",
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "provider_contract",
        "provider_manifest_evidence",
        "provider_conformance",
        "requires_real_sandbox_for",
    ]
    assert payload["available"] is False
    assert payload["adapter_ready"] is False
    assert payload["authentication_configured"] is False
    assert payload["keyboard_mouse_capture_supported"] is None
    assert payload["desktop_session_kind"] == ""
    assert payload["desktop_session_isolated"] is None
    assert payload["foreground_takeover_required"] is None
    assert payload["desktop_backend_kind"] == ""
    assert payload["desktop_backend_is_loopback"] is None
    assert payload["desktop_backend_ready_for_public_release"] is None
    assert payload["requires_real_virtual_desktop_backend"] is None
    assert payload["provider_contract"] == {}
    assert payload["provider_manifest_evidence"] == {}
    assert payload["provider_conformance"] is None
    assert payload["requires_real_sandbox_for"] == []
    assert payload["blocking_conditions"] == ["sandbox_desktop_provider_required"]
    assert payload["health"]["status"] == "not_configured"
    assert payload["health"]["blocking_conditions"] == [
        "sandbox_desktop_provider_required"
    ]
    assert default_status["status"] == "provider_required"
    assert default_status["blocking_conditions"] == ["sandbox_desktop_provider_required"]
    assert default_status["health"]["status"] == "not_configured"
    assert default_status["launch_hint"]["provider_id"] == "local-headless-desktop"
    assert explicit_status["provider_contract"]["ok"] is False
    assert "desktop_provider_missing_required_tools" in explicit_status[
        "provider_contract"
    ]["blocking_conditions"]
    assert default_status["launch_hint"]["env"]["OHA_YACHIYO_DESKTOP_PROVIDER_URL"] == (
        "http://127.0.0.1:19091"
    )
    assert default_status["launch_hint"]["foreground_mutation_supported"] is False
    assert default_status["launch_hint"]["desktop_session_kind"] == "headless_read_only"
    assert default_status["launch_hint"]["desktop_session_isolated"] is True
    assert default_status["launch_hint"]["isolated_provider"]["provider_id"] == (
        "local-isolated-desktop"
    )
    assert default_status["launch_hint"]["isolated_provider"]["smoke_command"] == [
        "python",
        "scripts/smoke_isolated_desktop_provider.py",
    ]
    assert "desktop.open_app" in default_status["launch_hint"]["isolated_provider"][
        "supported_tools"
    ]
    assert "desktop.click_ui_element" in default_status["launch_hint"][
        "isolated_provider"
    ]["env"]["OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS"]
    assert default_status["launch_hint"]["isolated_provider"][
        "keyboard_mouse_capture_supported"
    ] is True
    assert default_status["launch_hint"]["isolated_provider"][
        "desktop_session_kind"
    ] == "isolated_desktop"
    assert default_status["launch_hint"]["isolated_provider"][
        "desktop_session_isolated"
    ] is True
    assert default_status["launch_hint"]["controlled_provider"]["provider_id"] == (
        "local-controlled-desktop"
    )
    assert default_status["launch_hint"]["controlled_provider"]["smoke_command"] == [
        "python",
        "scripts/run_controlled_desktop_provider.py",
        "--manifest",
    ]
    assert default_status["launch_hint"]["controlled_provider"][
        "keyboard_mouse_capture_supported"
    ] is True
    assert default_status["launch_hint"]["controlled_provider"][
        "desktop_session_kind"
    ] == "user_foreground"
    assert default_status["launch_hint"]["controlled_provider"][
        "desktop_session_isolated"
    ] is False
    assert explicit_status["available"] is True
    assert explicit_status["adapter_ready"] is True
    assert explicit_status["status"] == "available"
    assert explicit_status["blocking_conditions"] == []
    assert explicit_status["health"]["status"] == "not_checked"
    assert explicit_status["launch_hint"]["provider_id"] == "local-headless-desktop"
    conformance_status = sandbox_desktop_provider_status(
        {
            "sandbox_provider": {
                "available": True,
                "provider_id": "sandbox-1",
                "adapter_ready": True,
                "provider_conformance": {
                    "ok": True,
                    "mode": "release_virtual_desktop_provider_conformance",
                    "runtime_checked": True,
                    "public_release_ready": True,
                    "covered_tools": ["desktop.list_apps"],
                },
            }
        }
    )
    conformance_snapshot = SandboxDesktopProviderSnapshot.model_validate(
        conformance_status
    )
    assert isinstance(
        conformance_snapshot.provider_conformance,
        DesktopProviderConformanceSnapshot,
    )
    assert conformance_snapshot.provider_conformance.public_release_ready is True
    assert conformance_snapshot.provider_conformance.covered_tools == [
        "desktop.list_apps"
    ]
    with pytest.raises(ValidationError):
        SandboxDesktopProviderSnapshot(unknown=True)
    with pytest.raises(ValidationError):
        DesktopProviderHealthSnapshot(unknown=True)


def test_sandbox_desktop_provider_snapshot_preserves_cua_health_diagnostics() -> None:
    health_report = {
        "schema_version": "1",
        "overall": "ok",
        "checks": [
            {
                "name": "tcc_accessibility",
                "status": "pass",
                "message": "Accessibility is available.",
            }
        ],
    }

    snapshot = SandboxDesktopProviderSnapshot.model_validate(
        {
            "available": True,
            "provider_id": "cua-background",
            "provider_kind": "background_desktop",
            "health": {
                "ok": True,
                "checked": True,
                "status": "healthy",
                "health_report": health_report,
                "cached": True,
            },
        }
    )

    assert snapshot.health is not None
    assert snapshot.health.health_report == health_report
    assert snapshot.health.cached is True
    assert _json(snapshot)["health"]["health_report"] == health_report

    with pytest.raises(ValidationError, match="unknown_health_field"):
        SandboxDesktopProviderSnapshot.model_validate(
            {"health": {"unknown_health_field": True}}
        )


def test_desktop_execution_route_decision_reports_provider_boundaries() -> None:
    preview_route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy={"mode": "preview_input"},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
            sandbox_recommended=True,
        ),
    )
    sandbox_ready_route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy={"mode": "sandbox_preferred"},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
            sandbox_recommended=True,
        ),
        metadata={
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "sandbox-1",
                "supported_tools": ["desktop.safe_type_text"],
            }
        },
    )
    browser_route = desktop_execution_route_decision(
        "browser.open_url",
        policy={"mode": "preview_input"},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="tool_native",
            isolation="browser_target",
        ),
    )
    readonly_provider_route = desktop_execution_route_decision(
        "desktop.list_apps",
        policy={"mode": "supervised_live"},
        execution_mode=DesktopExecutionModeSnapshot(mode="tool_native"),
        metadata={
            "desktop_provider_route_readonly": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "sandbox-1",
                "supported_tools": ["desktop.list_apps"],
            },
        },
    )
    foreground_provider_route = desktop_execution_route_decision(
        "app.focus_and_click_ui_element",
        policy={"mode": "supervised_live"},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
        ),
        metadata={
            "desktop_provider_route_foreground": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "sandbox-1",
                "supported_tools": ["app.focus_and_click_ui_element"],
            },
        },
    )

    snapshot = DesktopExecutionRouteSnapshot.model_validate(preview_route)
    payload = _json(snapshot)

    assert list(payload) == [
        "route_id",
        "tool_name",
        "requested_mode",
        "selected_provider_kind",
        "selected_provider_id",
        "provider_readiness_status",
        "status",
        "can_execute",
        "can_auto_start",
        "provider_execution_required",
        "sandbox_required",
        "background_desktop_preferred",
        "isolated_desktop_preferred",
        "foreground_takeover_allowed",
        "desktop_execution_session_policy",
        "user_foreground_takeover_risk",
        "requires_user_foreground_session",
        "foreground_mutation_supported",
        "keyboard_mouse_capture_supported",
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "simulated_desktop_provider",
        "provider_contract_blocking_conditions",
        "fallback_mode",
        "reason",
        "blocking_conditions",
        "source",
    ]
    assert payload["provider_readiness_status"] == "provider_required"
    assert payload["status"] == "provider_required"
    assert payload["can_execute"] is False
    assert payload["provider_execution_required"] is False
    assert payload["blocking_conditions"] == ["sandbox_desktop_provider_required"]
    assert sandbox_ready_route["status"] == "sandbox_ready"
    assert sandbox_ready_route["can_execute"] is True
    assert sandbox_ready_route["provider_execution_required"] is True
    assert sandbox_ready_route["selected_provider_id"] == "sandbox-1"
    assert sandbox_ready_route["requires_user_foreground_session"] is False
    assert sandbox_ready_route["user_foreground_takeover_risk"] is False
    assert readonly_provider_route["status"] == "sandbox_ready"
    assert readonly_provider_route["can_execute"] is True
    assert readonly_provider_route["selected_provider_id"] == "sandbox-1"
    assert readonly_provider_route["sandbox_required"] is True
    assert foreground_provider_route["status"] == "sandbox_ready"
    assert foreground_provider_route["can_execute"] is True
    assert foreground_provider_route["selected_provider_id"] == "sandbox-1"
    assert foreground_provider_route["sandbox_required"] is True
    assert foreground_provider_route["requires_user_foreground_session"] is False
    assert foreground_provider_route["user_foreground_takeover_risk"] is False
    assert browser_route["status"] == "ready"
    assert browser_route["selected_provider_kind"] == "browser_target"
    assert browser_route["can_execute"] is True
    with pytest.raises(ValidationError):
        DesktopExecutionRouteSnapshot(unknown=True)


def test_readonly_desktop_route_can_auto_start_provider_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START",
        "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
        "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL",
        "OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL",
        "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL",
        "OHA_YACHIYO_DESKTOP_PROVIDER_START_COMMAND",
        "OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST",
    ):
        monkeypatch.delenv(key, raising=False)

    local_route = desktop_execution_route_decision(
        "desktop.list_apps",
        policy={"mode": "preview_input", "prefer_isolated_desktop": True},
        execution_mode=DesktopExecutionModeSnapshot(mode="tool_native"),
        metadata={"desktop_provider_route_readonly": True},
    )

    assert local_route["status"] == "provider_required"
    assert local_route["can_execute"] is False
    assert local_route["selected_provider_kind"] == "sandbox_desktop"
    assert local_route["sandbox_required"] is True
    assert local_route["requires_user_foreground_session"] is False
    assert local_route["blocking_conditions"] == [
        "sandbox_desktop_provider_required"
    ]

    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "true")

    provider_route = desktop_execution_route_decision(
        "desktop.list_apps",
        policy={"mode": "preview_input", "prefer_isolated_desktop": True},
        execution_mode=DesktopExecutionModeSnapshot(mode="tool_native"),
        metadata={"desktop_provider_route_readonly": True},
    )

    assert provider_route["status"] == "provider_required"
    assert provider_route["can_execute"] is False
    assert provider_route["can_auto_start"] is True
    assert provider_route["sandbox_required"] is True
    assert provider_route["blocking_conditions"] == [
        "sandbox_desktop_provider_required"
    ]
    assert "Readonly desktop discovery" in provider_route["reason"]


def test_desktop_execution_route_blocks_loopback_provider_backend() -> None:
    route = desktop_execution_route_decision(
        "app.open",
        policy={"mode": "supervised_live", "prefer_isolated_desktop": True},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=False,
        ),
        metadata={
            "desktop_provider_route_foreground": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "local-isolated-desktop",
                "provider_kind": "sandbox_desktop",
                "supported_tools": ["app.open"],
                "desktop_session_kind": "isolated_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "loopback_session_harness",
                "desktop_backend_is_loopback": True,
                "desktop_backend_ready_for_public_release": False,
                "requires_real_virtual_desktop_backend": True,
            },
        },
    )
    allowed_route = desktop_execution_route_decision(
        "app.open",
        policy={"mode": "supervised_live", "prefer_isolated_desktop": True},
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=False,
        ),
        metadata={
            "desktop_provider_route_foreground": True,
            "allow_simulated_desktop_provider": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "local-isolated-desktop",
                "provider_kind": "sandbox_desktop",
                "supported_tools": ["app.open"],
                "desktop_session_kind": "isolated_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "loopback_session_harness",
                "desktop_backend_is_loopback": True,
                "requires_real_virtual_desktop_backend": True,
            },
        },
    )

    assert route["status"] == "real_virtual_desktop_provider_required"
    assert route["can_execute"] is False
    assert route["selected_provider_id"] == "local-isolated-desktop"
    assert route["desktop_backend_kind"] == "loopback_session_harness"
    assert route["desktop_backend_is_loopback"] is True
    assert route["requires_real_virtual_desktop_backend"] is True
    assert route["simulated_desktop_provider"] is True
    assert route["reason"].startswith(
        "Current desktop provider is a loopback or simulated harness."
    )
    assert "can be routed through" not in route["reason"]
    assert route["blocking_conditions"] == [
        "loopback_desktop_backend",
        "real_virtual_desktop_backend_required",
    ]
    assert allowed_route["status"] == "sandbox_ready"
    assert allowed_route["can_execute"] is True
    assert allowed_route["simulated_desktop_provider"] is True


def test_isolated_provider_session_rejects_running_loopback_for_real_backend_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_calls: list[dict[str, Any]] = []

    def fake_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "running",
            "running": True,
            "provider_id": "local-isolated-desktop",
            "supported_tools": ["app.open"],
            "desktop_session_kind": "isolated_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "desktop_backend_kind": "loopback_session_harness",
            "desktop_backend_is_loopback": True,
            "desktop_backend_ready_for_public_release": False,
            "requires_real_virtual_desktop_backend": True,
            "provider_status": {
                "supported_tools": ["app.open"],
                "desktop_backend_kind": "loopback_session_harness",
                "desktop_backend_is_loopback": True,
                "desktop_backend_ready_for_public_release": False,
                "requires_real_virtual_desktop_backend": True,
            },
        }

    def fake_start(request: dict[str, Any] | None = None) -> dict[str, Any]:
        start_calls.append(dict(request or {}))
        return {"ok": True, "status": "running", "running": True}

    monkeypatch.setattr(
        isolated_session_module,
        "isolated_desktop_provider_session_status",
        fake_status,
    )
    monkeypatch.setattr(
        isolated_session_module,
        "start_isolated_desktop_provider_session",
        fake_start,
    )

    session = isolated_session_module.ensure_isolated_desktop_provider_session_for_envelope(
        {
            "requests": [
                {
                    "request_id": "request:1:app.open",
                    "tool_name": "app.open",
                    "desktop_execution_route": {
                        "selected_provider_kind": "sandbox_desktop",
                        "status": "real_virtual_desktop_provider_required",
                        "can_execute": False,
                        "requires_real_virtual_desktop_backend": True,
                        "blocking_conditions": [
                            "loopback_desktop_backend",
                            "real_virtual_desktop_backend_required",
                        ],
                    },
                }
            ]
        },
        auto_start=True,
    )

    assert start_calls == []
    assert session["ok"] is False
    assert session["status"] == "real_virtual_desktop_provider_required"
    assert session["running"] is True
    assert session["requires_real_virtual_desktop_backend"] is True
    assert "loopback_desktop_backend" in session["blocking_conditions"]
    assert "desktop_backend_not_release_ready" in session["blocking_conditions"]
    assert "real_virtual_desktop_backend_required" in session["blocking_conditions"]


def test_isolated_provider_session_auto_start_passes_real_backend_requirement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_calls: list[dict[str, Any]] = []

    def fake_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "stopped",
            "running": False,
            "provider_id": "",
            "supported_tools": [],
        }

    def fake_start(request: dict[str, Any] | None = None) -> dict[str, Any]:
        start_calls.append(dict(request or {}))
        return {
            "ok": False,
            "status": "real_virtual_desktop_provider_required",
            "running": False,
            "started": False,
            "provider_id": "real-virtual-desktop",
            "desktop_session_kind": "virtual_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "keyboard_mouse_capture_supported": True,
            "desktop_backend_kind": "candidate_virtual_backend",
            "desktop_backend_is_loopback": False,
            "desktop_backend_ready_for_public_release": False,
            "requires_real_virtual_desktop_backend": True,
            "blocking_conditions": [
                "configured_virtual_desktop_provider_required",
                "real_virtual_desktop_backend_required",
            ],
        }

    monkeypatch.setattr(
        isolated_session_module,
        "isolated_desktop_provider_session_status",
        fake_status,
    )
    monkeypatch.setattr(
        isolated_session_module,
        "start_isolated_desktop_provider_session",
        fake_start,
    )

    session = isolated_session_module.ensure_isolated_desktop_provider_session_for_envelope(
        {
            "requests": [
                {
                    "request_id": "request:1:app.open",
                    "tool_name": "app.open",
                    "desktop_execution_route": {
                        "selected_provider_kind": "sandbox_desktop",
                        "status": "real_virtual_desktop_provider_required",
                        "can_execute": False,
                        "blocking_conditions": [
                            "real_virtual_desktop_backend_required"
                        ],
                    },
                }
            ]
        },
        auto_start=True,
    )

    assert start_calls == [
        {
            "tools": ["app.open"],
            "requires_real_virtual_desktop_backend": True,
        }
    ]
    assert session["ok"] is False
    assert session["status"] == "real_virtual_desktop_provider_required"
    assert session["running"] is False
    assert session["started"] is False
    assert session["desktop_session_kind"] == "virtual_desktop"
    assert session["desktop_session_isolated"] is True
    assert session["foreground_takeover_required"] is False
    assert session["keyboard_mouse_capture_supported"] is True
    assert session["provider_contract"]["desktop_session_kind"] == "virtual_desktop"
    assert session["provider_contract"]["desktop_session_isolated"] is True
    assert session["provider_contract"]["foreground_takeover_required"] is False
    assert session["provider_contract"]["keyboard_mouse_capture_supported"] is True
    assert session["requires_real_virtual_desktop_backend"] is True
    assert session["blocking_conditions"] == [
        "configured_virtual_desktop_provider_required",
        "real_virtual_desktop_backend_required",
    ]


def test_isolated_provider_session_annotation_readies_readonly_discovery_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start_calls: list[dict[str, Any]] = []

    def fake_status() -> dict[str, Any]:
        return {
            "ok": True,
            "status": "stopped",
            "running": False,
            "provider_id": "",
            "supported_tools": [],
        }

    def fake_start(request: dict[str, Any] | None = None) -> dict[str, Any]:
        start_calls.append(dict(request or {}))
        return {
            "ok": True,
            "status": "running",
            "running": True,
            "started": True,
            "provider_id": "local-isolated-desktop",
            "url": "http://127.0.0.1:19093",
            "desktop_session_kind": "isolated_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "keyboard_mouse_capture_supported": True,
            "supported_tools": [
                "desktop.list_apps",
                "app.open",
                "desktop.verify",
            ],
            "provider_status": {
                "desktop_session_kind": "isolated_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "keyboard_mouse_capture_supported": True,
                "supported_tools": [
                    "desktop.list_apps",
                    "app.open",
                    "desktop.verify",
                ],
            },
        }

    monkeypatch.setattr(
        isolated_session_module,
        "isolated_desktop_provider_session_status",
        fake_status,
    )
    monkeypatch.setattr(
        isolated_session_module,
        "start_isolated_desktop_provider_session",
        fake_start,
    )

    provider_required_route = {
        "selected_provider_kind": "sandbox_desktop",
        "status": "provider_required",
        "can_execute": False,
        "can_auto_start": True,
        "sandbox_required": True,
        "blocking_conditions": ["sandbox_desktop_provider_required"],
    }
    envelope = {
        "requests": [
            {
                "request_id": "request:1:desktop.list_apps",
                "tool_name": "desktop.list_apps",
                "desktop_execution_route": dict(provider_required_route),
                "desktop_execution_policy": {
                    "mode": "preview_input",
                    "prefer_isolated_desktop": True,
                },
                "execution_mode": {"mode": "tool_native"},
            },
            {
                "request_id": "request:2:app.open",
                "tool_name": "app.open",
                "desktop_execution_route": dict(provider_required_route),
                "desktop_execution_policy": {
                    "mode": "preview_input",
                    "prefer_isolated_desktop": True,
                    "avoid_user_foreground_takeover": True,
                },
                "execution_mode": {
                    "mode": "supervised_live",
                    "foreground_control": True,
                },
            },
            {
                "request_id": "request:3:desktop.verify",
                "tool_name": "desktop.verify",
                "desktop_execution_route": dict(provider_required_route),
                "desktop_execution_policy": {
                    "mode": "preview_input",
                    "prefer_isolated_desktop": True,
                },
                "execution_mode": {"mode": "tool_native"},
            },
        ]
    }

    session = isolated_session_module.ensure_isolated_desktop_provider_session_for_envelope(
        envelope,
        auto_start=True,
    )
    annotated = isolated_session_module.annotate_envelope_with_desktop_provider_session(
        envelope,
        session,
    )

    assert start_calls == [
        {"tools": ["app.open", "desktop.list_apps", "desktop.verify"]}
    ]
    assert session["needed"] is True
    assert session["running"] is True
    assert session["request_ids"] == [
        "request:1:desktop.list_apps",
        "request:2:app.open",
        "request:3:desktop.verify",
    ]
    for request in annotated["requests"]:
        assert request["desktop_provider_session"]["provider_id"] == (
            "local-isolated-desktop"
        )
        assert request["sandbox_provider"]["provider_id"] == "local-isolated-desktop"
        assert request["desktop_execution_route"]["status"] == "sandbox_ready"
        assert request["desktop_execution_route"]["can_execute"] is True


def test_agent_studio_route_requires_isolated_provider_for_keyboard_mouse_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    metadata = with_agent_studio_desktop_execution_policy({"source": "studio"})
    route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy=metadata["desktop_execution_policy"],
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
        ),
        metadata=metadata,
    )

    assert route["status"] == "sandbox_desktop_session_required"
    assert route["can_execute"] is False
    assert route["selected_provider_kind"] == LOCAL_DESKTOP_PROVIDER_KIND
    assert route["sandbox_required"] is True
    assert route["requires_user_foreground_session"] is True
    assert route["foreground_takeover_allowed"] is False
    assert route["blocking_conditions"] == ["sandbox_desktop_session_required"]


def test_agent_studio_route_blocks_keyboard_mouse_without_isolated_session() -> None:
    route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy={
            "mode": "supervised_live",
            "prefer_isolated_desktop": True,
            "avoid_user_foreground_takeover": True,
            "require_sandbox_for_keyboard_mouse": True,
        },
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
        ),
        metadata={
            "desktop_provider_route_foreground": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "foreground-control",
                "provider_kind": "sandbox_desktop",
                "supported_tools": ["desktop.safe_type_text"],
                "keyboard_mouse_capture_supported": True,
                "desktop_session_kind": "user_foreground",
                "desktop_session_isolated": False,
                "foreground_takeover_required": True,
            },
        },
    )

    assert route["status"] == "sandbox_desktop_session_required"
    assert route["can_execute"] is False
    assert route["selected_provider_id"] == "foreground-control"
    assert route["blocking_conditions"] == ["sandbox_desktop_session_required"]


def test_desktop_policy_prefer_isolated_routes_keyboard_mouse_without_extra_metadata() -> None:
    route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy={
            "mode": "supervised_live",
            "prefer_isolated_desktop": True,
            "avoid_user_foreground_takeover": True,
            "require_sandbox_for_keyboard_mouse": True,
        },
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
        ),
        metadata={
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "foreground-control",
                "provider_kind": "sandbox_desktop",
                "supported_tools": ["desktop.safe_type_text"],
                "keyboard_mouse_capture_supported": True,
                "desktop_session_kind": "user_foreground",
                "desktop_session_isolated": False,
                "foreground_takeover_required": True,
            },
        },
    )

    assert route["status"] == "sandbox_desktop_session_required"
    assert route["can_execute"] is False
    assert route["selected_provider_id"] == "foreground-control"
    assert route["blocking_conditions"] == ["sandbox_desktop_session_required"]


def test_daily_policy_requires_explicit_permission_for_user_foreground_provider() -> None:
    provider = {
        "available": True,
        "adapter_ready": True,
        "provider_id": "foreground-control",
        "provider_kind": "sandbox_desktop",
        "supported_tools": ["app.open"],
        "keyboard_mouse_capture_supported": True,
        "desktop_session_kind": "user_foreground",
        "desktop_session_isolated": False,
        "foreground_takeover_required": True,
    }
    route = desktop_execution_route_decision(
        "app.open",
        policy=daily_entrypoint_desktop_execution_policy(surface="chat"),
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=False,
        ),
        metadata={"sandbox_provider": provider},
    )
    explicit_route = desktop_execution_route_decision(
        "app.open",
        policy=daily_entrypoint_desktop_execution_policy(surface="chat"),
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=False,
        ),
        metadata={
            "allow_user_foreground_takeover": True,
            "sandbox_provider": provider,
        },
    )

    assert route["status"] == "sandbox_desktop_session_required"
    assert route["can_execute"] is False
    assert route["selected_provider_id"] == "foreground-control"
    assert route["user_foreground_takeover_risk"] is True
    assert route["blocking_conditions"] == ["sandbox_desktop_session_required"]
    assert explicit_route["status"] == "provider_ready"
    assert explicit_route["can_execute"] is True
    assert explicit_route["foreground_takeover_allowed"] is True


def test_daily_policy_routes_app_launch_through_isolated_provider_without_foreground_mode() -> None:
    route = desktop_execution_route_decision(
        "app.open",
        policy=daily_entrypoint_desktop_execution_policy(surface="chat"),
        execution_mode=DesktopExecutionModeSnapshot(mode="tool_native"),
        metadata={
            "desktop_provider_route_foreground": True,
            "sandbox_provider": {
                "available": True,
                "adapter_ready": True,
                "provider_id": "isolated-provider",
                "provider_kind": "sandbox_desktop",
                "supported_tools": ["app.open"],
                "desktop_session_kind": "isolated_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "virtual_desktop_backend",
                "desktop_backend_is_loopback": False,
                "desktop_backend_ready_for_public_release": True,
                "requires_real_virtual_desktop_backend": False,
            },
        },
    )

    assert route["status"] == "sandbox_ready"
    assert route["selected_provider_kind"] == "sandbox_desktop"
    assert route["selected_provider_id"] == "isolated-provider"
    assert route["provider_execution_required"] is True
    assert route["requires_user_foreground_session"] is False
    assert route["foreground_takeover_required"] is False


def test_daily_policy_auto_start_never_enables_direct_foreground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "true")
    route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy=daily_entrypoint_desktop_execution_policy(surface="chat"),
        execution_mode=DesktopExecutionModeSnapshot(
            mode="supervised_live",
            foreground_control=True,
            keyboard_mouse_capture=True,
        ),
        metadata={},
    )

    assert route["status"] == "provider_required"
    assert route["can_execute"] is False
    assert route["can_auto_start"] is False
    assert route["sandbox_required"] is False
    assert route["selected_provider_kind"] == "background_desktop"
    assert route["foreground_takeover_allowed"] is False
    assert route["fallback_mode"] == "user_handoff"
    assert route["blocking_conditions"] == ["cua_driver_not_installed"]


def test_daily_entrypoint_desktop_execution_policy_defaults_to_background() -> None:
    policy = daily_entrypoint_desktop_execution_policy(surface="bubble")
    metadata = with_daily_entrypoint_desktop_execution_policy(
        {"source": "launcher"},
        surface="bubble",
    )
    explicit = with_daily_entrypoint_desktop_execution_policy(
        {"desktop_execution_policy": {"mode": "supervised_live"}},
        surface="bubble",
    )
    live_foreground = with_daily_entrypoint_desktop_execution_policy(
        {"allow_user_foreground_takeover": True},
        surface="bubble",
    )
    request = {
        "tool": "desktop.safe_type_text",
        "desktop_execution_policy": policy,
        "input": {"text": "hello"},
    }

    assert policy["mode"] == "preview_input"
    assert policy["allow_live_foreground"] is False
    assert policy["prefer_background_desktop"] is True
    assert policy["prefer_isolated_desktop"] is False
    assert policy["avoid_user_foreground_takeover"] is True
    assert policy["require_sandbox_for_keyboard_mouse"] is False
    assert (
        desktop_provider_session_auto_start_recommended_for_requests([request])
        is False
    )
    assert policy["allow_media_control"] is True
    assert metadata["desktop_execution_policy"]["mode"] == "preview_input"
    assert metadata["desktop_execution_policy"]["source"] == "daily_bubble"
    assert metadata["desktop_execution_policy"]["prefer_background_desktop"] is True
    assert metadata["desktop_execution_policy"]["prefer_isolated_desktop"] is False
    assert metadata["desktop_execution_policy"]["avoid_user_foreground_takeover"] is True
    assert metadata["desktop_execution_policy"]["require_sandbox_for_keyboard_mouse"] is False
    assert explicit["desktop_execution_policy"] == {"mode": "supervised_live"}
    assert live_foreground["desktop_execution_policy"]["mode"] == "allow"
    assert live_foreground["desktop_execution_policy"]["allow_live_foreground"] is True
    assert live_foreground["desktop_execution_policy"]["prefer_background_desktop"] is False
    assert live_foreground["desktop_execution_policy"]["prefer_isolated_desktop"] is False
    assert (
        live_foreground["desktop_execution_policy"]["avoid_user_foreground_takeover"]
        is False
    )
    assert metadata["desktop_provider_health_probe"] is True
    assert metadata["desktop_provider_route_readonly"] is True
    assert metadata["desktop_provider_route_foreground"] is True


def test_desktop_provider_session_auto_start_default_uses_provider_config(
    monkeypatch,
) -> None:
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_START_COMMAND", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST", raising=False)

    assert desktop_provider_session_auto_start_default() is False

    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST", "/tmp/provider.json")

    assert desktop_provider_session_auto_start_default() is True

    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "false")

    assert desktop_provider_session_auto_start_default() is False

    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "true")

    assert desktop_provider_session_auto_start_default() is True


def test_provider_manifest_resolution_keeps_env_runtime_local_and_prefers_session_metadata(
    tmp_path,
    monkeypatch,
) -> None:
    configured_manifest = str(tmp_path / "configured-provider.json")
    explicit_manifest = str(tmp_path / "explicit-provider.json")
    nested_manifest = str(tmp_path / "nested-provider.json")
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST", configured_manifest)

    daily = with_daily_entrypoint_desktop_execution_policy(
        {"source": "launcher"},
        surface="live2d",
    )
    studio = with_agent_studio_desktop_execution_policy({"source": "studio"})
    explicit = with_daily_entrypoint_desktop_execution_policy(
        {"provider_manifest_path": explicit_manifest},
        surface="chat",
    )
    nested = with_agent_studio_desktop_execution_policy(
        {
            "metadata": {"provider_manifest": configured_manifest},
            "desktop_provider_session": {"provider_manifest": nested_manifest},
        }
    )

    assert "provider_manifest" not in daily
    assert "provider_manifest" not in studio
    assert explicit["provider_manifest_path"] == explicit_manifest
    assert "provider_manifest" not in explicit
    assert nested["desktop_provider_session"]["provider_manifest"] == nested_manifest
    assert "provider_manifest" not in nested
    assert (
        desktop_policy_module.desktop_provider_manifest_path_from_metadata(nested)
        == nested_manifest
    )


def test_desktop_provider_session_strict_foreground_default_uses_provider_readiness(
    monkeypatch,
) -> None:
    for key in (
        "OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START",
        "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
        "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL",
        "OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL",
        "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL",
        "OHA_YACHIYO_DESKTOP_PROVIDER_START_COMMAND",
        "OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST",
    ):
        monkeypatch.delenv(key, raising=False)

    assert desktop_provider_session_strict_foreground_default() is False
    assert (
        desktop_provider_session_strict_foreground_default(
            {
                "sandbox_provider": {
                    "available": True,
                    "adapter_ready": True,
                    "desktop_session_isolated": True,
                    "foreground_takeover_required": False,
                    "keyboard_mouse_capture_supported": True,
                    "supported_tools": ["desktop.safe_click"],
                }
            }
        )
        is True
    )
    assert (
        desktop_provider_session_strict_foreground_default(
            {
                "allow_user_foreground_takeover": True,
                "sandbox_provider": {
                    "available": True,
                    "adapter_ready": True,
                    "desktop_session_isolated": True,
                    "foreground_takeover_required": False,
                    "keyboard_mouse_capture_supported": True,
                    "supported_tools": ["desktop.safe_click"],
                },
            }
        )
        is False
    )
    assert (
        desktop_provider_session_strict_foreground_default(
            {
                "sandbox_provider": {
                    "available": True,
                    "adapter_ready": True,
                    "desktop_session_isolated": True,
                    "foreground_takeover_required": False,
                    "keyboard_mouse_capture_supported": False,
                    "supported_tools": ["desktop.ui_elements"],
                }
            }
        )
        is False
    )


def test_desktop_provider_session_auto_start_recommended_for_input_tasks() -> None:
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [{"tool": "desktop.safe_type_text", "input": {"text": "hello"}}],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "app.open_and_click_ui_element",
                    "approval_required": True,
                    "input": {"app_name": "Slack", "target": "Send"},
                }
            ],
        )
        is False
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "app.focus_and_click_ui_element",
                    "input": {"app_name": "Slack", "target": "Send"},
                }
            ],
        )
        is False
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "app.open_and_safe_click",
                    "input": {"app_name": "Notes", "x": 32, "y": 48},
                }
            ],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [{"tool": "desktop.safe_click", "input": {"x": 32, "y": 48}}],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "app.open_and_safe_shortcut",
                    "input": {"app_name": "Notes", "action": "new_note"},
                }
            ],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [{"tool": "app.open", "input": {"app_name": "Music"}}],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [{"tool": "media.music_app_open_and_play", "input": {"app_name": "Music"}}],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "desktop.open_path",
                    "input": {"path": "/tmp/report.pdf"},
                    "desktop_execution_policy": {
                        "mode": "preview_input",
                        "prefer_isolated_desktop": True,
                        "avoid_user_foreground_takeover": True,
                        "require_sandbox_for_keyboard_mouse": True,
                    },
                }
            ],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "desktop.open_path",
                    "input": {"path": "/tmp/report.pdf"},
                    "desktop_execution_policy": {
                        "mode": "allow",
                        "allow_live_foreground": True,
                    },
                }
            ],
        )
        is False
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "desktop.verify",
                    "desktop_execution_route": {
                        "blocking_conditions": [
                            "sandbox_keyboard_mouse_provider_required"
                        ]
                    },
                }
            ],
        )
        is False
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            [
                {
                    "tool": "desktop.list_apps",
                    "desktop_execution_route": {
                        "status": "provider_required",
                        "can_auto_start": True,
                        "blocking_conditions": [
                            "sandbox_desktop_provider_required"
                        ],
                    },
                }
            ],
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            {
                "execution_strategy": {
                    "preferred_environment": "isolated_desktop",
                    "interaction_mode": "foreground",
                    "keyboard_mouse_step_count": 1,
                    "sandbox_required": True,
                },
                "requests": [
                    {
                        "tool_name": "desktop.safe_type_text",
                        "input": {"text": "hello"},
                    }
                ],
            }
        )
        is True
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            {
                "execution_strategy": {
                    "preferred_environment": "isolated_desktop",
                    "interaction_mode": "foreground",
                    "keyboard_mouse_step_count": 1,
                    "sandbox_required": True,
                },
                "requests": [
                    {
                        "tool_name": "app.focus_and_click_ui_element",
                        "input": {"app_name": "Slack", "target": "Send"},
                    }
                ],
            }
        )
        is False
    )
    assert (
        desktop_provider_session_auto_start_recommended_for_requests(
            {
                "execution_strategy": {
                    "preferred_environment": "isolated_desktop",
                    "interaction_mode": "foreground",
                    "approval_step_count": 1,
                    "keyboard_mouse_step_count": 1,
                },
                "requests": [
                    {
                        "tool_name": "desktop.safe_type_text",
                        "approval_required": True,
                    }
                ],
            }
        )
        is False
    )


def test_daily_desktop_runtime_execution_envelope_uses_daily_policy_for_strategy() -> None:
    metadata = with_daily_entrypoint_desktop_execution_policy(
        {"launcher_mode": "bubble"},
        surface="bubble",
    )
    envelope = daily_desktop_runtime_execution_envelope(
        "在当前应用输入 hello",
        allowed_tools=["desktop.safe_type_text"],
        metadata=metadata,
    )

    request_policy = envelope["requests"][0]["desktop_execution_policy"]
    assert request_policy["mode"] == "preview_input"
    assert request_policy["prefer_background_desktop"] is True
    assert request_policy["prefer_isolated_desktop"] is False
    assert envelope["execution_strategy"]["preferred_environment"] == (
        "background_desktop"
    )
    assert envelope["execution_strategy"]["interaction_mode"] == "background"
    assert envelope["execution_strategy"]["keyboard_mouse_step_count"] >= 1
    assert envelope["execution_strategy"]["foreground_takeover_allowed"] is False
    assert envelope["execution_strategy"]["sandbox_required"] is False
    assert envelope["execution_strategy"]["provider_auto_start_recommended"] is False


def test_runtime_execution_envelope_inherits_policy_without_overriding_explicit_request() -> None:
    stale_route = {
        "status": "provider_required",
        "can_execute": False,
        "selected_provider_kind": "none",
        "blocking_conditions": ["sandbox_desktop_provider_required"],
    }
    explicit_route = {
        "status": "supervised_live",
        "can_execute": True,
        "selected_provider_kind": "local_desktop",
        "blocking_conditions": [],
    }
    envelope = {
        "envelope_id": "envelope-policy-inheritance",
        "desktop_execution_route": dict(stale_route),
        "requests": [
            {
                "tool_name": "app.open",
                "input": {"app_name": "TextEdit"},
                "desktop_execution_route": dict(stale_route),
            },
            {
                "tool_name": "app.focus",
                "input": {"app_name": "Music"},
                "desktop_execution_policy": {
                    "mode": "supervised_live",
                    "allow_live_foreground": True,
                },
                "desktop_execution_route": dict(explicit_route),
            },
        ],
    }
    background_policy = daily_entrypoint_desktop_execution_policy(surface="chat")

    effective = runtime_execution_envelope_with_desktop_execution_policy(
        envelope,
        background_policy,
    )

    assert "desktop_execution_policy" not in envelope
    assert envelope["desktop_execution_route"] == stale_route
    assert effective["desktop_execution_policy"] == background_policy
    assert "desktop_execution_route" not in effective
    inherited_request, explicit_request = effective["requests"]
    assert inherited_request["desktop_execution_policy"] == background_policy
    assert "desktop_execution_route" not in inherited_request
    assert explicit_request["desktop_execution_policy"] == {
        "mode": "supervised_live",
        "allow_live_foreground": True,
    }
    assert explicit_request["desktop_execution_route"] == explicit_route


def test_daily_desktop_direct_metadata_request_carries_daily_policy() -> None:
    request = daily_desktop_direct_metadata_request(
        {
            "desktop_permission_recovery": True,
            "recovery_risk_level": "low",
            "recovery_tool": "app.focus_and_safe_shortcut",
            "recovery_input": {"app_name": "Finder", "action": "rename_selected"},
            "launcher_mode": "live2d",
        },
        allowed_tools=["app.focus_and_safe_shortcut"],
    )

    assert request is not None
    policy = request["desktop_execution_policy"]
    assert request["tool"] == "app.focus_and_safe_shortcut"
    assert policy["mode"] == "preview_input"
    assert policy["source"] == "daily_live2d"
    assert policy["prefer_background_desktop"] is True
    assert policy["prefer_isolated_desktop"] is False
    assert policy["avoid_user_foreground_takeover"] is True
    assert policy["require_sandbox_for_keyboard_mouse"] is False


def test_daily_desktop_direct_metadata_request_preserves_explicit_policy() -> None:
    request = daily_desktop_direct_metadata_request(
        {
            "desktop_permission_recovery": True,
            "recovery_risk_level": "low",
            "recovery_tool": "app.focus",
            "recovery_input": {"app_name": "Music"},
            "desktop_execution_policy": {
                "mode": "supervised_live",
                "source": "explicit_test",
            },
        },
        allowed_tools=["app.focus"],
    )

    assert request is not None
    assert request["desktop_execution_policy"] == {
        "mode": "supervised_live",
        "source": "explicit_test",
    }


def test_agent_studio_desktop_execution_policy_requests_provider_health_probe() -> None:
    metadata = with_agent_studio_desktop_execution_policy({"source": "studio"})
    explicit = with_agent_studio_desktop_execution_policy(
        {"desktop_execution_policy": {"mode": "supervised_live"}}
    )
    daily = with_daily_entrypoint_desktop_execution_policy(
        {"source": "launcher"},
        surface="chat",
    )

    assert metadata["desktop_execution_policy"]["mode"] == "supervised_live"
    assert metadata["desktop_execution_policy"]["allow_live_foreground"] is False
    assert metadata["desktop_execution_policy"]["prefer_isolated_desktop"] is True
    assert metadata["desktop_execution_policy"]["avoid_user_foreground_takeover"] is True
    assert metadata["desktop_execution_policy"]["require_sandbox_for_keyboard_mouse"] is True
    assert metadata["desktop_provider_health_probe"] is True
    assert metadata["desktop_provider_route_readonly"] is True
    assert metadata["desktop_provider_route_foreground"] is True
    assert explicit["desktop_execution_policy"] == {"mode": "supervised_live"}
    assert explicit["desktop_provider_health_probe"] is True
    assert explicit["desktop_provider_route_readonly"] is True
    assert explicit["desktop_provider_route_foreground"] is True
    assert daily["desktop_provider_health_probe"] is True
    assert daily["desktop_provider_route_readonly"] is True
    assert daily["desktop_provider_route_foreground"] is True


def test_sandbox_desktop_provider_status_probes_health_when_metadata_requests_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {
                    "ok": True,
                    "status": "ready",
                    "version": "0.1.0",
                    "supported_tools": ["desktop.permission_preflight"],
                    "capabilities": ["desktop_discovery"],
                }
            ).encode("utf-8")

        def getcode(self) -> int:
            return self.status

    def fake_urlopen(request: Any, *, timeout: float) -> FakeResponse:
        calls.append(request.full_url)
        return FakeResponse()

    monkeypatch.setattr(
        "apps.shell.agent.runtime.desktop_execution_providers.urlopen_with_bundled_ca",
        fake_urlopen,
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_URL", "http://127.0.0.1:19091")
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_ID", "local-headless-desktop")
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS",
        "desktop.permission_preflight",
    )

    unchecked = sandbox_desktop_provider_status({})
    probed = sandbox_desktop_provider_status({"desktop_provider_health_probe": True})

    assert unchecked["health"]["checked"] is False
    assert calls == ["http://127.0.0.1:19091/status"]
    assert probed["available"] is True
    assert probed["health"]["checked"] is True
    assert probed["health"]["status"] == "ready"
    assert probed["health"]["provider_version"] == "0.1.0"
    assert probed["health"]["supported_tools"] == ["desktop.permission_preflight"]


def test_sandbox_desktop_provider_status_exposes_virtual_provider_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_URL", "http://127.0.0.1:29093")

    monkeypatch.setattr(
        desktop_policy_module,
        "desktop_execution_provider_status_from_env",
        lambda probe_health=False: {
            "configured": True,
            "available": True,
            "adapter_ready": True,
            "authentication_configured": True,
            "capabilities": [
                "virtual_desktop",
                "idempotent_tool_requests",
            ],
            "provider_kind": "sandbox_desktop",
            "provider_id": "real-virtual-desktop",
            "status": "available",
            "desktop_session_kind": "virtual_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "desktop_backend_kind": "virtual_desktop_backend",
            "desktop_backend_is_loopback": False,
            "desktop_backend_ready_for_public_release": True,
            "requires_real_virtual_desktop_backend": False,
            "supported_tools": [
                "desktop.list_apps",
                "app.open",
                "desktop.inspect_app",
                "media.music_app_open_and_play",
                "media.music_app_control",
                "desktop.read_ui",
                "desktop.click_ui_element",
                "desktop.safe_type_text",
                "desktop.safe_shortcut",
                "desktop.verify",
            ],
        },
    )

    status = sandbox_desktop_provider_status({"desktop_provider_health_probe": True})

    assert status["desktop_backend_kind"] == "virtual_desktop_backend"
    assert status["desktop_backend_is_loopback"] is False
    assert status["desktop_backend_ready_for_public_release"] is True
    assert status["requires_real_virtual_desktop_backend"] is False
    assert status["provider_contract"]["ok"] is True
    assert status["provider_contract"]["contract_version"] == (
        "oha-yachiyo.desktop-provider.v1"
    )


def test_sandbox_desktop_provider_status_reads_provider_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    for key in list(os.environ):
        if key.startswith(
            (
                "OHA_YACHIYO_DESKTOP_PROVIDER_",
                "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    manifest_path = tmp_path / "release-provider-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider_id": "release-virtual-desktop",
                "provider_kind": "sandbox_desktop",
                "endpoint_urls": {
                    "status": "http://127.0.0.1:29097/status",
                    "execute": "http://127.0.0.1:29097/tools/execute",
                },
                "supported_tools": list(OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS),
                "capabilities": [
                    "virtual_desktop",
                    "idempotent_tool_requests",
                ],
                "keyboard_mouse_capture_supported": True,
                "foreground_mutation_supported": True,
                "desktop_session_kind": "virtual_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "vnc_virtual_desktop",
                "desktop_backend_is_loopback": False,
                "desktop_backend_ready_for_public_release": True,
                "requires_real_virtual_desktop_backend": False,
                "authentication": {
                    "token_env": "TEST_RELEASE_PROVIDER_TOKEN",
                },
            }
        ),
        encoding="utf-8",
    )
    calls: list[tuple[dict[str, str], bool]] = []

    def fake_provider_status(
        environ: dict[str, str] | None = None,
        *,
        probe_health: bool = False,
    ) -> dict[str, Any]:
        env = dict(environ or {})
        calls.append((env, probe_health))
        return {
            "configured": True,
            "available": True,
            "adapter_ready": True,
            "authentication_configured": bool(
                env.get("OHA_YACHIYO_DESKTOP_PROVIDER_TOKEN")
            ),
            "provider_kind": "sandbox_desktop",
            "provider_id": env["OHA_YACHIYO_DESKTOP_PROVIDER_ID"],
            "status": "available",
            "desktop_session_kind": env[
                "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND"
            ],
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "keyboard_mouse_capture_supported": True,
            "desktop_backend_kind": env["OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_KIND"],
            "desktop_backend_is_loopback": False,
            "desktop_backend_ready_for_public_release": True,
            "requires_real_virtual_desktop_backend": False,
            "supported_tools": env["OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS"].split(","),
            "capabilities": env[
                "OHA_YACHIYO_DESKTOP_PROVIDER_CAPABILITIES"
            ].split(","),
        }

    monkeypatch.setattr(
        desktop_policy_module,
        "desktop_execution_provider_status_from_env",
        fake_provider_status,
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST", str(manifest_path))
    monkeypatch.setenv("TEST_RELEASE_PROVIDER_TOKEN", "release-provider-token")

    status = sandbox_desktop_provider_status({"desktop_provider_health_probe": True})
    route = desktop_execution_route_decision(
        "desktop.safe_type_text",
        policy=daily_entrypoint_desktop_execution_policy(),
        execution_mode={
            "foreground_control": True,
            "keyboard_mouse_capture": True,
            "mode": "supervised_live",
        },
        metadata={"desktop_provider_health_probe": True},
    )

    assert calls[0][1] is True
    assert calls[0][0]["OHA_YACHIYO_DESKTOP_PROVIDER_URL"] == (
        "http://127.0.0.1:29097"
    )
    assert calls[0][0]["OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL"] == (
        "http://127.0.0.1:29097/tools/execute"
    )
    assert status["source"] == "provider_manifest"
    assert status["provider_id"] == "release-virtual-desktop"
    assert status["desktop_session_kind"] == "virtual_desktop"
    assert status["desktop_session_isolated"] is True
    assert status["foreground_takeover_required"] is False
    assert status["desktop_backend_kind"] == "vnc_virtual_desktop"
    assert status["desktop_backend_is_loopback"] is False
    assert status["desktop_backend_ready_for_public_release"] is True
    assert status["requires_real_virtual_desktop_backend"] is False
    assert status["provider_contract"]["ok"] is True
    assert route["status"] == "sandbox_ready"
    assert route["selected_provider_id"] == "release-virtual-desktop"
    assert route["provider_execution_required"] is True


def test_runtime_provider_registry_executes_manifest_backed_provider(
    tmp_path,
) -> None:
    manifest_path = tmp_path / "release-provider-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider_id": "release-virtual-desktop",
                "provider_kind": "sandbox_desktop",
                "endpoint_urls": {
                    "status": "http://127.0.0.1:29097/status",
                    "execute": "http://127.0.0.1:29097/tools/execute",
                },
                "supported_tools": ["desktop.safe_type_text"],
                "keyboard_mouse_capture_supported": True,
                "foreground_mutation_supported": True,
                "desktop_session_kind": "virtual_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "vnc_virtual_desktop",
                "desktop_backend_is_loopback": False,
                "desktop_backend_ready_for_public_release": True,
                "requires_real_virtual_desktop_backend": False,
            }
        ),
        encoding="utf-8",
    )
    calls: list[dict[str, Any]] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def getcode(self) -> int:
            return self.status

        def read(self) -> bytes:
            return json.dumps(
                {
                    "ok": True,
                    "result": {
                        "ok": True,
                        "tool": "desktop.safe_type_text",
                        "data": {"character_count": 5},
                    },
                }
            ).encode("utf-8")

    def fake_urlopen(request: Any, timeout: float = 0) -> FakeResponse:
        calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "payload": json.loads(request.data.decode("utf-8")),
                "authorization": request.headers.get("Authorization", ""),
            }
        )
        return FakeResponse()

    env = {"OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST": str(manifest_path)}
    status = desktop_execution_provider_status_from_env(env, probe_health=False)
    registry = desktop_execution_provider_registry_from_env(env, urlopen=fake_urlopen)
    route = {
        "route_id": "desktop-route:desktop.safe_type_text",
        "tool_name": "desktop.safe_type_text",
        "selected_provider_kind": "sandbox_desktop",
        "selected_provider_id": "release-virtual-desktop",
        "status": "sandbox_ready",
        "can_execute": True,
        "provider_execution_required": True,
        "sandbox_required": True,
        "desktop_session_isolated": True,
        "foreground_takeover_required": False,
        "desktop_backend_is_loopback": False,
        "desktop_backend_ready_for_public_release": True,
        "requires_real_virtual_desktop_backend": False,
    }

    result = registry.execute_if_routed(
        "desktop.safe_type_text",
        {"text": "hello"},
        tool_request={
            "tool": "desktop.safe_type_text",
            "desktop_execution_route": route,
        },
        broker=None,
    )

    assert status["configured"] is True
    assert status["provider_id"] == "release-virtual-desktop"
    assert status["endpoint_origin"] == "http://127.0.0.1:29097"
    assert status["endpoint_path"] == "/tools/execute"
    assert status["source"] == "provider_manifest"
    assert result is not None
    assert result["ok"] is True
    assert result["desktop_execution_provider_routed"] is True
    assert result["desktop_execution_provider"] == {
        "provider_kind": "sandbox_desktop",
        "provider_id": "release-virtual-desktop",
        "adapter_registered": True,
        "route_id": "desktop-route:desktop.safe_type_text",
    }
    assert calls == [
        {
            "url": "http://127.0.0.1:29097/tools/execute",
            "timeout": 20.0,
            "payload": {
                "tool": "desktop.safe_type_text",
                "input": {"text": "hello"},
                "approved": False,
                "route": route,
                "tool_request": {
                    "tool": "desktop.safe_type_text",
                    "desktop_execution_route": route,
                },
                "provider": {
                    "provider_kind": "sandbox_desktop",
                    "provider_id": "release-virtual-desktop",
                },
            },
            "authorization": "",
        }
    ]


def test_desktop_recovery_action_metadata_snapshot_json_shape_is_stable() -> None:
    snapshot = DesktopRecoveryActionMetadataSnapshot(
        recovery_tool="system.settings_open",
        recovery_input={"target": "屏幕录制权限"},
        recovery_permission_target="screen_recording",
        recovery_risk_level="low",
        recovery_retry_tool="screen.capture",
        recovery_retry_input={"display_id": "main"},
        recovery_retry_input_schema={
            "type": "object",
            "required": ["x", "y"],
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
        },
        recovery_retry_input_source="screen_capture_artifact",
        recovery_retry_artifact_tool="screen.capture",
        recovery_retry_artifact_kind="image",
        required_retry_fields=["x", "y"],
        recommended_tools=["screen.capture", "desktop.click"],
        recovery_retry_prompt="截图当前屏幕",
        recovery_followup_tool="desktop.type_text",
        recovery_followup_input={
            "text_source": "original_request",
            "character_count": 5,
        },
        action_target={"action": "capture", "target": "main_display"},
        observation_evidence={"source_tool": "screen.capture"},
        observation_retry={
            "tool": "screen.capture",
            "reason": "permission_recovered",
        },
        sandbox_provider=SandboxDesktopProviderSnapshot(
            status="provider_required",
            blocking_conditions=["sandbox_desktop_provider_required"],
        ),
        desktop_execution_route=DesktopExecutionRouteSnapshot(
            tool_name="screen.capture",
            status="ready",
        ),
        verification_targets=[{"step_id": "verify-screen", "todo_id": "todo-screen"}],
        task_verification_targets=[
            {"step_id": "verify-screen", "todo_title": "Verify screenshot"}
        ],
        recovery_retry_source_event_type="agent.desktop.permission_recovery",
        recovery_retry_source_tool_call_id="tool-call-1",
        source_task_id="task-source-screen",
        source_task_title="截图当前桌面",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "daily_desktop_intent",
        "desktop_permission_recovery",
        "desktop_permission_retry",
        "recovery_action_kind",
        "recovery_tool",
        "recovery_input",
        "recovery_permission_target",
        "recovery_risk_level",
        "recovery_retry_tool",
        "recovery_retry_input",
        "recovery_retry_input_schema",
        "recovery_retry_input_source",
        "recovery_retry_artifact_tool",
        "recovery_retry_artifact_kind",
        "required_retry_fields",
        "recommended_tools",
        "recovery_retry_prompt",
        "recovery_followup_tool",
        "recovery_followup_input",
        "action_target",
        "observation_evidence",
        "observation_retry",
        "sandbox_provider",
        "desktop_execution_route",
        "verification_targets",
        "task_verification_targets",
        "recovery_retry_source_event_type",
        "recovery_retry_source_tool_call_id",
        "source_task_id",
        "source_task_title",
    ]
    assert payload["daily_desktop_intent"] is True
    assert payload["desktop_permission_recovery"] is True
    assert payload["recovery_tool"] == "system.settings_open"
    assert payload["recovery_retry_tool"] == "screen.capture"
    assert payload["recovery_followup_tool"] == "desktop.type_text"
    assert payload["recovery_followup_input"] == {
        "text_source": "original_request",
        "character_count": 5,
    }
    assert payload["action_target"] == {"action": "capture", "target": "main_display"}
    assert payload["observation_evidence"] == {"source_tool": "screen.capture"}
    assert payload["observation_retry"] == {
        "tool": "screen.capture",
        "reason": "permission_recovered",
    }
    assert payload["sandbox_provider"]["status"] == "provider_required"
    assert payload["desktop_execution_route"]["status"] == "ready"
    assert payload["verification_targets"] == [
        {"step_id": "verify-screen", "todo_id": "todo-screen"}
    ]
    assert payload["task_verification_targets"] == [
        {"step_id": "verify-screen", "todo_title": "Verify screenshot"}
    ]
    assert payload["required_retry_fields"] == ["x", "y"]
    with pytest.raises(ValidationError):
        DesktopRecoveryActionMetadataSnapshot(
            recovery_tool="system.settings_open",
            unknown=True,
        )


def test_desktop_action_risk_snapshot_json_shape_is_stable() -> None:
    snapshot = DesktopActionRiskSnapshot(
        action_id="foreground_type_text",
        risk_level="medium",
        title="Type into foreground UI",
        description="Enter text into the current foreground target.",
        tools=["desktop.type_text"],
        requires_approval=False,
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "action_id",
        "risk_level",
        "title",
        "description",
        "tools",
        "requires_approval",
        "execution_mode",
    ]
    assert payload["risk_level"] == "medium"
    assert payload["execution_mode"] is None
    with pytest.raises(ValidationError):
        DesktopActionRiskSnapshot(
            action_id="read_screen",
            risk_level="low",
            title="Read screen",
            unknown=True,
        )


def test_desktop_execution_capability_policy_marks_registered_tools_available() -> None:
    capabilities = desktop_execution_capability_snapshots(
        platform_name="Darwin",
        registered_tools={
            "screen.capture",
            "desktop.permissions",
            "desktop.active_window",
            "desktop.list_apps",
            "desktop.windows",
            "desktop.ui_elements",
            "app.status",
            "app.open",
            "system.settings_open",
            "app.focus",
            "app.focus_window",
            "app.show",
            "app.hide",
            "app.minimize",
            "app.quit",
            "desktop.quit_app",
            "media.apple_music_play",
            "media.apple_music_open_and_play",
            "media.apple_music_control",
            "media.music_app_open_and_play",
        },
    )

    assert list(capabilities) == [
        "desktop_execution",
        "screen_capture",
        "active_window",
        "app_control",
        "media_control",
        "foreground_activation",
        "foreground_input",
        "browser_control",
    ]
    assert capabilities["desktop_execution"]["available"] is True
    assert "desktop.permissions" in capabilities["desktop_execution"]["available_tools"]
    assert "desktop.list_apps" in capabilities["desktop_execution"]["available_tools"]
    assert capabilities["screen_capture"]["available"] is True
    assert capabilities["screen_capture"]["available_tools"] == ["screen.capture"]
    assert capabilities["foreground_input"]["available"] is False
    assert capabilities["foreground_input"]["unavailable_tools"] == [
        "app.open_and_safe_type_text",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.hide_app",
        "desktop.show_all_apps",
        "desktop.minimize_window",
        "desktop.close_window",
        "desktop.safe_shortcut",
        "desktop.safe_key",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.safe_click",
        "desktop.safe_scroll",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.shortcut",
        "desktop.hotkey",
        "desktop.submit_foreground",
        "desktop.type",
        "desktop.type_text",
        "desktop.click",
    ]
    assert capabilities["foreground_input"]["risk_default"] == "medium"
    assert capabilities["browser_control"]["available"] is False
    assert capabilities["screen_capture"]["diagnostic_route"] == "/screen/current"


def test_desktop_execution_capability_policy_applies_missing_permissions() -> None:
    capabilities = desktop_execution_capability_snapshots(
        platform_name="Darwin",
        registered_tools={
            "screen.capture",
            "desktop.active_window",
            "app.open",
            "system.settings_open",
            "app.focus",
            "app.show",
            "app.hide",
            "app.minimize",
            "media.apple_music_play",
            "media.apple_music_open_and_play",
            "media.apple_music_control",
            "media.music_app_open_and_play",
            "desktop.hotkey",
            "desktop.type_text",
            "desktop.click",
        },
        missing_permissions={
            "screen_capture": ["screen_recording"],
            "foreground_input": ["accessibility"],
        },
    )

    assert capabilities["desktop_execution"]["available"] is False
    assert capabilities["screen_capture"]["available"] is False
    assert capabilities["screen_capture"]["missing_permissions"] == ["screen_recording"]
    assert capabilities["foreground_input"]["available"] is False
    assert capabilities["foreground_input"]["missing_permissions"] == ["accessibility"]
    assert capabilities["media_control"]["available"] is False
    assert capabilities["media_control"]["available_tools"] == [
        "media.apple_music_play",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
    ]
    assert capabilities["media_control"]["degraded_tools"] == [
        "media.music_app_open_and_play",
    ]


def test_desktop_execution_capability_policy_applies_runtime_blockers() -> None:
    capabilities = desktop_execution_capability_snapshots(
        platform_name="Darwin",
        registered_tools={
            "desktop.active_window",
            "desktop.permissions",
            "desktop.click",
            "desktop.type_text",
            "app.open",
            "app.focus",
        },
        blocking_conditions={
            "desktop_execution": ["desktop_session_locked"],
            "foreground_input": ["desktop_session_locked"],
        },
    )

    assert capabilities["desktop_execution"]["available"] is False
    assert capabilities["desktop_execution"]["missing_permissions"] == []
    assert capabilities["desktop_execution"]["blocking_conditions"] == [
        "desktop_session_locked"
    ]
    assert capabilities["desktop_execution"]["available_tools"] == []
    assert "desktop.click" in capabilities["desktop_execution"]["unavailable_tools"]
    assert capabilities["foreground_input"]["available"] is False
    assert capabilities["foreground_input"]["missing_permissions"] == []
    assert capabilities["foreground_input"]["blocking_conditions"] == [
        "desktop_session_locked"
    ]
    assert "desktop.type_text" in capabilities["foreground_input"]["unavailable_tools"]


def test_desktop_execution_capability_policy_models_foreground_activation_gap() -> None:
    capabilities = desktop_execution_capability_snapshots(
        platform_name="Darwin",
        registered_tools={
            "app.open",
            "app.focus",
            "app.focus_window",
            "app.open_and_safe_type_text",
            "app.focus_and_safe_type_text",
        },
        blocking_conditions={
            "foreground_activation": ["foreground_focus_unavailable"],
        },
    )

    assert capabilities["foreground_activation"]["available"] is False
    assert capabilities["foreground_activation"]["missing_permissions"] == []
    assert capabilities["foreground_activation"]["blocking_conditions"] == [
        "foreground_focus_unavailable"
    ]
    assert "app.open" in capabilities["app_control"]["available_tools"]
    assert "app.focus" in capabilities["app_control"]["unavailable_tools"]
    assert "app.open_and_safe_type_text" in capabilities["foreground_input"]["unavailable_tools"]


def test_desktop_execution_capability_policy_reports_tool_level_degradation() -> None:
    capabilities = desktop_execution_capability_snapshots(
        platform_name="Darwin",
        registered_tools={
            "screen.capture",
            "desktop.active_window",
            "app.open",
            "system.settings_open",
            "app.focus",
            "app.hide",
            "app.minimize",
            "media.apple_music_play",
            "media.apple_music_open_and_play",
            "media.apple_music_control",
            "media.music_app_open_and_play",
            "desktop.hotkey",
            "desktop.type_text",
            "desktop.click",
            "browser.open_url",
            "browser.open_url_and_extract_text",
            "browser.open_url_and_screenshot",
            "browser.current_page",
            "browser.click",
            "browser.type_text",
            "browser.extract_text",
            "browser.screenshot",
        },
        missing_permissions={
            "app_control": ["automation"],
            "media_control": ["automation"],
            "browser_control": ["chrome_cdp"],
        },
    )

    app_control = capabilities["app_control"]
    media_control = capabilities["media_control"]
    browser_control = capabilities["browser_control"]
    root = capabilities["desktop_execution"]

    assert app_control["available"] is False
    assert app_control["available_tools"] == [
        "app.open",
        "system.settings_open",
        "app.hide",
        "app.minimize",
    ]
    assert app_control["unavailable_tools"] == [
        "app.status",
        "desktop.open_app",
        "desktop.focus_app",
        "app.focus",
        "app.focus_window",
        "app.show",
        "app.quit",
        "desktop.quit_app",
        "notes.create",
        "reminders.create",
        "calendar.create_event",
    ]
    assert media_control["available"] is False
    assert media_control["degraded_tools"] == [
        "media.apple_music_play",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
    ]
    assert browser_control["available"] is False
    assert browser_control["degraded_tools"] == [
        "browser.open_url",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
        "browser.click",
        "browser.type_text",
        "browser.screenshot",
    ]
    assert "browser.current_page" in browser_control["unavailable_tools"]
    assert "browser.extract_text" in browser_control["unavailable_tools"]
    assert "app.open" in root["available_tools"]
    assert "media.apple_music_play" in root["degraded_tools"]
    assert "media.apple_music_open_and_play" in root["degraded_tools"]
    assert "media.apple_music_control" in root["degraded_tools"]


def test_desktop_execution_policy_records_risk_boundaries() -> None:
    assert desktop_tool_risk_level("screen.capture") == "low"
    assert desktop_tool_risk_level("desktop.permissions") == "low"
    assert desktop_tool_risk_level("desktop.running_apps") == "low"
    assert desktop_tool_risk_level("desktop.list_apps") == "low"
    assert desktop_tool_risk_level("desktop.open_app") == "low"
    assert desktop_tool_risk_level("desktop.focus_app") == "low"
    assert desktop_tool_risk_level("desktop.list_windows") == "low"
    assert desktop_tool_risk_level("desktop.read_ui") == "low"
    assert desktop_tool_risk_level("desktop.windows") == "low"
    assert desktop_tool_risk_level("desktop.ui_elements") == "low"
    assert desktop_tool_risk_level("desktop.verify") == "low"
    assert desktop_tool_risk_level("app.status") == "low"
    assert desktop_tool_risk_level("app.show") == "low"
    assert desktop_tool_risk_level("app.focus_window") == "low"
    assert desktop_tool_risk_level("app.open_and_safe_type_text") == "low"
    assert desktop_tool_risk_level("app.focus_and_safe_type_text") == "low"
    assert desktop_tool_risk_level("app.open_and_safe_shortcut") == "low"
    assert desktop_tool_risk_level("app.focus_and_safe_shortcut") == "low"
    assert desktop_tool_risk_level("app.open_and_safe_key") == "low"
    assert desktop_tool_risk_level("app.focus_and_safe_key") == "low"
    assert desktop_tool_risk_level("app.open_and_hotkey") == "medium"
    assert desktop_tool_risk_level("app.focus_and_hotkey") == "medium"
    assert desktop_tool_risk_level("app.open_and_safe_scroll") == "low"
    assert desktop_tool_risk_level("app.focus_and_safe_scroll") == "low"
    assert desktop_tool_risk_level("app.open_and_safe_click") == "low"
    assert desktop_tool_risk_level("app.focus_and_safe_click") == "low"
    assert desktop_tool_risk_level("app.open_and_click_ui_element") == "medium"
    assert desktop_tool_risk_level("app.focus_and_click_ui_element") == "medium"
    assert desktop_tool_risk_level("app.open_and_type_into_ui_element") == "medium"
    assert desktop_tool_risk_level("app.focus_and_type_into_ui_element") == "medium"
    assert desktop_tool_risk_level("app.hide") == "low"
    assert desktop_tool_risk_level("app.minimize") == "low"
    assert desktop_tool_risk_level("app.quit") == "medium"
    assert desktop_tool_risk_level("desktop.quit_app") == "medium"
    assert desktop_tool_risk_level("desktop.hide_app") == "low"
    assert desktop_tool_risk_level("desktop.minimize_window") == "low"
    assert desktop_tool_risk_level("desktop.safe_shortcut") == "low"
    assert desktop_tool_risk_level("desktop.safe_key") == "low"
    assert desktop_tool_risk_level("desktop.safe_type_text") == "low"
    assert desktop_tool_risk_level("desktop.search_submit") == "low"
    assert desktop_tool_risk_level("desktop.safe_click") == "low"
    assert desktop_tool_risk_level("desktop.safe_scroll") == "low"
    assert desktop_tool_risk_level("desktop.close_window") == "medium"
    assert desktop_tool_risk_level("desktop.click_ui_element") == "medium"
    assert desktop_tool_risk_level("desktop.type_into_ui_element") == "medium"
    assert desktop_tool_risk_level("desktop.submit_foreground") == "high"
    assert desktop_tool_risk_level("desktop.shortcut") == "medium"
    assert desktop_tool_risk_level("desktop.type") == "medium"
    assert desktop_tool_risk_level("desktop.type_text") == "medium"
    assert desktop_tool_risk_level("desktop.click") == "medium"
    assert desktop_tool_risk_level("desktop.reveal_path") == "low"
    assert desktop_tool_risk_level("desktop.open_path") == "low"
    assert desktop_tool_risk_level("desktop.open_path_with_app") == "low"
    assert desktop_tool_risk_level("app.open_path_with_app") == "low"
    assert desktop_tool_risk_level("media.apple_music_status") == "low"
    assert desktop_tool_risk_level("media.music_app_open_and_play") == "low"
    assert desktop_tool_risk_level("system.settings_open") == "low"
    assert desktop_tool_risk_level("system.volume") == "low"
    assert desktop_tool_risk_level("system.brightness") == "low"
    assert desktop_tool_risk_level("system.display_sleep") == "low"
    assert desktop_tool_risk_level("system.screen_saver_start") == "low"
    assert desktop_tool_risk_level("clipboard.write") == "low"
    assert desktop_tool_risk_level("notes.create") == "low"
    assert desktop_tool_risk_level("reminders.create") == "low"
    assert desktop_tool_risk_level("calendar.create_event") == "low"
    assert desktop_tool_risk_level("browser.open_url") == "low"
    assert desktop_tool_risk_level("browser.open_url_and_extract_text") == "low"
    assert desktop_tool_risk_level("browser.open_url_and_screenshot") == "low"
    assert desktop_tool_risk_level("browser.click") == "medium"
    assert desktop_tool_risk_level("terminal.run") is None
    assert desktop_tool_execution_mode("desktop.ui_elements").mode == (
        "read_only_observation"
    )
    inspect_default = desktop_tool_execution_mode_for_input(
        "desktop.inspect_app",
        {"app_name": "PixelForge"},
    )
    assert inspect_default.mode == "read_only_observation"
    assert inspect_default.foreground_control is False
    assert inspect_default.keyboard_mouse_capture is False
    assert inspect_default.sandbox_recommended is False
    inspect_foreground = desktop_tool_execution_mode_for_input(
        "desktop.inspect_app",
        {"app_name": "PixelForge", "open_if_needed": True, "focus": True},
    )
    assert inspect_foreground.mode == "supervised_live"
    assert inspect_foreground.foreground_control is True
    assert inspect_foreground.keyboard_mouse_capture is False
    assert inspect_foreground.sandbox_recommended is True
    assert desktop_tool_execution_mode("app.open").foreground_control is True
    assert desktop_tool_execution_mode("app.open").keyboard_mouse_capture is False
    assert desktop_tool_execution_mode("desktop.safe_type_text").mode == (
        "supervised_live"
    )
    assert desktop_tool_execution_mode("desktop.safe_type_text").keyboard_mouse_capture is True
    assert desktop_tool_execution_mode("desktop.safe_type_text").sandbox_recommended is True
    assert (
        desktop_tool_execution_mode("media.music_app_open_and_play").mode
        == "supervised_live"
    )
    assert (
        desktop_tool_execution_mode("media.music_app_open_and_play").keyboard_mouse_capture
        is False
    )
    assert desktop_tool_execution_mode("media.apple_music_status").mode == (
        "read_only_observation"
    )
    assert desktop_tool_execution_mode("browser.open_url").isolation == "browser_target"
    assert desktop_tool_execution_mode("terminal.run").approval_recommended is True
    assert desktop_action_risk_level("read_screen") == "low"
    assert desktop_action_risk_level("diagnose_permissions") == "low"
    assert desktop_action_risk_level("open_path") == "low"
    assert desktop_action_risk_level("create_note") == "low"
    assert desktop_action_risk_level("create_reminder") == "low"
    assert desktop_action_risk_level("create_calendar_event") == "low"
    assert desktop_action_risk_level("control_system_volume") == "low"
    assert desktop_action_risk_level("control_system_brightness") == "low"
    assert desktop_action_risk_level("control_display_sleep") == "low"
    assert desktop_action_risk_level("control_screen_saver") == "low"
    assert desktop_action_risk_level("write_clipboard") == "low"
    assert desktop_action_risk_level("show_app") == "low"
    assert desktop_action_risk_level("focus_app_window") == "low"
    assert desktop_action_risk_level("hide_app") == "low"
    assert desktop_action_risk_level("minimize_app") == "low"
    assert desktop_action_risk_level("foreground_hide_app") == "low"
    assert desktop_action_risk_level("foreground_minimize_window") == "low"
    assert desktop_action_risk_level("foreground_safe_shortcut") == "low"
    assert desktop_action_risk_level("foreground_safe_key") == "low"
    assert desktop_action_risk_level("foreground_safe_type_text") == "low"
    assert desktop_action_risk_level("foreground_search_submit") == "low"
    assert desktop_action_risk_level("foreground_safe_click") == "low"
    assert desktop_action_risk_level("foreground_safe_scroll") == "low"
    assert desktop_action_risk_level("quit_app") == "medium"
    assert desktop_action_risk_level("foreground_close_window") == "medium"
    assert desktop_action_risk_level("foreground_click_ui_element") == "medium"
    assert desktop_action_risk_level("foreground_type_into_ui_element") == "medium"
    assert desktop_action_risk_level("foreground_type_text") == "medium"
    assert desktop_action_risk_level("foreground_submit") == "high"
    assert desktop_action_risk_level("send_message") == "high"
    assert is_high_risk_desktop_action("raw_shell") is True
    assert is_high_risk_desktop_action("system_settings") is True
    assert is_high_risk_desktop_action("play_music") is False


def test_desktop_action_risk_catalog_covers_product_boundaries() -> None:
    catalog = {item.action_id: item for item in desktop_action_risk_snapshots()}

    assert list(catalog)[:32] == [
        "read_screen",
        "diagnose_permissions",
        "read_active_window",
        "read_running_apps",
        "discover_apps",
        "read_windows",
        "read_ui_elements",
        "read_app_status",
        "open_app",
        "focus_app",
        "focus_app_window",
        "show_app",
        "hide_app",
        "minimize_app",
        "quit_app",
        "reveal_path",
        "open_path",
        "open_path_with_app",
        "play_or_pause_media",
        "control_system_volume",
        "control_system_brightness",
        "control_display_sleep",
        "control_screen_saver",
        "write_clipboard",
        "create_note",
        "create_reminder",
        "create_calendar_event",
        "foreground_safe_shortcut",
        "foreground_safe_key",
        "foreground_safe_type_text",
        "foreground_search_submit",
        "foreground_safe_click",
    ]
    assert catalog["read_screen"].risk_level == "low"
    assert catalog["read_screen"].tools == ["screen.capture"]
    assert catalog["diagnose_permissions"].risk_level == "low"
    assert catalog["diagnose_permissions"].tools == ["desktop.permissions"]
    assert catalog["read_running_apps"].risk_level == "low"
    assert catalog["read_running_apps"].tools == ["desktop.running_apps"]
    assert catalog["discover_apps"].risk_level == "low"
    assert catalog["discover_apps"].tools == ["desktop.list_apps"]
    assert catalog["read_windows"].risk_level == "low"
    assert catalog["read_windows"].tools == ["desktop.list_windows", "desktop.windows"]
    assert catalog["read_ui_elements"].risk_level == "low"
    assert catalog["read_ui_elements"].tools == ["desktop.read_ui", "desktop.ui_elements"]
    assert catalog["read_ui_elements"].execution_mode is not None
    assert catalog["read_ui_elements"].execution_mode.mode == "read_only_observation"
    assert catalog["read_app_status"].risk_level == "low"
    assert catalog["read_app_status"].tools == ["app.status"]
    assert catalog["focus_app_window"].risk_level == "low"
    assert catalog["focus_app_window"].tools == ["app.focus_window"]
    assert catalog["show_app"].risk_level == "low"
    assert catalog["show_app"].tools == ["app.show"]
    assert catalog["hide_app"].risk_level == "low"
    assert catalog["hide_app"].tools == ["app.hide"]
    assert catalog["minimize_app"].risk_level == "low"
    assert catalog["minimize_app"].tools == ["app.minimize"]
    assert catalog["quit_app"].risk_level == "medium"
    assert catalog["quit_app"].tools == ["app.quit", "desktop.quit_app"]
    assert catalog["reveal_path"].risk_level == "low"
    assert catalog["reveal_path"].tools == ["desktop.reveal_path"]
    assert catalog["open_path"].risk_level == "low"
    assert catalog["open_path"].tools == ["desktop.open_path"]
    assert catalog["open_path_with_app"].risk_level == "low"
    assert catalog["open_path_with_app"].tools == [
        "desktop.open_path_with_app",
        "app.open_path_with_app",
    ]
    assert catalog["play_or_pause_media"].tools == [
        "media.apple_music_play",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
        "media.music_app_open_and_play",
        "media.music_app_control",
        "media.system_control",
    ]
    assert catalog["system_settings"].tools == ["system.settings_open"]
    assert catalog["control_system_volume"].tools == ["system.volume"]
    assert catalog["control_system_brightness"].tools == ["system.brightness"]
    assert catalog["control_display_sleep"].tools == ["system.display_sleep"]
    assert catalog["control_screen_saver"].tools == ["system.screen_saver_start"]
    assert catalog["write_clipboard"].tools == ["clipboard.write"]
    assert catalog["create_note"].tools == ["notes.create"]
    assert catalog["create_reminder"].tools == ["reminders.create"]
    assert catalog["create_calendar_event"].tools == ["calendar.create_event"]
    assert catalog["foreground_safe_shortcut"].risk_level == "low"
    assert catalog["foreground_safe_shortcut"].tools == ["desktop.safe_shortcut"]
    assert catalog["foreground_safe_key"].risk_level == "low"
    assert catalog["foreground_safe_key"].tools == ["desktop.safe_key"]
    assert catalog["foreground_safe_type_text"].risk_level == "low"
    assert catalog["foreground_safe_type_text"].tools == ["desktop.safe_type_text"]
    assert catalog["foreground_safe_type_text"].execution_mode is not None
    assert catalog["foreground_safe_type_text"].execution_mode.mode == "supervised_live"
    assert catalog["foreground_safe_type_text"].execution_mode.keyboard_mouse_capture is True
    assert catalog["foreground_safe_type_text"].execution_mode.sandbox_recommended is True
    assert catalog["foreground_search_submit"].risk_level == "low"
    assert catalog["foreground_search_submit"].tools == ["desktop.search_submit"]
    assert catalog["foreground_search_submit"].requires_approval is False
    assert catalog["foreground_safe_click"].risk_level == "low"
    assert catalog["foreground_safe_click"].tools == ["desktop.safe_click"]
    assert catalog["foreground_safe_scroll"].risk_level == "low"
    assert catalog["foreground_safe_scroll"].tools == ["desktop.safe_scroll"]
    assert catalog["foreground_hide_app"].risk_level == "low"
    assert catalog["foreground_hide_app"].tools == ["desktop.hide_app"]
    assert catalog["foreground_minimize_window"].risk_level == "low"
    assert catalog["foreground_minimize_window"].tools == ["desktop.minimize_window"]
    assert catalog["foreground_click_ui_element"].risk_level == "medium"
    assert catalog["foreground_click_ui_element"].tools == [
        "desktop.click_ui_element",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
    ]
    assert catalog["foreground_type_into_ui_element"].risk_level == "medium"
    assert catalog["foreground_type_into_ui_element"].tools == [
        "desktop.type_into_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
    ]
    assert catalog["foreground_click"].risk_level == "medium"
    assert catalog["foreground_click"].requires_approval is False
    assert catalog["foreground_close_window"].risk_level == "medium"
    assert catalog["foreground_close_window"].tools == ["desktop.close_window"]
    assert catalog["foreground_submit"].risk_level == "high"
    assert catalog["foreground_submit"].requires_approval is True
    assert catalog["foreground_submit"].tools == ["desktop.submit_foreground"]
    assert catalog["delete_or_overwrite_user_file"].risk_level == "high"
    assert catalog["delete_or_overwrite_user_file"].requires_approval is True
    assert catalog["credential_access"].requires_approval is True


def test_main_chat_desktop_prompt_separates_search_submit_from_foreground_send() -> None:
    instructions = MAIN_CHAT_DESKTOP_AGENT_INSTRUCTIONS

    assert "desktop.search_submit" in instructions
    assert "提交搜索/查找 query 用 desktop.search_submit" in instructions
    assert "发送消息、提交表单、确认破坏性或外部动作必须用 desktop.submit_foreground" in instructions


def test_tool_catalog_snapshot_json_shape_is_stable() -> None:
    snapshot = ToolCatalogSnapshot(
        tools=[
            ToolCatalogItemSnapshot(
                tool_name="media.apple_music_play",
                function_name="media_apple_music_play",
                description="Play music",
                capability_id="media_control",
                risk_level="low",
                approval_required=False,
                input_schema={"type": "object"},
                model_tool_schema={"type": "function"},
                missing_permissions=["music_app"],
                blocking_conditions=["desktop_session_locked"],
                fallback_notes=["Open Music when direct playback is unavailable."],
                diagnostic_route="/ui/native-agent/diagnostics/cache",
            )
        ],
        capabilities={
            "media_control": DesktopExecutionCapabilitySnapshot(
                available=False,
                platform="macos",
                missing_permissions=["music_app"],
                tools=["media.apple_music_play"],
                risk_default="low",
                diagnostic_route="/ui/native-agent/diagnostics/cache",
            )
        },
        plugins=[
            RestrictedToolPluginSnapshot(
                plugin_id="notes",
                enabled=False,
                tool_names=["plugin.notes.echo"],
                tools=[
                    RestrictedPluginToolSnapshot(
                        tool_name="plugin.notes.echo",
                        tool_id="echo",
                        function_name="plugin_notes_echo",
                        risk_level="medium",
                    )
                ],
                skill_docs="Use echo for notes.",
            )
        ],
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "tools",
        "capabilities",
        "sandbox_provider",
        "controlled_provider_diagnostics",
        "plugins",
        "legacy_cleanup_coverage",
        "source",
    ]
    assert payload["tools"][0]["tool_name"] == "media.apple_music_play"
    assert payload["tools"][0]["input_schema"] == {"type": "object"}
    assert payload["tools"][0]["blocking_conditions"] == ["desktop_session_locked"]
    assert payload["tools"][0]["fallback_notes"] == [
        "Open Music when direct playback is unavailable."
    ]
    assert payload["plugins"][0]["plugin_id"] == "notes"
    assert payload["plugins"][0]["enabled"] is False
    assert payload["plugins"][0]["tools"][0]["risk_level"] == "medium"
    assert payload["sandbox_provider"] is None
    assert payload["legacy_cleanup_coverage"] is None
    with pytest.raises(ValidationError):
        ToolCatalogItemSnapshot(
            tool_name="terminal.run",
            function_name="terminal_run",
            unknown=True,
        )
    with pytest.raises(ValidationError):
        RestrictedToolPluginSnapshot(plugin_id="notes", unknown=True)

    install_request = _json(
        InstallRestrictedToolPluginRequest(plugin_id="notes", enabled=False)
    )
    update_request = _json(UpdateRestrictedToolPluginRequest(enabled=True))
    assert install_request == {"plugin_id": "notes", "enabled": False}
    assert update_request == {"enabled": True}
    with pytest.raises(ValidationError):
        InstallRestrictedToolPluginRequest(plugin_id="notes", unknown=True)
    with pytest.raises(ValidationError):
        UpdateRestrictedToolPluginRequest(enabled=True, unknown=True)


def test_runtime_tool_catalog_surfaces_desktop_risk_schema_and_fallbacks() -> None:
    catalog = runtime_tool_catalog_snapshot(
        platform_name="Darwin",
        missing_permissions={
            "media_control": ["music_app"],
            "browser_control": ["chrome_cdp"],
        },
        blocking_conditions={
            "foreground_input": ["desktop_session_locked"],
        },
    )
    tools = {tool.tool_name: tool for tool in catalog.tools}

    music = tools["media.apple_music_play"]
    music_status = tools["media.apple_music_status"]
    music_app = tools["media.music_app_open_and_play"]
    settings = tools["system.settings_open"]
    brightness = tools["system.brightness"]
    display_sleep = tools["system.display_sleep"]
    screen_saver = tools["system.screen_saver_start"]
    permissions = tools["desktop.permissions"]
    list_apps = tools["desktop.list_apps"]
    open_app_alias = tools["desktop.open_app"]
    focus_app_alias = tools["desktop.focus_app"]
    list_windows_alias = tools["desktop.list_windows"]
    read_ui_alias = tools["desktop.read_ui"]
    verify_alias = tools["desktop.verify"]
    quit_app = tools["app.quit"]
    named_show_app = tools["app.show"]
    named_focus_window = tools["app.focus_window"]
    open_safe_type_text = tools["app.open_and_safe_type_text"]
    focus_safe_shortcut = tools["app.focus_and_safe_shortcut"]
    open_safe_key = tools["app.open_and_safe_key"]
    focus_safe_key = tools["app.focus_and_safe_key"]
    open_hotkey = tools["app.open_and_hotkey"]
    focus_hotkey = tools["app.focus_and_hotkey"]
    open_safe_scroll = tools["app.open_and_safe_scroll"]
    focus_safe_scroll = tools["app.focus_and_safe_scroll"]
    open_safe_click = tools["app.open_and_safe_click"]
    focus_safe_click = tools["app.focus_and_safe_click"]
    open_click_ui_element = tools["app.open_and_click_ui_element"]
    focus_click_ui_element = tools["app.focus_and_click_ui_element"]
    open_type_into_ui_element = tools["app.open_and_type_into_ui_element"]
    focus_type_into_ui_element = tools["app.focus_and_type_into_ui_element"]
    named_hide_app = tools["app.hide"]
    named_minimize_app = tools["app.minimize"]
    note_tool = tools["notes.create"]
    reminder = tools["reminders.create"]
    calendar_event = tools["calendar.create_event"]
    hide_app = tools["desktop.hide_app"]
    safe_shortcut = tools["desktop.safe_shortcut"]
    safe_key = tools["desktop.safe_key"]
    safe_type_text = tools["desktop.safe_type_text"]
    search_submit = tools["desktop.search_submit"]
    safe_click = tools["desktop.safe_click"]
    safe_scroll = tools["desktop.safe_scroll"]
    shortcut_alias = tools["desktop.shortcut"]
    type_alias = tools["desktop.type"]
    click_ui_element = tools["desktop.click_ui_element"]
    type_into_ui_element = tools["desktop.type_into_ui_element"]
    minimize_window = tools["desktop.minimize_window"]
    close_window = tools["desktop.close_window"]
    browser_search = tools["browser.search"]
    browser_open = tools["browser.open"]
    browser = tools["browser.open_url"]
    browser_extract = tools["browser.extract"]
    browser_open_extract = tools["browser.open_url_and_extract_text"]
    browser_open_screenshot = tools["browser.open_url_and_screenshot"]
    fs_find_files = tools["fs.find_files"]
    fs_read_file = tools["fs.read_file"]
    fs_move_file = tools["fs.move_file"]
    python_run = tools["python.run"]
    terminal = tools["terminal.run"]

    assert music.capability_id == "media_control"
    assert music.risk_level == "low"
    assert music.input_schema["required"] == ["query"]
    assert music.missing_permissions == ["music_app"]
    assert any("Music" in note for note in music.fallback_notes)
    assert music_status.capability_id == "media_control"
    assert music_status.risk_level == "low"
    assert music_status.input_schema["required"] == []
    assert music_status.missing_permissions == ["music_app"]
    assert any("without changing playback" in note for note in music_status.fallback_notes)
    assert music_app.capability_id == "media_control"
    assert music_app.risk_level == "low"
    assert music_app.input_schema["required"] == ["app_name"]
    assert music_app.missing_permissions == []
    assert any("media play key" in note for note in music_app.fallback_notes)
    assert settings.capability_id == "app_control"
    assert settings.risk_level == "low"
    assert settings.input_schema["required"] == ["target"]
    assert settings.missing_permissions == []
    assert any("does not change settings" in note for note in settings.fallback_notes)
    assert brightness.capability_id == "desktop_execution"
    assert brightness.risk_level == "low"
    assert brightness.input_schema["required"] == ["action"]
    assert any("brightness key events" in note for note in brightness.fallback_notes)
    assert display_sleep.capability_id == "desktop_execution"
    assert display_sleep.risk_level == "low"
    assert display_sleep.input_schema["required"] == []
    assert any("displaysleepnow" in note for note in display_sleep.fallback_notes)
    assert screen_saver.capability_id == "desktop_execution"
    assert screen_saver.risk_level == "low"
    assert screen_saver.input_schema["required"] == []
    assert any("ScreenSaverEngine" in note for note in screen_saver.fallback_notes)
    assert permissions.capability_id == "desktop_execution"
    assert permissions.risk_level == "low"
    assert any("missing desktop permission" in note for note in permissions.fallback_notes)
    assert list_apps.capability_id == "desktop_execution"
    assert list_apps.risk_level == "low"
    assert list_apps.input_schema["required"] == []
    assert any("without opening" in note for note in list_apps.fallback_notes)
    assert open_app_alias.capability_id == "app_control"
    assert open_app_alias.risk_level == "low"
    assert open_app_alias.input_schema["required"] == ["app_name"]
    assert any("alias for app.open" in note for note in open_app_alias.fallback_notes)
    assert focus_app_alias.capability_id == "app_control"
    assert focus_app_alias.risk_level == "low"
    assert focus_app_alias.input_schema["required"] == ["app_name"]
    assert any("alias for app.focus" in note for note in focus_app_alias.fallback_notes)
    assert list_windows_alias.capability_id == "active_window"
    assert list_windows_alias.risk_level == "low"
    assert any("alias for desktop.windows" in note for note in list_windows_alias.fallback_notes)
    assert read_ui_alias.capability_id == "active_window"
    assert read_ui_alias.risk_level == "low"
    assert read_ui_alias.execution_mode is not None
    assert read_ui_alias.execution_mode.mode == "read_only_observation"
    assert any("alias for desktop.ui_elements" in note for note in read_ui_alias.fallback_notes)
    assert verify_alias.capability_id == "active_window"
    assert verify_alias.risk_level == "low"
    assert verify_alias.input_schema["required"] == []
    assert any("Read-only post-operation verification" in note for note in verify_alias.fallback_notes)
    assert quit_app.capability_id == "app_control"
    assert quit_app.risk_level == "medium"
    assert quit_app.approval_required is False
    assert quit_app.input_schema["required"] == ["app_name"]
    assert any("approval" in note for note in quit_app.fallback_notes)
    foreground_quit_app = tools["desktop.quit_app"]
    assert foreground_quit_app.capability_id == "app_control"
    assert foreground_quit_app.risk_level == "medium"
    assert foreground_quit_app.input_schema["properties"] == {}
    assert any("foreground app" in note for note in foreground_quit_app.fallback_notes)
    assert named_show_app.capability_id == "app_control"
    assert named_show_app.risk_level == "low"
    assert named_show_app.input_schema["required"] == ["app_name"]
    assert any("show, unhide, restore" in note for note in named_show_app.fallback_notes)
    assert named_focus_window.capability_id == "app_control"
    assert named_focus_window.risk_level == "low"
    assert named_focus_window.input_schema["required"] == ["app_name", "title_contains"]
    assert any(
        "matching app window" in note for note in named_focus_window.fallback_notes
    )
    assert note_tool.capability_id == "app_control"
    assert note_tool.risk_level == "low"
    assert note_tool.input_schema["required"] == ["body"]
    assert any("Notes" in note for note in note_tool.fallback_notes)
    assert reminder.capability_id == "app_control"
    assert reminder.risk_level == "low"
    assert reminder.input_schema["required"] == ["title"]
    assert any("Reminders" in note for note in reminder.fallback_notes)
    assert calendar_event.capability_id == "app_control"
    assert calendar_event.risk_level == "low"
    assert calendar_event.input_schema["required"] == ["title", "start_at"]
    assert any("Calendar" in note for note in calendar_event.fallback_notes)
    assert open_safe_type_text.capability_id == "foreground_input"
    assert open_safe_type_text.risk_level == "low"
    assert open_safe_type_text.input_schema["required"] == ["app_name", "text"]
    assert any("typing only text explicitly provided" in note for note in open_safe_type_text.fallback_notes)
    assert focus_safe_shortcut.capability_id == "foreground_input"
    assert focus_safe_shortcut.risk_level == "low"
    assert focus_safe_shortcut.input_schema["required"] == ["app_name", "action"]
    assert "paste" in focus_safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "new_document" in focus_safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "new_message" in focus_safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "new_event" in focus_safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "finder_get_info" in focus_safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert any("whitelisted safe shortcuts" in note for note in focus_safe_shortcut.fallback_notes)
    assert open_safe_key.capability_id == "foreground_input"
    assert open_safe_key.risk_level == "low"
    assert open_safe_key.input_schema["required"] == ["app_name", "action"]
    assert "tab" in open_safe_key.input_schema["properties"]["action"]["enum"]
    assert "shift_tab" in open_safe_key.input_schema["properties"]["action"]["enum"]
    assert "return" not in open_safe_key.input_schema["properties"]["action"]["enum"]
    assert any("whitelisted foreground navigation keys" in note for note in open_safe_key.fallback_notes)
    assert focus_safe_key.capability_id == "foreground_input"
    assert focus_safe_key.risk_level == "low"
    assert focus_safe_key.input_schema["required"] == ["app_name", "action"]
    assert "arrow_down" in focus_safe_key.input_schema["properties"]["action"]["enum"]
    assert "shift_tab" in focus_safe_key.input_schema["properties"]["action"]["enum"]
    assert open_hotkey.capability_id == "foreground_input"
    assert open_hotkey.risk_level == "medium"
    assert open_hotkey.input_schema["required"] == ["app_name", "key"]
    assert "command" in open_hotkey.input_schema["properties"]["modifiers"]["items"]["enum"]
    assert any("approval is required" in note for note in open_hotkey.fallback_notes)
    assert focus_hotkey.capability_id == "foreground_input"
    assert focus_hotkey.risk_level == "medium"
    assert focus_hotkey.input_schema["required"] == ["app_name", "key"]
    assert open_safe_scroll.capability_id == "foreground_input"
    assert open_safe_scroll.risk_level == "low"
    assert open_safe_scroll.input_schema["required"] == ["app_name", "direction"]
    assert open_safe_scroll.input_schema["properties"]["direction"]["enum"] == ["up", "down"]
    assert any("explicit foreground up/down page requests" in note for note in open_safe_scroll.fallback_notes)
    assert focus_safe_scroll.capability_id == "foreground_input"
    assert focus_safe_scroll.risk_level == "low"
    assert focus_safe_scroll.input_schema["required"] == ["app_name", "direction"]
    assert focus_safe_scroll.input_schema["properties"]["direction"]["enum"] == ["up", "down"]
    assert open_safe_click.capability_id == "foreground_input"
    assert open_safe_click.risk_level == "low"
    assert open_safe_click.input_schema["required"] == ["app_name", "x", "y"]
    assert open_safe_click.input_schema["properties"]["x"]["type"] == "number"
    assert any("coordinates explicitly provided by the user" in note for note in open_safe_click.fallback_notes)
    assert focus_safe_click.capability_id == "foreground_input"
    assert focus_safe_click.risk_level == "low"
    assert focus_safe_click.input_schema["required"] == ["app_name", "x", "y"]
    assert focus_safe_click.input_schema["properties"]["y"]["type"] == "number"
    assert open_click_ui_element.capability_id == "foreground_input"
    assert open_click_ui_element.risk_level == "medium"
    assert open_click_ui_element.input_schema["required"] == ["app_name", "target"]
    assert open_click_ui_element.input_schema["properties"]["target"]["type"] == "string"
    assert any("approval is required" in note for note in open_click_ui_element.fallback_notes)
    assert focus_click_ui_element.capability_id == "foreground_input"
    assert focus_click_ui_element.risk_level == "medium"
    assert focus_click_ui_element.input_schema["required"] == ["app_name", "target"]
    assert focus_click_ui_element.input_schema["properties"]["click_count"]["maximum"] == 3
    assert open_type_into_ui_element.capability_id == "foreground_input"
    assert open_type_into_ui_element.risk_level == "medium"
    assert open_type_into_ui_element.input_schema["required"] == ["app_name", "target", "text"]
    assert open_type_into_ui_element.input_schema["properties"]["target"]["type"] == "string"
    assert any("approval is required" in note for note in open_type_into_ui_element.fallback_notes)
    assert focus_type_into_ui_element.capability_id == "foreground_input"
    assert focus_type_into_ui_element.risk_level == "medium"
    assert focus_type_into_ui_element.input_schema["required"] == ["app_name", "target", "text"]
    assert named_hide_app.capability_id == "app_control"
    assert named_hide_app.risk_level == "low"
    assert named_hide_app.input_schema["required"] == ["app_name"]
    assert any("hides a running app" in note for note in named_hide_app.fallback_notes)
    assert named_minimize_app.capability_id == "app_control"
    assert named_minimize_app.risk_level == "low"
    assert named_minimize_app.input_schema["required"] == ["app_name"]
    assert any("minimizes windows for a running app" in note for note in named_minimize_app.fallback_notes)
    assert hide_app.capability_id == "foreground_input"
    assert hide_app.risk_level == "low"
    assert hide_app.input_schema["properties"] == {}
    assert any("hides the current foreground app" in note for note in hide_app.fallback_notes)
    assert safe_shortcut.capability_id == "foreground_input"
    assert safe_shortcut.risk_level == "low"
    assert safe_shortcut.input_schema["required"] == ["action"]
    assert "copy" in safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "new_document" in safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "new_event" in safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "screenshot_selection" in safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert "screenshot_toolbar" in safe_shortcut.input_schema["properties"]["action"]["enum"]
    assert any("whitelisted common shortcut" in note for note in safe_shortcut.fallback_notes)
    assert safe_key.capability_id == "foreground_input"
    assert safe_key.risk_level == "low"
    assert safe_key.input_schema["required"] == ["action"]
    assert "tab" in safe_key.input_schema["properties"]["action"]["enum"]
    assert "shift_tab" in safe_key.input_schema["properties"]["action"]["enum"]
    assert "return" not in safe_key.input_schema["properties"]["action"]["enum"]
    assert any("whitelisted foreground navigation keys" in note for note in safe_key.fallback_notes)
    assert safe_type_text.capability_id == "foreground_input"
    assert safe_type_text.risk_level == "low"
    assert safe_type_text.execution_mode is not None
    assert safe_type_text.execution_mode.mode == "supervised_live"
    assert safe_type_text.execution_mode.keyboard_mouse_capture is True
    assert safe_type_text.execution_mode.sandbox_recommended is True
    assert safe_type_text.input_schema["required"] == ["text"]
    assert safe_type_text.blocking_conditions == ["desktop_session_locked"]
    assert any("explicitly provided by the user" in note for note in safe_type_text.fallback_notes)
    assert search_submit.capability_id == "foreground_input"
    assert search_submit.risk_level == "low"
    assert search_submit.approval_required is False
    assert search_submit.input_schema["properties"] == {}
    assert any("search/find query" in note for note in search_submit.fallback_notes)
    assert safe_click.capability_id == "foreground_input"
    assert safe_click.risk_level == "low"
    assert safe_click.input_schema["required"] == ["x", "y"]
    assert any("coordinates explicitly provided by the user" in note for note in safe_click.fallback_notes)
    assert safe_scroll.capability_id == "foreground_input"
    assert safe_scroll.risk_level == "low"
    assert safe_scroll.input_schema["required"] == ["direction"]
    assert safe_scroll.input_schema["properties"]["direction"]["enum"] == ["up", "down"]
    assert any("scrolls only explicit foreground up/down page requests" in note for note in safe_scroll.fallback_notes)
    assert shortcut_alias.capability_id == "foreground_input"
    assert shortcut_alias.risk_level == "medium"
    assert shortcut_alias.approval_required is False
    assert shortcut_alias.input_schema["required"] == ["key"]
    assert any("alias for desktop.hotkey" in note for note in shortcut_alias.fallback_notes)
    assert type_alias.capability_id == "foreground_input"
    assert type_alias.risk_level == "medium"
    assert type_alias.approval_required is False
    assert type_alias.input_schema["required"] == ["text"]
    assert any("alias for desktop.type_text" in note for note in type_alias.fallback_notes)
    assert click_ui_element.capability_id == "foreground_input"
    assert click_ui_element.risk_level == "medium"
    assert click_ui_element.input_schema["required"] == ["target"]
    assert any("inferred coordinate" in note for note in click_ui_element.fallback_notes)
    assert type_into_ui_element.capability_id == "foreground_input"
    assert type_into_ui_element.risk_level == "medium"
    assert type_into_ui_element.input_schema["required"] == ["target", "text"]
    assert any("types user-provided text" in note for note in type_into_ui_element.fallback_notes)
    assert minimize_window.capability_id == "foreground_input"
    assert minimize_window.risk_level == "low"
    assert minimize_window.input_schema["properties"] == {}
    assert any("minimizes the current foreground window" in note for note in minimize_window.fallback_notes)
    assert close_window.capability_id == "foreground_input"
    assert close_window.risk_level == "medium"
    assert close_window.input_schema["properties"] == {}
    assert any("foreground window" in note for note in close_window.fallback_notes)
    submit_foreground = tools["desktop.submit_foreground"]
    assert submit_foreground.capability_id == "foreground_input"
    assert submit_foreground.risk_level == "high"
    assert submit_foreground.approval_required is True
    assert submit_foreground.input_schema["required"] == ["action"]
    assert submit_foreground.input_schema["properties"]["action"]["enum"] == [
        "send",
        "submit",
        "confirm",
    ]
    assert any("Always requires approval" in note for note in submit_foreground.fallback_notes)
    assert browser.capability_id == "browser_control"
    assert browser.risk_level == "low"
    assert browser.missing_permissions == ["chrome_cdp"]
    assert any("Chrome CDP" in note for note in browser.fallback_notes)
    assert browser_search.capability_id == "browser_control"
    assert browser_search.risk_level == "low"
    assert browser_search.input_schema["required"] == ["query"]
    assert any("Portable alias" in note for note in browser_search.fallback_notes)
    assert browser_open.capability_id == "browser_control"
    assert browser_open.risk_level == "low"
    assert browser_open.input_schema["required"] == ["url"]
    assert any("browser.open_url" in note for note in browser_open.fallback_notes)
    assert browser_extract.capability_id == "browser_control"
    assert browser_extract.risk_level == "low"
    assert browser_extract.input_schema["required"] == []
    assert any("browser.extract_text" in note for note in browser_extract.fallback_notes)
    assert browser_open_extract.capability_id == "browser_control"
    assert browser_open_extract.risk_level == "low"
    assert browser_open_extract.input_schema["required"] == ["url"]
    assert any("text extraction" in note for note in browser_open_extract.fallback_notes)
    assert browser_open_screenshot.capability_id == "browser_control"
    assert browser_open_screenshot.risk_level == "low"
    assert browser_open_screenshot.input_schema["required"] == ["url"]
    assert any("captures the page" in note for note in browser_open_screenshot.fallback_notes)
    assert fs_find_files.capability_id == "workspace"
    assert fs_find_files.risk_level == "low"
    assert fs_find_files.input_schema["required"] == []
    assert any("workspace.list" in note for note in fs_find_files.fallback_notes)
    assert fs_read_file.capability_id == "workspace"
    assert fs_read_file.risk_level == "low"
    assert fs_read_file.input_schema["required"] == ["path"]
    assert any("workspace.read" in note for note in fs_read_file.fallback_notes)
    assert fs_move_file.capability_id == "file.organization"
    assert fs_move_file.risk_level == "high"
    assert fs_move_file.approval_required is True
    assert fs_move_file.input_schema["required"] == ["path", "operation"]
    assert any("file.organize" in note for note in fs_move_file.fallback_notes)
    assert python_run.capability_id == "terminal"
    assert python_run.risk_level == "high"
    assert python_run.approval_required is True
    assert python_run.input_schema["required"] == []
    assert any("terminal.run" in note for note in python_run.fallback_notes)
    assert terminal.risk_level == "high"
    assert terminal.approval_required is True


def test_runtime_tool_catalog_surfaces_sandbox_provider_capabilities() -> None:
    catalog = runtime_tool_catalog_snapshot(
        sandbox_provider={
            "available": True,
            "provider_id": "sandbox-1",
            "provider_kind": "sandbox_desktop",
            "adapter_ready": True,
            "supported_tools": [
                "desktop.list_apps",
                "app.focus_and_click_ui_element",
            ],
        }
    )
    tools = {tool.tool_name: tool for tool in catalog.tools}

    assert catalog.sandbox_provider is not None
    assert catalog.sandbox_provider.provider_id == "sandbox-1"
    assert catalog.sandbox_provider.supported_tools == [
        "desktop.list_apps",
        "app.focus_and_click_ui_element",
    ]
    assert tools["desktop.list_apps"].provider_supported is True
    assert tools["desktop.list_apps"].provider_ready is True
    assert tools["desktop.list_apps"].provider_id == "sandbox-1"
    assert tools["desktop.list_apps"].provider_kind == "sandbox_desktop"
    assert tools["desktop.list_apps"].requires_user_foreground_session is False
    assert tools["desktop.list_apps"].user_foreground_takeover_risk is False
    assert any(
        "Sandbox desktop provider can execute" in note
        for note in tools["desktop.list_apps"].fallback_notes
    )
    assert tools["desktop.submit_foreground"].provider_supported is False
    assert "desktop.list_apps" in (
        catalog.capabilities["desktop_execution"].provider_supported_tools
    )
    assert "app.focus_and_click_ui_element" in (
        catalog.capabilities["desktop_execution"].provider_supported_tools
    )
    assert catalog.capabilities["desktop_execution"].provider_ready_tools == (
        catalog.capabilities["desktop_execution"].provider_supported_tools
    )
    assert catalog.capabilities["foreground_activation"].provider_supported_tools == [
        "app.focus_and_click_ui_element"
    ]


def test_runtime_tool_catalog_surfaces_controlled_provider_diagnostics() -> None:
    diagnostics = ControlledDesktopProviderDiagnosticSnapshot(
        ready=False,
        configured=False,
        status="isolated_provider_required",
        provider_id="local-isolated-desktop",
        blocking_conditions=["isolated_desktop_provider_required"],
        launch_command=[
            "python",
            "scripts/run_isolated_desktop_provider.py",
            "--host",
            "127.0.0.1",
            "--port",
            "19093",
        ],
        smoke_command=[
            "python",
            "scripts/smoke_isolated_desktop_provider.py",
        ],
        env={
            "OHA_YACHIYO_DESKTOP_PROVIDER_URL": "http://127.0.0.1:19093",
        },
        session_manager={
            "source": "isolated_provider_session_manager",
            "status": "stopped",
            "running": False,
        },
    )

    catalog = runtime_tool_catalog_snapshot(
        controlled_provider_diagnostics=diagnostics
    )
    payload = catalog.model_dump(mode="json")

    assert catalog.controlled_provider_diagnostics is not None
    assert (
        payload["controlled_provider_diagnostics"]["status"]
        == "isolated_provider_required"
    )
    assert payload["controlled_provider_diagnostics"]["launch_command"] == [
        "python",
        "scripts/run_isolated_desktop_provider.py",
        "--host",
        "127.0.0.1",
        "--port",
        "19093",
    ]
    assert payload["controlled_provider_diagnostics"]["session_manager"] == {
        "source": "isolated_provider_session_manager",
        "status": "stopped",
        "running": False,
    }


def test_controlled_provider_diagnostics_marks_configured_keyboard_provider_ready(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
        "http://127.0.0.1:19092",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_ID",
        "local-controlled-desktop",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS",
        ",".join(OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS),
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_CAPABILITIES",
        "virtual_desktop,idempotent_tool_requests",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_MUTATION_SUPPORTED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND",
        "isolated_desktop",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED",
        "false",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_KIND",
        "virtual_desktop_backend",
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_IS_LOOPBACK", "false")
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_READY_FOR_PUBLIC_RELEASE",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_REQUIRES_REAL_VIRTUAL_DESKTOP_BACKEND",
        "false",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_TOKEN",
        "release-provider-token",
    )
    provider = desktop_execution_provider_status_from_env(probe_health=False)

    diagnostics = controlled_desktop_provider_diagnostics_snapshot(
        sandbox_provider=provider
    )

    assert diagnostics.ready is True
    assert diagnostics.configured is True
    assert diagnostics.status == "ready"
    assert diagnostics.provider_id == "local-controlled-desktop"
    assert diagnostics.keyboard_mouse_capture_supported is True
    assert diagnostics.foreground_mutation_supported is True
    assert diagnostics.desktop_session_kind == "isolated_desktop"
    assert diagnostics.desktop_session_isolated is True
    assert diagnostics.foreground_takeover_required is False
    assert diagnostics.release_ready is True
    assert diagnostics.desktop_backend_kind == "virtual_desktop_backend"
    assert diagnostics.desktop_backend_is_loopback is False
    assert diagnostics.desktop_backend_ready_for_public_release is True
    assert diagnostics.requires_real_virtual_desktop_backend is False
    assert diagnostics.provider_contract["ok"] is True
    assert diagnostics.provider_conformance is not None
    assert diagnostics.provider_conformance.ok is True
    assert diagnostics.provider_conformance.mode == "provider_diagnostics_contract_check"
    assert diagnostics.provider_conformance.runtime_checked is True
    assert diagnostics.provider_conformance.public_release_ready is True
    assert diagnostics.provider_conformance.release_candidate is True
    assert diagnostics.provider_conformance.covered_tools == list(
        OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS
    )
    assert diagnostics.public_release_readiness.ready is True
    assert diagnostics.public_release_readiness.blocking_conditions == []
    assert [
        action.id for action in diagnostics.public_release_readiness.next_actions
    ] == ["run_isolated_provider_smoke", "run_public_release_smoke"]
    assert diagnostics.blocking_conditions == []
    assert diagnostics.endpoint_origin == "http://127.0.0.1:19092"
    assert "desktop.safe_type_text" in diagnostics.supported_tools


def test_controlled_provider_diagnostics_reads_provider_manifest_before_start(
    monkeypatch,
    tmp_path,
) -> None:
    for key in list(os.environ):
        if key.startswith(
            (
                "OHA_YACHIYO_DESKTOP_PROVIDER_",
                "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_",
            )
        ):
            monkeypatch.delenv(key, raising=False)
    for key in isolated_session_module._ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    manifest_path = tmp_path / "release-provider-manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider_id": "release-virtual-desktop",
                "provider_kind": "sandbox_desktop",
                "endpoint_urls": {
                    "status": "http://127.0.0.1:29097/status",
                    "execute": "http://127.0.0.1:29097/tools/execute",
                },
                "supported_tools": list(OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS),
                "capabilities": [
                    "virtual_desktop",
                    "idempotent_tool_requests",
                ],
                "keyboard_mouse_capture_supported": True,
                "foreground_mutation_supported": True,
                "desktop_session_kind": "virtual_desktop",
                "desktop_session_isolated": True,
                "foreground_takeover_required": False,
                "desktop_backend_kind": "vnc_virtual_desktop",
                "desktop_backend_is_loopback": False,
                "desktop_backend_ready_for_public_release": True,
                "requires_real_virtual_desktop_backend": False,
                "authentication": {
                    "token_env": "TEST_RELEASE_PROVIDER_TOKEN",
                },
                "entrypoint": {
                    "command": "python provider.py --host 127.0.0.1 --port 0",
                },
                "smoke_command": [
                    "python",
                    "scripts/smoke_isolated_desktop_provider.py",
                    "--use-configured-provider",
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST", str(manifest_path))
    monkeypatch.setenv("TEST_RELEASE_PROVIDER_TOKEN", "release-provider-token")

    diagnostics = controlled_desktop_provider_diagnostics_snapshot()

    assert "OHA_YACHIYO_DESKTOP_PROVIDER_URL" not in os.environ
    assert "OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL" not in os.environ
    assert diagnostics.configured is True
    assert diagnostics.source == "provider_manifest"
    assert diagnostics.status == "virtual_desktop_provider_contract_required"
    assert diagnostics.provider_id == "release-virtual-desktop"
    assert diagnostics.keyboard_mouse_capture_supported is True
    assert diagnostics.desktop_session_kind == "virtual_desktop"
    assert diagnostics.desktop_session_isolated is True
    assert diagnostics.foreground_takeover_required is False
    assert diagnostics.desktop_backend_kind == "vnc_virtual_desktop"
    assert diagnostics.desktop_backend_is_loopback is False
    assert diagnostics.desktop_backend_ready_for_public_release is True
    assert diagnostics.requires_real_virtual_desktop_backend is False
    assert diagnostics.provider_contract["blocking_conditions"] == [
        "desktop_execution_provider_unavailable",
        "desktop_execution_provider_adapter_unavailable",
    ]
    assert diagnostics.provider_conformance is not None
    assert diagnostics.provider_conformance.public_release_ready is False
    assert diagnostics.provider_conformance.release_candidate is False
    assert diagnostics.provider_conformance.release_blocking_conditions == [
        "desktop_execution_provider_unavailable",
        "desktop_execution_provider_adapter_unavailable",
    ]
    assert "desktop_execution_provider_unreachable" in diagnostics.blocking_conditions
    assert diagnostics.launch_command == [
        "python",
        "provider.py",
        "--host",
        "127.0.0.1",
        "--port",
        "0",
    ]
    assert diagnostics.endpoint_origin == "http://127.0.0.1:29097"
    assert diagnostics.smoke_command == [
        "python",
        "scripts/smoke_isolated_desktop_provider.py",
        "--use-configured-provider",
    ]


def test_controlled_provider_diagnostics_blocks_loopback_release_backend(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
        "http://127.0.0.1:19092",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_ID",
        "local-controlled-desktop",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS",
        ",".join(OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS),
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_MUTATION_SUPPORTED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND",
        "isolated_desktop",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED",
        "true",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED",
        "false",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_KIND",
        "loopback-dev-provider",
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_IS_LOOPBACK", "true")
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_READY_FOR_PUBLIC_RELEASE",
        "false",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_REQUIRES_REAL_VIRTUAL_DESKTOP_BACKEND",
        "true",
    )
    provider = desktop_execution_provider_status_from_env(probe_health=False)

    diagnostics = controlled_desktop_provider_diagnostics_snapshot(
        sandbox_provider=provider
    )

    assert diagnostics.ready is False
    assert diagnostics.release_ready is False
    assert diagnostics.configured is True
    assert diagnostics.status == "virtual_desktop_provider_contract_required"
    assert diagnostics.reason.startswith("Configured provider is not release-ready")
    assert diagnostics.provider_contract["ok"] is False
    assert diagnostics.provider_conformance is not None
    assert diagnostics.provider_conformance.public_release_ready is False
    assert "loopback_desktop_backend" in (
        diagnostics.provider_conformance.release_blocking_conditions
    )
    assert "loopback_desktop_backend" in diagnostics.blocking_conditions
    assert "desktop_backend_not_release_ready" in diagnostics.blocking_conditions
    assert "real_virtual_desktop_backend_required" in diagnostics.blocking_conditions
    assert diagnostics.public_release_readiness.ready is False
    assert "loopback_desktop_backend" in (
        diagnostics.public_release_readiness.blocking_conditions
    )
    assert "desktop_backend_not_release_ready" in (
        diagnostics.public_release_readiness.blocking_conditions
    )
    assert [
        action.id for action in diagnostics.public_release_readiness.next_actions
    ] == [
        "run_isolated_provider_smoke",
        "attach_real_virtual_desktop_backend",
        "run_public_release_smoke",
    ]


def test_controlled_provider_diagnostics_requires_isolated_desktop_session(
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
        "http://127.0.0.1:19092",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_ID",
        "local-controlled-desktop",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS",
        "desktop.list_apps,desktop.safe_type_text",
    )
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED",
        "true",
    )
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND", "user_foreground")
    monkeypatch.setenv("OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED", "false")
    monkeypatch.setenv(
        "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED",
        "true",
    )
    provider = desktop_execution_provider_status_from_env(probe_health=False)

    diagnostics = controlled_desktop_provider_diagnostics_snapshot(
        sandbox_provider=provider
    )

    assert diagnostics.ready is False
    assert diagnostics.configured is True
    assert diagnostics.status == "isolated_desktop_session_required"
    assert diagnostics.desktop_session_kind == "user_foreground"
    assert diagnostics.desktop_session_isolated is False
    assert diagnostics.foreground_takeover_required is True
    assert "sandbox_desktop_session_required" in diagnostics.blocking_conditions


def test_runtime_tool_catalog_marks_local_provider_input_tools_as_direct_ready() -> None:
    catalog = runtime_tool_catalog_snapshot(
        sandbox_provider=local_desktop_execution_provider_status()
    )
    tools = {tool.tool_name: tool for tool in catalog.tools}

    assert catalog.sandbox_provider is not None
    assert catalog.sandbox_provider.keyboard_mouse_capture_supported is True
    assert catalog.sandbox_provider.requires_real_sandbox_for == []
    assert tools["app.open"].provider_ready is True
    assert tools["app.open"].requires_user_foreground_session is True
    assert tools["app.open"].user_foreground_takeover_risk is True
    assert tools["desktop.safe_type_text"].provider_supported is True
    assert "desktop.safe_type_text" in (
        catalog.capabilities["foreground_input"].provider_ready_tools
    )
    assert "app.focus" in (
        catalog.capabilities["foreground_activation"].provider_ready_tools
    )


def test_runtime_tool_catalog_surfaces_multi_file_data_analysis_schema() -> None:
    catalog = runtime_tool_catalog_snapshot()
    tools = {tool.tool_name: tool for tool in catalog.tools}

    data_analysis = tools["data.analyze"]

    assert data_analysis.capability_id == "data"
    assert data_analysis.risk_level == "low"
    assert data_analysis.approval_required is False
    assert data_analysis.input_schema["required"] == []
    assert data_analysis.input_schema["properties"]["paths"] == {
        "type": "array",
        "items": {"type": "string"},
        "maxItems": 100,
        "description": "Relative data file paths for multi-file analysis. Provide this, path, or content.",
    }
    assert "artifact_paths" in data_analysis.input_schema["properties"]


def test_low_level_runtime_tools_keep_builtin_data_analysis_execution() -> None:
    tools = runtime_execution_tool_names(
        intent_kind="data_analysis",
        prefer_low_level=True,
    )

    requests = planner_direct_tool_requests(
        "请分析 data/sales.csv 并输出报告",
        tools,
    )

    assert "data.analyze" in tools
    assert requests[0]["tool"] == "data.analyze"
    assert requests[0]["input"]["path"] == "data/sales.csv"


def test_runtime_tool_catalog_surfaces_foreground_activation_blockers_per_tool() -> None:
    catalog = runtime_tool_catalog_snapshot(
        platform_name="Darwin",
        blocking_conditions={
            "foreground_activation": ["foreground_focus_unavailable"],
        },
    )
    tools = {tool.tool_name: tool for tool in catalog.tools}

    assert tools["app.open"].blocking_conditions == []
    assert tools["app.focus"].blocking_conditions == ["foreground_focus_unavailable"]
    assert tools["app.open_and_safe_type_text"].blocking_conditions == [
        "foreground_focus_unavailable"
    ]
    assert catalog.capabilities["foreground_activation"].blocking_conditions == [
        "foreground_focus_unavailable"
    ]


def test_runtime_tool_catalog_does_not_root_block_app_launch_or_discovery() -> None:
    catalog = runtime_tool_catalog_snapshot(
        platform_name="Darwin",
        blocking_conditions={
            "desktop_execution": ["screen_capture_blank"],
            "active_window": ["screen_capture_blank"],
            "foreground_input": ["screen_capture_blank"],
        },
    )
    tools = {tool.tool_name: tool for tool in catalog.tools}

    assert tools["desktop.list_apps"].blocking_conditions == []
    assert tools["app.open"].blocking_conditions == []
    assert tools["desktop.active_window"].blocking_conditions == ["screen_capture_blank"]
    assert tools["desktop.ui_elements"].blocking_conditions == ["screen_capture_blank"]


def test_desktop_execution_envelopes_keep_executable_verification_fallback() -> None:
    tools = runtime_execution_tool_names(
        intent_kind="desktop_operation",
        prefer_low_level=True,
    )
    decision = RuntimePlanner().decision(
        "打开 PixelForge",
        allowed_tools=tools,
        metadata={
            "desktop_blocking_conditions_by_capability": {
                "desktop_execution": ["screen_capture_blank"],
                "active_window": ["screen_capture_blank"],
                "foreground_input": ["screen_capture_blank"],
            }
        },
    )
    full_plan_envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=tools,
        full_plan=True,
    )
    subset_envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=tools,
    )
    assert full_plan_envelope is not None
    assert subset_envelope is not None
    full_plan_requests = full_plan_envelope.requests

    assert [
        (request.tool_name, request.status) for request in full_plan_requests
    ] == [
        ("desktop.list_apps", "planned"),
        ("app.open", "planned"),
        ("screen.capture", "planned"),
    ]
    assert full_plan_requests[2].step_id == "verify-desktop-result"
    assert full_plan_requests[2].policy_reason == ""
    assert full_plan_requests[2].observation_evidence["source_tool"] == (
        "screen.capture"
    )
    assert full_plan_requests[2].observation_evidence["app_name"] == "PixelForge"
    assert full_plan_requests[2].observation_retry["reason"] == "verification_failed"

    assert [
        (request.tool_name, request.status) for request in subset_envelope.requests
    ] == [
        ("desktop.list_apps", "planned"),
        ("app.open", "planned"),
    ]
    assert all(
        request.tool_name != "desktop.verify"
        and request.status not in {"blocked", "unavailable"}
        for request in subset_envelope.requests
    )

    executable = runtime_execution_requests_from_envelope_payload(
        subset_envelope.model_dump(mode="json"),
        allowed_tools=tools,
    )
    assert [request["tool"] for request in executable] == [
        "desktop.list_apps",
        "app.open",
    ]


def test_runtime_planner_execution_strategy_prefers_background_desktop_for_daily_keyboard_mouse() -> None:
    tools = runtime_execution_tool_names(
        intent_kind="desktop_operation",
        prefer_low_level=True,
    )
    metadata = with_daily_entrypoint_desktop_execution_policy(
        {"source": "chat"},
        surface="chat",
    )
    decision = RuntimePlanner().decision(
        "在当前应用输入 hello",
        allowed_tools=tools,
        metadata=metadata,
    )

    strategy = decision.plan.execution_strategy

    assert strategy is not None
    assert strategy.preferred_environment == "background_desktop"
    assert strategy.interaction_mode == "background"
    assert strategy.policy_mode == "preview"
    assert strategy.background_desktop_preferred is True
    assert strategy.isolated_desktop_preferred is False
    assert strategy.foreground_takeover_allowed is False
    assert strategy.user_foreground_takeover_risk is False
    assert strategy.sandbox_required is False
    assert strategy.provider_auto_start_recommended is False
    assert strategy.local_foreground_fallback_allowed is False
    assert strategy.keyboard_mouse_step_count >= 1
    assert "keyboard_mouse_capture_planned" in strategy.reasons
    assert "policy_prefers_background_desktop" in strategy.reasons
    assert "run_in_background_desktop_provider" in strategy.mitigations
    assert "apply_risk_policy_before_keyboard_mouse" in strategy.mitigations

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=tools,
        metadata=metadata,
    )

    assert envelope is not None
    assert envelope.execution_strategy is not None
    assert envelope.execution_strategy.strategy_id == strategy.strategy_id
    assert envelope.execution_strategy.preferred_environment == "background_desktop"
    assert envelope.execution_strategy.sandbox_required is False
    assert envelope.execution_strategy.provider_auto_start_recommended is False


def test_runtime_execution_strategy_marks_explicit_live_foreground_risk() -> None:
    decision = RuntimePlanner().decision(
        "在当前应用输入 hello",
        allowed_tools=["desktop.safe_type_text"],
        metadata={
            "allow_user_foreground_takeover": True,
            "desktop_execution_policy": {"mode": "allow"},
        },
    )

    strategy = decision.plan.execution_strategy

    assert strategy is not None
    assert strategy.preferred_environment == "user_foreground"
    assert strategy.interaction_mode == "foreground"
    assert strategy.foreground_takeover_allowed is True
    assert strategy.user_foreground_takeover_risk is True
    assert strategy.sandbox_required is False
    assert strategy.provider_auto_start_recommended is False
    assert strategy.local_foreground_fallback_allowed is False
    assert "user_foreground_takeover_allowed" in strategy.reasons


def test_runtime_execution_strategy_keeps_low_risk_app_activation_in_background() -> None:
    metadata = with_daily_entrypoint_desktop_execution_policy(
        {"source": "chat"},
        surface="chat",
    )
    decision = RuntimePlanner().decision(
        "打开 PixelForge",
        allowed_tools=["desktop.list_apps", "app.open", "desktop.active_window"],
        metadata=metadata,
    )

    strategy = decision.plan.execution_strategy

    assert strategy is not None
    assert strategy.preferred_environment == "background_desktop"
    assert strategy.interaction_mode == "background"
    assert strategy.keyboard_mouse_step_count == 0
    assert strategy.user_foreground_takeover_risk is False
    assert strategy.sandbox_required is False
    assert strategy.provider_auto_start_recommended is False
    assert strategy.local_foreground_fallback_allowed is False
    assert "policy_prefers_background_desktop" in strategy.reasons


def test_runtime_execution_envelope_blocks_keyboard_mouse_without_isolated_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL", raising=False)
    tools = runtime_execution_tool_names(
        intent_kind="desktop_operation",
        prefer_low_level=True,
    )
    metadata = with_agent_studio_desktop_execution_policy({"source": "studio"})
    decision = RuntimePlanner().decision(
        "在当前应用输入 hello",
        allowed_tools=tools,
        metadata=metadata,
    )
    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=tools,
        metadata=metadata,
    )

    assert envelope is not None
    input_request = next(
        request
        for request in envelope.requests
        if request.tool_name == "desktop.safe_type_text"
    )
    assert input_request.sandbox_provider is not None
    assert input_request.sandbox_provider.provider_id == LOCAL_DESKTOP_PROVIDER_ID
    assert input_request.sandbox_provider.keyboard_mouse_capture_supported is True
    assert input_request.desktop_execution_route is not None
    assert input_request.desktop_execution_route.status == (
        "sandbox_desktop_session_required"
    )
    assert input_request.desktop_execution_route.can_execute is False
    assert input_request.desktop_execution_route.sandbox_required is True
    assert input_request.desktop_execution_route.selected_provider_kind == (
        LOCAL_DESKTOP_PROVIDER_KIND
    )
    assert input_request.desktop_execution_route.blocking_conditions == [
        "sandbox_desktop_session_required"
    ]


def test_runtime_tool_catalog_surfaces_restricted_plugin_metadata_and_uninstall() -> None:
    clear_restricted_tool_plugins()

    def echo_tool(payload, context):
        return {"ok": True, "text": payload["text"], "plugin_id": context.plugin_id}

    try:
        register_restricted_tool_plugin(
            RestrictedToolPlugin(
                plugin_id="notes",
                tools=(
                    RestrictedPluginTool(
                        tool_id="echo",
                        description="Echo text through a restricted test plugin.",
                        properties={"text": {"type": "string"}},
                        required=("text",),
                        risk_level="medium",
                        execute=echo_tool,
                    ),
                ),
                skill_docs="Use this plugin when an Agent Desk note needs a short echo.",
            )
        )
        tools = {tool.tool_name: tool for tool in runtime_tool_catalog_snapshot().tools}
        plugins = {
            plugin.plugin_id: plugin
            for plugin in runtime_tool_catalog_snapshot().plugins
        }
        plugin_tool = tools["plugin.notes.echo"]

        assert plugin_tool.capability_id == "plugin_tool"
        assert plugin_tool.risk_level == "medium"
        assert plugin_tool.approval_required is False
        assert plugin_tool.source == "plugin:notes"
        assert plugin_tool.input_schema["required"] == ["text"]
        assert "Restricted tool-only plugin: notes." in plugin_tool.fallback_notes
        assert any("Agent Desk note" in note for note in plugin_tool.fallback_notes)
        assert plugins["notes"].enabled is True
        assert plugins["notes"].tool_names == ["plugin.notes.echo"]
        assert plugins["notes"].tools[0].function_name == "plugin_notes_echo"
        assert plugins["notes"].tools[0].risk_level == "medium"

        unregister_restricted_tool_plugin("notes")
        tools_after_unregister = {
            tool.tool_name for tool in runtime_tool_catalog_snapshot().tools
        }
        assert "plugin.notes.echo" not in tools_after_unregister
    finally:
        clear_restricted_tool_plugins()


def test_runtime_tool_catalog_surfaces_restricted_plugin_install_state() -> None:
    clear_restricted_tool_plugins()
    manager = RestrictedToolPluginManager()

    def echo_tool(payload, context):
        return {"ok": True, "text": payload["text"], "plugin_id": context.plugin_id}

    plugin = RestrictedToolPlugin(
        plugin_id="notes",
        tools=(
            RestrictedPluginTool(
                tool_id="echo",
                description="Echo text through a managed restricted test plugin.",
                properties={"text": {"type": "string"}},
                required=("text",),
                risk_level="medium",
                execute=echo_tool,
            ),
        ),
        skill_docs="Use this plugin when an Agent Desk note needs a short echo.",
    )

    try:
        manager.install(plugin, enabled=False)
        disabled_catalog = runtime_tool_catalog_snapshot(
            plugin_states=manager.list_installed()
        )
        disabled_plugins = {
            plugin.plugin_id: plugin for plugin in disabled_catalog.plugins
        }

        assert "plugin.notes.echo" not in {
            tool.tool_name for tool in disabled_catalog.tools
        }
        assert disabled_plugins["notes"].enabled is False
        assert disabled_plugins["notes"].tool_names == ["plugin.notes.echo"]
        assert disabled_plugins["notes"].tools == []
        assert "Agent Desk note" in disabled_plugins["notes"].skill_docs

        manager.enable("notes")
        enabled_catalog = runtime_tool_catalog_snapshot(
            plugin_states=manager.list_installed()
        )
        enabled_plugins = {
            plugin.plugin_id: plugin for plugin in enabled_catalog.plugins
        }

        assert "plugin.notes.echo" in {
            tool.tool_name for tool in enabled_catalog.tools
        }
        assert enabled_plugins["notes"].enabled is True
        assert enabled_plugins["notes"].tools[0].tool_name == "plugin.notes.echo"
        assert enabled_plugins["notes"].tools[0].risk_level == "medium"

        manager.uninstall("notes")
        uninstalled_catalog = runtime_tool_catalog_snapshot(
            plugin_states=manager.list_installed()
        )
        assert uninstalled_catalog.plugins == []
        assert "plugin.notes.echo" not in {
            tool.tool_name for tool in uninstalled_catalog.tools
        }
    finally:
        clear_restricted_tool_plugins()


def test_agent_desk_snapshot_json_shape_is_stable() -> None:
    snapshot = AgentDeskSnapshot(
        agent_id="agent-1",
        root_path="/workspace/agent-1",
        items=[
            AgentDeskItemSnapshot(
                path="desk-notes.md",
                name="desk-notes.md",
                kind="note",
                size_bytes=12,
                mime_type="text/markdown",
                preview_text="# Notes",
                updated_at="2026-06-22T00:00:00Z",
            ),
            AgentDeskItemSnapshot(
                path="inputs/brief.md",
                name="brief.md",
                kind="file",
                size_bytes=20,
                mime_type="text/markdown",
                preview_text="Brief",
                updated_at="2026-06-22T00:00:01Z",
            ),
        ],
        updated_at="2026-06-22T00:00:02Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "agent_id",
        "root_path",
        "notes_path",
        "metadata_path",
        "items",
        "updated_at",
    ]
    assert payload["notes_path"] == "desk-notes.md"
    assert payload["metadata_path"] == ".yachiyo-desk.json"
    assert payload["items"][0]["kind"] == "note"
    assert payload["items"][1]["path"] == "inputs/brief.md"
    with pytest.raises(ValidationError):
        AgentDeskSnapshot(agent_id="agent-1", root_path="/workspace", unknown=True)
    with pytest.raises(ValidationError):
        SaveAgentDeskNoteRequest(content="note", unknown=True)
    with pytest.raises(ValidationError):
        SaveAgentDeskFileRequest(path="brief.md", content="body", unknown=True)
    with pytest.raises(ValidationError):
        AgentDeskFileEventRequest(path="brief.md", unknown=True)
    file_event = _json(
        AgentDeskFileEventRequest(
            path="inputs/brief.md",
            event_type="modified",
            delay_seconds=0,
        )
    )
    assert file_event == {
        "path": "inputs/brief.md",
        "event_type": "modified",
        "delay_seconds": 0,
    }


def test_chat_runnable_catalog_snapshot_json_shape_is_stable() -> None:
    snapshot = ChatRunnableCatalogSnapshot(
        agents=[
            ChatRunnableSnapshot(
                runnable_id="agent-1",
                agent_id="agent-1",
                kind="agent",
                name="Planner",
                tool_capabilities=["workspace.read", "workspace.write_patch"],
                approval_required_tools=["workspace.write_patch"],
            )
        ],
        workflows=[
            ChatRunnableSnapshot(
                runnable_id="workflow-1",
                workflow_id="workflow-1",
                kind="workflow",
                name="Review workflow",
                output_contract="workflow",
                participants=[
                    ChatRunnableParticipantSnapshot(
                        runnable_id="agent-1",
                        agent_id="agent-1",
                        kind="agent",
                        name="Planner",
                    )
                ],
            )
        ],
        groups=[
            ChatRunnableSnapshot(
                runnable_id="group-1",
                group_id="group-1",
                kind="group",
                name="Review group",
                output_contract="group_run",
                participants=[
                    ChatRunnableParticipantSnapshot(
                        runnable_id="agent-1",
                        agent_id="agent-1",
                        kind="agent",
                        name="Planner",
                    )
                ],
            )
        ],
    )

    payload = _json(snapshot)

    assert list(payload) == ["agents", "workflows", "groups"]
    assert list(payload["agents"][0]) == [
        "runnable_id",
        "agent_id",
        "workflow_id",
        "group_id",
        "kind",
        "name",
        "nickname",
        "description",
        "avatar_url",
        "category",
        "output_contract",
        "enabled",
        "tool_capabilities",
        "approval_required_tools",
        "participants",
    ]
    assert payload["agents"][0]["agent_id"] == "agent-1"
    assert payload["agents"][0]["tool_capabilities"] == ["workspace.read", "workspace.write_patch"]
    assert payload["agents"][0]["approval_required_tools"] == ["workspace.write_patch"]
    assert payload["workflows"][0]["workflow_id"] == "workflow-1"
    assert payload["workflows"][0]["participants"][0]["agent_id"] == "agent-1"
    assert payload["groups"][0]["group_id"] == "group-1"
    assert payload["groups"][0]["output_contract"] == "group_run"
    assert "tool_policy" not in payload["agents"][0]
    assert "nodes" not in payload["workflows"][0]
    assert "edges" not in payload["workflows"][0]


def test_run_timeline_snapshot_json_shape_covers_runtime_debug_objects() -> None:
    snapshot = RunTimelineSnapshot(
        run_id="run-1",
        parent_run_id=None,
        group_run_id="group-run-1",
        workflow_run_id="workflow-run-1",
        agent_id="agent-1",
        status="running",
        title="Ship docs",
        task_id="task-1",
        session_id="chat-1",
        task_run_link_created_at="2026-06-14T00:00:00Z",
        task_run_link_updated_at="2026-06-14T00:00:02Z",
        task_run_link_run_status="running",
        task_run_link_last_event_sequence=7,
        rerun_of_run_id="original-run-1",
        rerun_of_kind="agent_run",
        rerun_of_status="completed",
        rerun_of_runnable_id="agent-1",
        rerun_of_runnable_name="Planner",
        rerun_original_created_at="2026-06-13T00:00:00Z",
        rerun_original_updated_at="2026-06-13T00:00:03Z",
        runtime_debug=RuntimeDebugSummarySnapshot(
            run_id="run-1",
            task_id="task-1",
            event_count=1,
            tool_call_count=1,
            approval_count=1,
            artifact_count=1,
            child_run_count=1,
        ),
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="envelope-1",
            decision_id="decision-1",
            plan_id="plan-1",
            intent_kind="data_analysis",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="request-1",
                    tool_name="workspace.read",
                    runtime_stage="discover",
                )
            ],
            runtime_stage_counts={"discover": 1},
        ),
        recovery_source=RecoveryRunProvenanceSnapshot(
            source="agent_studio_replan_recovery",
            kind="replan",
            source_run_id="original-run-1",
            source_task_id="task-1",
            replan_request_id="replan-1",
            recovery_action_id="replan-1:action:1:desktop.list_apps",
            recovery_tool="desktop.list_apps",
            recovery_input_preview={"query": "Music"},
        ),
        events=[
            PublicRunEvent(
                event_id="event-1",
                run_id="run-1",
                sequence=1,
                event_type="workflow.node.agent",
            )
        ],
        tool_calls=[
            ToolCallSnapshot(
                tool_call_id="tool-1",
                run_id="run-1",
                tool_name="workspace.read",
                status="completed",
                input_preview={"path": "README.md"},
            )
        ],
        memory_traces=[
            MemoryTraceSnapshot(
                trace_id="memory-trace-1",
                run_id="run-1",
                event_type="memory.retrieved",
                title="Memory retrieved",
            )
        ],
        skill_traces=[
            SkillTraceSnapshot(
                trace_id="skill-trace-1",
                run_id="run-1",
                event_type="skill.selected",
                title="Demo Skill",
            )
        ],
        approvals=[
            ApprovalCardSnapshot(approval_id="approval-1", run_id="run-1", title="Approve")
        ],
        artifacts=[
            ArtifactSnapshot(
                artifact_id="artifact-1",
                run_id="run-1",
                title="Report",
                kind="markdown",
            )
        ],
        children=[
            RunTimelineChildSnapshot(
                run_id="child-run-1",
                status="completed",
                kind="agent_run",
                parent_run_id="run-1",
                group_run_id="group-run-1",
                run_group_id="group-run-1",
                workflow_run_id="workflow-run-1",
                workflow_node_id="review",
                workflow_node_label="Review",
                agent_id="agent-2",
                workflow_id="workflow-1",
            )
        ],
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "run_id",
        "parent_run_id",
        "group_run_id",
        "run_group_id",
        "workflow_run_id",
        "agent_id",
        "status",
        "title",
        "task_id",
        "session_id",
        "task_run_link_created_at",
        "task_run_link_updated_at",
        "task_run_link_run_status",
        "task_run_link_last_event_sequence",
        "rerun_of_run_id",
        "rerun_of_kind",
        "rerun_of_status",
        "rerun_of_runnable_id",
        "rerun_of_runnable_name",
        "rerun_original_created_at",
        "rerun_original_updated_at",
        "planner_summary",
        "runtime_debug",
        "runtime_execution_envelope",
        "task_core",
        "task_progress",
        "replan_recoveries",
        "recovery_source",
        "events",
        "tool_calls",
        "memory_traces",
        "skill_traces",
        "approvals",
        "pending_approval",
        "artifacts",
        "children",
        "created_at",
        "updated_at",
    ]
    assert payload["task_id"] == "task-1"
    assert payload["session_id"] == "chat-1"
    assert payload["task_run_link_last_event_sequence"] == 7
    assert payload["rerun_of_run_id"] == "original-run-1"
    assert payload["planner_summary"] is None
    assert payload["runtime_debug"]["child_run_count"] == 1
    assert payload["runtime_execution_envelope"]["envelope_id"] == "envelope-1"
    assert payload["runtime_execution_envelope"]["requests"][0]["runtime_stage"] == "discover"
    assert payload["recovery_source"]["kind"] == "replan"
    assert payload["recovery_source"]["recovery_tool"] == "desktop.list_apps"
    assert payload["tool_calls"][0]["tool_name"] == "workspace.read"
    assert payload["memory_traces"][0]["event_type"] == "memory.retrieved"
    assert payload["skill_traces"][0]["event_type"] == "skill.selected"
    assert payload["children"][0]["run_id"] == "child-run-1"
    assert payload["children"][0]["parent_run_id"] == "run-1"
    assert payload["children"][0]["group_run_id"] == "group-run-1"
    assert payload["children"][0]["workflow_node_id"] == "review"
    assert payload["children"][0]["planner_summary"] is None


def test_run_timeline_snapshot_overlays_runtime_request_status_for_studio() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "status": "waiting_approval",
            "runtime_execution_envelope": {
                "envelope_id": "execution-envelope-runtime-plan-1",
                "decision_id": "decision-1",
                "plan_id": "runtime-plan-1",
                "intent_kind": "desktop_operation",
                "requests": [
                    {
                        "request_id": "request-discover",
                        "step_id": "discover-desktop-state",
                        "tool_name": "desktop.list_apps",
                        "runtime_stage": "discover",
                    },
                    {
                        "request_id": "request-open",
                        "step_id": "open-or-focus-app",
                        "tool_name": "app.open",
                        "runtime_stage": "operate",
                    },
                ],
                "runtime_stage_counts": {"discover": 1, "operate": 1},
            },
            "tool_calls": [
                {
                    "tool_call_id": "tool-call-discover",
                    "tool_name": "desktop.list_apps",
                    "step_id": "discover-desktop-state",
                    "status": "completed",
                }
            ],
            "pending_approval": {
                "approval_id": "approval-open",
                "tool_name": "app.open",
                "step_id": "open-or-focus-app",
                "status": "pending",
                "title": "Open app",
            },
        }
    )

    envelope = snapshot.runtime_execution_envelope
    assert envelope is not None
    assert [request.status for request in envelope.requests] == [
        "completed",
        "waiting_approval",
    ]
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.completed_runtime_request_count == 1
    assert snapshot.runtime_debug.waiting_runtime_request_count == 1
    assert snapshot.runtime_debug.current_request_tool_name == "app.open"


def test_run_timeline_snapshot_projects_recovery_source_metadata() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "recovery-run-1",
            "status": "running",
            "metadata": {
                "source": "agent_studio_tool_recovery",
                "desktop_permission_recovery": True,
                "source_run_id": "run-1",
                "source_tool_call_id": "tool-call-1",
                "source_tool_name": "desktop.open_app",
                "tool_recovery_action_id": "tool-action-1",
                "recovery_action_kind": "retry_original",
                "recovery_tool": "desktop.open_app",
                "recovery_input": {"app_name": "Music", "token": "secret"},
                "recovery_permission_target": "app_launch",
                "recovery_risk_level": "low",
                "source_task_id": "task-1",
                "source_task_title": "Open Apple Music",
            },
        }
    )

    assert snapshot.recovery_source is not None
    assert snapshot.recovery_source.source == "agent_studio_tool_recovery"
    assert snapshot.recovery_source.kind == "tool"
    assert snapshot.recovery_source.source_run_id == "run-1"
    assert snapshot.recovery_source.source_tool_call_id == "tool-call-1"
    assert snapshot.recovery_source.recovery_action_id == "tool-action-1"
    assert snapshot.recovery_source.recovery_action_kind == "retry_original"
    assert snapshot.recovery_source.recovery_tool == "desktop.open_app"
    assert snapshot.recovery_source.recovery_input_preview["app_name"] == "Music"
    assert snapshot.recovery_source.source_task_id == "task-1"


def test_run_timeline_snapshot_projects_runtime_envelope_retry_recovery() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "runtime-run-1",
            "agent_id": "agent-1",
            "task_id": "task-1",
            "status": "failed",
            "runtime_execution_envelope": {
                "envelope_id": "runtime-envelope-1",
                "decision_id": "decision-runtime-1",
                "plan_id": "runtime-plan-1",
                "intent_kind": "desktop_operation",
                "requests": [
                    {
                        "request_id": "runtime-request-open-app",
                        "step_id": "open-app",
                        "capability_id": "desktop.app_control",
                        "decision_id": "decision-runtime-1",
                        "plan_id": "runtime-plan-1",
                        "core_id": "task-core-1",
                        "tool_name": "desktop.open_app",
                        "input": {"app_name": "Apple Music"},
                        "status": "blocked",
                        "runtime_stage": "verify",
                        "replan_triggers": ["verification_failed"],
                        "replan_signal_ids": ["replan-open-app-verify"],
                        "observation_evidence": {
                            "blocking_condition": "foreground_focus_unavailable",
                            "foreground_required": True,
                            "foreground_ready": False,
                        },
                        "observation_retry": {
                            "tool": "desktop.open_app",
                            "input": {"app_name": "Music"},
                            "reason": "foreground_focus_unavailable",
                        },
                        "verification_targets": [
                            {
                                "step_id": "discover-app",
                                "tool_name": "desktop.list_apps",
                            }
                        ],
                        "task_verification_targets": [
                            {
                                "step_id": "open-app",
                                "todo_id": "todo-open-app",
                            }
                        ],
                    }
                ],
                "runtime_stage_counts": {"operate": 1},
                "replan_signal_count": 1,
            },
        }
    )

    assert len(snapshot.replan_recoveries) == 1
    recovery = snapshot.replan_recoveries[0]
    assert recovery.request_id == "runtime-retry:runtime-request-open-app"
    assert recovery.run_id == "runtime-run-1"
    assert recovery.task_id == "task-1"
    assert recovery.source_step_id == "open-app"
    assert recovery.source_tool_name == "desktop.open_app"
    assert recovery.permission_target == "foreground_focus"
    assert recovery.planning_reason == "runtime_execution_observation_retry"
    assert recovery.observation_evidence["blocking_condition"] == (
        "foreground_focus_unavailable"
    )
    assert recovery.verification_targets == [
        {
            "step_id": "discover-app",
            "tool_name": "desktop.list_apps",
        },
        {
            "step_id": "open-app",
            "todo_id": "todo-open-app",
        },
    ]
    assert recovery.recovery_actions[0].action_id == (
        "runtime-retry:runtime-request-open-app:action:1:desktop.open_app"
    )
    assert recovery.recovery_actions[0].input == {"app_name": "Music"}
    assert recovery.recovery_actions[0].selected is True
    assert recovery.recovery_actions[0].verification_targets == [
        {
            "step_id": "discover-app",
            "tool_name": "desktop.list_apps",
        },
        {
            "step_id": "open-app",
            "todo_id": "todo-open-app",
        }
    ]
    assert recovery.recovery_actions[0].metadata["runtime_stage"] == "verify"
    assert recovery.recovery_actions[0].metadata["replan_triggers"] == [
        "verification_failed"
    ]
    assert recovery.recovery_actions[0].metadata["replan_signal_ids"] == [
        "replan-open-app-verify"
    ]
    assert recovery.recovery_actions[0].metadata["verification_target_step_ids"] == [
        "discover-app",
        "open-app",
    ]


def test_group_run_snapshot_projects_runtime_envelope_retry_recovery() -> None:
    snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "runtime-group-run-1",
            "group_id": "group-1",
            "title": "Desktop group",
            "status": "failed",
            "task_id": "task-group-1",
            "objective": "Open Apple Music",
            "participants": [
                {
                    "agent_id": "agent-1",
                    "name": "Operator",
                    "role": "operator",
                }
            ],
            "runtime_execution_envelope": {
                "envelope_id": "runtime-group-envelope-1",
                "decision_id": "decision-group-runtime-1",
                "plan_id": "runtime-group-plan-1",
                "intent_kind": "multi_agent",
                "requests": [
                    {
                        "request_id": "runtime-group-request-open-app",
                        "step_id": "open-app",
                        "capability_id": "desktop.app_control",
                        "group_run_id": "runtime-group-run-1",
                        "tool_name": "desktop.open_app",
                        "status": "blocked",
                        "observation_evidence": {
                            "blocking_condition": "desktop_session_locked",
                        },
                        "observation_retry": {
                            "tool": "desktop.open_app",
                            "input": {"app_name": "Music"},
                            "reason": "desktop_session_locked",
                        },
                    }
                ],
                "runtime_stage_counts": {"operate": 1},
                "replan_signal_count": 1,
            },
        }
    )

    assert len(snapshot.replan_recoveries) == 1
    recovery = snapshot.replan_recoveries[0]
    assert recovery.request_id == "runtime-retry:runtime-group-request-open-app"
    assert recovery.run_id == "runtime-group-run-1"
    assert recovery.group_run_id == "runtime-group-run-1"
    assert recovery.task_id == "task-group-1"
    assert recovery.permission_target == "desktop_session_unlocked"
    assert recovery.recovery_actions[0].tool == "desktop.open_app"
    assert recovery.recovery_actions[0].input == {"app_name": "Music"}


def test_workflow_run_snapshot_projects_runtime_envelope_retry_recovery() -> None:
    snapshot = workflow_run_snapshot_from_payload(
        {
            "run_id": "runtime-workflow-run-1",
            "workflow_run_id": "runtime-workflow-run-1",
            "workflow_id": "workflow-1",
            "kind": "workflow_run",
            "status": "failed",
            "task_id": "task-workflow-1",
            "objective": "Open Apple Music from workflow",
            "runtime_execution_envelope": {
                "envelope_id": "runtime-workflow-envelope-1",
                "decision_id": "decision-workflow-runtime-1",
                "plan_id": "runtime-workflow-plan-1",
                "intent_kind": "workflow_orchestration",
                "requests": [
                    {
                        "request_id": "runtime-workflow-request-open-app",
                        "step_id": "workflow-open-app",
                        "capability_id": "desktop.app_control",
                        "workflow_run_id": "runtime-workflow-run-1",
                        "workflow_id": "workflow-1",
                        "workflow_node_id": "open-music",
                        "workflow_node_label": "Open Music",
                        "tool_name": "desktop.open_app",
                        "status": "blocked",
                        "observation_evidence": {
                            "blocking_condition": "screen_capture_blank",
                        },
                        "observation_retry": {
                            "tool": "screen.capture",
                            "input": {"reason": "verify Music window"},
                            "reason": "screen_capture_blank",
                        },
                    }
                ],
                "runtime_stage_counts": {"verify": 1},
                "replan_signal_count": 1,
            },
        }
    )

    assert snapshot.runtime_execution_envelope is not None
    assert snapshot.runtime_execution_envelope.intent_kind == "workflow_orchestration"
    assert len(snapshot.replan_recoveries) == 1
    recovery = snapshot.replan_recoveries[0]
    assert recovery.request_id == "runtime-retry:runtime-workflow-request-open-app"
    assert recovery.run_id == "runtime-workflow-run-1"
    assert recovery.workflow_run_id == "runtime-workflow-run-1"
    assert recovery.task_id == "task-workflow-1"
    assert recovery.permission_target == "desktop_screen_visible"
    assert recovery.source_step_id == "workflow-open-app"
    assert recovery.recovery_actions[0].tool == "screen.capture"
    assert recovery.recovery_actions[0].input == {"reason": "verify Music window"}


def test_run_timeline_events_inherit_parent_task_core_context() -> None:
    snapshot = run_timeline_snapshot_from_payload(
        {
            "run_id": "run-1",
            "status": "running",
            "task_id": "task-1",
            "task_core": {
                "core_id": "core-1",
                "workspace": {
                    "workspace_id": "workspace-1",
                    "title": "Workspace",
                    "context": {"task_id": "task-1"},
                },
            },
            "events": [
                {
                    "event_type": "tool.requested",
                    "sequence": 1,
                    "detail": "workspace.read",
                    "payload": {
                        "tool_call_id": "call-1",
                        "tool": "workspace.read",
                        "input_preview": {"path": "README.md"},
                    },
                }
            ],
        }
    )

    event = snapshot.events[0]
    tool_call = snapshot.tool_calls[0]
    assert event.core_id == "core-1"
    assert event.workspace_id == "workspace-1"
    assert event.task_id == "task-1"
    assert event.payload["core_id"] == "core-1"
    assert event.payload["workspace_id"] == "workspace-1"
    assert event.payload["task_id"] == "task-1"
    assert tool_call.core_id == "core-1"
    assert tool_call.workspace_id == "workspace-1"
    assert tool_call.task_id == "task-1"
    assert tool_call.input_preview["core_id"] == "core-1"
    assert tool_call.input_preview["workspace_id"] == "workspace-1"
    assert tool_call.input_preview["task_id"] == "task-1"
    assert snapshot.runtime_debug is not None
    assert snapshot.runtime_debug.task_id == "task-1"
    assert snapshot.runtime_debug.event_count == 1
    assert snapshot.runtime_debug.tool_call_count == 1
    assert snapshot.runtime_debug.latest_tool_name == "workspace.read"


def test_group_run_events_inherit_parent_task_core_context() -> None:
    snapshot = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-1",
            "group_id": "group-1",
            "status": "running",
            "task_id": "task-1",
            "task_core": {
                "core_id": "core-1",
                "workspace": {
                    "workspace_id": "workspace-1",
                    "title": "Workspace",
                    "context": {"task_id": "task-1"},
                },
            },
            "events": [
                {
                    "event_type": "agent.tool.call",
                    "detail": "workspace.read",
                    "payload": {
                        "tool_call_id": "call-1",
                        "tool": "workspace.read",
                        "input_preview": {"path": "README.md"},
                    },
                }
            ],
        }
    )

    started_event = snapshot.events[0]
    tool_call = snapshot.tool_calls[0]
    assert started_event.event_type == "group.run.started"
    assert started_event.core_id == "core-1"
    assert started_event.workspace_id == "workspace-1"
    assert started_event.task_id == "task-1"
    assert tool_call.core_id == "core-1"
    assert tool_call.workspace_id == "workspace-1"
    assert tool_call.task_id == "task-1"
    assert tool_call.input_preview["core_id"] == "core-1"
    assert tool_call.input_preview["workspace_id"] == "workspace-1"
    assert tool_call.input_preview["task_id"] == "task-1"


def test_tool_call_snapshot_keeps_runtime_trace_fields() -> None:
    snapshot = ToolCallSnapshot(
        tool_call_id="tool-1",
        run_id="run-1",
        source_run_id="child-run-1",
        source_runnable_id="agent-1",
        source_runnable_name="Planner",
        workflow_id="workflow-1",
        workflow_run_id="workflow-run-1",
        workflow_node_id="read",
        workflow_node_label="Read Files",
        group_id="group-1",
        group_run_id="group-run-1",
        replan_triggers=["verification_failed"],
        replan_signal_ids=["signal-1"],
        runtime_doctrine="discover_operate_verify",
        runtime_stage="operate",
        runtime_role="desktop_ui_action",
        requires_observation=True,
        requires_post_action_verification=True,
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="tool-envelope-1",
            decision_id="decision-1",
            plan_id="runtime-plan-1",
            intent_kind="desktop_operation",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="tool-request-1",
                    tool_name="desktop.click_ui_element",
                    risk_level="medium",
                )
            ],
        ),
        runtime_execution_metadata={"yachiyo_runtime_planner": True},
        deferred_tool="desktop.click_ui_element",
        deferred_input={"target": "Export", "limit": 80},
        deferred_context={"step_id": "operate-foreground-ui"},
        deferred_continuation=[{"tool": "desktop.ui_elements", "step_id": "verify"}],
        action_target={"action": "click", "label": "Export"},
        observation_evidence={"source_tool": "desktop.ui_elements", "strategy": "button"},
        observation_retry={"from_tool": "desktop.ui_elements", "reason": "target_not_found"},
        verification_targets=[{"step_id": "verify-export", "todo_id": "todo-export"}],
        tool_name="workspace.read",
        status="completed",
        risk_level="low",
        policy_reason="Read-only workspace inspection.",
        input_preview={"path": "README.md"},
        output_preview={"ok": True},
        approval_id="approval-1",
        started_at="2026-06-14T00:00:00Z",
        completed_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "tool_call_id",
        "run_id",
        "source_run_id",
        "source_runnable_id",
        "source_runnable_name",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "group_id",
        "group_run_id",
        "core_id",
        "workspace_id",
        "task_id",
        "source",
        "planning_reason",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "intent_kind",
        "step_id",
        "planner_step_id",
        "capability_id",
        "replan_request_id",
        "replan_trigger",
        "replan_triggers",
        "replan_signal_ids",
        "runtime_doctrine",
        "runtime_stage",
        "runtime_role",
        "requires_observation",
        "requires_post_action_verification",
        "runtime_execution_envelope",
        "runtime_execution_metadata",
        "deferred_tool",
        "deferred_input",
        "deferred_context",
        "deferred_continuation",
        "action_target",
        "observation_evidence",
        "observation_retry",
        "task_workspace_items",
        "verification_targets",
        "task_verification_targets",
        "tool_name",
        "status",
        "risk_level",
        "policy_reason",
        "input_preview",
        "output_preview",
        "metadata",
        "foreground_lock_busy",
        "foreground_lock_holder",
        "approval_id",
        "started_at",
        "completed_at",
    ]
    assert payload["foreground_lock_busy"] is False
    assert payload["foreground_lock_holder"] is None
    assert payload["source_runnable_name"] == "Planner"
    assert payload["workflow_node_id"] == "read"
    assert payload["group_run_id"] == "group-run-1"
    assert payload["runtime_stage"] == "operate"
    assert payload["policy_reason"] == "Read-only workspace inspection."
    assert payload["requires_post_action_verification"] is True
    assert payload["runtime_execution_envelope"]["envelope_id"] == "tool-envelope-1"
    assert payload["runtime_execution_envelope"]["requests"][0]["request_id"] == (
        "tool-request-1"
    )
    assert payload["runtime_execution_metadata"] == {"yachiyo_runtime_planner": True}
    assert payload["verification_targets"] == [
        {"step_id": "verify-export", "todo_id": "todo-export"}
    ]
    assert payload["action_target"] == {"action": "click", "label": "Export"}
    assert payload["observation_evidence"] == {
        "source_tool": "desktop.ui_elements",
        "strategy": "button",
    }
    assert payload["observation_retry"] == {
        "from_tool": "desktop.ui_elements",
        "reason": "target_not_found",
    }
    assert payload["deferred_tool"] == "desktop.click_ui_element"
    assert payload["deferred_input"] == {"target": "Export", "limit": 80}


def test_tool_call_snapshot_from_event_keeps_policy_reason() -> None:
    from apps.shell.yachiyo_agent.tool_call_event_snapshots import (
        tool_call_snapshots_from_events,
    )

    calls = tool_call_snapshots_from_events(
        [
            PublicRunEvent(
                event_id="event-1",
                run_id="run-1",
                sequence=1,
                event_type="agent.tool.approval_required",
                detail="desktop.click_ui_element",
                payload={
                    "tool_call_id": "call-1",
                    "tool_name": "desktop.click_ui_element",
                    "input_preview": {"target": "Export"},
                    "risk_level": "medium",
                    "policy_reason": "Clicking a foreground UI element needs approval.",
                    "runtime_execution_envelope": {
                        "envelope_id": "tool-envelope-1",
                        "decision_id": "decision-1",
                        "plan_id": "runtime-plan-1",
                        "intent_kind": "desktop_operation",
                        "requests": [
                            {
                                "request_id": "tool-request-1",
                                "tool_name": "desktop.click_ui_element",
                                "risk_level": "medium",
                            }
                        ],
                    },
                    "runtime_execution_metadata": {"yachiyo_runtime_planner": True},
                },
                created_at="2026-06-14T00:00:00Z",
            )
        ]
    )

    assert len(calls) == 1
    assert calls[0].risk_level == "medium"
    assert calls[0].policy_reason == "Clicking a foreground UI element needs approval."
    assert calls[0].input_preview["policy_reason"] == (
        "Clicking a foreground UI element needs approval."
    )
    assert calls[0].runtime_execution_envelope is not None
    assert calls[0].runtime_execution_envelope.envelope_id == "tool-envelope-1"
    assert calls[0].runtime_execution_metadata == {"yachiyo_runtime_planner": True}


def test_tool_call_snapshots_merge_legacy_approval_with_completion_event() -> None:
    from apps.shell.yachiyo_agent.tool_call_snapshots import (
        tool_call_snapshots_from_payloads,
    )

    calls = tool_call_snapshots_from_payloads(
        [
            {
                "tool_call_id": "legacy-call-1",
                "run_id": "run-1",
                "tool_name": "desktop.quit_app",
                "status": "waiting_approval",
                "approval_id": "approval-1",
                "input_preview": {},
            }
        ],
        run_id="run-1",
        events=[
            PublicRunEvent(
                event_id="event-2",
                run_id="run-1",
                sequence=2,
                event_type="agent.desktop.intent_completed",
                detail="desktop.quit_app",
                payload={
                    "tool": "desktop.quit_app",
                    "tools": ["desktop.quit_app", "desktop.active_window"],
                    "source": "runtime_planner",
                    "summary": "已请求退出当前应用。",
                },
                created_at="2026-06-14T00:00:01Z",
            )
        ],
    )

    assert len(calls) == 1
    assert calls[0].status == "completed"
    assert calls[0].approval_id == "approval-1"


def test_memory_trace_snapshot_keeps_runtime_trace_fields() -> None:
    snapshot = MemoryTraceSnapshot(
        trace_id="trace-1",
        run_id="run-1",
        event_id="event-1",
        sequence=3,
        event_type="memory.retrieved",
        status="completed",
        action="retrieved",
        memory_id="memory-1",
        memory_kind="preference",
        memory_scope="global",
        count=1,
        source_run_id="child-run-1",
        source_runnable_id="agent-1",
        source_runnable_name="Researcher",
        workflow_id="workflow-1",
        workflow_run_id="workflow-run-1",
        workflow_node_id="retrieve",
        workflow_node_label="Retrieve Context",
        group_id="group-1",
        group_run_id="group-run-1",
        title="Memory retrieved",
        detail="retrieved · preference · global",
        payload_preview={"count": 1},
        created_at="2026-06-14T00:00:00Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "trace_id",
        "run_id",
        "event_id",
        "sequence",
        "event_type",
        "status",
        "action",
        "memory_id",
        "memory_kind",
        "memory_scope",
        "count",
        "source_run_id",
        "source_runnable_id",
        "source_runnable_name",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "group_id",
        "group_run_id",
        "core_id",
        "workspace_id",
        "task_id",
        "title",
        "detail",
        "payload_preview",
        "created_at",
    ]
    assert payload["memory_id"] == "memory-1"
    assert payload["workflow_node_id"] == "retrieve"
    assert payload["group_run_id"] == "group-run-1"


def test_skill_trace_snapshot_keeps_runtime_trace_fields() -> None:
    snapshot = SkillTraceSnapshot(
        trace_id="trace-1",
        run_id="run-1",
        event_id="event-1",
        sequence=4,
        event_type="skill.dispatch.read",
        status="completed",
        skill_id="skill-1",
        skill_name="Demo Skill",
        source_ref="skills/demo/SKILL.md",
        source_type="local_dir",
        tool_name="skill.read",
        source_run_id="child-run-1",
        source_runnable_id="agent-1",
        source_runnable_name="Researcher",
        workflow_id="workflow-1",
        workflow_run_id="workflow-run-1",
        workflow_node_id="read-skill",
        workflow_node_label="Read Skill",
        group_id="group-1",
        group_run_id="group-run-1",
        title="Demo Skill",
        detail="Read project docs · skills/demo/SKILL.md · local_dir",
        payload_preview={"tool": "skill.read"},
        created_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "trace_id",
        "run_id",
        "event_id",
        "sequence",
        "event_type",
        "status",
        "skill_id",
        "skill_name",
        "source_ref",
        "source_type",
        "tool_name",
        "source_run_id",
        "source_runnable_id",
        "source_runnable_name",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "group_id",
        "group_run_id",
        "core_id",
        "workspace_id",
        "task_id",
        "title",
        "detail",
        "payload_preview",
        "created_at",
    ]
    assert payload["skill_id"] == "skill-1"
    assert payload["source_ref"] == "skills/demo/SKILL.md"
    assert payload["workflow_node_id"] == "read-skill"


def test_run_event_page_snapshot_json_shape_is_stable() -> None:
    snapshot = RunEventPageSnapshot(
        run_id="run-1",
        after_sequence=1,
        limit=2,
        next_after_sequence=3,
        has_more=True,
        events=[
            PublicRunEvent(
                event_id="event-2",
                run_id="run-1",
                sequence=2,
                event_type="agent.tool.call",
                title="Tool call",
            )
        ],
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "run_id",
        "after_sequence",
        "limit",
        "next_after_sequence",
        "has_more",
        "events",
    ]
    assert payload["run_id"] == "run-1"
    assert payload["after_sequence"] == 1
    assert payload["next_after_sequence"] == 3
    assert payload["has_more"] is True
    assert payload["events"][0]["event_type"] == "agent.tool.call"


def test_artifact_content_snapshot_json_shape_is_stable() -> None:
    snapshot = ArtifactContentSnapshot(
        run_id="run-1",
        task_id="task-1",
        path="reports/out.md",
        content="# Report",
        mime_type="text/markdown",
        truncated=True,
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "ok",
        "run_id",
        "task_id",
        "path",
        "content",
        "mime_type",
        "truncated",
    ]
    assert payload["ok"] is True
    assert payload["run_id"] == "run-1"
    assert payload["task_id"] == "task-1"
    assert payload["path"] == "reports/out.md"
    assert payload["content"] == "# Report"
    assert payload["truncated"] is True


def test_artifact_snapshot_keeps_runtime_trace_fields() -> None:
    snapshot = ArtifactSnapshot(
        artifact_id="artifact-1",
        run_id="run-1",
        source_run_id="run-source-1",
        source_tool="artifact.write",
        source_runnable_id="agent-1",
        source_runnable_name="Planner",
        workflow_id="workflow-1",
        workflow_run_id="workflow-run-1",
        workflow_node_id="report",
        workflow_node_label="Report",
        group_id="group-1",
        group_run_id="group-run-1",
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="artifact-envelope-1",
            decision_id="decision-1",
            plan_id="runtime-plan-1",
            intent_kind="report_generation",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="artifact-request-1",
                    tool_name="artifact.write",
                    risk_level="low",
                )
            ],
        ),
        runtime_execution_metadata={"yachiyo_runtime_planner": True},
        title="Report",
        kind="workflow_artifact",
        planned_kind="markdown",
        source_kind="csv",
        requested_outputs=["report"],
        manifest_index=0,
        path="reports/out.md",
        mime_type="text/markdown",
        size_bytes=42,
        preview_text="# Report",
        url="/ui/runs/run-1/artifacts/reports/out.md",
        created_at="2026-06-14T00:00:00Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "artifact_id",
        "run_id",
        "source_run_id",
        "source_tool",
        "source_runnable_id",
        "source_runnable_name",
        "workflow_id",
        "workflow_run_id",
        "workflow_node_id",
        "workflow_node_label",
        "group_id",
        "group_run_id",
        "core_id",
        "workspace_id",
        "task_id",
        "runtime_execution_envelope",
        "runtime_execution_metadata",
        "title",
        "kind",
        "planned_kind",
        "source_kind",
        "requested_outputs",
        "manifest_index",
        "path",
        "mime_type",
        "size_bytes",
        "preview_text",
        "url",
        "created_at",
    ]
    assert payload["source_tool"] == "artifact.write"
    assert payload["workflow_node_id"] == "report"
    assert payload["group_run_id"] == "group-run-1"
    assert payload["runtime_execution_envelope"]["envelope_id"] == "artifact-envelope-1"
    assert payload["runtime_execution_envelope"]["requests"][0]["tool_name"] == (
        "artifact.write"
    )
    assert payload["runtime_execution_metadata"] == {"yachiyo_runtime_planner": True}
    assert payload["planned_kind"] == "markdown"
    assert payload["source_kind"] == "csv"
    assert payload["requested_outputs"] == ["report"]
    assert payload["manifest_index"] == 0


def test_public_run_event_mapping_preserves_runtime_trace_payload_fields() -> None:
    event = public_run_event_from_payload(
        {
            "event": "memory.write.add",
            "run_id": "run-1",
            "sequence": 7,
            "memory_id": "memory-1",
            "memory_kind": "preference",
            "skill_id": "skill-1",
            "skill_name": "Workspace Reviewer",
            "workflow_node_id": "node-1",
            "workflow_node_label": "Review",
            "member_agent_id": "agent-2",
            "group_id": "group-1",
            "artifact_path": "reports/out.md",
            "payload": {
                "skill_id": "skill-from-payload",
                "result": {"ok": True},
            },
            "visibility": "internal",
            "sensitivity": "public",
            "created_at": "2026-06-14T00:00:00Z",
        }
    )

    assert event.event_type == "memory.write.add"
    assert event.run_id == "run-1"
    assert event.sequence == 7
    assert event.visibility == "internal"
    assert event.sensitivity == "public"
    assert event.payload["memory_id"] == "memory-1"
    assert event.payload["memory_kind"] == "preference"
    assert event.payload["skill_id"] == "skill-from-payload"
    assert event.payload["skill_name"] == "Workspace Reviewer"
    assert event.payload["workflow_node_id"] == "node-1"
    assert event.payload["workflow_node_label"] == "Review"
    assert event.payload["member_agent_id"] == "agent-2"
    assert event.payload["group_id"] == "group-1"
    assert event.payload["artifact_path"] == "reports/out.md"
    assert event.payload["result"] == {"ok": True}
    assert "event" not in event.payload
    assert "visibility" not in event.payload


def test_public_run_event_mapping_promotes_run_correlation_fields() -> None:
    event = public_run_event_from_payload(
        {
            "event_type": "workflow.node.agent",
            "run_id": "child-run-1",
            "sequence": 3,
            "parent_run_id": "workflow-run-1",
            "source_run_id": "child-run-source-1",
            "payload": {
                "workflow_id": "workflow-1",
                "workflow_run_id": "workflow-run-1",
                "workflow_node_id": "research",
                "workflow_node_label": "Research",
                "group_id": "group-1",
                "run_group_id": "group-run-1",
                "core_id": "core-1",
                "workspace_id": "workspace-1",
                "task_id": "task-1",
                "member_agent_id": "agent-1",
                "member_agent_name": "Researcher",
            },
        }
    )

    assert event.parent_run_id == "workflow-run-1"
    assert event.source_run_id == "child-run-source-1"
    assert event.workflow_id == "workflow-1"
    assert event.workflow_run_id == "workflow-run-1"
    assert event.workflow_node_id == "research"
    assert event.workflow_node_label == "Research"
    assert event.group_id == "group-1"
    assert event.group_run_id == "group-run-1"
    assert event.run_group_id == "group-run-1"
    assert event.core_id == "core-1"
    assert event.workspace_id == "workspace-1"
    assert event.task_id == "task-1"
    assert event.agent_id == "agent-1"
    assert event.agent_name == "Researcher"
    assert event.member_agent_id == "agent-1"
    assert event.member_agent_name == "Researcher"
    assert event.source_runnable_id == "agent-1"
    assert event.source_runnable_name == "Researcher"
    assert event.payload["workflow_run_id"] == "workflow-run-1"
    assert event.payload["run_group_id"] == "group-run-1"
    assert event.payload["core_id"] == "core-1"
    assert event.payload["workspace_id"] == "workspace-1"
    assert event.payload["task_id"] == "task-1"


def test_secret_public_run_event_keeps_top_level_correlation_fields() -> None:
    event = public_run_event_from_payload(
        {
            "event_type": "agent.tool.call",
            "run_id": "child-run-1",
            "parent_run_id": "workflow-run-1",
            "sensitivity": "secret",
            "payload": {
                "workflow_run_id": "workflow-run-1",
                "group_run_id": "group-run-1",
                "core_id": "core-secret",
                "workspace_id": "workspace-secret",
                "task_id": "task-secret",
                "tool": "terminal.run",
                "command": "secret-token",
            },
        }
    )

    assert event.parent_run_id == "workflow-run-1"
    assert event.workflow_run_id == "workflow-run-1"
    assert event.group_run_id == "group-run-1"
    assert event.core_id == "core-secret"
    assert event.workspace_id == "workspace-secret"
    assert event.task_id == "task-secret"
    assert event.payload == {"redacted": True, "reason": "secret_event"}


def test_agent_definition_snapshot_keeps_editing_fields() -> None:
    snapshot = AgentDefinitionSnapshot(
        agent_id="agent-1",
        name="Planner",
        description="Plans work",
        instructions="Use concise steps.",
        persona_prompt="You are Yachiyo.",
        model_config={"provider": "model_profile"},
        skill_ids=["skill-1"],
    )

    payload = _json(snapshot)

    assert payload["instructions"] == "Use concise steps."
    assert payload["persona_prompt"] == "You are Yachiyo."
    assert payload["model_config"] == {"provider": "model_profile"}


def test_group_run_and_workflow_snapshots_keep_group_and_workflow_fields() -> None:
    member = AgentGroupMemberSnapshot(agent_id="agent-1", name="Planner", role="planner")
    group = AgentGroupSnapshot(
        group_id="group-1",
        name="Research team",
        description="Multi-agent research group",
        members=[member],
        mode="debate",
        moderator_agent_id="agent-1",
        default_model="gpt-test",
        memory_scope="hybrid",
        tool_policy_id="policy-1",
    )
    group_run = GroupRunSnapshot(
        group_run_id="group-run-1",
        group_id="group-1",
        title="Compare options",
        status="running",
        objective="Find the safest option",
        participants=[member],
        runtime_execution_envelope=RuntimeExecutionEnvelopeSnapshot(
            envelope_id="group-envelope-1",
            decision_id="decision-1",
            plan_id="plan-1",
            intent_kind="group_collaboration",
            requests=[
                RuntimeExecutionRequestSnapshot(
                    request_id="group-request-1",
                    tool_name="group.delegate",
                    runtime_stage="orchestrate",
                )
            ],
            runtime_stage_counts={"orchestrate": 1},
        ),
        events=[
            PublicRunEvent(
                run_id="group-run-1",
                event_type="group.member.started",
                detail="Planner started",
            )
        ],
        tool_calls=[
            ToolCallSnapshot(
                tool_call_id="tool-1",
                run_id="agent-run-1",
                source_runnable_id="agent-1",
                group_run_id="group-run-1",
                tool_name="workspace.read",
                status="completed",
            )
        ],
        memory_traces=[
            MemoryTraceSnapshot(
                trace_id="memory-trace-1",
                run_id="agent-run-1",
                event_type="memory.retrieved",
                source_runnable_id="agent-1",
                group_run_id="group-run-1",
                title="Memory retrieved",
            )
        ],
        skill_traces=[
            SkillTraceSnapshot(
                trace_id="skill-trace-1",
                run_id="agent-run-1",
                event_type="skill.selected",
                source_runnable_id="agent-1",
                group_run_id="group-run-1",
                title="Skill selected",
            )
        ],
    )
    workflow = WorkflowSnapshot(
        workflow_id="workflow-1",
        name="Review workflow",
        nodes=[{"id": "start", "type": "start"}],
        edges=[],
        default_input_schema={"type": "object"},
    )
    workflow_run = WorkflowRunSnapshot(
        run_id="workflow-run-1",
        workflow_run_id="workflow-run-1",
        workflow_id="workflow-1",
        status="running",
        title="Review docs",
        objective="Review docs",
        events=[
            PublicRunEvent(
                run_id="workflow-run-1",
                event_type="workflow.node.started",
                detail="Start",
            )
        ],
        children=[RunTimelineChildSnapshot(run_id="agent-run-1", status="running")],
    )

    assert _json(group)["mode"] == "debate"
    assert _json(group)["members"][0]["role"] == "planner"
    assert _json(group_run)["participants"][0]["agent_id"] == "agent-1"
    assert _json(group_run)["runtime_execution_envelope"]["intent_kind"] == "group_collaboration"
    assert _json(group_run)["runtime_execution_envelope"]["requests"][0]["runtime_stage"] == "orchestrate"
    assert _json(group_run)["events"][0]["event_type"] == "group.member.started"
    assert _json(group_run)["tool_calls"][0]["tool_name"] == "workspace.read"
    assert _json(group_run)["memory_traces"][0]["event_type"] == "memory.retrieved"
    assert _json(group_run)["skill_traces"][0]["event_type"] == "skill.selected"
    assert _json(workflow)["default_input_schema"] == {"type": "object"}
    assert _json(workflow_run)["run_id"] == "workflow-run-1"
    assert _json(workflow_run)["workflow_id"] == "workflow-1"
    assert _json(workflow_run)["events"][0]["event_type"] == "workflow.node.started"
    assert _json(workflow_run)["children"][0]["run_id"] == "agent-run-1"


def test_group_run_snapshot_rolls_child_debug_state_into_participants() -> None:
    group_run = group_run_snapshot_from_payload(
        {
            "group_run_id": "group-run-1",
            "group_id": "group-1",
            "title": "Desktop team",
            "status": "approval_required",
            "objective": "Open Music and report back",
            "participants": [
                {"agent_id": "agent-1", "name": "Music Agent", "role": "operator"},
                {"agent_id": "agent-2", "name": "Reviewer", "role": "reviewer"},
            ],
            "runs": [
                {
                    "run_id": "run-1",
                    "agent_id": "agent-1",
                    "status": "approval_required",
                    "user_goal": "Play a song",
                    "tool_calls": [
                        {
                            "tool_call_id": "tool-1",
                            "tool_name": "media.apple_music_play",
                            "status": "completed",
                            "source_runnable_id": "agent-1",
                        }
                    ],
                    "pending_approval": {
                        "approval_id": "approval-1",
                        "title": "Approve message send",
                        "tool_name": "desktop.type_text",
                        "status": "pending",
                    },
                    "artifacts": [
                        {
                            "artifact_id": "artifact-1",
                            "title": "Music search",
                            "kind": "screenshot",
                            "path": "music.png",
                        }
                    ],
                }
            ],
        }
    )

    music_agent = group_run.participants[0]
    reviewer = group_run.participants[1]
    assert music_agent.run_id == "run-1"
    assert music_agent.run_status == "approval_required"
    assert music_agent.tool_calls[0].tool_name == "media.apple_music_play"
    assert music_agent.pending_approvals[0].approval_id == "approval-1"
    assert music_agent.artifacts[0].path == "music.png"
    assert reviewer.tool_calls == []
    assert [approval.approval_id for approval in group_run.pending_approvals] == ["approval-1"]
    assert [artifact.path for artifact in group_run.shared_artifacts] == ["music.png"]
    assert group_run.runtime_debug is not None
    assert group_run.runtime_debug.group_id == "group-1"
    assert group_run.runtime_debug.group_run_id == "group-run-1"
    assert group_run.runtime_debug.tool_call_count == 1
    assert group_run.runtime_debug.pending_approval_count == 1
    assert group_run.runtime_debug.artifact_count == 1
    assert group_run.runtime_debug.child_run_count == 1


def test_agent_definition_snapshot_serializes_model_config_alias() -> None:
    snapshot = AgentDefinitionSnapshot(
        agent_id="agent-1",
        name="Planner",
        model_settings={"provider": "model_profile"},
    )

    payload = _json(snapshot)

    assert "model_config" in payload
    assert "model_settings" not in payload
    assert payload["model_config"] == {"provider": "model_profile"}


def test_skill_snapshot_keeps_skill_library_fields() -> None:
    snapshot = SkillSnapshot(
        skill_id="skill-1",
        name="Workspace Reviewer",
        description="Reviews workspace files",
        source_path="/skills/workspace-reviewer",
        local_path="/managed/skills/workspace-reviewer",
        folder_id="folder-1",
        folder_name="Review",
        source_type="local_dir",
        origin_path="/skills/workspace-reviewer",
        source_ref="workspace-reviewer",
        content_hash="hash-1",
        last_synced_at="2026-06-14T00:00:00Z",
        sync_status="imported",
        content_summary="Review project files",
        skill_markdown="# Workspace Reviewer",
        asset_paths=["assets/icon.png"],
        enabled=True,
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "skill_id",
        "name",
        "description",
        "source_path",
        "local_path",
        "folder_id",
        "folder_name",
        "source_type",
        "origin_path",
        "source_ref",
        "content_hash",
        "last_synced_at",
        "sync_status",
        "content_summary",
        "skill_markdown",
        "asset_paths",
        "enabled",
        "created_at",
        "updated_at",
    ]
    assert payload["asset_paths"] == ["assets/icon.png"]


def test_skill_folder_snapshot_keeps_skill_library_grouping_fields() -> None:
    snapshot = SkillFolderSnapshot(
        folder_id="folder-1",
        name="Review",
        description="Review skills",
        source_scope="installed",
        sort_order=2,
        skill_count=3,
        installed_count=2,
        native_count=1,
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "folder_id",
        "name",
        "description",
        "source_scope",
        "sort_order",
        "skill_count",
        "installed_count",
        "native_count",
        "created_at",
        "updated_at",
    ]
    assert payload["source_scope"] == "installed"


def test_skill_source_root_snapshot_keeps_skill_discovery_fields() -> None:
    snapshot = SkillSourceRootSnapshot(
        path="/skills/native",
        source_type="native_global",
        library="native",
        exists=True,
        skill_count=4,
    )

    payload = _json(snapshot)

    assert list(payload) == ["path", "source_type", "library", "exists", "skill_count"]
    assert payload["library"] == "native"


def test_memory_snapshot_keeps_runtime_memory_fields() -> None:
    snapshot = MemorySnapshot(
        memory_id="memory-1",
        scope="global",
        kind="preference",
        content="Prefer concise status updates.",
        source_session_id="chat-1",
        source_message_id="message-1",
        source_task_id="task-1",
        source_run_id="run-1",
        confidence=0.9,
        pinned=True,
        user_confirmed=True,
        created_at="2026-06-14T00:00:00Z",
        updated_at="2026-06-14T00:00:01Z",
        deleted_at=None,
    )

    payload = _json(snapshot)

    assert list(payload) == [
        "memory_id",
        "scope",
        "kind",
        "content",
        "content_hash",
        "project_id",
        "source_session_id",
        "source_message_id",
        "source_task_id",
        "source_run_id",
        "confidence",
        "pinned",
        "user_confirmed",
        "enabled",
        "actor",
        "created_at",
        "updated_at",
        "deleted_at",
    ]
    assert payload["content_hash"] is None
    assert payload["project_id"] is None
    assert payload["enabled"] is True
    assert payload["actor"] == "agent_tool"
    assert payload["source_run_id"] == "run-1"
    assert payload["pinned"] is True


def test_future_task_snapshots_keep_runtime_schedule_fields() -> None:
    future_task = FutureTaskSnapshot(
        future_task_id="future-1",
        title="Follow up later",
        prompt="Follow up on the report",
        runnable_id="agent-1",
        runnable_name="Planner",
        scheduled_at_epoch=1781433600.0,
        source_run_id="run-source-1",
        last_run_id="run-1",
        run_count=1,
    )
    triggered = FutureTaskTriggerResultSnapshot(
        future_task=future_task,
        run=RunTimelineSnapshot(run_id="run-1", status="completed"),
    )

    payload = _json(future_task)
    triggered_payload = _json(triggered)

    assert list(payload) == [
        "future_task_id",
        "title",
        "prompt",
        "runnable_id",
        "runnable_name",
        "status",
        "scheduled_at_epoch",
        "cron",
        "source_run_id",
        "last_run_id",
        "run_count",
        "error",
        "created_at",
        "updated_at",
        "cancelled_at",
        "trigger_in_flight",
    ]
    assert payload["last_run_id"] == "run-1"
    assert triggered_payload["future_task"]["future_task_id"] == "future-1"
    assert triggered_payload["run"]["run_id"] == "run-1"


def test_start_chat_task_request_keeps_runnable_target_fields() -> None:
    request = StartChatTaskRequest(
        prompt="Build report",
        conversation_id="chat-1",
        workflow_id="workflow-1",
        group_id="group-1",
        metadata={"client_task_id": "task-workflow-1"},
    )

    payload = request.model_dump(mode="json", exclude_none=True)

    assert payload == {
        "prompt": "Build report",
        "conversation_id": "chat-1",
        "workflow_id": "workflow-1",
        "group_id": "group-1",
        "metadata": {"client_task_id": "task-workflow-1"},
    }


def test_start_chat_task_request_keeps_public_attachments() -> None:
    request = StartChatTaskRequest(
        prompt="Analyze this image",
        conversation_id="chat-1",
        attachments=[
            {
                "id": "attachment-1",
                "name": "chart.png",
                "mime_type": "image/png",
                "size": 8,
                "data_url": "data:image/png;base64,ZmFrZS1wbmc=",
            }
        ],
    )

    payload = request.model_dump(mode="json", exclude_none=True)

    assert payload["attachments"] == [
        {
            "id": "attachment-1",
            "name": "chart.png",
            "mime_type": "image/png",
            "size": 8,
            "data_url": "data:image/png;base64,ZmFrZS1wbmc=",
        }
    ]


def test_studio_save_requests_keep_public_field_names() -> None:
    agent = SaveAgentRequest(
        agent_id="agent-1",
        name="Planner",
        model_config={"provider": "model_profile"},
        tool_policy={"allowed_tools": ["workspace.read"]},
        skill_ids=["skill-1"],
    )
    group = SaveAgentGroupRequest(
        group_id="group-1",
        name="Research Team",
        members=[SaveAgentGroupMemberRequest(agent_id="agent-1", role="planner")],
        mode="debate",
        memory_scope="hybrid",
    )
    workflow = SaveWorkflowRequest(
        workflow_id="workflow-1",
        name="Review workflow",
        nodes=[{"id": "start", "type": "start"}],
        edges=[],
        default_input_schema={"type": "object"},
    )

    agent_payload = agent.model_dump(mode="json", by_alias=True, exclude_none=True)
    group_payload = group.model_dump(mode="json", exclude_none=True)
    workflow_payload = workflow.model_dump(mode="json", exclude_none=True)

    assert "model_config" in agent_payload
    assert "model_settings" not in agent_payload
    assert agent_payload["model_config"] == {"provider": "model_profile"}
    assert group_payload["members"][0]["agent_id"] == "agent-1"
    assert group_payload["mode"] == "debate"
    assert workflow_payload["nodes"][0]["type"] == "start"
    assert workflow_payload["default_input_schema"] == {"type": "object"}
