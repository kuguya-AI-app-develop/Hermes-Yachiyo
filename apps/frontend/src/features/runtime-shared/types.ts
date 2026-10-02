export type TaskStatus = 'queued' | 'running' | 'waiting_approval' | 'completed' | 'failed' | 'cancelled';
export type ApprovalStatus = 'pending' | 'approved' | 'rejected' | 'cancelled' | 'expired';
export type GroupMode = 'moderated' | 'round_robin' | 'debate' | 'pipeline' | 'parallel' | 'custom';
export type MemoryScope = 'shared' | 'per_agent' | 'hybrid';
export type DesktopExecutionRisk = 'low' | 'medium' | 'high';
export type DesktopExecutionMode =
  | 'preview'
  | 'tool_native'
  | 'read_only_observation'
  | 'supervised_live'
  | 'sandbox_preferred'
  | 'handoff_required';
export type DesktopIsolationKind =
  | 'none'
  | 'process'
  | 'browser_profile'
  | 'browser_target'
  | 'background_desktop'
  | 'sandbox_desktop'
  | 'headless'
  | 'user_handoff';
export type DesktopExecutionPolicyMode =
  | 'allow'
  | 'preview'
  | 'preview_input'
  | 'sandbox_preferred'
  | 'handoff_required'
  | 'supervised_live';
export type TaskIntentKind =
  | 'desktop_operation'
  | 'data_analysis'
  | 'report_generation'
  | 'web_research'
  | 'file_operation'
  | 'file_access'
  | 'file_organization'
  | 'communication'
  | 'schedule'
  | 'media_playback'
  | 'system_control'
  | 'clipboard_operation'
  | 'information_capture'
  | 'code_task'
  | 'workflow_orchestration'
  | 'multi_agent'
  | 'general';
export type CapabilityCategory =
  | 'desktop'
  | 'data'
  | 'file'
  | 'terminal'
  | 'browser'
  | 'artifact'
  | 'capture'
  | 'clipboard'
  | 'communication'
  | 'schedule'
  | 'media'
  | 'system'
  | 'workflow'
  | 'group'
  | 'memory'
  | 'skill'
  | 'general';

export type ReadinessSnapshot = {
  ready: boolean;
  status?: string;
  message?: string | null;
  capabilities?: Record<string, unknown>;
};

export type DesktopExecutionCapabilitySnapshot = {
  available?: boolean;
  platform?: string;
  missing_permissions?: string[];
  blocking_conditions?: string[];
  tools?: string[];
  available_tools?: string[];
  degraded_tools?: string[];
  unavailable_tools?: string[];
  provider_supported_tools?: string[];
  provider_ready_tools?: string[];
  provider_blocked_tools?: string[];
  risk_default?: DesktopExecutionRisk;
  diagnostic_route?: string | null;
};

export type DesktopExecutionModeSnapshot = {
  mode?: DesktopExecutionMode | string;
  isolation?: DesktopIsolationKind | string;
  foreground_control?: boolean;
  keyboard_mouse_capture?: boolean;
  sandbox_recommended?: boolean;
  user_handoff_recommended?: boolean;
  approval_recommended?: boolean;
  reason?: string;
  mitigations?: string[];
};

export type DesktopExecutionPolicySnapshot = {
  mode?: DesktopExecutionPolicyMode | string;
  allow_live_foreground?: boolean | null;
  prefer_background_desktop?: boolean;
  prefer_isolated_desktop?: boolean;
  avoid_user_foreground_takeover?: boolean;
  require_sandbox_for_keyboard_mouse?: boolean;
  allow_media_control?: boolean;
  source?: string;
  reason?: string;
};

export type RuntimeExecutionPreferredEnvironment =
  | 'structured_runtime'
  | 'background_desktop'
  | 'isolated_desktop'
  | 'user_foreground'
  | 'user_handoff'
  | string;

export type RuntimeExecutionInteractionMode =
  | 'background'
  | 'read_only'
  | 'foreground'
  | 'handoff'
  | string;

export type RuntimeExecutionStrategySnapshot = {
  strategy_id: string;
  preferred_environment?: RuntimeExecutionPreferredEnvironment;
  interaction_mode?: RuntimeExecutionInteractionMode;
  policy_mode?: DesktopExecutionPolicyMode | string;
  background_desktop_preferred?: boolean;
  isolated_desktop_preferred?: boolean;
  foreground_takeover_allowed?: boolean;
  user_foreground_takeover_risk?: boolean;
  sandbox_required?: boolean;
  provider_auto_start_recommended?: boolean;
  local_foreground_fallback_allowed?: boolean;
  foreground_control_step_count?: number;
  keyboard_mouse_step_count?: number;
  sandbox_recommended_step_count?: number;
  approval_step_count?: number;
  handoff_step_count?: number;
  reasons?: string[];
  mitigations?: string[];
  source?: string;
};

export type DesktopProviderHealthSnapshot = {
  ok?: boolean;
  checked?: boolean;
  status?: string;
  status_code?: number | null;
  provider_kind?: DesktopIsolationKind | string;
  provider_id?: string;
  provider_version?: string;
  endpoint_origin?: string;
  endpoint_path?: string;
  transport?: string;
  health_report?: Record<string, unknown>;
  cached?: boolean;
  blocking_conditions?: string[];
  supported_tools?: string[];
  capabilities?: string[];
  foreground_mutation_supported?: boolean | null;
  keyboard_mouse_capture_supported?: boolean | null;
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean | null;
  foreground_takeover_required?: boolean | null;
  desktop_backend_kind?: string;
  desktop_backend_is_loopback?: boolean | null;
  desktop_backend_ready_for_public_release?: boolean | null;
  requires_real_virtual_desktop_backend?: boolean | null;
  requires_real_sandbox_for?: string[];
  error?: string;
};

export type DesktopProviderConformanceSnapshot = {
  ok?: boolean;
  mode?: string;
  runtime_checked?: boolean;
  release_candidate?: boolean;
  public_release_ready?: boolean;
  smoke_ok?: boolean | null;
  provider_contract_ok?: boolean | null;
  required_tools?: string[];
  covered_tools?: string[];
  missing_required_tools?: string[];
  failed_tools?: string[];
  blocking_conditions?: string[];
  release_blocking_conditions?: string[];
  provider_contract_blocking_conditions?: string[];
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean | null;
  foreground_takeover_required?: boolean | null;
  authentication_configured?: boolean;
  idempotent_tool_requests_supported?: boolean;
  desktop_backend_kind?: string;
  desktop_backend_is_loopback?: boolean | null;
  desktop_backend_ready_for_public_release?: boolean | null;
  requires_real_virtual_desktop_backend?: boolean | null;
};

export type SandboxDesktopProviderLaunchHint = {
  provider_id?: string;
  provider_kind?: DesktopIsolationKind | string;
  execution_mode?: string;
  command?: string[];
  env?: Record<string, string>;
  smoke_command?: string[];
  foreground_mutation_supported?: boolean;
  keyboard_mouse_capture_supported?: boolean;
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean;
  foreground_takeover_required?: boolean;
  requires_real_sandbox_for?: string[];
  isolated_provider?: {
    provider_id?: string;
    provider_kind?: DesktopIsolationKind | string;
    execution_mode?: string;
    command?: string[];
    smoke_command?: string[];
    env?: Record<string, string>;
    foreground_mutation_supported?: boolean;
    keyboard_mouse_capture_supported?: boolean;
    desktop_session_kind?: string;
    desktop_session_isolated?: boolean;
    foreground_takeover_required?: boolean;
    requires_runtime_approval?: boolean;
  };
  controlled_provider?: {
    provider_id?: string;
    provider_kind?: DesktopIsolationKind | string;
    execution_mode?: string;
    command?: string[];
    smoke_command?: string[];
    env?: Record<string, string>;
    foreground_mutation_supported?: boolean;
    keyboard_mouse_capture_supported?: boolean;
    desktop_session_kind?: string;
    desktop_session_isolated?: boolean;
    foreground_takeover_required?: boolean;
    requires_runtime_approval?: boolean;
  };
};

export type SandboxDesktopProviderSnapshot = {
  available?: boolean;
  provider_id?: string;
  provider_kind?: DesktopIsolationKind | string;
  status?: string;
  adapter_ready?: boolean;
  authentication_configured?: boolean;
  reason?: string;
  blocking_conditions?: string[];
  supported_tools?: string[];
  recommended_for?: string[];
  diagnostic_route?: string | null;
  source?: string;
  health?: DesktopProviderHealthSnapshot | null;
  launch_hint?: SandboxDesktopProviderLaunchHint;
  foreground_mutation_supported?: boolean;
  keyboard_mouse_capture_supported?: boolean;
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean | null;
  foreground_takeover_required?: boolean | null;
  desktop_backend_kind?: string;
  desktop_backend_is_loopback?: boolean | null;
  desktop_backend_ready_for_public_release?: boolean | null;
  requires_real_virtual_desktop_backend?: boolean | null;
  provider_contract?: Record<string, unknown>;
  provider_conformance?: DesktopProviderConformanceSnapshot | null;
  requires_real_sandbox_for?: string[];
};

export type ControlledDesktopProviderDiagnosticSnapshot = {
  ready?: boolean;
  release_ready?: boolean;
  configured?: boolean;
  authentication_configured?: boolean;
  status?: string;
  provider_id?: string;
  provider_kind?: DesktopIsolationKind | string;
  execution_mode?: string;
  source?: string;
  reason?: string;
  blocking_conditions?: string[];
  supported_tools?: string[];
  capabilities?: string[];
  foreground_mutation_supported?: boolean | null;
  keyboard_mouse_capture_supported?: boolean | null;
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean | null;
  foreground_takeover_required?: boolean | null;
  desktop_backend_kind?: string;
  desktop_backend_is_loopback?: boolean | null;
  desktop_backend_ready_for_public_release?: boolean | null;
  requires_real_virtual_desktop_backend?: boolean | null;
  provider_contract?: Record<string, unknown>;
  provider_conformance?: DesktopProviderConformanceSnapshot | null;
  requires_real_sandbox_for?: string[];
  requires_runtime_approval?: boolean;
  approval_required_tools?: string[];
  launch_command?: string[];
  smoke_command?: string[];
  env?: Record<string, string>;
  endpoint_origin?: string;
  endpoint_path?: string;
  status_endpoint_path?: string;
  health?: DesktopProviderHealthSnapshot | null;
  manifest?: Record<string, unknown>;
  session_manager?: Record<string, unknown>;
  public_release_readiness?: PublicReleaseReadinessSnapshot | null;
};

export type PublicReleaseReadinessSnapshot = {
  ready?: boolean;
  backend_ready?: boolean;
  provider_manifest?: string;
  configured_virtual_desktop_provider_requested?: boolean;
  blocking_conditions?: string[];
  backend?: Record<string, unknown>;
  next_actions?: PublicReleaseNextActionSnapshot[];
  required_commands?: Record<string, string>;
};

export type PublicReleaseNextActionSnapshot = {
  id?: string;
  title?: string;
  reason?: string;
  command?: string;
  required_environment?: string[];
  required_contract_fields?: Record<string, unknown>;
  required_tools?: string[];
};

export type DesktopExecutionRouteSnapshot = {
  route_id?: string;
  tool_name?: string;
  requested_mode?: DesktopExecutionPolicyMode | string;
  selected_provider_kind?: DesktopIsolationKind | string;
  selected_provider_id?: string;
  status?: string;
  can_execute?: boolean;
  can_auto_start?: boolean;
  provider_execution_required?: boolean;
  sandbox_required?: boolean;
  background_desktop_preferred?: boolean;
  isolated_desktop_preferred?: boolean;
  foreground_takeover_allowed?: boolean;
  desktop_execution_session_policy?: string;
  user_foreground_takeover_risk?: boolean;
  requires_user_foreground_session?: boolean;
  foreground_mutation_supported?: boolean | null;
  keyboard_mouse_capture_supported?: boolean | null;
  desktop_session_kind?: string;
  desktop_session_isolated?: boolean | null;
  foreground_takeover_required?: boolean | null;
  desktop_backend_kind?: string;
  desktop_backend_is_loopback?: boolean | null;
  desktop_backend_ready_for_public_release?: boolean | null;
  requires_real_virtual_desktop_backend?: boolean | null;
  simulated_desktop_provider?: boolean;
  provider_contract_blocking_conditions?: string[];
  fallback_mode?: DesktopExecutionPolicyMode | string;
  reason?: string;
  blocking_conditions?: string[];
  source?: string;
};

export type DesktopActionRiskSnapshot = {
  action_id: string;
  risk_level: DesktopExecutionRisk;
  title: string;
  description?: string;
  tools?: string[];
  requires_approval?: boolean;
  execution_mode?: DesktopExecutionModeSnapshot | null;
};

export type ToolCatalogItemSnapshot = {
  tool_name: string;
  function_name: string;
  description?: string;
  capability_id?: string | null;
  risk_level?: DesktopExecutionRisk | string | null;
  execution_mode?: DesktopExecutionModeSnapshot | null;
  provider_supported?: boolean;
  provider_ready?: boolean;
  provider_kind?: DesktopIsolationKind | string;
  provider_id?: string;
  user_foreground_takeover_risk?: boolean;
  requires_user_foreground_session?: boolean;
  approval_required?: boolean;
  input_schema?: Record<string, unknown>;
  model_tool_schema?: Record<string, unknown>;
  missing_permissions?: string[];
  blocking_conditions?: string[];
  fallback_notes?: string[];
  diagnostic_route?: string | null;
  source?: string;
};

export type RestrictedPluginToolSnapshot = {
  tool_name: string;
  tool_id?: string;
  function_name?: string;
  risk_level?: DesktopExecutionRisk | string | null;
  enabled?: boolean;
};

export type RestrictedToolPluginSnapshot = {
  plugin_id: string;
  enabled?: boolean;
  tool_names?: string[];
  tools?: RestrictedPluginToolSnapshot[];
  skill_docs?: string;
  source?: string;
};

export type InstallRestrictedToolPluginRequest = {
  plugin_id: string;
  enabled?: boolean;
};

export type UpdateRestrictedToolPluginRequest = {
  enabled?: boolean | null;
};

export type LegacyCleanupCoverageSnapshot = {
  legacy_boundary?: string;
  planner_owner?: string;
  total_samples?: number;
  cleanup_readiness?: string;
  remaining_fallback_count?: number;
  planner_covered_fallback_count?: number;
  compatibility_cleanup_pending_count?: number;
  areas?: Record<string, number>;
  prompts?: string[];
  covered_intents?: string[];
  covered_capabilities?: string[];
  covered_tools?: string[];
  area_contracts?: LegacyCleanupAreaContractSnapshot[];
  sample_contracts?: LegacyCleanupSampleContractSnapshot[];
  planner_owned_entrypoints?: LegacyPlannerOwnedEntrypointSnapshot[];
  remaining_fallback_contracts?: LegacyCleanupFallbackContractSnapshot[];
};

export type LegacyCleanupAreaContractSnapshot = {
  area: string;
  sample_count?: number;
  planner_intents?: string[];
  planner_capabilities?: string[];
  planner_tools?: string[];
};

export type LegacyCleanupSampleContractSnapshot = {
  prompt: string;
  area: string;
  planner_owner?: string;
  legacy_boundary?: string;
  cleanup_status?: string;
  planner_intents?: string[];
  planner_capabilities?: string[];
  planner_tools?: string[];
};

export type LegacyPlannerOwnedEntrypointSnapshot = {
  entrypoint_id: string;
  title: string;
  owner?: string;
  legacy_shape_preserved?: boolean;
  tools?: string[];
  example_prompts?: string[];
};

export type LegacyCleanupFallbackContractSnapshot = {
  fallback_id: string;
  title: string;
  reason?: string;
  owner?: string;
  planner_owner?: string;
  legacy_boundary?: string;
  status?: string;
  planner_coverage_status?: string;
  cleanup_blocker?: string;
  example_prompts?: string[];
  planner_evidence_prompts?: string[];
  required_before_delete?: string[];
};

export type ToolCatalogSnapshot = {
  tools: ToolCatalogItemSnapshot[];
  capabilities?: Record<string, DesktopExecutionCapabilitySnapshot>;
  sandbox_provider?: SandboxDesktopProviderSnapshot | null;
  controlled_provider_diagnostics?: ControlledDesktopProviderDiagnosticSnapshot | null;
  plugins?: RestrictedToolPluginSnapshot[];
  legacy_cleanup_coverage?: LegacyCleanupCoverageSnapshot | null;
  source?: string;
};

export type CapabilitySnapshot = {
  capability_id: string;
  title: string;
  category: CapabilityCategory | string;
  description?: string;
  tools?: string[];
  available_tools?: string[];
  missing_tools?: string[];
  risk_level?: DesktopExecutionRisk | string;
  approval_required?: boolean;
  discovery_actions?: string[];
  execution_actions?: string[];
  output_kinds?: string[];
  source?: string;
};

export type CapabilityPlanItemSnapshot = {
  capability_id: string;
  title: string;
  category?: CapabilityCategory | string;
  status?: 'available' | 'degraded' | 'missing' | string;
  required?: boolean;
  preferred?: boolean;
  reason?: string;
  selected_tools?: string[];
  available_tools?: string[];
  missing_tools?: string[];
  planned_step_ids?: string[];
  discovery_actions?: string[];
  execution_actions?: string[];
  output_kinds?: string[];
  risk_level?: DesktopExecutionRisk | string;
  approval_required?: boolean;
};

export type CapabilityPlanSnapshot = {
  plan_id: string;
  title: string;
  intent_kind?: TaskIntentKind | string;
  items?: CapabilityPlanItemSnapshot[];
  required_capabilities?: string[];
  preferred_capabilities?: string[];
  available_capabilities?: string[];
  missing_capabilities?: string[];
  approvals_required?: string[];
  source?: string;
};

export type TaskIntentSnapshot = {
  intent_id: string;
  kind: TaskIntentKind | string;
  title: string;
  user_goal?: string;
  confidence?: number;
  description?: string;
  inputs?: Record<string, unknown>;
  expected_outputs?: string[];
  required_capabilities?: string[];
  preferred_capabilities?: string[];
  missing_inputs?: string[];
  risk_level?: DesktopExecutionRisk | string;
  source?: string;
};

export type ToolPlanStepSnapshot = {
  step_id: string;
  title: string;
  capability_id: string;
  action?: string;
  tool_name?: string | null;
  input_preview?: Record<string, unknown>;
  risk_level?: DesktopExecutionRisk | string;
  execution_mode?: DesktopExecutionModeSnapshot | null;
  approval_required?: boolean;
  depends_on?: string[];
  reason?: string;
  fallback_tools?: string[];
  status?: 'planned' | 'unavailable' | 'skipped' | string;
};

export type ToolPlanSnapshot = {
  plan_id: string;
  title: string;
  steps?: ToolPlanStepSnapshot[];
  required_capabilities?: string[];
  missing_capabilities?: string[];
  approvals_required?: string[];
  artifacts_expected?: string[];
  open_questions?: string[];
  source?: string;
};

export type TaskWorkspaceItemSnapshot = {
  item_id: string;
  title: string;
  kind?: 'input' | 'scratch' | 'artifact' | 'checkpoint' | 'todo' | 'memory' | 'other' | string;
  path?: string | null;
  description?: string;
  source_step_id?: string | null;
  status?: string;
  metadata?: Record<string, unknown>;
};

export type TaskWorkspaceSnapshot = {
  workspace_id: string;
  title: string;
  root_label?: string;
  summary?: string;
  items?: TaskWorkspaceItemSnapshot[];
  context?: Record<string, unknown>;
  source?: string;
};

export type TaskTodoItemSnapshot = {
  todo_id: string;
  title: string;
  status?: 'pending' | 'in_progress' | 'blocked' | 'completed' | 'skipped' | string;
  capability_id?: string;
  step_id?: string | null;
  tool_name?: string | null;
  approval_required?: boolean;
  depends_on?: string[];
  reason?: string;
  metadata?: Record<string, unknown>;
};

export type TaskCheckpointSnapshot = {
  checkpoint_id: string;
  title: string;
  status?: 'planned' | 'ready' | 'waiting_approval' | 'blocked' | 'completed' | string;
  after_step_id?: string | null;
  depends_on?: string[];
  verifies?: string[];
  replan_on_failure?: boolean;
  payload?: Record<string, unknown>;
};

export type ReplanSignalSnapshot = {
  signal_id: string;
  trigger: string;
  source_step_id?: string | null;
  condition?: string;
  target?: string;
  fallback_tools?: string[];
  reason?: string;
};

export type TaskCoreSnapshot = {
  core_id: string;
  workspace: TaskWorkspaceSnapshot;
  todos?: TaskTodoItemSnapshot[];
  checkpoints?: TaskCheckpointSnapshot[];
  replan_signals?: ReplanSignalSnapshot[];
  source?: string;
};

export type TaskProgressSummarySnapshot = {
  core_id?: string | null;
  workspace_id?: string | null;
  status?: string;
  current_step_id?: string | null;
  current_step_title?: string | null;
  current_tool_name?: string | null;
  total_todos?: number;
  completed_todos?: number;
  active_todos?: number;
  blocked_todos?: number;
  skipped_todos?: number;
  total_checkpoints?: number;
  completed_checkpoints?: number;
  blocked_checkpoints?: number;
  waiting_approval_checkpoints?: number;
  total_workspace_items?: number;
  completed_workspace_items?: number;
  blocked_workspace_items?: number;
  pending_verification_count?: number;
  failed_verification_count?: number;
  verified_verification_count?: number;
  latest_verification_status?: string | null;
  latest_verification_step_id?: string | null;
  replan_request_count?: number;
  latest_replan_request_id?: string | null;
  latest_replan_trigger?: string | null;
  latest_replan_step_id?: string | null;
  needs_replan?: boolean;
  needs_user_action?: boolean;
  desktop_provider_session_status?: string | null;
  desktop_provider_session_needed?: boolean;
  desktop_provider_session_running?: boolean;
  desktop_provider_session_started?: boolean;
  desktop_provider_session_provider_id?: string | null;
  desktop_provider_session_tool_names?: string[];
  desktop_provider_session_needs_user_action?: boolean;
  desktop_provider_session_needs_replan?: boolean;
  blocked_step_ids?: string[];
  approval_step_ids?: string[];
  progress_text?: string;
  source?: string;
};

export type TaskReplanRequestSnapshot = {
  request_id: string;
  trigger: string;
  status?: 'requested' | 'planned' | 'running' | 'completed' | 'blocked' | string;
  run_id?: string | null;
  task_id?: string | null;
  decision_id?: string | null;
  plan_id?: string | null;
  core_id?: string | null;
  source_step_id?: string | null;
  source_tool_name?: string | null;
  target_capability_id?: string;
  condition?: string;
  reason?: string;
  failure_event_type?: string;
  failure_detail?: string;
  fallback_tools?: string[];
  recovery_actions?: ReplanRecoveryActionSnapshot[];
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  verification_targets?: Array<Record<string, unknown>>;
  task_verification_targets?: Array<Record<string, unknown>>;
  replan_prompt?: string;
  route_to_studio?: boolean;
  metadata?: Record<string, unknown>;
  created_at?: string;
  source?: string;
};

export type ReplanRecoveryActionSnapshot = {
  action_id: string;
  label: string;
  tool: string;
  input?: Record<string, unknown>;
  planning_reason?: string;
  permission_target?: string;
  risk_level?: string;
  approval_required?: boolean;
  approval_id?: string | null;
  approval_status?: string | null;
  selected?: boolean;
  deferred_tool?: string | null;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  verification_targets?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  source?: string;
};

export type ReplanRecoverySnapshot = {
  request_id: string;
  trigger: string;
  status?: 'requested' | 'planned' | 'running' | 'completed' | 'blocked' | string;
  run_id?: string | null;
  task_id?: string | null;
  group_run_id?: string | null;
  workflow_run_id?: string | null;
  decision_id?: string | null;
  plan_id?: string | null;
  core_id?: string | null;
  source_step_id?: string | null;
  source_tool_name?: string | null;
  target_capability_id?: string;
  fallback_tools?: string[];
  verification_targets?: Array<Record<string, unknown>>;
  selected_tool_name?: string | null;
  selected_step_id?: string | null;
  planning_reason?: string;
  recovery_action_label?: string;
  recovery_actions?: ReplanRecoveryActionSnapshot[];
  permission_target?: string;
  risk_level?: string;
  approval_id?: string | null;
  approval_status?: string | null;
  approval_ids?: string[];
  deferred_tool?: string | null;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  tool_call_id?: string | null;
  tool_call_ids?: string[];
  artifact_ids?: string[];
  artifact_paths?: string[];
  tool_status?: string | null;
  todo_status?: string | null;
  checkpoint_status?: string | null;
  failure_detail?: string;
  result_preview?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  recovery_event_ids?: string[];
  created_at?: string;
  updated_at?: string;
  source?: string;
};

export type RuntimePlanSnapshot = {
  plan_id: string;
  intent: TaskIntentSnapshot;
  capabilities?: CapabilitySnapshot[];
  capability_plan?: CapabilityPlanSnapshot | null;
  tool_plan: ToolPlanSnapshot;
  execution_strategy?: RuntimeExecutionStrategySnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  route_to_studio?: boolean;
  timeline_preview?: Array<Record<string, unknown>>;
  source?: string;
};

export type PlannerDecisionSnapshot = {
  decision_id: string;
  prompt: string;
  selected_intent: TaskIntentSnapshot;
  candidate_intents?: TaskIntentSnapshot[];
  plan: RuntimePlanSnapshot;
  created_at?: string;
  source?: string;
};

export type ReplanContinuationSnapshot = {
  continuation_id: string;
  request_id: string;
  action_id?: string | null;
  tool_name: string;
  prompt: string;
  title?: string;
  source_run_id?: string | null;
  source_task_id?: string | null;
  source_group_run_id?: string | null;
  source_workflow_run_id?: string | null;
  agent_id?: string | null;
  conversation_id?: string | null;
  client_run_id?: string | null;
  direct_tool_requests?: Array<Record<string, unknown>>;
  metadata?: Record<string, unknown>;
  task_context?: Record<string, unknown>;
  daily_desktop_planning_context?: string;
  approval_required?: boolean;
  auto_start_eligible?: boolean;
  auto_start_reason?: string;
  auto_start_blockers?: string[];
  risk_level?: string;
  source?: string;
};

export type RuntimeCheckpointPolicySnapshot = {
  checkpoint_ids?: string[];
  checkpoint_titles?: string[];
  verifies?: string[];
  replan_on_failure?: boolean;
  replan_triggers?: string[];
  replan_signal_ids?: string[];
  fallback_tools?: string[];
  verification_target_step_ids?: string[];
  requires_approval?: boolean;
  requires_observation?: boolean;
  requires_post_action_verification?: boolean;
  source?: string;
};

export type DesktopExecutionLoopSnapshot = {
  stage?: string;
  role?: string;
  action?: string;
  target_kind?: string;
  selection_source?: string;
  app_name?: string;
  query?: string;
  source_tool?: string;
  retry_tool?: string;
  retry_reason?: string;
  retry_input?: Record<string, unknown>;
  verification_target_step_ids?: string[];
  requires_observation?: boolean;
  requires_post_action_verification?: boolean;
  can_auto_retry?: boolean;
  source?: string;
};

export type RuntimeExecutionRequestSnapshot = {
  request_id: string;
  decision_id?: string | null;
  plan_id?: string | null;
  tool_plan_id?: string | null;
  intent_kind?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  group_run_id?: string | null;
  run_group_id?: string | null;
  group_id?: string | null;
  workflow_run_id?: string | null;
  workflow_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  workflow_node_kind?: string | null;
  step_id?: string | null;
  capability_id?: string | null;
  capability_title?: string;
  capability_status?: string;
  capability_reason?: string;
  capability_selected_tools?: string[];
  capability_planned_step_ids?: string[];
  tool_name: string;
  protocol?: string;
  input?: Record<string, unknown>;
  planning_reason?: string;
  presentation?: string;
  approval_required?: boolean;
  risk_level?: string;
  execution_mode?: DesktopExecutionModeSnapshot | null;
  desktop_execution_policy?: DesktopExecutionPolicySnapshot | null;
  sandbox_provider?: SandboxDesktopProviderSnapshot | null;
  desktop_execution_route?: DesktopExecutionRouteSnapshot | null;
  desktop_provider_session?: Record<string, unknown>;
  policy_reason?: string;
  continue_to_model?: boolean;
  depends_on?: string[];
  fallback_tools?: string[];
  status?: string;
  runtime_doctrine?: string;
  runtime_stage?: string;
  runtime_role?: string;
  requires_observation?: boolean;
  requires_post_action_verification?: boolean;
  replan_triggers?: string[];
  replan_signal_ids?: string[];
  followup_target?: Record<string, unknown>;
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  event_ids?: string[];
  tool_call_ids?: string[];
  approval_ids?: string[];
  artifact_ids?: string[];
  artifact_paths?: string[];
  task_todo?: TaskTodoItemSnapshot | null;
  task_checkpoints?: TaskCheckpointSnapshot[];
  task_workspace_items?: TaskWorkspaceItemSnapshot[];
  verification_targets?: Array<Record<string, unknown>>;
  task_verification_targets?: Array<Record<string, unknown>>;
  verification_status?: string | null;
  verification_step_id?: string | null;
  verification_event_ids?: string[];
  verification_artifact_paths?: string[];
  checkpoint_policy?: RuntimeCheckpointPolicySnapshot | null;
  desktop_loop?: DesktopExecutionLoopSnapshot | null;
  source?: string;
};

export type RuntimeExecutionEnvelopeSnapshot = {
  envelope_id: string;
  decision_id: string;
  plan_id: string;
  intent_kind: string;
  capability_plan?: CapabilityPlanSnapshot | null;
  execution_strategy?: RuntimeExecutionStrategySnapshot | null;
  requests?: RuntimeExecutionRequestSnapshot[];
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  approvals_required?: string[];
  artifacts_expected?: string[];
  open_questions?: string[];
  route_to_studio?: boolean;
  desktop_execution_policy?: DesktopExecutionPolicySnapshot | null;
  sandbox_provider?: SandboxDesktopProviderSnapshot | null;
  desktop_execution_route?: DesktopExecutionRouteSnapshot | null;
  desktop_provider_session?: Record<string, unknown>;
  runtime_doctrine?: string;
  runtime_stage_counts?: Record<string, number>;
  replan_signal_count?: number;
  source?: string;
};

export type PlannerTraceSummarySnapshot = {
  source?: string;
  decision_id?: string | null;
  plan_id?: string | null;
  intent_kind?: string | null;
  intent_title?: string | null;
  route_to_studio?: boolean | null;
  selection_source?: string | null;
  selection_role?: string | null;
  selection_reason?: string | null;
  planner_entrypoint?: string | null;
  entrypoint_source?: string | null;
  launcher_mode?: string | null;
  launcher_surface?: string | null;
  runnable_kind?: string | null;
  followup_target?: Record<string, unknown>;
  plan_tools?: string[];
  selected_tools?: string[];
  plan_capabilities?: string[];
  required_capabilities?: string[];
  missing_capabilities?: string[];
  approvals_required?: string[];
  artifacts_expected?: string[];
  open_questions?: string[];
  step_count?: number;
  event_count?: number;
};

export type RuntimeDebugSummarySnapshot = {
  source?: string;
  run_id?: string | null;
  task_id?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  planner_decision_id?: string | null;
  planner_plan_id?: string | null;
  intent_kind?: string | null;
  intent_title?: string | null;
  route_to_studio?: boolean | null;
  task_status?: string | null;
  current_step_id?: string | null;
  current_step_title?: string | null;
  current_tool_name?: string | null;
  current_workspace_item_id?: string | null;
  current_workspace_item_title?: string | null;
  current_workspace_item_kind?: string | null;
  current_workspace_item_path?: string | null;
  current_workspace_item_status?: string | null;
  total_todos?: number;
  completed_todos?: number;
  blocked_todos?: number;
  total_checkpoints?: number;
  completed_checkpoints?: number;
  blocked_checkpoints?: number;
  total_workspace_items?: number;
  completed_workspace_items?: number;
  blocked_workspace_items?: number;
  runtime_stage_counts?: Record<string, number>;
  runtime_doctrine?: string | null;
  runtime_stage?: string | null;
  runtime_role?: string | null;
  plan_tools?: string[];
  plan_capabilities?: string[];
  runtime_request_count?: number;
  pending_runtime_request_count?: number;
  completed_runtime_request_count?: number;
  recovered_runtime_request_count?: number;
  failed_runtime_request_count?: number;
  blocked_runtime_request_count?: number;
  blocked_direct_request_count?: number;
  blocked_runtime_request_tools?: string[];
  waiting_runtime_request_count?: number;
  current_request_id?: string | null;
  current_request_tool_name?: string | null;
  current_request_status?: string | null;
  latest_request_id?: string | null;
  latest_request_tool_name?: string | null;
  latest_request_status?: string | null;
  latest_blocked_request_tool_name?: string | null;
  latest_blocked_request_status?: string | null;
  latest_blocked_request_reason?: string | null;
  desktop_provider_session_status?: string | null;
  desktop_provider_session_needed?: boolean;
  desktop_provider_session_running?: boolean;
  desktop_provider_session_started?: boolean;
  desktop_provider_session_provider_id?: string | null;
  desktop_provider_session_reason?: string | null;
  desktop_provider_session_tool_names?: string[];
  desktop_provider_session_kind?: string | null;
  desktop_provider_session_isolated?: boolean | null;
  desktop_provider_session_foreground_takeover_required?: boolean | null;
  desktop_provider_session_keyboard_mouse_capture_supported?: boolean | null;
  desktop_provider_session_supported_tools?: string[];
  desktop_provider_backend_kind?: string | null;
  desktop_provider_backend_is_loopback?: boolean | null;
  desktop_provider_backend_ready_for_public_release?: boolean | null;
  desktop_provider_requires_real_virtual_backend?: boolean | null;
  desktop_provider_contract_ok?: boolean | null;
  desktop_provider_contract_version?: string | null;
  desktop_provider_contract_blocking_conditions?: string[];
  desktop_provider_manifest_ok?: boolean | null;
  desktop_provider_manifest_remote_endpoint_allowed?: boolean | null;
  desktop_provider_manifest_remote_endpoint_urls?: string[];
  desktop_provider_manifest_blocking_conditions?: string[];
  desktop_provider_conformance_ok?: boolean | null;
  desktop_provider_conformance_mode?: string | null;
  desktop_provider_conformance_smoke_ok?: boolean | null;
  desktop_provider_conformance_public_release_ready?: boolean | null;
  desktop_provider_conformance_release_candidate?: boolean | null;
  desktop_provider_conformance_release_blocking_conditions?: string[];
  desktop_provider_conformance_missing_required_tools?: string[];
  desktop_provider_conformance_failed_tools?: string[];
  desktop_execution_session_mode?: string | null;
  desktop_execution_session_label?: string | null;
  event_count?: number;
  tool_call_count?: number;
  completed_tool_call_count?: number;
  failed_tool_call_count?: number;
  blocked_tool_call_count?: number;
  waiting_tool_call_count?: number;
  approval_count?: number;
  pending_approval_count?: number;
  artifact_count?: number;
  memory_trace_count?: number;
  skill_trace_count?: number;
  child_run_count?: number;
  replan_recovery_count?: number;
  needs_user_action?: boolean;
  needs_replan?: boolean;
  latest_event_type?: string | null;
  current_capability_id?: string | null;
  latest_replan_request_id?: string | null;
  latest_replan_trigger?: string | null;
  latest_replan_status?: string | null;
  latest_recovery_failed?: boolean;
  latest_failed_recovery_tool?: string | null;
  latest_failed_recovery_action_id?: string | null;
  latest_failed_recovery_reason?: string | null;
  latest_recovery_action_id?: string | null;
  latest_recovery_tool?: string | null;
  latest_recovery_action_label?: string | null;
  latest_recovery_action_count?: number;
  latest_deferred_tool?: string | null;
  deferred_continuation_count?: number;
  latest_deferred_continuation_tool?: string | null;
  latest_tool_call_id?: string | null;
  latest_tool_name?: string | null;
  latest_tool_status?: string | null;
  latest_approval_id?: string | null;
  latest_approval_tool_name?: string | null;
  latest_approval_status?: string | null;
  latest_artifact_id?: string | null;
  latest_artifact_kind?: string | null;
  latest_artifact_path?: string | null;
  debug_surfaces?: string[];
};

export type PublicRunEvent = {
  event_id?: string | null;
  run_id: string;
  sequence: number;
  schema_version?: number;
  event_type: string;
  parent_run_id?: string | null;
  source_run_id?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  run_group_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  agent_id?: string | null;
  agent_name?: string | null;
  member_agent_id?: string | null;
  member_agent_name?: string | null;
  title?: string | null;
  detail?: string | null;
  actor?: string | null;
  visibility?: 'user' | 'internal' | string;
  sensitivity?: 'public' | 'secret' | string;
  payload?: Record<string, unknown>;
  created_at?: string;
};

export type RunEventPageSnapshot = {
  run_id: string;
  after_sequence: number;
  limit: number;
  next_after_sequence: number;
  has_more: boolean;
  events: PublicRunEvent[];
};

export type RerunRunRequest = {
  scope?: 'run' | 'workflow_node' | 'workflow_branch' | string;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  workflow_edge_branch?: string | null;
  workflow_node_selected_target?: string | null;
  reason?: string | null;
  metadata?: Record<string, unknown>;
};

export type ApprovalCardSnapshot = {
  approval_id: string;
  run_id?: string | null;
  source_run_id?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  source?: string | null;
  planning_reason?: string | null;
  step_id?: string | null;
  planner_step_id?: string | null;
  capability_id?: string | null;
  capability_title?: string | null;
  capability_status?: string | null;
  capability_reason?: string | null;
  capability_selected_tools?: string[];
  capability_planned_step_ids?: string[];
  decision_id?: string | null;
  plan_id?: string | null;
  tool_plan_id?: string | null;
  intent_kind?: string | null;
  replan_request_id?: string | null;
  replan_trigger?: string | null;
  task_workspace_items?: Array<Record<string, unknown>>;
  verification_targets?: Array<Record<string, unknown>>;
  task_verification_targets?: Array<Record<string, unknown>>;
  replan_triggers?: string[];
  replan_signal_ids?: string[];
  runtime_doctrine?: string | null;
  runtime_stage?: string | null;
  runtime_role?: string | null;
  requires_observation?: boolean | null;
  requires_post_action_verification?: boolean | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_execution_metadata?: Record<string, unknown>;
  deferred_tool?: string | null;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  title: string;
  description?: string | null;
  status?: ApprovalStatus | string;
  tool_name?: string | null;
  risk_level?: string | null;
  input_preview?: Record<string, unknown>;
  policy_reason?: string | null;
  requested_at?: string;
  resolved_at?: string | null;
  open_in_studio_url?: string | null;
};

export type ArtifactSnapshot = {
  artifact_id: string;
  run_id?: string | null;
  source_run_id?: string | null;
  source_tool?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_execution_metadata?: Record<string, unknown>;
  source?: string | null;
  planning_reason?: string | null;
  decision_id?: string | null;
  plan_id?: string | null;
  tool_plan_id?: string | null;
  intent_kind?: string | null;
  step_id?: string | null;
  planner_step_id?: string | null;
  capability_id?: string | null;
  capability_title?: string | null;
  capability_status?: string | null;
  capability_reason?: string | null;
  capability_selected_tools?: string[];
  capability_planned_step_ids?: string[];
  replan_request_id?: string | null;
  replan_trigger?: string | null;
  replan_triggers?: string[];
  replan_signal_ids?: string[];
  runtime_doctrine?: string | null;
  runtime_stage?: string | null;
  runtime_role?: string | null;
  requires_observation?: boolean | null;
  requires_post_action_verification?: boolean | null;
  title: string;
  kind: string;
  planned_kind?: string | null;
  source_kind?: string | null;
  requested_outputs?: string[] | null;
  manifest_index?: number | null;
  path?: string | null;
  mime_type?: string | null;
  size_bytes?: number | null;
  preview_text?: string | null;
  url?: string | null;
  created_at?: string;
};

export type ArtifactContentSnapshot = {
  ok?: boolean;
  run_id?: string | null;
  task_id?: string | null;
  path: string;
  content?: string;
  mime_type?: string | null;
  truncated?: boolean;
};

export type ToolCallSnapshot = {
  tool_call_id: string;
  run_id?: string | null;
  source_run_id?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  source?: string | null;
  planning_reason?: string | null;
  decision_id?: string | null;
  plan_id?: string | null;
  tool_plan_id?: string | null;
  intent_kind?: string | null;
  step_id?: string | null;
  planner_step_id?: string | null;
  capability_id?: string | null;
  capability_title?: string | null;
  capability_status?: string | null;
  capability_reason?: string | null;
  capability_selected_tools?: string[];
  capability_planned_step_ids?: string[];
  replan_request_id?: string | null;
  replan_trigger?: string | null;
  task_workspace_items?: Array<Record<string, unknown>>;
  verification_targets?: Array<Record<string, unknown>>;
  task_verification_targets?: Array<Record<string, unknown>>;
  replan_triggers?: string[];
  replan_signal_ids?: string[];
  runtime_doctrine?: string | null;
  runtime_stage?: string | null;
  runtime_role?: string | null;
  requires_observation?: boolean | null;
  requires_post_action_verification?: boolean | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_execution_metadata?: Record<string, unknown>;
  deferred_tool?: string | null;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  action_target?: Record<string, unknown>;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  tool_name: string;
  status: string;
  risk_level?: string | null;
  policy_reason?: string | null;
  input_preview?: Record<string, unknown>;
  output_preview?: Record<string, unknown>;
  metadata?: Record<string, unknown>;
  approval_id?: string | null;
  started_at?: string;
  completed_at?: string | null;
};

export type MemoryTraceSnapshot = {
  trace_id: string;
  run_id: string;
  event_id?: string | null;
  sequence?: number;
  event_type: string;
  status?: string;
  action?: string | null;
  memory_id?: string | null;
  memory_kind?: string | null;
  memory_scope?: string | null;
  count?: number;
  source_run_id?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  title: string;
  detail?: string | null;
  payload_preview?: Record<string, unknown>;
  created_at?: string;
};

export type SkillTraceSnapshot = {
  trace_id: string;
  run_id: string;
  event_id?: string | null;
  sequence?: number;
  event_type: string;
  status?: string;
  skill_id?: string | null;
  skill_name?: string | null;
  source_ref?: string | null;
  source_type?: string | null;
  tool_name?: string | null;
  source_run_id?: string | null;
  source_runnable_id?: string | null;
  source_runnable_name?: string | null;
  workflow_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  group_id?: string | null;
  group_run_id?: string | null;
  core_id?: string | null;
  workspace_id?: string | null;
  task_id?: string | null;
  title: string;
  detail?: string | null;
  payload_preview?: Record<string, unknown>;
  created_at?: string;
};

export type AgentTaskSnapshot = {
  task_id: string;
  conversation_id?: string | null;
  title: string;
  status: TaskStatus;
  summary?: string | null;
  current_step?: string | null;
  progress_text?: string | null;
  needs_user_action?: boolean;
  pending_approvals?: ApprovalCardSnapshot[];
  recent_events?: PublicRunEvent[];
  tool_calls?: ToolCallSnapshot[];
  artifacts?: ArtifactSnapshot[];
  metadata?: Record<string, unknown>;
  planner_summary?: PlannerTraceSummarySnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  open_in_studio_url?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type AgentTaskLightSnapshot = {
  task_id: string;
  conversation_id?: string | null;
  title: string;
  status: TaskStatus;
  detail?: string | null;
  needs_user_action?: boolean;
  pending_approval?: ApprovalCardSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  open_in_studio_url?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type RunTimelineChildSnapshot = {
  run_id: string;
  title?: string | null;
  status?: string;
  kind?: string | null;
  parent_run_id?: string | null;
  group_run_id?: string | null;
  run_group_id?: string | null;
  workflow_run_id?: string | null;
  workflow_node_id?: string | null;
  workflow_node_label?: string | null;
  agent_id?: string | null;
  workflow_id?: string | null;
  planner_summary?: PlannerTraceSummarySnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
};

export type RecoveryRunProvenanceSnapshot = {
  source?: string;
  kind?: string;
  source_run_id?: string | null;
  source_group_run_id?: string | null;
  source_workflow_run_id?: string | null;
  source_task_id?: string | null;
  source_task_title?: string | null;
  source_tool_call_id?: string | null;
  source_tool_name?: string | null;
  replan_request_id?: string | null;
  replan_trigger?: string | null;
  recovery_action_id?: string | null;
  recovery_action_kind?: string | null;
  recovery_tool?: string | null;
  recovery_input_preview?: Record<string, unknown>;
  recovery_permission_target?: string | null;
  recovery_risk_level?: string | null;
  approval_required?: boolean;
  task_core_context?: Record<string, unknown>;
};

export type RunTimelineSnapshot = {
  run_id: string;
  parent_run_id?: string | null;
  group_run_id?: string | null;
  run_group_id?: string | null;
  workflow_run_id?: string | null;
  agent_id?: string | null;
  status: string;
  title?: string | null;
  task_id?: string | null;
  session_id?: string | null;
  task_run_link_created_at?: string | null;
  task_run_link_updated_at?: string | null;
  task_run_link_run_status?: string | null;
  task_run_link_last_event_sequence?: number | null;
  rerun_of_run_id?: string | null;
  rerun_of_kind?: string | null;
  rerun_of_status?: string | null;
  rerun_of_runnable_id?: string | null;
  rerun_of_runnable_name?: string | null;
  rerun_original_created_at?: string | null;
  rerun_original_updated_at?: string | null;
  planner_summary?: PlannerTraceSummarySnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  recovery_source?: RecoveryRunProvenanceSnapshot | null;
  events?: PublicRunEvent[];
  tool_calls?: ToolCallSnapshot[];
  memory_traces?: MemoryTraceSnapshot[];
  skill_traces?: SkillTraceSnapshot[];
  approvals?: ApprovalCardSnapshot[];
  pending_approval?: ApprovalCardSnapshot | null;
  artifacts?: ArtifactSnapshot[];
  children?: RunTimelineChildSnapshot[];
  created_at?: string;
  updated_at?: string;
};

export type AgentDefinitionSnapshot = {
  agent_id: string;
  name: string;
  nickname?: string | null;
  description?: string | null;
  instructions?: string | null;
  persona_prompt?: string | null;
  avatar_url?: string | null;
  category?: string | null;
  model_mode?: string | null;
  execution_backend?: string | null;
  model_profile_id?: string | null;
  vision_model_profile_id?: string | null;
  model_config?: Record<string, unknown>;
  tool_policy?: Record<string, unknown>;
  workspace_policy?: Record<string, unknown>;
  skill_ids?: string[];
  output_contract?: string | null;
  enabled?: boolean;
  virtual?: boolean;
  system?: boolean;
  builtin?: boolean;
  editable?: boolean;
  deletable?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type AgentDeskItemSnapshot = {
  path: string;
  name: string;
  kind: 'file' | 'directory' | 'note';
  size_bytes?: number | null;
  mime_type?: string | null;
  preview_text?: string | null;
  updated_at?: string;
};

export type AgentDeskSnapshot = {
  agent_id: string;
  root_path: string;
  notes_path?: string;
  metadata_path?: string;
  items?: AgentDeskItemSnapshot[];
  updated_at?: string;
};

export type SkillSnapshot = {
  skill_id: string;
  name: string;
  description?: string | null;
  source_path?: string | null;
  local_path?: string | null;
  folder_id?: string | null;
  folder_name?: string | null;
  source_type?: string | null;
  origin_path?: string | null;
  source_ref?: string | null;
  content_hash?: string | null;
  last_synced_at?: string | null;
  sync_status?: string | null;
  content_summary?: string | null;
  skill_markdown?: string | null;
  asset_paths?: string[];
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type SkillFolderSnapshot = {
  folder_id: string;
  name: string;
  description?: string | null;
  source_scope?: 'all' | 'installed' | 'native' | string;
  sort_order?: number;
  skill_count?: number;
  installed_count?: number;
  native_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type SkillSourceRootSnapshot = {
  path: string;
  source_type: string;
  library?: 'native' | 'installed' | string | null;
  exists?: boolean;
  skill_count?: number;
};

export type MemorySnapshot = {
  memory_id: string;
  scope: string;
  kind: string;
  content: string;
  source_session_id?: string | null;
  source_message_id?: string | null;
  source_task_id?: string | null;
  source_run_id?: string | null;
  confidence?: number;
  pinned?: boolean;
  user_confirmed?: boolean;
  created_at?: string;
  updated_at?: string;
  deleted_at?: string | null;
};

export type FutureTaskSnapshot = {
  future_task_id: string;
  title: string;
  prompt: string;
  runnable_id?: string | null;
  runnable_name?: string | null;
  status: 'scheduled' | 'triggered' | 'cancelled' | 'failed' | string;
  scheduled_at_epoch: number;
  cron?: string | null;
  source_run_id?: string | null;
  last_run_id?: string | null;
  run_count?: number;
  error?: string | null;
  created_at?: string;
  updated_at?: string;
  cancelled_at?: string | null;
  trigger_in_flight?: boolean;
};

export type AgentGroupMemberSnapshot = {
  agent_id: string;
  name: string;
  role?: string | null;
  sort_order?: number;
  enabled?: boolean;
  run_id?: string | null;
  run_status?: string | null;
  tool_calls?: ToolCallSnapshot[];
  pending_approvals?: ApprovalCardSnapshot[];
  artifacts?: ArtifactSnapshot[];
};

export type AgentGroupSnapshot = {
  group_id: string;
  name: string;
  description?: string | null;
  members: AgentGroupMemberSnapshot[];
  mode: GroupMode | string;
  moderator_agent_id?: string | null;
  default_model?: string | null;
  memory_scope?: MemoryScope | string;
  tool_policy_id?: string | null;
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type GroupRunSnapshot = {
  group_run_id: string;
  run_group_id?: string | null;
  group_id: string;
  title: string;
  status: string;
  objective: string;
  participants: AgentGroupMemberSnapshot[];
  active_speaker_agent_id?: string | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  planner_summary?: PlannerTraceSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  events?: PublicRunEvent[];
  runs?: RunTimelineSnapshot[];
  child_run_ids?: string[];
  tool_calls?: ToolCallSnapshot[];
  memory_traces?: MemoryTraceSnapshot[];
  skill_traces?: SkillTraceSnapshot[];
  shared_artifacts?: ArtifactSnapshot[];
  pending_approvals?: ApprovalCardSnapshot[];
  final_answer?: string | null;
  created_at?: string;
  updated_at?: string;
};

export type WorkflowSnapshot = {
  workflow_id: string;
  name: string;
  description?: string | null;
  nodes?: Array<Record<string, unknown>>;
  edges?: Array<Record<string, unknown>>;
  default_input_schema?: Record<string, unknown>;
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type ChatRunnableParticipantSnapshot = {
  runnable_id: string;
  agent_id?: string | null;
  workflow_id?: string | null;
  group_id?: string | null;
  kind: 'agent' | 'workflow';
  name: string;
  nickname?: string | null;
  avatar_url?: string | null;
  category?: string | null;
  enabled?: boolean;
};

export type ChatRunnableSnapshot = {
  runnable_id: string;
  agent_id?: string | null;
  workflow_id?: string | null;
  group_id?: string | null;
  kind: 'agent' | 'workflow' | 'group';
  name: string;
  nickname?: string | null;
  description?: string | null;
  avatar_url?: string | null;
  category?: string | null;
  output_contract?: string | null;
  enabled?: boolean;
  tool_capabilities?: string[];
  approval_required_tools?: string[];
  participants?: ChatRunnableParticipantSnapshot[];
};

export type ChatRunnableCatalogSnapshot = {
  agents: ChatRunnableSnapshot[];
  workflows: ChatRunnableSnapshot[];
  groups: ChatRunnableSnapshot[];
};

export type WorkflowRunSnapshot = RunTimelineSnapshot & {
  workflow_id?: string | null;
  objective?: string;
  current_node_id?: string | null;
  current_node_label?: string | null;
  final_answer?: string | null;
};

export type PlannerOrchestrationStartSnapshot = {
  kind: 'workflow' | 'group_run' | string;
  status: 'started' | 'handoff' | 'unsupported' | 'target_not_found' | string;
  decision: PlannerDecisionSnapshot;
  run_id?: string | null;
  workflow_run_id?: string | null;
  group_run_id?: string | null;
  run_group_id?: string | null;
  target_id?: string | null;
  target_name?: string | null;
  objective?: string;
  title?: string;
  route_to_studio?: boolean;
  message?: string;
  workflow_run?: WorkflowRunSnapshot | null;
  group_run?: GroupRunSnapshot | null;
};

export type FutureTaskTriggerResultSnapshot = {
  ok?: boolean;
  future_task?: FutureTaskSnapshot | null;
  run?: RunTimelineSnapshot | null;
  error?: string | null;
};

export type StartChatTaskRequest = {
  prompt: string;
  conversation_id?: string | null;
  title?: string | null;
  agent_id?: string | null;
  workflow_id?: string | null;
  group_id?: string | null;
  attachments?: Array<{
    id: string;
    name: string;
    mime_type: string;
    size: number;
    width?: number;
    height?: number;
    data_url: string;
  }>;
  metadata?: Record<string, unknown>;
};

export type StartPlannerOrchestrationRequest = {
  prompt: string;
  objective?: string | null;
  title?: string | null;
  target_id?: string | null;
  target_name?: string | null;
  allowed_tools?: string[] | null;
  client_run_id?: string | null;
  metadata?: Record<string, unknown>;
};
