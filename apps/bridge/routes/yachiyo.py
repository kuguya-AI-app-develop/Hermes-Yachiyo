"""Yachiyo public Agent facade routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from apps.bridge.routes import yachiyo_chat_handlers
from apps.bridge.routes import yachiyo_studio_handlers
from apps.bridge.routes.yachiyo_models import (
    AgentSkillBody,
    DesktopProviderSessionBody,
    FutureTaskCancelBody,
    FutureTaskTriggerBody,
    MemoryBody,
    PlanExecutionBody,
    PlanTaskBody,
    RerunRunBody,
    RunReplanRecoveryActionBody,
    RunToolRecoveryActionBody,
    RestrictedToolPluginInstallBody,
    RestrictedToolPluginUpdateBody,
    SkillFolderBody,
    SkillImportBody,
    SkillInstallBody,
    SkillUpdateBody,
    StartAgentRunBody,
    StartGroupRunBody,
    StartNextReplanContinuationBody,
    StartPlannerOrchestrationBody,
    StartWorkflowRunBody,
    TaskApprovalRequest,
    VirtualDesktopGuestProvisionBody,
)
from apps.shell.yachiyo_agent import (
    AgentDeskFileEventRequest,
    SaveAgentDeskFileRequest,
    SaveAgentDeskNoteRequest,
    SaveAgentGroupRequest,
    SaveAgentRequest,
    SaveWorkflowRequest,
    StartChatTaskRequest,
    StartGroupRunRequest,
    StartWorkflowRunRequest,
)

router = APIRouter(prefix="/yachiyo", tags=["Yachiyo Agent"])


@router.get("/readiness")
@router.get("/chat/readiness")
async def readiness(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_chat_handlers.readiness(http_request)


@router.get("/runnables")
@router.get("/chat/runnables")
async def list_chat_runnables(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_chat_handlers.list_runnables(http_request)


@router.get("/tasks")
@router.get("/chat/tasks")
async def list_tasks(
    conversation_id: str | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.list_tasks(conversation_id, http_request)


@router.post("/tasks")
@router.post("/chat/tasks")
async def start_task(
    request: StartChatTaskRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.start_task(request, http_request)


@router.post("/tasks/{task_id}/replan-recovery-actions/start")
@router.post("/chat/tasks/{task_id}/replan-recovery-actions/start")
async def start_task_replan_recovery_action(
    task_id: str,
    request: RunReplanRecoveryActionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.start_replan_recovery_action(
        task_id,
        request,
        http_request,
    )


@router.post("/tasks/{task_id}/replan-recovery-actions/start-next")
@router.post("/chat/tasks/{task_id}/replan-recovery-actions/start-next")
async def start_task_next_replan_continuation(
    task_id: str,
    request: StartNextReplanContinuationBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.start_next_replan_continuation(
        task_id,
        request,
        http_request,
    )


@router.post("/tasks/plan")
@router.post("/chat/tasks/plan")
async def plan_task_execution(
    request: PlanExecutionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.plan_task_execution(request, http_request)


@router.get("/tasks/{task_id}")
@router.get("/chat/tasks/{task_id}")
async def get_task(task_id: str, http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_chat_handlers.get_task(task_id, http_request)


@router.get("/tasks/{task_id}/timeline")
@router.get("/chat/tasks/{task_id}/timeline")
async def get_task_timeline(
    task_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.get_task_timeline(task_id, http_request)


@router.get("/tasks/{task_id}/events")
@router.get("/chat/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    http_request: Request = None,  # type: ignore[assignment]
    after_sequence: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.get_task_events(
        task_id,
        http_request,
        after_sequence,
        limit,
    )


@router.get("/tasks/{task_id}/artifacts/{artifact_path:path}")
@router.get("/chat/tasks/{task_id}/artifacts/{artifact_path:path}")
async def get_task_artifact(
    task_id: str,
    artifact_path: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.read_task_artifact(
        task_id,
        artifact_path,
        http_request,
    )


@router.post("/tasks/{task_id}/approve")
@router.post("/chat/tasks/{task_id}/approve")
async def approve_task(
    task_id: str,
    request: TaskApprovalRequest | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.approve_task(task_id, request, http_request)


@router.post("/tasks/{task_id}/approvals/{approval_id}/approve")
@router.post("/chat/tasks/{task_id}/approvals/{approval_id}/approve")
async def approve_task_approval(
    task_id: str,
    approval_id: str,
    request: TaskApprovalRequest | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.approve_task(
        task_id,
        _task_approval_request_with_id(request, approval_id),
        http_request,
    )


@router.post("/tasks/{task_id}/reject")
@router.post("/chat/tasks/{task_id}/reject")
async def reject_task(
    task_id: str,
    request: TaskApprovalRequest | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.reject_task(task_id, request, http_request)


@router.post("/tasks/{task_id}/approvals/{approval_id}/reject")
@router.post("/chat/tasks/{task_id}/approvals/{approval_id}/reject")
async def reject_task_approval(
    task_id: str,
    approval_id: str,
    request: TaskApprovalRequest | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.reject_task(
        task_id,
        _task_approval_request_with_id(request, approval_id),
        http_request,
    )


@router.post("/tasks/{task_id}/cancel")
@router.post("/chat/tasks/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_chat_handlers.cancel_task(task_id, http_request)


def _task_approval_request_with_id(
    request: TaskApprovalRequest | None,
    approval_id: str,
) -> TaskApprovalRequest:
    payload = request.model_dump(exclude_none=True) if request is not None else {}
    payload["approval_id"] = approval_id
    return TaskApprovalRequest.model_validate(payload)


@router.get("/studio/agents")
async def list_studio_agents(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_agents(http_request)


@router.get("/studio/tools")
async def list_studio_tools(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_tool_catalog(http_request)


@router.get("/studio/tools/desktop-provider/session")
async def get_studio_desktop_provider_session(
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.desktop_provider_session_status(http_request)


@router.post("/studio/tools/desktop-provider/session/start")
async def start_studio_desktop_provider_session(
    request: DesktopProviderSessionBody | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_desktop_provider_session(
        request,
        http_request,
    )


@router.post("/studio/tools/desktop-provider/session/stop")
async def stop_studio_desktop_provider_session(
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.stop_desktop_provider_session(http_request)


@router.post("/studio/tools/desktop-provider/provision")
async def provision_studio_virtual_desktop_guest(
    request: VirtualDesktopGuestProvisionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.provision_virtual_desktop_guest(
        request,
        http_request,
    )


@router.post("/studio/planner")
async def plan_studio_task(
    request: PlanTaskBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.plan_task(request, http_request)


@router.post("/studio/planner/execution")
async def plan_studio_execution(
    request: PlanExecutionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.plan_execution(request, http_request)


@router.post("/studio/planner/orchestration/start")
async def start_studio_planner_orchestration(
    request: StartPlannerOrchestrationBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_planner_orchestration(request, http_request)


@router.get("/studio/tools/restricted-plugins")
async def list_studio_restricted_tool_plugins(
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.list_restricted_tool_plugins(http_request)


@router.post("/studio/tools/restricted-plugins")
async def install_studio_restricted_tool_plugin(
    request: RestrictedToolPluginInstallBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.install_restricted_tool_plugin(
        request,
        http_request,
    )


@router.patch("/studio/tools/restricted-plugins/{plugin_id}")
async def update_studio_restricted_tool_plugin(
    plugin_id: str,
    request: RestrictedToolPluginUpdateBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_restricted_tool_plugin(
        plugin_id,
        request,
        http_request,
    )


@router.delete("/studio/tools/restricted-plugins/{plugin_id}")
async def uninstall_studio_restricted_tool_plugin(
    plugin_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.uninstall_restricted_tool_plugin(
        plugin_id,
        http_request,
    )


@router.post("/studio/agents")
async def save_studio_agent(
    request: SaveAgentRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.save_agent(request, http_request)


@router.patch("/studio/agents/{agent_id}")
async def update_studio_agent(
    agent_id: str,
    request: SaveAgentRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_agent(agent_id, request, http_request)


@router.get("/studio/agents/{agent_id}")
async def get_studio_agent(
    agent_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_agent(agent_id, http_request)


@router.delete("/studio/agents/{agent_id}")
async def delete_studio_agent(
    agent_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_agent(agent_id, http_request)


@router.post("/studio/agents/{agent_id}/test-model")
async def test_studio_agent_model(
    agent_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.test_agent_model(agent_id, http_request)


@router.get("/studio/agents/{agent_id}/desk")
async def get_studio_agent_desk(
    agent_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_agent_desk(agent_id, http_request)


@router.post("/studio/agents/{agent_id}/desk/note")
async def write_studio_agent_desk_note(
    agent_id: str,
    request: SaveAgentDeskNoteRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.write_agent_desk_note(agent_id, request, http_request)


@router.post("/studio/agents/{agent_id}/desk/files")
async def write_studio_agent_desk_file(
    agent_id: str,
    request: SaveAgentDeskFileRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.write_agent_desk_file(agent_id, request, http_request)


@router.post("/studio/agents/{agent_id}/desk/file-events")
async def trigger_studio_agent_desk_file_event(
    agent_id: str,
    request: AgentDeskFileEventRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.trigger_agent_desk_file_event(
        agent_id,
        request,
        http_request,
    )


@router.post("/studio/agents/{agent_id}/skills")
async def attach_studio_agent_skill(
    agent_id: str,
    request: AgentSkillBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.attach_agent_skill(agent_id, request, http_request)


@router.delete("/studio/agents/{agent_id}/skills/{skill_id}")
async def detach_studio_agent_skill(
    agent_id: str,
    skill_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.detach_agent_skill(agent_id, skill_id, http_request)


@router.get("/studio/skills")
async def list_studio_skills(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_skills(http_request)


@router.get("/studio/skills/sources")
async def list_studio_skill_sources(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_skill_sources(http_request)


@router.post("/studio/skills/import")
async def import_studio_skill(
    request: SkillImportBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.import_skill(request, http_request)


@router.post("/studio/skills/sync")
async def sync_studio_native_skills(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.sync_native_skills(http_request)


@router.post("/studio/skills/install")
async def install_studio_skill(
    request: SkillInstallBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.install_skill(request, http_request)


@router.patch("/studio/skills/{skill_id}")
async def update_studio_skill(
    skill_id: str,
    request: SkillUpdateBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_skill(skill_id, request, http_request)


@router.delete("/studio/skills/{skill_id}")
async def delete_studio_skill(
    skill_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_skill(skill_id, http_request)


@router.get("/studio/skill-folders")
async def list_studio_skill_folders(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_skill_folders(http_request)


@router.post("/studio/skill-folders")
async def create_studio_skill_folder(
    request: SkillFolderBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.create_skill_folder(request, http_request)


@router.patch("/studio/skill-folders/{folder_id}")
async def update_studio_skill_folder(
    folder_id: str,
    request: SkillFolderBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_skill_folder(folder_id, request, http_request)


@router.delete("/studio/skill-folders/{folder_id}")
async def delete_studio_skill_folder(
    folder_id: str,
    delete_skills: bool = False,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_skill_folder(folder_id, delete_skills, http_request)


@router.get("/studio/memories")
async def list_studio_memories(
    include_deleted: bool = False,
    limit: int = 100,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.list_memories(include_deleted, limit, http_request)


@router.post("/studio/memories")
async def create_studio_memory(
    request: MemoryBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.create_memory(request, http_request)


@router.patch("/studio/memories/{memory_id}")
async def update_studio_memory(
    memory_id: str,
    request: MemoryBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_memory(memory_id, request, http_request)


@router.post("/studio/memories/{memory_id}/consent-capability")
async def issue_studio_memory_consent_capability(
    memory_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.issue_memory_consent_capability(
        memory_id,
        http_request,
    )


@router.delete("/studio/memories/{memory_id}")
async def delete_studio_memory(
    memory_id: str,
    reason: str = "",
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_memory(memory_id, reason, http_request)


@router.get("/studio/future-tasks")
async def list_studio_future_tasks(
    include_finished: bool = True,
    limit: int = 100,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.list_future_tasks(include_finished, limit, http_request)


@router.post("/studio/future-tasks/trigger-due")
async def trigger_due_studio_future_tasks(
    request: FutureTaskTriggerBody | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.trigger_due_future_tasks(request, http_request)


@router.post("/studio/future-tasks/{future_task_id}/cancel")
async def cancel_studio_future_task(
    future_task_id: str,
    request: FutureTaskCancelBody | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.cancel_future_task(future_task_id, request, http_request)


@router.post("/studio/agents/{agent_id}/runs")
async def start_studio_agent_run(
    agent_id: str,
    request: StartAgentRunBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_agent_run(agent_id, request, http_request)


@router.get("/studio/groups")
async def list_studio_groups(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_groups(http_request)


@router.post("/studio/groups")
async def save_studio_group(
    request: SaveAgentGroupRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.save_group(request, http_request)


@router.patch("/studio/groups/{group_id}")
async def update_studio_group(
    group_id: str,
    request: SaveAgentGroupRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_group(group_id, request, http_request)


@router.get("/studio/groups/{group_id}")
async def get_studio_group(
    group_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_group(group_id, http_request)


@router.post("/studio/groups/{group_id}/runs")
async def start_studio_group_run(
    group_id: str,
    request: StartGroupRunBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_group_run(group_id, request, http_request)


@router.get("/studio/group-runs")
async def list_studio_group_runs(
    http_request: Request = None,  # type: ignore[assignment]
    limit: int = 50,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.list_group_runs(limit, http_request)


@router.get("/studio/group-runs/{group_run_id}")
async def get_studio_group_run(
    group_run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_group_run(group_run_id, http_request)


@router.post("/studio/group-runs/{group_run_id}/replan-recovery-actions/start")
async def start_studio_group_run_replan_recovery_action(
    group_run_id: str,
    request: RunReplanRecoveryActionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_group_replan_recovery_action(
        group_run_id,
        request,
        http_request,
    )


@router.post("/studio/group-runs/{group_run_id}/replan-recovery-actions/start-next")
async def start_studio_group_run_next_replan_continuation(
    group_run_id: str,
    request: StartNextReplanContinuationBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_next_group_replan_continuation(
        group_run_id,
        request,
        http_request,
    )


@router.post("/studio/group-runs/{group_run_id}/tool-recovery-actions/start")
async def start_studio_group_run_tool_recovery_action(
    group_run_id: str,
    request: RunToolRecoveryActionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_group_tool_recovery_action(
        group_run_id,
        request,
        http_request,
    )


@router.get("/studio/group-runs/{group_run_id}/events")
async def get_studio_group_run_events(
    group_run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
    after_sequence: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_group_run_events(
        group_run_id,
        http_request,
        after_sequence,
        limit,
    )


@router.get("/studio/workflows")
async def list_studio_workflows(http_request: Request = None) -> dict[str, Any]:  # type: ignore[assignment]
    return await yachiyo_studio_handlers.list_workflows(http_request)


@router.post("/studio/workflows")
async def save_studio_workflow(
    request: SaveWorkflowRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.save_workflow(request, http_request)


@router.patch("/studio/workflows/{workflow_id}")
async def update_studio_workflow(
    workflow_id: str,
    request: SaveWorkflowRequest,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.update_workflow(workflow_id, request, http_request)


@router.get("/studio/workflows/{workflow_id}")
async def get_studio_workflow(
    workflow_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_workflow(workflow_id, http_request)


@router.delete("/studio/workflows/{workflow_id}")
async def delete_studio_workflow(
    workflow_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_workflow(workflow_id, http_request)


@router.post("/studio/workflows/{workflow_id}/runs")
async def start_studio_workflow_run(
    workflow_id: str,
    request: StartWorkflowRunBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_workflow_run(workflow_id, request, http_request)


@router.get("/studio/runs/{run_id}/timeline")
async def get_studio_run_timeline(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_run_timeline(run_id, http_request)


@router.get("/studio/runs")
async def list_studio_runs(
    http_request: Request = None,  # type: ignore[assignment]
    limit: int = 50,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.list_runs(limit, http_request)


@router.get("/studio/runs/{run_id}")
async def get_studio_run(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_run_timeline(run_id, http_request)


@router.post("/studio/runs/{run_id}/rerun")
async def rerun_studio_run(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
    request_body: RerunRunBody | None = None,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.rerun_run(run_id, request_body, http_request)


@router.post("/studio/runs/{run_id}/replan-recovery-actions/start")
async def start_studio_run_replan_recovery_action(
    run_id: str,
    request: RunReplanRecoveryActionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_replan_recovery_action(
        run_id,
        request,
        http_request,
    )


@router.post("/studio/runs/{run_id}/replan-recovery-actions/start-next")
async def start_studio_run_next_replan_continuation(
    run_id: str,
    request: StartNextReplanContinuationBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_next_replan_continuation(
        run_id,
        request,
        http_request,
    )


@router.post("/studio/runs/{run_id}/tool-recovery-actions/start")
async def start_studio_run_tool_recovery_action(
    run_id: str,
    request: RunToolRecoveryActionBody,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.start_tool_recovery_action(
        run_id,
        request,
        http_request,
    )


@router.post("/studio/runs/{run_id}/cancel")
async def cancel_studio_run(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.cancel_run(run_id, http_request)


@router.delete("/studio/runs/{run_id}")
async def delete_studio_run(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.delete_run(run_id, http_request)


@router.post("/studio/runs/{run_id}/approval/approve")
async def approve_studio_run_approval(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
    request: TaskApprovalRequest | None = None,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.approve_run_approval(run_id, request, http_request)


@router.post("/studio/runs/{run_id}/approval/reject")
async def reject_studio_run_approval(
    run_id: str,
    request: TaskApprovalRequest | None = None,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.reject_run_approval(run_id, request, http_request)


@router.get("/studio/runs/{run_id}/artifacts/{artifact_path:path}")
async def get_studio_run_artifact(
    run_id: str,
    artifact_path: str,
    http_request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.read_run_artifact(run_id, artifact_path, http_request)


@router.get("/studio/runs/{run_id}/events")
async def get_studio_run_events(
    run_id: str,
    http_request: Request = None,  # type: ignore[assignment]
    after_sequence: int = 0,
    limit: int = 200,
) -> dict[str, Any]:
    return await yachiyo_studio_handlers.get_run_events(
        run_id,
        after_sequence,
        limit,
        http_request,
    )
