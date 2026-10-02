"""Public Yachiyo Agent contracts shared by Chat and Agent Studio."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

TaskStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "completed",
    "failed",
    "cancelled",
]
GroupMode = Literal["moderated", "round_robin", "debate", "pipeline", "parallel", "custom"]
MemoryScope = Literal["shared", "per_agent", "hybrid"]
ApprovalStatus = Literal["pending", "approved", "rejected", "cancelled", "expired"]
DesktopExecutionRisk = Literal["low", "medium", "high"]
DesktopExecutionMode = Literal[
    "preview",
    "tool_native",
    "read_only_observation",
    "supervised_live",
    "sandbox_preferred",
    "handoff_required",
]
DesktopIsolationKind = Literal[
    "none",
    "process",
    "browser_profile",
    "browser_target",
    "background_desktop",
    "sandbox_desktop",
    "headless",
    "user_handoff",
]
DesktopExecutionPolicyMode = Literal[
    "allow",
    "preview",
    "preview_input",
    "sandbox_preferred",
    "handoff_required",
    "supervised_live",
]
RuntimeExecutionPreferredEnvironment = Literal[
    "structured_runtime",
    "background_desktop",
    "isolated_desktop",
    "user_foreground",
    "user_handoff",
]
RuntimeExecutionInteractionMode = Literal[
    "background",
    "read_only",
    "foreground",
    "handoff",
]
RecoveryActionKind = Literal[
    "permission_recovery",
    "retry_original",
    "observe_desktop_state",
    "observe_desktop_controls",
    "supervised_live_retry",
    "sandbox_desktop_handoff",
]
TaskWorkspaceItemKind = Literal[
    "input",
    "scratch",
    "artifact",
    "checkpoint",
    "todo",
    "memory",
    "other",
]
TaskTodoStatus = Literal["pending", "in_progress", "blocked", "completed", "skipped"]
TaskCheckpointStatus = Literal[
    "planned",
    "ready",
    "waiting_approval",
    "blocked",
    "completed",
]
TaskReplanStatus = Literal["requested", "planned", "running", "completed", "blocked"]
TaskIntentKind = Literal[
    "desktop_operation",
    "data_analysis",
    "report_generation",
    "web_research",
    "file_operation",
    "file_access",
    "file_organization",
    "communication",
    "schedule",
    "media_playback",
    "system_control",
    "clipboard_operation",
    "information_capture",
    "code_task",
    "workflow_orchestration",
    "multi_agent",
    "general",
]
CapabilityCategory = Literal[
    "desktop",
    "data",
    "file",
    "terminal",
    "browser",
    "artifact",
    "capture",
    "clipboard",
    "communication",
    "schedule",
    "media",
    "system",
    "workflow",
    "group",
    "memory",
    "skill",
    "general",
]


class _PublicSnapshot(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ReadinessSnapshot(_PublicSnapshot):
    ready: bool
    status: str = ""
    message: str | None = None
    capabilities: dict[str, Any] = Field(default_factory=dict)


class DesktopExecutionCapabilitySnapshot(_PublicSnapshot):
    available: bool = False
    platform: str = ""
    missing_permissions: list[str] = Field(default_factory=list)
    blocking_conditions: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    degraded_tools: list[str] = Field(default_factory=list)
    unavailable_tools: list[str] = Field(default_factory=list)
    provider_supported_tools: list[str] = Field(default_factory=list)
    provider_ready_tools: list[str] = Field(default_factory=list)
    provider_blocked_tools: list[str] = Field(default_factory=list)
    risk_default: DesktopExecutionRisk = "low"
    diagnostic_route: str | None = None


class DesktopExecutionModeSnapshot(_PublicSnapshot):
    mode: DesktopExecutionMode | str = "tool_native"
    isolation: DesktopIsolationKind | str = "none"
    foreground_control: bool = False
    keyboard_mouse_capture: bool = False
    sandbox_recommended: bool = False
    user_handoff_recommended: bool = False
    approval_recommended: bool = False
    reason: str = ""
    mitigations: list[str] = Field(default_factory=list)


class DesktopExecutionPolicySnapshot(_PublicSnapshot):
    mode: DesktopExecutionPolicyMode | str = "allow"
    allow_live_foreground: bool | None = None
    prefer_background_desktop: bool = False
    prefer_isolated_desktop: bool = False
    avoid_user_foreground_takeover: bool = False
    require_sandbox_for_keyboard_mouse: bool = False
    allow_media_control: bool = True
    source: str = ""
    reason: str = ""


class DesktopProviderHealthSnapshot(_PublicSnapshot):
    ok: bool = False
    checked: bool = False
    status: str = "not_checked"
    status_code: int | None = None
    provider_kind: DesktopIsolationKind | str = "sandbox_desktop"
    provider_id: str = ""
    provider_version: str = ""
    endpoint_origin: str = ""
    endpoint_path: str = ""
    transport: str = ""
    health_report: dict[str, Any] = Field(default_factory=dict)
    cached: bool = False
    blocking_conditions: list[str] = Field(default_factory=list)
    supported_tools: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    foreground_mutation_supported: bool | None = None
    keyboard_mouse_capture_supported: bool | None = None
    desktop_session_kind: str = ""
    desktop_session_isolated: bool | None = None
    foreground_takeover_required: bool | None = None
    desktop_backend_kind: str = ""
    desktop_backend_is_loopback: bool | None = None
    desktop_backend_ready_for_public_release: bool | None = None
    requires_real_virtual_desktop_backend: bool | None = None
    requires_real_sandbox_for: list[str] = Field(default_factory=list)
    error: str = ""


class DesktopProviderConformanceSnapshot(_PublicSnapshot):
    ok: bool = False
    mode: str = ""
    runtime_checked: bool = False
    release_candidate: bool = False
    public_release_ready: bool = False
    smoke_ok: bool | None = None
    provider_contract_ok: bool | None = None
    required_tools: list[str] = Field(default_factory=list)
    covered_tools: list[str] = Field(default_factory=list)
    missing_required_tools: list[str] = Field(default_factory=list)
    failed_tools: list[str] = Field(default_factory=list)
    blocking_conditions: list[str] = Field(default_factory=list)
    release_blocking_conditions: list[str] = Field(default_factory=list)
    provider_contract_blocking_conditions: list[str] = Field(default_factory=list)
    desktop_session_kind: str = ""
    desktop_session_isolated: bool | None = None
    foreground_takeover_required: bool | None = None
    authentication_configured: bool = False
    idempotent_tool_requests_supported: bool = False
    desktop_backend_kind: str = ""
    desktop_backend_is_loopback: bool | None = None
    desktop_backend_ready_for_public_release: bool | None = None
    requires_real_virtual_desktop_backend: bool | None = None


class SandboxDesktopProviderSnapshot(_PublicSnapshot):
    available: bool = False
    provider_id: str = ""
    provider_kind: DesktopIsolationKind | str = "sandbox_desktop"
    status: str = "provider_required"
    adapter_ready: bool = False
    authentication_configured: bool = False
    reason: str = ""
    blocking_conditions: list[str] = Field(default_factory=list)
    supported_tools: list[str] = Field(default_factory=list)
    recommended_for: list[str] = Field(default_factory=list)
    diagnostic_route: str | None = None
    source: str = "runtime"
    health: DesktopProviderHealthSnapshot | None = None
    launch_hint: dict[str, Any] = Field(default_factory=dict)
    foreground_mutation_supported: bool | None = None
    keyboard_mouse_capture_supported: bool | None = None
    desktop_session_kind: str = ""
    desktop_session_isolated: bool | None = None
    foreground_takeover_required: bool | None = None
    desktop_backend_kind: str = ""
    desktop_backend_is_loopback: bool | None = None
    desktop_backend_ready_for_public_release: bool | None = None
    requires_real_virtual_desktop_backend: bool | None = None
    provider_contract: dict[str, Any] = Field(default_factory=dict)
    provider_manifest_evidence: dict[str, Any] = Field(default_factory=dict)
    provider_conformance: DesktopProviderConformanceSnapshot | None = None
    requires_real_sandbox_for: list[str] = Field(default_factory=list)


class PublicReleaseNextActionSnapshot(_PublicSnapshot):
    id: str = ""
    title: str = ""
    reason: str = ""
    command: str = ""
    required_environment: list[str] = Field(default_factory=list)
    required_contract_fields: dict[str, Any] = Field(default_factory=dict)
    required_tools: list[str] = Field(default_factory=list)


class PublicReleaseReadinessSnapshot(_PublicSnapshot):
    ready: bool = False
    backend_ready: bool = False
    provider_manifest: str = ""
    configured_virtual_desktop_provider_requested: bool = False
    blocking_conditions: list[str] = Field(default_factory=list)
    backend: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[PublicReleaseNextActionSnapshot] = Field(default_factory=list)
    required_commands: dict[str, str] = Field(default_factory=dict)


class ControlledDesktopProviderDiagnosticSnapshot(_PublicSnapshot):
    ready: bool = False
    release_ready: bool = False
    configured: bool = False
    authentication_configured: bool = False
    status: str = "controlled_provider_required"
    provider_id: str = "local-controlled-desktop"
    provider_kind: DesktopIsolationKind | str = "sandbox_desktop"
    execution_mode: str = "controlled_desktop"
    source: str = "runtime"
    reason: str = ""
    blocking_conditions: list[str] = Field(default_factory=list)
    supported_tools: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    foreground_mutation_supported: bool | None = None
    keyboard_mouse_capture_supported: bool | None = None
    desktop_session_kind: str = ""
    desktop_session_isolated: bool | None = None
    foreground_takeover_required: bool | None = None
    desktop_backend_kind: str = ""
    desktop_backend_is_loopback: bool | None = None
    desktop_backend_ready_for_public_release: bool | None = None
    requires_real_virtual_desktop_backend: bool | None = None
    provider_contract: dict[str, Any] = Field(default_factory=dict)
    provider_conformance: DesktopProviderConformanceSnapshot | None = None
    requires_real_sandbox_for: list[str] = Field(default_factory=list)
    requires_runtime_approval: bool = True
    approval_required_tools: list[str] = Field(default_factory=list)
    launch_command: list[str] = Field(default_factory=list)
    smoke_command: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    endpoint_origin: str = ""
    endpoint_path: str = ""
    status_endpoint_path: str = ""
    health: DesktopProviderHealthSnapshot | None = None
    manifest: dict[str, Any] = Field(default_factory=dict)
    session_manager: dict[str, Any] = Field(default_factory=dict)
    public_release_readiness: PublicReleaseReadinessSnapshot = Field(
        default_factory=PublicReleaseReadinessSnapshot
    )


class DesktopExecutionRouteSnapshot(_PublicSnapshot):
    route_id: str = "desktop_execution_route"
    tool_name: str = ""
    requested_mode: DesktopExecutionPolicyMode | str = "allow"
    selected_provider_kind: DesktopIsolationKind | str = "none"
    selected_provider_id: str = ""
    provider_readiness_status: str = ""
    status: str = "ready"
    can_execute: bool = True
    can_auto_start: bool = True
    provider_execution_required: bool = False
    sandbox_required: bool = False
    background_desktop_preferred: bool = False
    isolated_desktop_preferred: bool = False
    foreground_takeover_allowed: bool = False
    desktop_execution_session_policy: str = ""
    user_foreground_takeover_risk: bool = False
    requires_user_foreground_session: bool = False
    foreground_mutation_supported: bool | None = None
    keyboard_mouse_capture_supported: bool | None = None
    desktop_session_kind: str = ""
    desktop_session_isolated: bool | None = None
    foreground_takeover_required: bool | None = None
    desktop_backend_kind: str = ""
    desktop_backend_is_loopback: bool | None = None
    desktop_backend_ready_for_public_release: bool | None = None
    requires_real_virtual_desktop_backend: bool | None = None
    simulated_desktop_provider: bool = False
    provider_contract_blocking_conditions: list[str] = Field(default_factory=list)
    fallback_mode: DesktopExecutionPolicyMode | str = ""
    reason: str = ""
    blocking_conditions: list[str] = Field(default_factory=list)
    source: str = "runtime"


class DesktopActionRiskSnapshot(_PublicSnapshot):
    action_id: str
    risk_level: DesktopExecutionRisk
    title: str
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    execution_mode: DesktopExecutionModeSnapshot | None = None


class DesktopRecoveryActionMetadataSnapshot(_PublicSnapshot):
    daily_desktop_intent: bool = True
    desktop_permission_recovery: bool = True
    desktop_permission_retry: bool | None = None
    recovery_action_kind: RecoveryActionKind | None = None
    recovery_tool: str
    recovery_input: dict[str, Any] = Field(default_factory=dict)
    recovery_permission_target: str = ""
    recovery_risk_level: DesktopExecutionRisk | str | None = None
    recovery_retry_tool: str | None = None
    recovery_retry_input: dict[str, Any] = Field(default_factory=dict)
    recovery_retry_input_schema: dict[str, Any] = Field(default_factory=dict)
    recovery_retry_input_source: str | None = None
    recovery_retry_artifact_tool: str | None = None
    recovery_retry_artifact_kind: str | None = None
    required_retry_fields: list[str] = Field(default_factory=list)
    recommended_tools: list[str] = Field(default_factory=list)
    recovery_retry_prompt: str | None = None
    recovery_followup_tool: str | None = None
    recovery_followup_input: dict[str, Any] = Field(default_factory=dict)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    sandbox_provider: SandboxDesktopProviderSnapshot | None = None
    desktop_execution_route: DesktopExecutionRouteSnapshot | None = None
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    task_verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    recovery_retry_source_event_type: str | None = None
    recovery_retry_source_tool_call_id: str | None = None
    source_task_id: str | None = None
    source_task_title: str | None = None


class ToolCatalogItemSnapshot(_PublicSnapshot):
    tool_name: str
    function_name: str
    description: str = ""
    capability_id: str | None = None
    risk_level: DesktopExecutionRisk | str | None = None
    execution_mode: DesktopExecutionModeSnapshot | None = None
    provider_supported: bool = False
    provider_ready: bool = False
    provider_kind: DesktopIsolationKind | str = ""
    provider_id: str = ""
    user_foreground_takeover_risk: bool = False
    requires_user_foreground_session: bool = False
    approval_required: bool = False
    input_schema: dict[str, Any] = Field(default_factory=dict)
    model_tool_schema: dict[str, Any] = Field(default_factory=dict)
    missing_permissions: list[str] = Field(default_factory=list)
    blocking_conditions: list[str] = Field(default_factory=list)
    fallback_notes: list[str] = Field(default_factory=list)
    diagnostic_route: str | None = None
    source: str = "runtime"


class RestrictedPluginToolSnapshot(_PublicSnapshot):
    tool_name: str
    tool_id: str = ""
    function_name: str = ""
    risk_level: DesktopExecutionRisk | str | None = None
    enabled: bool = False


class RestrictedToolPluginSnapshot(_PublicSnapshot):
    plugin_id: str
    enabled: bool = False
    tool_names: list[str] = Field(default_factory=list)
    tools: list[RestrictedPluginToolSnapshot] = Field(default_factory=list)
    skill_docs: str = ""
    source: str = "restricted_tool_plugin"


class InstallRestrictedToolPluginRequest(_PublicSnapshot):
    plugin_id: str
    enabled: bool = True


class UpdateRestrictedToolPluginRequest(_PublicSnapshot):
    enabled: bool | None = None


class LegacyCleanupSampleContractSnapshot(_PublicSnapshot):
    prompt: str
    area: str
    planner_owner: str = "runtime_planner"
    legacy_boundary: str = "legacy_daily_desktop_intent"
    cleanup_status: str = "planner_covered"
    planner_intents: list[str] = Field(default_factory=list)
    planner_capabilities: list[str] = Field(default_factory=list)
    planner_tools: list[str] = Field(default_factory=list)


class LegacyCleanupAreaContractSnapshot(_PublicSnapshot):
    area: str
    sample_count: int = 0
    planner_intents: list[str] = Field(default_factory=list)
    planner_capabilities: list[str] = Field(default_factory=list)
    planner_tools: list[str] = Field(default_factory=list)


class LegacyCleanupFallbackContractSnapshot(_PublicSnapshot):
    fallback_id: str
    title: str
    reason: str = ""
    owner: str = "runtime_planner"
    planner_owner: str = "runtime_planner"
    legacy_boundary: str = "legacy_daily_desktop_intent"
    status: str = "planner_covered_compat_cleanup_pending"
    planner_coverage_status: str = "planner_covered"
    cleanup_blocker: str = "legacy_response_shape_compatibility"
    example_prompts: list[str] = Field(default_factory=list)
    planner_evidence_prompts: list[str] = Field(default_factory=list)
    required_before_delete: list[str] = Field(default_factory=list)


class LegacyPlannerOwnedEntrypointSnapshot(_PublicSnapshot):
    entrypoint_id: str
    title: str
    owner: str = "runtime_planner"
    legacy_shape_preserved: bool = True
    tools: list[str] = Field(default_factory=list)
    example_prompts: list[str] = Field(default_factory=list)


class LegacyCleanupCoverageSnapshot(_PublicSnapshot):
    legacy_boundary: str = "legacy_daily_desktop_intent"
    planner_owner: str = "runtime_planner"
    total_samples: int = 0
    cleanup_readiness: str = "planner_covered_compat_cleanup_pending"
    remaining_fallback_count: int = 0
    planner_covered_fallback_count: int = 0
    compatibility_cleanup_pending_count: int = 0
    areas: dict[str, int] = Field(default_factory=dict)
    prompts: list[str] = Field(default_factory=list)
    covered_intents: list[str] = Field(default_factory=list)
    covered_capabilities: list[str] = Field(default_factory=list)
    covered_tools: list[str] = Field(default_factory=list)
    area_contracts: list[LegacyCleanupAreaContractSnapshot] = Field(default_factory=list)
    sample_contracts: list[LegacyCleanupSampleContractSnapshot] = Field(default_factory=list)
    planner_owned_entrypoints: list[LegacyPlannerOwnedEntrypointSnapshot] = Field(default_factory=list)
    remaining_fallback_contracts: list[LegacyCleanupFallbackContractSnapshot] = Field(default_factory=list)


class ToolCatalogSnapshot(_PublicSnapshot):
    tools: list[ToolCatalogItemSnapshot] = Field(default_factory=list)
    capabilities: dict[str, DesktopExecutionCapabilitySnapshot] = Field(default_factory=dict)
    sandbox_provider: SandboxDesktopProviderSnapshot | None = None
    controlled_provider_diagnostics: ControlledDesktopProviderDiagnosticSnapshot | None = None
    plugins: list[RestrictedToolPluginSnapshot] = Field(default_factory=list)
    legacy_cleanup_coverage: LegacyCleanupCoverageSnapshot | None = None
    source: str = "runtime"


class CapabilitySnapshot(_PublicSnapshot):
    capability_id: str
    title: str
    category: CapabilityCategory | str = "general"
    description: str = ""
    tools: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    risk_level: DesktopExecutionRisk | str = "low"
    approval_required: bool = False
    discovery_actions: list[str] = Field(default_factory=list)
    execution_actions: list[str] = Field(default_factory=list)
    output_kinds: list[str] = Field(default_factory=list)
    source: str = "capability_registry"


class CapabilityPlanItemSnapshot(_PublicSnapshot):
    capability_id: str
    title: str
    category: CapabilityCategory | str = "general"
    status: Literal["available", "degraded", "missing"] | str = "available"
    required: bool = False
    preferred: bool = False
    reason: str = ""
    selected_tools: list[str] = Field(default_factory=list)
    available_tools: list[str] = Field(default_factory=list)
    missing_tools: list[str] = Field(default_factory=list)
    planned_step_ids: list[str] = Field(default_factory=list)
    discovery_actions: list[str] = Field(default_factory=list)
    execution_actions: list[str] = Field(default_factory=list)
    output_kinds: list[str] = Field(default_factory=list)
    risk_level: DesktopExecutionRisk | str = "low"
    approval_required: bool = False


class CapabilityPlanSnapshot(_PublicSnapshot):
    plan_id: str
    title: str
    intent_kind: TaskIntentKind | str = "general"
    items: list[CapabilityPlanItemSnapshot] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_capabilities: list[str] = Field(default_factory=list)
    available_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)
    source: str = "runtime_planner"


class TaskIntentSnapshot(_PublicSnapshot):
    intent_id: str
    kind: TaskIntentKind | str
    title: str
    user_goal: str = ""
    confidence: float = 0.0
    description: str = ""
    inputs: dict[str, Any] = Field(default_factory=dict)
    expected_outputs: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    preferred_capabilities: list[str] = Field(default_factory=list)
    missing_inputs: list[str] = Field(default_factory=list)
    risk_level: DesktopExecutionRisk | str = "low"
    source: str = "task_intent_router"


class RuntimeInputBindingSnapshot(_PublicSnapshot):
    """Planner-declared, Runtime-resolved dataflow between two exact steps."""

    binding_id: str
    source_step_id: str
    source_tool_name: str
    source_result_path: str
    target_input_path: str
    value_type: Literal["string", "string_list", "number", "bool"] | str = "string"
    required: bool = True
    max_bytes: int = 4096


class ToolPlanStepSnapshot(_PublicSnapshot):
    step_id: str
    title: str
    capability_id: str
    action: str = ""
    tool_name: str | None = None
    input_preview: dict[str, Any] = Field(default_factory=dict)
    risk_level: DesktopExecutionRisk | str = "low"
    execution_mode: DesktopExecutionModeSnapshot | None = None
    approval_required: bool = False
    depends_on: list[str] = Field(default_factory=list)
    input_bindings: list[RuntimeInputBindingSnapshot] = Field(default_factory=list)
    reason: str = ""
    fallback_tools: list[str] = Field(default_factory=list)
    status: Literal["planned", "unavailable", "skipped"] | str = "planned"


class ToolPlanSnapshot(_PublicSnapshot):
    plan_id: str
    title: str
    steps: list[ToolPlanStepSnapshot] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)
    artifacts_expected: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    source: str = "runtime_planner"


class RuntimeExecutionStrategySnapshot(_PublicSnapshot):
    strategy_id: str
    preferred_environment: RuntimeExecutionPreferredEnvironment | str = "structured_runtime"
    interaction_mode: RuntimeExecutionInteractionMode | str = "background"
    policy_mode: DesktopExecutionPolicyMode | str = "allow"
    background_desktop_preferred: bool = False
    isolated_desktop_preferred: bool = False
    foreground_takeover_allowed: bool = False
    user_foreground_takeover_risk: bool = False
    sandbox_required: bool = False
    provider_auto_start_recommended: bool = False
    local_foreground_fallback_allowed: bool = False
    foreground_control_step_count: int = 0
    keyboard_mouse_step_count: int = 0
    sandbox_recommended_step_count: int = 0
    approval_step_count: int = 0
    handoff_step_count: int = 0
    reasons: list[str] = Field(default_factory=list)
    mitigations: list[str] = Field(default_factory=list)
    source: str = "runtime_planner"


class TaskWorkspaceItemSnapshot(_PublicSnapshot):
    item_id: str
    title: str
    kind: TaskWorkspaceItemKind | str = "other"
    path: str | None = None
    description: str = ""
    source_step_id: str | None = None
    status: str = "planned"
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskWorkspaceSnapshot(_PublicSnapshot):
    workspace_id: str
    title: str
    root_label: str = "runtime://task-workspace"
    summary: str = ""
    items: list[TaskWorkspaceItemSnapshot] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    source: str = "runtime_planner"


class TaskTodoItemSnapshot(_PublicSnapshot):
    todo_id: str
    title: str
    status: TaskTodoStatus | str = "pending"
    capability_id: str = ""
    step_id: str | None = None
    tool_name: str | None = None
    approval_required: bool = False
    depends_on: list[str] = Field(default_factory=list)
    reason: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class TaskCheckpointSnapshot(_PublicSnapshot):
    checkpoint_id: str
    title: str
    status: TaskCheckpointStatus | str = "planned"
    after_step_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    verifies: list[str] = Field(default_factory=list)
    replan_on_failure: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


class ReplanSignalSnapshot(_PublicSnapshot):
    signal_id: str
    trigger: str
    source_step_id: str | None = None
    condition: str = ""
    target: str = ""
    fallback_tools: list[str] = Field(default_factory=list)
    reason: str = ""


class GoalCriterionSnapshot(_PublicSnapshot):
    """Planner-owned completion condition, independent of any application."""

    criterion_id: str
    description: str
    effectful: bool = False
    required: bool = True
    response_satisfiable: bool = False
    required_capabilities: list[str] = Field(default_factory=list)
    required_effects: list[str] = Field(default_factory=list)
    required_verification_predicates: list[str] = Field(default_factory=list)
    expected: dict[str, Any] = Field(default_factory=dict)
    source_step_ids: list[str] = Field(default_factory=list)
    verifier_step_ids: list[str] = Field(default_factory=list)


class GoalContractSnapshot(_PublicSnapshot):
    """Immutable root-goal template carried by plan/envelope snapshots."""

    contract_id: str
    original_goal: str
    intent_kind: str = "general"
    criteria: list[GoalCriterionSnapshot] = Field(default_factory=list)
    max_total_attempts: int = 12
    max_subgoal_attempts: int = 2
    source: str = "runtime_planner"


class TaskCoreSnapshot(_PublicSnapshot):
    core_id: str
    workspace: TaskWorkspaceSnapshot
    todos: list[TaskTodoItemSnapshot] = Field(default_factory=list)
    checkpoints: list[TaskCheckpointSnapshot] = Field(default_factory=list)
    replan_signals: list[ReplanSignalSnapshot] = Field(default_factory=list)
    goal_contract: GoalContractSnapshot | None = None
    source: str = "runtime_planner"


class TaskProgressSummarySnapshot(_PublicSnapshot):
    core_id: str | None = None
    workspace_id: str | None = None
    status: str = "unknown"
    current_step_id: str | None = None
    current_step_title: str | None = None
    current_tool_name: str | None = None
    total_todos: int = 0
    completed_todos: int = 0
    active_todos: int = 0
    blocked_todos: int = 0
    skipped_todos: int = 0
    total_checkpoints: int = 0
    completed_checkpoints: int = 0
    blocked_checkpoints: int = 0
    waiting_approval_checkpoints: int = 0
    total_workspace_items: int = 0
    completed_workspace_items: int = 0
    blocked_workspace_items: int = 0
    pending_verification_count: int = 0
    failed_verification_count: int = 0
    verified_verification_count: int = 0
    latest_verification_status: str | None = None
    latest_verification_step_id: str | None = None
    replan_request_count: int = 0
    latest_replan_request_id: str | None = None
    latest_replan_trigger: str | None = None
    latest_replan_step_id: str | None = None
    needs_replan: bool = False
    needs_user_action: bool = False
    desktop_provider_session_status: str | None = None
    desktop_provider_session_needed: bool = False
    desktop_provider_session_running: bool = False
    desktop_provider_session_started: bool = False
    desktop_provider_session_provider_id: str | None = None
    desktop_provider_session_tool_names: list[str] = Field(default_factory=list)
    desktop_provider_session_needs_user_action: bool = False
    desktop_provider_session_needs_replan: bool = False
    blocked_step_ids: list[str] = Field(default_factory=list)
    approval_step_ids: list[str] = Field(default_factory=list)
    progress_text: str = ""
    source: str = "task_progress_projection"


class TaskReplanRequestSnapshot(_PublicSnapshot):
    request_id: str
    trigger: str
    status: TaskReplanStatus | str = "requested"
    run_id: str | None = None
    task_id: str | None = None
    decision_id: str | None = None
    plan_id: str | None = None
    core_id: str | None = None
    source_step_id: str | None = None
    source_tool_name: str | None = None
    target_capability_id: str = ""
    condition: str = ""
    reason: str = ""
    failure_event_type: str = ""
    failure_detail: str = ""
    fallback_tools: list[str] = Field(default_factory=list)
    recovery_actions: list[dict[str, Any]] = Field(default_factory=list)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    task_verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    replan_prompt: str = ""
    route_to_studio: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""
    source: str = "runtime_planner"


class ReplanRecoveryActionSnapshot(_PublicSnapshot):
    action_id: str
    label: str
    tool: str
    input: dict[str, Any] = Field(default_factory=dict)
    planning_reason: str = ""
    permission_target: str = ""
    risk_level: str = ""
    approval_required: bool = False
    approval_id: str | None = None
    approval_status: str | None = None
    selected: bool = False
    deferred_tool: str | None = None
    deferred_input: dict[str, Any] = Field(default_factory=dict)
    deferred_context: dict[str, Any] = Field(default_factory=dict)
    deferred_continuation: list[dict[str, Any]] = Field(default_factory=list)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    desktop_execution_policy: dict[str, Any] = Field(default_factory=dict)
    desktop_execution_route: dict[str, Any] = Field(default_factory=dict)
    sandbox_provider: dict[str, Any] = Field(default_factory=dict)
    desktop_provider_session: dict[str, Any] = Field(default_factory=dict)
    desktop_loop: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    source: str = "replan_recovery_projection"


class ReplanRecoverySnapshot(_PublicSnapshot):
    request_id: str
    trigger: str
    status: TaskReplanStatus | str = "requested"
    run_id: str | None = None
    task_id: str | None = None
    group_run_id: str | None = None
    workflow_run_id: str | None = None
    decision_id: str | None = None
    plan_id: str | None = None
    core_id: str | None = None
    source_step_id: str | None = None
    source_tool_name: str | None = None
    target_capability_id: str = ""
    fallback_tools: list[str] = Field(default_factory=list)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    selected_tool_name: str | None = None
    selected_step_id: str | None = None
    planning_reason: str = ""
    recovery_action_label: str = ""
    recovery_actions: list[ReplanRecoveryActionSnapshot] = Field(default_factory=list)
    permission_target: str = ""
    risk_level: str = ""
    approval_id: str | None = None
    approval_status: str | None = None
    approval_ids: list[str] = Field(default_factory=list)
    deferred_tool: str | None = None
    deferred_input: dict[str, Any] = Field(default_factory=dict)
    deferred_context: dict[str, Any] = Field(default_factory=dict)
    deferred_continuation: list[dict[str, Any]] = Field(default_factory=list)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    desktop_execution_policy: dict[str, Any] = Field(default_factory=dict)
    desktop_execution_route: dict[str, Any] = Field(default_factory=dict)
    sandbox_provider: dict[str, Any] = Field(default_factory=dict)
    desktop_provider_session: dict[str, Any] = Field(default_factory=dict)
    desktop_loop: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str | None = None
    tool_call_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    tool_status: str | None = None
    todo_status: str | None = None
    checkpoint_status: str | None = None
    failure_detail: str = ""
    result_preview: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    recovery_event_ids: list[str] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    source: str = "replan_recovery_projection"


class ReplanContinuationSnapshot(_PublicSnapshot):
    continuation_id: str
    request_id: str
    action_id: str | None = None
    tool_name: str
    prompt: str
    title: str = ""
    source_run_id: str | None = None
    source_task_id: str | None = None
    source_group_run_id: str | None = None
    source_workflow_run_id: str | None = None
    agent_id: str | None = None
    conversation_id: str | None = None
    client_run_id: str | None = None
    direct_tool_requests: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    task_context: dict[str, Any] = Field(default_factory=dict)
    daily_desktop_planning_context: str = ""
    approval_required: bool = False
    auto_start_eligible: bool = False
    auto_start_reason: str = ""
    auto_start_blockers: list[str] = Field(default_factory=list)
    risk_level: str = ""
    source: str = "replan_continuation"


class RuntimePlanSnapshot(_PublicSnapshot):
    plan_id: str
    intent: TaskIntentSnapshot
    capabilities: list[CapabilitySnapshot] = Field(default_factory=list)
    capability_plan: CapabilityPlanSnapshot | None = None
    tool_plan: ToolPlanSnapshot
    execution_strategy: RuntimeExecutionStrategySnapshot | None = None
    task_core: TaskCoreSnapshot | None = None
    route_to_studio: bool = False
    timeline_preview: list[dict[str, Any]] = Field(default_factory=list)
    source: str = "runtime_planner"


class PlannerDecisionSnapshot(_PublicSnapshot):
    decision_id: str
    prompt: str
    selected_intent: TaskIntentSnapshot
    candidate_intents: list[TaskIntentSnapshot] = Field(default_factory=list)
    plan: RuntimePlanSnapshot
    created_at: str = ""
    source: str = "runtime_planner"


class RuntimeCheckpointPolicySnapshot(_PublicSnapshot):
    checkpoint_ids: list[str] = Field(default_factory=list)
    checkpoint_titles: list[str] = Field(default_factory=list)
    verifies: list[str] = Field(default_factory=list)
    replan_on_failure: bool = False
    replan_triggers: list[str] = Field(default_factory=list)
    replan_signal_ids: list[str] = Field(default_factory=list)
    fallback_tools: list[str] = Field(default_factory=list)
    verification_target_step_ids: list[str] = Field(default_factory=list)
    requires_approval: bool = False
    requires_observation: bool = False
    requires_post_action_verification: bool = False
    source: str = "runtime_checkpoint_policy"


class DesktopExecutionLoopSnapshot(_PublicSnapshot):
    stage: str = ""
    role: str = ""
    action: str = ""
    target_kind: str = ""
    selection_source: str = ""
    app_name: str = ""
    query: str = ""
    source_tool: str = ""
    retry_tool: str = ""
    retry_reason: str = ""
    retry_input: dict[str, Any] = Field(default_factory=dict)
    verification_target_step_ids: list[str] = Field(default_factory=list)
    requires_observation: bool = False
    requires_post_action_verification: bool = False
    can_auto_retry: bool = False
    source: str = "desktop_execution_loop"


class RuntimeExecutionRequestSnapshot(_PublicSnapshot):
    request_id: str
    step_id: str | None = None
    capability_id: str | None = None
    capability_title: str = ""
    capability_status: str = ""
    capability_reason: str = ""
    capability_selected_tools: list[str] = Field(default_factory=list)
    capability_planned_step_ids: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    plan_id: str | None = None
    tool_plan_id: str | None = None
    intent_kind: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    group_run_id: str | None = None
    run_group_id: str | None = None
    group_id: str | None = None
    workflow_run_id: str | None = None
    workflow_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    workflow_node_kind: str | None = None
    tool_name: str
    protocol: str = "json_fallback"
    input: dict[str, Any] = Field(default_factory=dict)
    planning_reason: str = ""
    presentation: str = ""
    approval_required: bool = False
    risk_level: str = "low"
    execution_mode: DesktopExecutionModeSnapshot | None = None
    desktop_execution_policy: DesktopExecutionPolicySnapshot | None = None
    sandbox_provider: SandboxDesktopProviderSnapshot | None = None
    desktop_execution_route: DesktopExecutionRouteSnapshot | None = None
    desktop_provider_session: dict[str, Any] = Field(default_factory=dict)
    policy_reason: str = ""
    continue_to_model: bool = False
    deferred_tool: str | None = None
    deferred_input: dict[str, Any] = Field(default_factory=dict)
    deferred_context: dict[str, Any] = Field(default_factory=dict)
    deferred_continuation: list[dict[str, Any]] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    input_bindings: list[RuntimeInputBindingSnapshot] = Field(default_factory=list)
    fallback_tools: list[str] = Field(default_factory=list)
    status: str = "planned"
    runtime_doctrine: str = ""
    runtime_stage: str = ""
    runtime_role: str = ""
    requires_observation: bool = False
    requires_post_action_verification: bool = False
    replan_triggers: list[str] = Field(default_factory=list)
    replan_signal_ids: list[str] = Field(default_factory=list)
    followup_target: dict[str, Any] = Field(default_factory=dict)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    event_ids: list[str] = Field(default_factory=list)
    tool_call_ids: list[str] = Field(default_factory=list)
    approval_ids: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)
    artifact_paths: list[str] = Field(default_factory=list)
    task_todo: dict[str, Any] = Field(default_factory=dict)
    task_checkpoints: list[dict[str, Any]] = Field(default_factory=list)
    task_workspace_items: list[dict[str, Any]] = Field(default_factory=list)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    task_verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    verification_status: str | None = None
    verification_step_id: str | None = None
    verification_event_ids: list[str] = Field(default_factory=list)
    verification_artifact_paths: list[str] = Field(default_factory=list)
    checkpoint_policy: RuntimeCheckpointPolicySnapshot | None = None
    desktop_loop: DesktopExecutionLoopSnapshot | None = None
    source: str = "runtime_planner"


class RuntimeExecutionEnvelopeSnapshot(_PublicSnapshot):
    envelope_id: str
    decision_id: str
    plan_id: str
    intent_kind: str
    capability_plan: CapabilityPlanSnapshot | None = None
    execution_strategy: RuntimeExecutionStrategySnapshot | None = None
    requests: list[RuntimeExecutionRequestSnapshot] = Field(default_factory=list)
    task_core: TaskCoreSnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None
    approvals_required: list[str] = Field(default_factory=list)
    artifacts_expected: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    route_to_studio: bool = False
    desktop_execution_policy: DesktopExecutionPolicySnapshot | None = None
    sandbox_provider: SandboxDesktopProviderSnapshot | None = None
    desktop_execution_route: DesktopExecutionRouteSnapshot | None = None
    desktop_provider_session: dict[str, Any] = Field(default_factory=dict)
    runtime_doctrine: str = ""
    runtime_stage_counts: dict[str, int] = Field(default_factory=dict)
    replan_signal_count: int = 0
    source: str = "runtime_planner"


class PlannerTraceSummarySnapshot(_PublicSnapshot):
    source: str = ""
    decision_id: str | None = None
    plan_id: str | None = None
    intent_kind: str | None = None
    intent_title: str | None = None
    route_to_studio: bool | None = None
    selection_source: str | None = None
    selection_role: str | None = None
    selection_reason: str | None = None
    planner_entrypoint: str | None = None
    entrypoint_source: str | None = None
    launcher_mode: str | None = None
    launcher_surface: str | None = None
    runnable_kind: str | None = None
    followup_target: dict[str, Any] = Field(default_factory=dict)
    plan_tools: list[str] = Field(default_factory=list)
    selected_tools: list[str] = Field(default_factory=list)
    plan_capabilities: list[str] = Field(default_factory=list)
    required_capabilities: list[str] = Field(default_factory=list)
    missing_capabilities: list[str] = Field(default_factory=list)
    approvals_required: list[str] = Field(default_factory=list)
    artifacts_expected: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    step_count: int = 0
    event_count: int = 0


class RuntimeDebugSummarySnapshot(_PublicSnapshot):
    source: str = "runtime_projection"
    run_id: str | None = None
    task_id: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    planner_decision_id: str | None = None
    planner_plan_id: str | None = None
    intent_kind: str | None = None
    intent_title: str | None = None
    route_to_studio: bool | None = None
    task_status: str | None = None
    current_step_id: str | None = None
    current_step_title: str | None = None
    current_tool_name: str | None = None
    current_workspace_item_id: str | None = None
    current_workspace_item_title: str | None = None
    current_workspace_item_kind: str | None = None
    current_workspace_item_path: str | None = None
    current_workspace_item_status: str | None = None
    total_todos: int = 0
    completed_todos: int = 0
    blocked_todos: int = 0
    total_checkpoints: int = 0
    completed_checkpoints: int = 0
    blocked_checkpoints: int = 0
    total_workspace_items: int = 0
    completed_workspace_items: int = 0
    blocked_workspace_items: int = 0
    runtime_stage_counts: dict[str, int] = Field(default_factory=dict)
    runtime_doctrine: str | None = None
    runtime_stage: str | None = None
    runtime_role: str | None = None
    plan_tools: list[str] = Field(default_factory=list)
    plan_capabilities: list[str] = Field(default_factory=list)
    runtime_request_count: int = 0
    pending_runtime_request_count: int = 0
    completed_runtime_request_count: int = 0
    recovered_runtime_request_count: int = 0
    failed_runtime_request_count: int = 0
    blocked_runtime_request_count: int = 0
    blocked_direct_request_count: int = 0
    blocked_runtime_request_tools: list[str] = Field(default_factory=list)
    waiting_runtime_request_count: int = 0
    current_request_id: str | None = None
    current_request_tool_name: str | None = None
    current_request_status: str | None = None
    latest_request_id: str | None = None
    latest_request_tool_name: str | None = None
    latest_request_status: str | None = None
    latest_blocked_request_tool_name: str | None = None
    latest_blocked_request_status: str | None = None
    latest_blocked_request_reason: str | None = None
    desktop_provider_session_status: str | None = None
    desktop_provider_session_needed: bool = False
    desktop_provider_session_running: bool = False
    desktop_provider_session_started: bool = False
    desktop_provider_session_provider_id: str | None = None
    desktop_provider_session_reason: str | None = None
    desktop_provider_session_tool_names: list[str] = Field(default_factory=list)
    desktop_provider_session_kind: str | None = None
    desktop_provider_session_isolated: bool | None = None
    desktop_provider_session_foreground_takeover_required: bool | None = None
    desktop_provider_session_keyboard_mouse_capture_supported: bool | None = None
    desktop_provider_session_supported_tools: list[str] = Field(default_factory=list)
    desktop_provider_backend_kind: str | None = None
    desktop_provider_backend_is_loopback: bool | None = None
    desktop_provider_backend_ready_for_public_release: bool | None = None
    desktop_provider_requires_real_virtual_backend: bool | None = None
    desktop_provider_contract_ok: bool | None = None
    desktop_provider_contract_version: str | None = None
    desktop_provider_contract_blocking_conditions: list[str] = Field(
        default_factory=list
    )
    desktop_provider_manifest_ok: bool | None = None
    desktop_provider_manifest_remote_endpoint_allowed: bool | None = None
    desktop_provider_manifest_remote_endpoint_urls: list[str] = Field(
        default_factory=list
    )
    desktop_provider_manifest_blocking_conditions: list[str] = Field(
        default_factory=list
    )
    desktop_provider_conformance_ok: bool | None = None
    desktop_provider_conformance_mode: str | None = None
    desktop_provider_conformance_smoke_ok: bool | None = None
    desktop_provider_conformance_public_release_ready: bool | None = None
    desktop_provider_conformance_release_candidate: bool | None = None
    desktop_provider_conformance_release_blocking_conditions: list[str] = Field(
        default_factory=list
    )
    desktop_provider_conformance_missing_required_tools: list[str] = Field(
        default_factory=list
    )
    desktop_provider_conformance_failed_tools: list[str] = Field(default_factory=list)
    desktop_execution_session_mode: str | None = None
    desktop_execution_session_label: str | None = None
    event_count: int = 0
    tool_call_count: int = 0
    completed_tool_call_count: int = 0
    failed_tool_call_count: int = 0
    blocked_tool_call_count: int = 0
    waiting_tool_call_count: int = 0
    approval_count: int = 0
    pending_approval_count: int = 0
    artifact_count: int = 0
    memory_trace_count: int = 0
    skill_trace_count: int = 0
    child_run_count: int = 0
    replan_recovery_count: int = 0
    needs_user_action: bool = False
    needs_replan: bool = False
    latest_event_type: str | None = None
    current_capability_id: str | None = None
    latest_replan_request_id: str | None = None
    latest_replan_trigger: str | None = None
    latest_replan_status: str | None = None
    latest_recovery_failed: bool = False
    latest_failed_recovery_tool: str | None = None
    latest_failed_recovery_action_id: str | None = None
    latest_failed_recovery_reason: str | None = None
    latest_recovery_action_id: str | None = None
    latest_recovery_tool: str | None = None
    latest_recovery_action_label: str | None = None
    latest_recovery_action_count: int = 0
    latest_deferred_tool: str | None = None
    deferred_continuation_count: int = 0
    latest_deferred_continuation_tool: str | None = None
    latest_tool_call_id: str | None = None
    latest_tool_name: str | None = None
    latest_tool_status: str | None = None
    latest_approval_id: str | None = None
    latest_approval_tool_name: str | None = None
    latest_approval_status: str | None = None
    latest_artifact_id: str | None = None
    latest_artifact_kind: str | None = None
    latest_artifact_path: str | None = None
    debug_surfaces: list[str] = Field(default_factory=list)


class PublicRunEvent(_PublicSnapshot):
    event_id: str | None = None
    run_id: str
    sequence: int = 0
    schema_version: int = 1
    event_type: str
    parent_run_id: str | None = None
    source_run_id: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    run_group_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    agent_id: str | None = None
    agent_name: str | None = None
    member_agent_id: str | None = None
    member_agent_name: str | None = None
    title: str | None = None
    detail: str | None = None
    actor: str | None = None
    visibility: Literal["user", "internal"] = "user"
    sensitivity: Literal["public", "secret"] = "public"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class RunEventPageSnapshot(_PublicSnapshot):
    run_id: str
    after_sequence: int = 0
    limit: int = 200
    next_after_sequence: int = 0
    has_more: bool = False
    events: list[PublicRunEvent] = Field(default_factory=list)


class ApprovalCardSnapshot(_PublicSnapshot):
    approval_id: str
    run_id: str | None = None
    source_run_id: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    source: str | None = None
    planning_reason: str | None = None
    step_id: str | None = None
    planner_step_id: str | None = None
    capability_id: str | None = None
    decision_id: str | None = None
    plan_id: str | None = None
    tool_plan_id: str | None = None
    intent_kind: str | None = None
    replan_request_id: str | None = None
    replan_trigger: str | None = None
    replan_triggers: list[str] = Field(default_factory=list)
    replan_signal_ids: list[str] = Field(default_factory=list)
    runtime_doctrine: str | None = None
    runtime_stage: str | None = None
    runtime_role: str | None = None
    requires_observation: bool = False
    requires_post_action_verification: bool = False
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    runtime_execution_metadata: dict[str, Any] = Field(default_factory=dict)
    deferred_tool: str | None = None
    deferred_input: dict[str, Any] = Field(default_factory=dict)
    deferred_context: dict[str, Any] = Field(default_factory=dict)
    deferred_continuation: list[dict[str, Any]] = Field(default_factory=list)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    task_workspace_items: list[dict[str, Any]] = Field(default_factory=list)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    task_verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    title: str
    description: str | None = None
    status: ApprovalStatus = "pending"
    tool_name: str | None = None
    risk_level: str | None = None
    input_preview: dict[str, Any] = Field(default_factory=dict)
    policy_reason: str | None = None
    requested_at: str = ""
    resolved_at: str | None = None
    open_in_studio_url: str | None = None


class ArtifactSnapshot(_PublicSnapshot):
    artifact_id: str
    run_id: str | None = None
    source_run_id: str | None = None
    source_tool: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    runtime_execution_metadata: dict[str, Any] = Field(default_factory=dict)
    title: str
    kind: str
    planned_kind: str | None = None
    source_kind: str | None = None
    requested_outputs: list[str] | None = None
    manifest_index: int | None = None
    path: str | None = None
    mime_type: str | None = None
    size_bytes: int | None = None
    preview_text: str | None = None
    url: str | None = None
    created_at: str = ""


class ArtifactContentSnapshot(_PublicSnapshot):
    ok: bool = True
    run_id: str | None = None
    task_id: str | None = None
    path: str
    content: str = ""
    mime_type: str | None = None
    truncated: bool = False


class ToolCallSnapshot(_PublicSnapshot):
    tool_call_id: str
    run_id: str | None = None
    source_run_id: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    source: str | None = None
    planning_reason: str | None = None
    decision_id: str | None = None
    plan_id: str | None = None
    tool_plan_id: str | None = None
    intent_kind: str | None = None
    step_id: str | None = None
    planner_step_id: str | None = None
    capability_id: str | None = None
    replan_request_id: str | None = None
    replan_trigger: str | None = None
    replan_triggers: list[str] = Field(default_factory=list)
    replan_signal_ids: list[str] = Field(default_factory=list)
    runtime_doctrine: str | None = None
    runtime_stage: str | None = None
    runtime_role: str | None = None
    requires_observation: bool = False
    requires_post_action_verification: bool = False
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    runtime_execution_metadata: dict[str, Any] = Field(default_factory=dict)
    deferred_tool: str | None = None
    deferred_input: dict[str, Any] = Field(default_factory=dict)
    deferred_context: dict[str, Any] = Field(default_factory=dict)
    deferred_continuation: list[dict[str, Any]] = Field(default_factory=list)
    action_target: dict[str, Any] = Field(default_factory=dict)
    observation_evidence: dict[str, Any] = Field(default_factory=dict)
    observation_retry: dict[str, Any] = Field(default_factory=dict)
    task_workspace_items: list[dict[str, Any]] = Field(default_factory=list)
    verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    task_verification_targets: list[dict[str, Any]] = Field(default_factory=list)
    tool_name: str
    status: str
    risk_level: str | None = None
    policy_reason: str | None = None
    input_preview: dict[str, Any] = Field(default_factory=dict)
    output_preview: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    foreground_lock_busy: bool = False
    foreground_lock_holder: str | None = None
    approval_id: str | None = None
    started_at: str = ""
    completed_at: str | None = None


class MemoryTraceSnapshot(_PublicSnapshot):
    trace_id: str
    run_id: str
    event_id: str | None = None
    sequence: int = 0
    event_type: str
    status: str = "completed"
    action: str | None = None
    memory_id: str | None = None
    memory_kind: str | None = None
    memory_scope: str | None = None
    count: int = 0
    source_run_id: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    title: str
    detail: str | None = None
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class SkillTraceSnapshot(_PublicSnapshot):
    trace_id: str
    run_id: str
    event_id: str | None = None
    sequence: int = 0
    event_type: str
    status: str = "completed"
    skill_id: str | None = None
    skill_name: str | None = None
    source_ref: str | None = None
    source_type: str | None = None
    tool_name: str | None = None
    source_run_id: str | None = None
    source_runnable_id: str | None = None
    source_runnable_name: str | None = None
    workflow_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    group_id: str | None = None
    group_run_id: str | None = None
    core_id: str | None = None
    workspace_id: str | None = None
    task_id: str | None = None
    title: str
    detail: str | None = None
    payload_preview: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class AgentTaskSnapshot(_PublicSnapshot):
    task_id: str
    conversation_id: str | None = None
    title: str
    status: TaskStatus
    summary: str | None = None
    current_step: str | None = None
    progress_text: str | None = None
    needs_user_action: bool = False
    pending_approvals: list[ApprovalCardSnapshot] = Field(default_factory=list)
    recent_events: list[PublicRunEvent] = Field(default_factory=list)
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    artifacts: list[ArtifactSnapshot] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    planner_summary: PlannerTraceSummarySnapshot | None = None
    runtime_debug: RuntimeDebugSummarySnapshot | None = None
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    task_core: TaskCoreSnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None
    replan_recoveries: list[ReplanRecoverySnapshot] = Field(default_factory=list)
    open_in_studio_url: str | None = None
    created_at: str = ""
    updated_at: str = ""


class AgentTaskLightSnapshot(_PublicSnapshot):
    task_id: str
    conversation_id: str | None = None
    title: str
    status: TaskStatus
    detail: str | None = None
    needs_user_action: bool = False
    pending_approval: ApprovalCardSnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None
    runtime_debug: RuntimeDebugSummarySnapshot | None = None
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    open_in_studio_url: str | None = None
    created_at: str = ""
    updated_at: str = ""


class RunTimelineChildSnapshot(_PublicSnapshot):
    run_id: str
    title: str | None = None
    status: str = ""
    kind: str | None = None
    parent_run_id: str | None = None
    group_run_id: str | None = None
    run_group_id: str | None = None
    workflow_run_id: str | None = None
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    agent_id: str | None = None
    workflow_id: str | None = None
    planner_summary: PlannerTraceSummarySnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None


class RecoveryRunProvenanceSnapshot(_PublicSnapshot):
    source: str = ""
    kind: str = ""
    source_run_id: str | None = None
    source_group_run_id: str | None = None
    source_workflow_run_id: str | None = None
    source_task_id: str | None = None
    source_task_title: str | None = None
    source_tool_call_id: str | None = None
    source_tool_name: str | None = None
    replan_request_id: str | None = None
    replan_trigger: str | None = None
    recovery_action_id: str | None = None
    recovery_action_kind: str | None = None
    recovery_tool: str | None = None
    recovery_input_preview: dict[str, Any] = Field(default_factory=dict)
    recovery_permission_target: str | None = None
    recovery_risk_level: str | None = None
    approval_required: bool = False
    task_core_context: dict[str, Any] = Field(default_factory=dict)


class RunTimelineSnapshot(_PublicSnapshot):
    run_id: str
    parent_run_id: str | None = None
    group_run_id: str | None = None
    run_group_id: str | None = None
    workflow_run_id: str | None = None
    agent_id: str | None = None
    status: str
    title: str | None = None
    task_id: str | None = None
    session_id: str | None = None
    task_run_link_created_at: str | None = None
    task_run_link_updated_at: str | None = None
    task_run_link_run_status: str | None = None
    task_run_link_last_event_sequence: int | None = None
    rerun_of_run_id: str | None = None
    rerun_of_kind: str | None = None
    rerun_of_status: str | None = None
    rerun_of_runnable_id: str | None = None
    rerun_of_runnable_name: str | None = None
    rerun_original_created_at: str | None = None
    rerun_original_updated_at: str | None = None
    planner_summary: PlannerTraceSummarySnapshot | None = None
    runtime_debug: RuntimeDebugSummarySnapshot | None = None
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    task_core: TaskCoreSnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None
    replan_recoveries: list[ReplanRecoverySnapshot] = Field(default_factory=list)
    recovery_source: RecoveryRunProvenanceSnapshot | None = None
    events: list[PublicRunEvent] = Field(default_factory=list)
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    memory_traces: list[MemoryTraceSnapshot] = Field(default_factory=list)
    skill_traces: list[SkillTraceSnapshot] = Field(default_factory=list)
    approvals: list[ApprovalCardSnapshot] = Field(default_factory=list)
    pending_approval: ApprovalCardSnapshot | None = None
    artifacts: list[ArtifactSnapshot] = Field(default_factory=list)
    children: list[RunTimelineChildSnapshot] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class AgentDefinitionSnapshot(_PublicSnapshot):
    agent_id: str
    name: str
    nickname: str | None = None
    description: str | None = None
    instructions: str | None = None
    persona_prompt: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    model_mode: str | None = None
    execution_backend: str | None = None
    model_profile_id: str | None = None
    vision_model_profile_id: str | None = None
    model_settings: dict[str, Any] = Field(
        default_factory=dict,
        alias="model_config",
        serialization_alias="model_config",
    )
    tool_policy: dict[str, Any] = Field(default_factory=dict)
    workspace_policy: dict[str, Any] = Field(default_factory=dict)
    skill_ids: list[str] = Field(default_factory=list)
    output_contract: str | None = None
    enabled: bool = True
    virtual: bool = False
    system: bool = False
    builtin: bool = False
    editable: bool = True
    deletable: bool = True
    created_at: str = ""
    updated_at: str = ""


class AgentDeskItemSnapshot(_PublicSnapshot):
    path: str
    name: str
    kind: Literal["file", "directory", "note"] = "file"
    size_bytes: int | None = None
    mime_type: str | None = None
    preview_text: str | None = None
    updated_at: str = ""


class AgentDeskSnapshot(_PublicSnapshot):
    agent_id: str
    root_path: str
    notes_path: str = "desk-notes.md"
    metadata_path: str = ".yachiyo-desk.json"
    items: list[AgentDeskItemSnapshot] = Field(default_factory=list)
    updated_at: str = ""


class SkillSnapshot(_PublicSnapshot):
    skill_id: str
    name: str
    description: str | None = None
    source_path: str | None = None
    local_path: str | None = None
    folder_id: str | None = None
    folder_name: str | None = None
    source_type: str | None = None
    origin_path: str | None = None
    source_ref: str | None = None
    content_hash: str | None = None
    last_synced_at: str | None = None
    sync_status: str | None = None
    content_summary: str | None = None
    skill_markdown: str | None = None
    asset_paths: list[str] = Field(default_factory=list)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class SkillFolderSnapshot(_PublicSnapshot):
    folder_id: str
    name: str
    description: str | None = None
    source_scope: Literal["all", "installed", "native"] | str = "all"
    sort_order: int = 0
    skill_count: int = 0
    installed_count: int = 0
    native_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SkillSourceRootSnapshot(_PublicSnapshot):
    path: str
    source_type: str
    library: str | None = None
    exists: bool = False
    skill_count: int = 0


class MemorySnapshot(_PublicSnapshot):
    memory_id: str
    scope: str
    kind: str
    content: str
    content_hash: str | None = None
    project_id: str | None = None
    source_session_id: str | None = None
    source_message_id: str | None = None
    source_task_id: str | None = None
    source_run_id: str | None = None
    confidence: float = 0.0
    pinned: bool = False
    user_confirmed: bool = False
    enabled: bool = True
    actor: str = "agent_tool"
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str | None = None


class FutureTaskSnapshot(_PublicSnapshot):
    future_task_id: str
    title: str
    prompt: str
    runnable_id: str | None = None
    runnable_name: str | None = None
    status: str = "scheduled"
    scheduled_at_epoch: float = 0.0
    cron: str | None = None
    source_run_id: str | None = None
    last_run_id: str | None = None
    run_count: int = 0
    error: str | None = None
    created_at: str = ""
    updated_at: str = ""
    cancelled_at: str | None = None
    trigger_in_flight: bool = False


class AgentGroupMemberSnapshot(_PublicSnapshot):
    agent_id: str
    name: str
    role: str | None = None
    sort_order: int = 0
    enabled: bool = True
    run_id: str | None = None
    run_status: str | None = None
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    pending_approvals: list[ApprovalCardSnapshot] = Field(default_factory=list)
    artifacts: list[ArtifactSnapshot] = Field(default_factory=list)


class AgentGroupSnapshot(_PublicSnapshot):
    group_id: str
    name: str
    description: str | None = None
    members: list[AgentGroupMemberSnapshot] = Field(default_factory=list)
    mode: GroupMode = "moderated"
    moderator_agent_id: str | None = None
    default_model: str | None = None
    memory_scope: MemoryScope = "shared"
    tool_policy_id: str | None = None
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class GroupRunSnapshot(_PublicSnapshot):
    group_run_id: str
    run_group_id: str | None = None
    group_id: str
    title: str
    status: str
    objective: str
    participants: list[AgentGroupMemberSnapshot] = Field(default_factory=list)
    active_speaker_agent_id: str | None = None
    task_core: TaskCoreSnapshot | None = None
    task_progress: TaskProgressSummarySnapshot | None = None
    planner_summary: PlannerTraceSummarySnapshot | None = None
    runtime_execution_envelope: RuntimeExecutionEnvelopeSnapshot | None = None
    runtime_debug: RuntimeDebugSummarySnapshot | None = None
    replan_recoveries: list[ReplanRecoverySnapshot] = Field(default_factory=list)
    events: list[PublicRunEvent] = Field(default_factory=list)
    runs: list[RunTimelineSnapshot] = Field(default_factory=list)
    child_run_ids: list[str] = Field(default_factory=list)
    tool_calls: list[ToolCallSnapshot] = Field(default_factory=list)
    memory_traces: list[MemoryTraceSnapshot] = Field(default_factory=list)
    skill_traces: list[SkillTraceSnapshot] = Field(default_factory=list)
    shared_artifacts: list[ArtifactSnapshot] = Field(default_factory=list)
    pending_approvals: list[ApprovalCardSnapshot] = Field(default_factory=list)
    final_answer: str | None = None
    created_at: str = ""
    updated_at: str = ""


class FutureTaskTriggerResultSnapshot(_PublicSnapshot):
    ok: bool = True
    future_task: FutureTaskSnapshot | None = None
    run: RunTimelineSnapshot | None = None
    error: str | None = None


class WorkflowSnapshot(_PublicSnapshot):
    workflow_id: str
    name: str
    description: str | None = None
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    default_input_schema: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    created_at: str = ""
    updated_at: str = ""


class ChatRunnableParticipantSnapshot(_PublicSnapshot):
    runnable_id: str
    agent_id: str | None = None
    workflow_id: str | None = None
    group_id: str | None = None
    kind: Literal["agent", "workflow"]
    name: str
    nickname: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    enabled: bool = True


class ChatRunnableSnapshot(_PublicSnapshot):
    runnable_id: str
    agent_id: str | None = None
    workflow_id: str | None = None
    group_id: str | None = None
    kind: Literal["agent", "workflow", "group"]
    name: str
    nickname: str | None = None
    description: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    output_contract: str | None = None
    enabled: bool = True
    tool_capabilities: list[str] = Field(default_factory=list)
    approval_required_tools: list[str] = Field(default_factory=list)
    participants: list[ChatRunnableParticipantSnapshot] = Field(default_factory=list)


class ChatRunnableCatalogSnapshot(_PublicSnapshot):
    agents: list[ChatRunnableSnapshot] = Field(default_factory=list)
    workflows: list[ChatRunnableSnapshot] = Field(default_factory=list)
    groups: list[ChatRunnableSnapshot] = Field(default_factory=list)


class WorkflowRunSnapshot(RunTimelineSnapshot):
    workflow_id: str | None = None
    objective: str = ""
    current_node_id: str | None = None
    current_node_label: str | None = None
    final_answer: str | None = None


class PlannerOrchestrationStartSnapshot(_PublicSnapshot):
    kind: Literal["workflow", "group_run"] | str
    status: Literal["started", "handoff", "unsupported", "target_not_found"] | str
    decision: PlannerDecisionSnapshot
    run_id: str | None = None
    workflow_run_id: str | None = None
    group_run_id: str | None = None
    run_group_id: str | None = None
    target_id: str | None = None
    target_name: str | None = None
    objective: str = ""
    title: str = ""
    route_to_studio: bool = True
    message: str = ""
    workflow_run: WorkflowRunSnapshot | None = None
    group_run: GroupRunSnapshot | None = None


class StartChatTaskRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    conversation_id: str | None = None
    title: str | None = None
    agent_id: str | None = None
    workflow_id: str | None = None
    group_id: str | None = None
    attachments: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="allow")

    approved: bool = True
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SaveAgentGroupMemberRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    role: str | None = None
    sort_order: int = 0
    enabled: bool = True


class SaveAgentRequest(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    agent_id: str | None = None
    name: str | None = None
    nickname: str | None = None
    description: str | None = None
    instructions: str | None = None
    persona_prompt: str | None = None
    avatar_url: str | None = None
    category: str | None = None
    model_mode: str | None = None
    execution_backend: str | None = None
    model_profile_id: str | None = None
    vision_model_profile_id: str | None = None
    model_settings: dict[str, Any] | None = Field(
        default=None,
        alias="model_config",
        serialization_alias="model_config",
    )
    tool_policy: dict[str, Any] | None = None
    workspace_policy: dict[str, Any] | None = None
    skill_ids: list[str] | None = None
    output_contract: str | None = None
    enabled: bool | None = None


class SaveAgentDeskNoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = ""


class SaveAgentDeskFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str = ""


class AgentDeskFileEventRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    event_type: Literal["created", "modified", "deleted", "changed"] = "changed"
    delay_seconds: int | float | None = 0


class SaveAgentGroupRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    group_id: str | None = None
    name: str | None = None
    description: str | None = None
    members: list[SaveAgentGroupMemberRequest] | None = None
    participant_ids: list[str] | None = None
    agent_ids: list[str] | None = None
    mode: GroupMode | None = None
    moderator_agent_id: str | None = None
    default_model: str | None = None
    memory_scope: MemoryScope | None = None
    tool_policy_id: str | None = None
    enabled: bool | None = None


class StartGroupRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    group_id: str
    objective: str
    title: str | None = None
    client_run_id: str | None = None


class StartAgentRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    agent_id: str
    objective: str
    title: str | None = None
    client_run_id: str | None = None


class SaveWorkflowRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    workflow_id: str | None = None
    name: str | None = None
    description: str | None = None
    nodes: list[dict[str, Any]] | None = None
    edges: list[dict[str, Any]] | None = None
    default_input_schema: dict[str, Any] | None = None
    enabled: bool | None = None


class StartWorkflowRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    workflow_id: str
    objective: str
    title: str | None = None
    client_run_id: str | None = None


class StartPlannerOrchestrationRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: str
    objective: str | None = None
    title: str | None = None
    target_id: str | None = None
    target_name: str | None = None
    allowed_tools: list[str] | None = None
    client_run_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class RerunRunRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    scope: Literal["run", "workflow_node", "workflow_branch"] | str = "run"
    workflow_node_id: str | None = None
    workflow_node_label: str | None = None
    workflow_edge_branch: str | None = None
    workflow_node_selected_target: str | None = None
    reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
