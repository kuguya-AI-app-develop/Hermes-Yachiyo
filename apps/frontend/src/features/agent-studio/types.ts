import type {
  AgentGroupMemberSnapshot,
  ApprovalCardSnapshot,
  ArtifactSnapshot,
  PlannerTraceSummarySnapshot,
  PublicRunEvent,
  RecoveryRunProvenanceSnapshot,
  ReplanRecoverySnapshot,
  RuntimeDebugSummarySnapshot,
  RuntimeExecutionEnvelopeSnapshot,
  TaskCoreSnapshot,
  TaskProgressSummarySnapshot,
} from '../runtime-shared/types';

export type AgentModelMode = 'follow_main' | 'profile' | 'custom_api';
export type AgentExecutionBackend = 'native_profile';

export type AgentSpec = {
  agent_id: string;
  name: string;
  nickname?: string;
  description?: string;
  avatar_url?: string;
  category?: string;
  instructions?: string;
  persona_prompt?: string;
  model_mode: AgentModelMode;
  execution_backend?: AgentExecutionBackend;
  model_profile_id?: string;
  vision_model_profile_id?: string;
  model_config: {
    provider?: 'openai_compatible' | string;
    base_url?: string;
    model?: string;
    api_key?: string;
    api_key_configured?: boolean;
  };
  tool_policy?: Record<string, unknown>;
  workspace_policy?: Record<string, unknown>;
  skill_ids?: string[];
  output_contract?: 'chat' | 'markdown' | 'diff' | 'report' | 'artifacts' | string;
  enabled?: boolean;
  virtual?: boolean;
  system?: boolean;
  builtin?: boolean;
  editable?: boolean;
  deletable?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type SkillSpec = {
  skill_id: string;
  name: string;
  description?: string;
  source_path?: string;
  local_path?: string;
  folder_id?: string;
  folder_name?: string;
  source_type?: string;
  origin_path?: string;
  source_ref?: string;
  content_hash?: string;
  last_synced_at?: string;
  sync_status?: string;
  content_summary?: string;
  skill_markdown?: string;
  asset_paths?: string[];
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type SkillSourceRoot = {
  path: string;
  source_type: string;
  library?: 'native' | 'installed' | string;
  exists?: boolean;
  skill_count?: number;
};

export type SkillFolderSpec = {
  folder_id: string;
  name: string;
  description?: string;
  source_scope?: 'all' | 'installed' | 'native' | string;
  sort_order?: number;
  skill_count?: number;
  installed_count?: number;
  native_count?: number;
  created_at?: string;
  updated_at?: string;
};

export type SkillSyncResult = {
  source?: string;
  source_type?: string;
  source_ref?: string;
  status: 'imported' | 'updated' | 'skipped' | 'failed' | string;
  skill_id?: string;
  name?: string;
  message?: string;
};

export type SkillSyncResponse = {
  ok?: boolean;
  roots?: SkillSourceRoot[];
  summary?: Record<string, number>;
  results?: SkillSyncResult[];
};

export type SkillInstallResponse = {
  ok?: boolean;
  installer?: string;
  command?: string[];
  started_at?: string;
  finished_at?: string;
  returncode?: number;
  stdout?: string;
  stderr?: string;
  sync?: SkillSyncResponse | null;
};

export type WorkflowNode = {
  id: string;
  type?: string;
  position?: { x: number; y: number };
  data?: Record<string, unknown>;
};

export type WorkflowEdge = {
  id?: string;
  source: string;
  target: string;
  data?: Record<string, unknown>;
  branch?: string;
  condition?: string;
  label?: string;
  sourceHandle?: string | null;
};

export type WorkflowSpec = {
  workflow_id: string;
  name: string;
  description?: string;
  nodes: WorkflowNode[];
  edges: WorkflowEdge[];
  default_input_schema?: Record<string, unknown>;
  enabled?: boolean;
  created_at?: string;
  updated_at?: string;
};

export type RunnableSummary = {
  id: string;
  name: string;
  nickname?: string;
  description?: string;
  avatar_url?: string;
  category?: string;
  output_contract?: 'chat' | 'markdown' | 'diff' | 'report' | 'artifacts' | 'workflow' | string;
  kind: 'agent' | 'workflow';
  enabled?: boolean;
  tool_policy?: {
    allowed_tools?: string[];
    approval_required?: Record<string, boolean>;
  };
  participants?: RunnableSummary[];
};

export type RunSpec = {
  run_id: string;
  parent_run_id?: string;
  run_group_id?: string;
  run_group_source?: string;
  task_id?: string;
  session_id?: string;
  task_run_link_created_at?: string;
  task_run_link_updated_at?: string;
  task_run_link_run_status?: string;
  task_run_link_last_event_sequence?: number;
  rerun_of_run_id?: string;
  rerun_of_kind?: string;
  rerun_of_status?: string;
  rerun_of_runnable_id?: string;
  rerun_of_runnable_name?: string;
  rerun_original_created_at?: string;
  rerun_original_updated_at?: string;
  recovery_source?: RecoveryRunProvenanceSnapshot | null;
  kind: 'agent_run' | 'workflow_run' | string;
  runnable_id: string;
  runnable_name?: string;
  status: string;
  user_goal?: string;
  result?: string;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  timeline?: Array<Record<string, unknown>>;
  artifacts?: Array<Record<string, unknown>>;
  pending_approval?: {
    approval_id?: string;
    tool?: string;
    input_preview?: Record<string, unknown> | string;
    open_in_studio_url?: string;
    policy_reason?: string;
    requested_at?: string;
    resolved_at?: string;
    risk_level?: string;
    run_id?: string;
    source_run_id?: string;
    status?: string;
    planning_reason?: string;
    decision_id?: string;
    plan_id?: string;
    tool_plan_id?: string;
    intent_kind?: string;
    step_id?: string;
    planner_step_id?: string;
    capability_id?: string;
    replan_request_id?: string;
    replan_trigger?: string;
    replan_triggers?: string[];
    replan_signal_ids?: string[];
    runtime_doctrine?: string;
    runtime_stage?: string;
    runtime_role?: string;
    requires_observation?: boolean;
    requires_post_action_verification?: boolean;
    action_target?: Record<string, unknown>;
    observation_evidence?: Record<string, unknown>;
    observation_retry?: Record<string, unknown>;
    task_workspace_items?: Array<Record<string, unknown>>;
    verification_targets?: Array<Record<string, unknown>>;
    task_verification_targets?: Array<Record<string, unknown>>;
  };
  created_at?: string;
  updated_at?: string;
  agent_run_id?: string;
  workflow_id?: string;
  workflow_run_id?: string;
  objective?: string;
  current_node_id?: string;
  current_node_label?: string;
  final_answer?: string;
};

export type RunGroupSpec = {
  run_group_id: string;
  group_id?: string;
  title: string;
  source?: string;
  workspace_dir?: string;
  status: string;
  objective?: string;
  summary?: string;
  participants?: AgentGroupMemberSnapshot[];
  events?: PublicRunEvent[];
  pending_approvals?: ApprovalCardSnapshot[];
  shared_artifacts?: ArtifactSnapshot[];
  planner_summary?: PlannerTraceSummarySnapshot | null;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  final_answer?: string;
  child_run_ids?: string[];
  created_at?: string;
  updated_at?: string;
};

export type MemorySpec = {
  memory_id: string;
  scope: string;
  kind: string;
  content: string;
  source_session_id?: string;
  source_message_id?: string;
  source_task_id?: string;
  source_run_id?: string;
  confidence?: number;
  pinned?: boolean;
  user_confirmed?: boolean;
  created_at?: string;
  updated_at?: string;
  deleted_at?: string;
};

export type FutureTaskSpec = {
  future_task_id: string;
  title: string;
  prompt: string;
  runnable_id?: string;
  runnable_name?: string;
  status: 'scheduled' | 'triggered' | 'cancelled' | 'failed' | string;
  scheduled_at_epoch: number;
  cron?: string;
  source_run_id?: string;
  last_run_id?: string;
  run_count?: number;
  error?: string;
  created_at?: string;
  updated_at?: string;
  cancelled_at?: string;
  trigger_in_flight?: boolean;
};

export type AgentDraft = {
  agent_id?: string;
  name: string;
  nickname: string;
  description: string;
  avatar_url: string;
  category: string;
  instructions: string;
  persona_prompt: string;
  model_mode: 'profile' | 'custom_api';
  model_profile_id: string;
  vision_model_profile_id: string;
  base_url: string;
  model: string;
  api_key: string;
  output_contract: string;
  allow_workspace_read: boolean;
  allow_workspace_write: boolean;
  allow_terminal: boolean;
  allow_artifacts: boolean;
  allow_screen_context: boolean;
  allow_app_control: boolean;
  allow_media_control: boolean;
  allow_foreground_input: boolean;
  allow_browser_control: boolean;
  default_workdir: string;
  readable_scopes: string;
  writable_scopes: string;
  enabled: boolean;
};
