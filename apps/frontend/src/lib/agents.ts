import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  shouldFallbackToLegacyRoute,
} from './bridge';
import type {
  YachiyoGroupRunSnapshot,
  YachiyoRunTimelineSnapshot,
} from '../features/yachiyo-studio/types';
import type {
  AgentGroupMemberSnapshot,
  ApprovalCardSnapshot,
  ArtifactSnapshot,
  PlannerTraceSummarySnapshot,
  PublicRunEvent,
  ReplanRecoverySnapshot,
  RuntimeDebugSummarySnapshot,
  RuntimeExecutionEnvelopeSnapshot,
  RunEventPageSnapshot,
  TaskCoreSnapshot,
  TaskProgressSummarySnapshot,
} from '../features/runtime-shared/types';
import {
  publicGroupRunToRunGroupSpec,
  publicRunTimelineToRunSpec,
  publicRunTimelineToStudioRunSpec,
} from '../features/agent-studio/utils/runs';

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
  kind: 'agent_run' | 'workflow_run' | string;
  runnable_id: string;
  runnable_name?: string;
  status: string;
  user_goal?: string;
  result?: string;
  runtime_execution_envelope?: RuntimeExecutionEnvelopeSnapshot | null;
  runtime_debug?: RuntimeDebugSummarySnapshot | null;
  task_core?: TaskCoreSnapshot | null;
  task_progress?: TaskProgressSummarySnapshot | null;
  replan_recoveries?: ReplanRecoverySnapshot[];
  timeline?: Array<Record<string, unknown>>;
  artifacts?: Array<Record<string, unknown>>;
  pending_approval?: {
    approval_id?: string;
    tool?: string;
    input_preview?: Record<string, unknown> | string;
    requested_at?: string;
  };
  created_at?: string;
  updated_at?: string;
  agent_run_id?: string;
  workflow_run_id?: string;
};

export type RunEventSpec = PublicRunEvent;

export type RunEventsPage = RunEventPageSnapshot;

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

export type FutureTaskTriggerResultSpec = {
  ok?: boolean;
  future_task?: FutureTaskSpec;
  run?: RunSpec;
  error?: string;
};

function uniqueByKey<T>(items: T[], getKey: (item: T) => string): T[] {
  const indexByKey = new Map<string, number>();
  const uniqueItems: T[] = [];
  items.forEach((item) => {
    const key = getKey(item).trim();
    if (!key) {
      uniqueItems.push(item);
      return;
    }
    const existingIndex = indexByKey.get(key);
    if (existingIndex === undefined) {
      indexByKey.set(key, uniqueItems.length);
      uniqueItems.push(item);
      return;
    }
    uniqueItems[existingIndex] = item;
  });
  return uniqueItems;
}

export async function listAgents(): Promise<AgentSpec[]> {
  const payload = await apiGet<{ agents?: AgentSpec[] }>('/yachiyo/studio/agents').catch(() => (
    apiGet<{ agents?: AgentSpec[] }>('/ui/agents')
  ));
  return uniqueByKey(payload.agents || [], (agent) => String(agent.agent_id || ''));
}

export async function createAgent(request: Partial<AgentSpec>): Promise<AgentSpec> {
  return apiPost<AgentSpec>('/yachiyo/studio/agents', request).catch(() => (
    apiPost<AgentSpec>('/ui/agents', request)
  ));
}

export async function updateAgent(agentId: string, request: Partial<AgentSpec>): Promise<AgentSpec> {
  return apiPatch<AgentSpec>(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}`, request).catch(() => (
    apiPatch<AgentSpec>(`/ui/agents/${encodeURIComponent(agentId)}`, request)
  ));
}

export async function deleteAgent(agentId: string): Promise<{ ok?: boolean }> {
  return apiDelete(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}`).catch(() => (
    apiDelete(`/ui/agents/${encodeURIComponent(agentId)}`)
  ));
}

export async function testAgentModel(agentId: string): Promise<{ ok?: boolean; message?: string; missing?: string[] }> {
  return apiPost(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}/test-model`).catch(() => (
    apiPost(`/ui/agents/${encodeURIComponent(agentId)}/test-model`)
  ));
}

export async function attachSkill(agentId: string, skillId: string): Promise<AgentSpec> {
  return apiPost<AgentSpec>(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}/skills`, { skill_id: skillId }).catch(() => (
    apiPost<AgentSpec>(`/ui/agents/${encodeURIComponent(agentId)}/skills`, { skill_id: skillId })
  ));
}

export async function detachSkill(agentId: string, skillId: string): Promise<AgentSpec> {
  return apiDelete<AgentSpec>(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}/skills/${encodeURIComponent(skillId)}`).catch(() => (
    apiDelete<AgentSpec>(`/ui/agents/${encodeURIComponent(agentId)}/skills/${encodeURIComponent(skillId)}`)
  ));
}

export async function listSkills(): Promise<SkillSpec[]> {
  const payload = await apiGet<{ skills?: SkillSpec[] }>('/yachiyo/studio/skills').catch(() => (
    apiGet<{ skills?: SkillSpec[] }>('/ui/skills')
  ));
  return payload.skills || [];
}

export async function importSkill(sourcePath: string, folderId?: string): Promise<SkillSpec> {
  return apiPost<SkillSpec>('/yachiyo/studio/skills/import', { source_path: sourcePath, folder_id: folderId || undefined }).catch(() => (
    apiPost<SkillSpec>('/ui/skills/import', { source_path: sourcePath, folder_id: folderId || undefined })
  ));
}

export async function listSkillSources(): Promise<SkillSourceRoot[]> {
  const payload = await apiGet<{ roots?: SkillSourceRoot[] }>('/yachiyo/studio/skills/sources').catch(() => (
    apiGet<{ roots?: SkillSourceRoot[] }>('/ui/skills/sources')
  ));
  return payload.roots || [];
}

export async function listSkillFolders(): Promise<SkillFolderSpec[]> {
  const payload = await apiGet<{ folders?: SkillFolderSpec[] }>('/yachiyo/studio/skill-folders').catch(() => (
    apiGet<{ folders?: SkillFolderSpec[] }>('/ui/skill-folders')
  ));
  return payload.folders || [];
}

export async function createSkillFolder(request: Partial<SkillFolderSpec>): Promise<SkillFolderSpec> {
  return apiPost<SkillFolderSpec>('/yachiyo/studio/skill-folders', request).catch(() => (
    apiPost<SkillFolderSpec>('/ui/skill-folders', request)
  ));
}

export async function updateSkillFolder(folderId: string, request: Partial<SkillFolderSpec>): Promise<SkillFolderSpec> {
  return apiPatch<SkillFolderSpec>(`/yachiyo/studio/skill-folders/${encodeURIComponent(folderId)}`, request).catch(() => (
    apiPatch<SkillFolderSpec>(`/ui/skill-folders/${encodeURIComponent(folderId)}`, request)
  ));
}

export async function deleteSkillFolder(folderId: string, options: { deleteSkills?: boolean } = {}): Promise<{ ok?: boolean; deleted_skill_count?: number }> {
  const query = options.deleteSkills ? '?delete_skills=true' : '';
  return apiDelete(`/yachiyo/studio/skill-folders/${encodeURIComponent(folderId)}${query}`).catch(() => (
    apiDelete(`/ui/skill-folders/${encodeURIComponent(folderId)}${query}`)
  ));
}

export async function syncNativeSkills(): Promise<SkillSyncResponse> {
  return apiPost<SkillSyncResponse>('/yachiyo/studio/skills/sync', {}).catch(() => (
    apiPost<SkillSyncResponse>('/ui/skills/sync', {})
  ));
}

export async function installSkillCommand(command: string, folderId?: string): Promise<SkillInstallResponse> {
  return apiPost<SkillInstallResponse>('/yachiyo/studio/skills/install', { command, folder_id: folderId || undefined }).catch(() => (
    apiPost<SkillInstallResponse>('/ui/skills/install', { command, folder_id: folderId || undefined })
  ));
}

export async function updateSkill(skillId: string, request: Partial<SkillSpec>): Promise<SkillSpec> {
  return apiPatch<SkillSpec>(`/yachiyo/studio/skills/${encodeURIComponent(skillId)}`, request).catch(() => (
    apiPatch<SkillSpec>(`/ui/skills/${encodeURIComponent(skillId)}`, request)
  ));
}

export async function deleteSkill(skillId: string): Promise<{ ok?: boolean }> {
  return apiDelete(`/yachiyo/studio/skills/${encodeURIComponent(skillId)}`).catch(() => (
    apiDelete(`/ui/skills/${encodeURIComponent(skillId)}`)
  ));
}

export async function listWorkflows(): Promise<WorkflowSpec[]> {
  const payload = await apiGet<{ workflows?: WorkflowSpec[] }>('/yachiyo/studio/workflows').catch(() => (
    apiGet<{ workflows?: WorkflowSpec[] }>('/ui/workflows')
  ));
  return payload.workflows || [];
}

export async function createWorkflow(request: Partial<WorkflowSpec>): Promise<WorkflowSpec> {
  return apiPost<WorkflowSpec>('/yachiyo/studio/workflows', request).catch(() => (
    apiPost<WorkflowSpec>('/ui/workflows', request)
  ));
}

export async function updateWorkflow(workflowId: string, request: Partial<WorkflowSpec>): Promise<WorkflowSpec> {
  return apiPatch<WorkflowSpec>(`/yachiyo/studio/workflows/${encodeURIComponent(workflowId)}`, request).catch(() => (
    apiPatch<WorkflowSpec>(`/ui/workflows/${encodeURIComponent(workflowId)}`, request)
  ));
}

export async function deleteWorkflow(workflowId: string): Promise<{ ok?: boolean }> {
  return apiDelete(`/yachiyo/studio/workflows/${encodeURIComponent(workflowId)}`).catch(() => (
    apiDelete(`/ui/workflows/${encodeURIComponent(workflowId)}`)
  ));
}

export async function listRunnables(): Promise<RunnableSummary[]> {
  const payload = await apiGet<{ runnables?: RunnableSummary[] }>('/ui/runnables');
  return payload.runnables || [];
}

export async function listMemories(): Promise<MemorySpec[]> {
  const payload = await apiGet<{ memories?: MemorySpec[] }>('/yachiyo/studio/memories').catch(() => (
    apiGet<{ memories?: MemorySpec[] }>('/ui/memories')
  ));
  return payload.memories || [];
}

export async function deleteMemory(memoryId: string, reason = ''): Promise<{ ok?: boolean }> {
  const query = reason ? `?reason=${encodeURIComponent(reason)}` : '';
  return apiDelete(`/yachiyo/studio/memories/${encodeURIComponent(memoryId)}${query}`).catch(() => (
    apiDelete(`/ui/memories/${encodeURIComponent(memoryId)}${query}`)
  ));
}

export async function listFutureTasks(): Promise<FutureTaskSpec[]> {
  const payload = await apiGet<{ future_tasks?: FutureTaskSpec[] }>('/yachiyo/studio/future-tasks').catch(() => (
    apiGet<{ future_tasks?: FutureTaskSpec[] }>('/ui/future-tasks')
  ));
  return payload.future_tasks || [];
}

export async function cancelFutureTask(futureTaskId: string, reason = ''): Promise<{ ok?: boolean; future_task?: FutureTaskSpec }> {
  return apiPost(`/yachiyo/studio/future-tasks/${encodeURIComponent(futureTaskId)}/cancel`, reason ? { reason } : {}).catch(() => (
    apiPost(`/ui/future-tasks/${encodeURIComponent(futureTaskId)}/cancel`, reason ? { reason } : {})
  ));
}

export async function triggerDueFutureTasks(): Promise<{ ok?: boolean; triggered?: FutureTaskTriggerResultSpec[] }> {
  return apiPost<{
    ok?: boolean;
    triggered?: Array<{ ok?: boolean; future_task?: FutureTaskSpec; run?: YachiyoRunTimelineSnapshot; error?: string }>;
  }>('/yachiyo/studio/future-tasks/trigger-due', {})
    .then((payload) => ({
      ...payload,
      triggered: (payload.triggered || []).map(futureTaskTriggerResultFromPublicSnapshot),
    }))
    .catch(() => apiPost('/ui/future-tasks/trigger-due', {}));
}

export async function listRuns(): Promise<RunSpec[]> {
  return apiGet<{ runs?: YachiyoRunTimelineSnapshot[] }>('/yachiyo/studio/runs')
    .then((payload) => (payload.runs || []).map(runSpecFromPublicTimelineSnapshot))
    .catch(async () => {
      const payload = await apiGet<{ runs?: RunSpec[] }>('/ui/runs');
      return payload.runs || [];
    });
}

export async function listRunGroups(): Promise<RunGroupSpec[]> {
  return apiGet<{ group_runs?: YachiyoGroupRunSnapshot[] }>('/yachiyo/studio/group-runs')
    .then((payload) => (payload.group_runs || []).map(runGroupSpecFromPublicGroupRun))
    .catch(async () => {
      const payload = await apiGet<{ run_groups?: RunGroupSpec[] }>('/ui/run-groups');
      return payload.run_groups || [];
    });
}

export async function getRunGroup(runGroupId: string): Promise<RunGroupSpec> {
  return apiGet<YachiyoGroupRunSnapshot>(`/yachiyo/studio/group-runs/${encodeURIComponent(runGroupId)}`)
    .then(runGroupSpecFromPublicGroupRun)
    .catch(() => apiGet(`/ui/run-groups/${encodeURIComponent(runGroupId)}`));
}

export async function getRun(runId: string): Promise<RunSpec> {
  return apiGet<YachiyoRunTimelineSnapshot>(`/yachiyo/studio/runs/${encodeURIComponent(runId)}/timeline`)
    .then(runSpecFromPublicTimelineSnapshot)
    .catch(() => apiGet(`/ui/runs/${encodeURIComponent(runId)}`));
}

export async function getRunEvents(runId: string, afterSequence = 0, limit = 200): Promise<RunEventsPage> {
  const query = new URLSearchParams({
    after_sequence: String(Math.max(0, afterSequence)),
    limit: String(Math.max(1, limit)),
  });
  return apiGet(`/yachiyo/studio/runs/${encodeURIComponent(runId)}/events?${query.toString()}`);
}

export async function deleteRun(runId: string): Promise<{ ok?: boolean; deleted_run_ids?: string[]; deleted_run_count?: number }> {
  return apiDelete(`/yachiyo/studio/runs/${encodeURIComponent(runId)}`).catch(() => (
    apiDelete(`/ui/runs/${encodeURIComponent(runId)}`)
  ));
}

export async function getRunArtifact(runId: string, path: string): Promise<{ ok?: boolean; path?: string; content?: string; truncated?: boolean }> {
  const encodedPath = path.split('/').map((part) => encodeURIComponent(part)).join('/');
  return apiGet(`/yachiyo/studio/runs/${encodeURIComponent(runId)}/artifacts/${encodedPath}`).catch(() => (
    apiGet(`/ui/runs/${encodeURIComponent(runId)}/artifacts/${encodedPath}`)
  ));
}

export async function createAgentRun(agentId: string, userGoal: string): Promise<RunSpec> {
  const clientRunId = createClientRunId();
  return apiPost<YachiyoRunTimelineSnapshot>(`/yachiyo/studio/agents/${encodeURIComponent(agentId)}/runs`, {
    objective: userGoal,
    client_run_id: clientRunId,
  })
    .then((snapshot) => publicRunTimelineToStudioRunSpec(snapshot, {
      kind: 'agent_run',
      runnableId: agentId,
      userGoal,
    }))
    .catch(() => (
      apiPost('/ui/agent-runs', { agent_id: agentId, user_goal: userGoal, client_run_id: clientRunId })
    ));
}

export async function createWorkflowRun(workflowId: string, userGoal: string): Promise<RunSpec> {
  const clientRunId = createClientRunId();
  return apiPost<YachiyoRunTimelineSnapshot>(`/yachiyo/studio/workflows/${encodeURIComponent(workflowId)}/runs`, {
    objective: userGoal,
    client_run_id: clientRunId,
  })
    .then((snapshot) => publicRunTimelineToStudioRunSpec(snapshot, {
      kind: 'workflow_run',
      runnableId: workflowId,
      userGoal,
    }))
    .catch(() => (
      apiPost('/ui/workflow-runs', { workflow_id: workflowId, user_goal: userGoal, client_run_id: clientRunId })
    ));
}

function createClientRunId() {
  if (globalThis.crypto?.randomUUID) return globalThis.crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`;
}

function runGroupSpecFromPublicGroupRun(snapshot: YachiyoGroupRunSnapshot): RunGroupSpec {
  return publicGroupRunToRunGroupSpec(snapshot);
}

function runSpecFromPublicTimelineSnapshot(snapshot: YachiyoRunTimelineSnapshot): RunSpec {
  return publicRunTimelineToRunSpec(snapshot);
}

function futureTaskTriggerResultFromPublicSnapshot(
  snapshot: { ok?: boolean; future_task?: FutureTaskSpec; run?: YachiyoRunTimelineSnapshot; error?: string },
): FutureTaskTriggerResultSpec {
  return {
    ...snapshot,
    run: snapshot.run ? runSpecFromPublicTimelineSnapshot(snapshot.run) : undefined,
  };
}

export async function rerunRun(runId: string): Promise<RunSpec> {
  return apiPost<YachiyoRunTimelineSnapshot>(`/yachiyo/studio/runs/${encodeURIComponent(runId)}/rerun`, {})
    .then(runSpecFromPublicTimelineSnapshot)
    .catch(() => apiPost(`/ui/runs/${encodeURIComponent(runId)}/rerun`, {}));
}

export async function cancelRun(runId: string): Promise<RunSpec> {
  return apiPost<YachiyoRunTimelineSnapshot>(`/yachiyo/studio/runs/${encodeURIComponent(runId)}/cancel`, {})
    .then(runSpecFromPublicTimelineSnapshot)
    .catch(() => apiPost(`/ui/runs/${encodeURIComponent(runId)}/cancel`, {}));
}

export async function approveRunApproval(runId: string, approvalId: string): Promise<RunSpec> {
  const body = { approval_id: approvalId };
  return apiPost<YachiyoRunTimelineSnapshot>(
    `/yachiyo/studio/runs/${encodeURIComponent(runId)}/approval/approve`,
    body,
  )
    .then(runSpecFromPublicTimelineSnapshot)
    .catch((error: unknown) => {
      if (!shouldFallbackToLegacyRoute(error)) throw error;
      return apiPost(`/ui/runs/${encodeURIComponent(runId)}/approval/approve`, body);
    });
}

export async function rejectRunApproval(
  runId: string,
  approvalId: string,
  reason = '',
): Promise<RunSpec> {
  const body = { approval_id: approvalId, reason: reason || undefined };
  return apiPost<YachiyoRunTimelineSnapshot>(
    `/yachiyo/studio/runs/${encodeURIComponent(runId)}/approval/reject`,
    body,
  )
    .then(runSpecFromPublicTimelineSnapshot)
    .catch((error: unknown) => {
      if (!shouldFallbackToLegacyRoute(error)) throw error;
      return apiPost(`/ui/runs/${encodeURIComponent(runId)}/approval/reject`, body);
    });
}
