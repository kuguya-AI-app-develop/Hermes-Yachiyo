export type RuntimeToolRecoveryAction = {
  action_id?: string;
  action_kind?:
    | 'permission_recovery'
    | 'retry_original'
    | 'observe_desktop_state'
    | 'observe_desktop_controls'
    | 'supervised_live_retry'
    | 'sandbox_desktop_handoff'
    | 'desktop_provider_session_start';
  approval_required?: boolean;
  approval_id?: string;
  approval_status?: string;
  auto_start_eligible?: boolean;
  auto_start_reason?: string;
  auto_start_blockers?: string[];
  action_target?: Record<string, unknown>;
  deferred_tool?: string;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  desktop_execution_policy?: Record<string, unknown>;
  desktop_execution_route?: Record<string, unknown>;
  sandbox_provider?: Record<string, unknown>;
  input: Record<string, unknown>;
  label: string;
  metadata?: Record<string, unknown>;
  permission_target: string;
  prompt: string;
  recommended_tools?: string[];
  replan_request_id?: string;
  required_retry_fields?: string[];
  risk_level?: string;
  retry_input?: Record<string, unknown>;
  retry_input_schema?: Record<string, unknown>;
  retry_input_source?: string;
  retry_artifact_tool?: string;
  retry_artifact_kind?: string;
  followup_input?: Record<string, unknown>;
  followup_tool?: string;
  retry_prompt?: string;
  retry_source_event_type?: string;
  retry_source_tool_call_id?: string;
  retry_tool?: string;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  selected?: boolean;
  task_verification_targets?: Array<Record<string, unknown>>;
  tool: string;
  verification_targets?: Array<Record<string, unknown>>;
};

export type RuntimeToolRecoveryRetryContext = Pick<
  RuntimeToolRecoveryAction,
  | 'recommended_tools'
  | 'required_retry_fields'
  | 'retry_input'
  | 'retry_input_schema'
  | 'retry_input_source'
  | 'retry_artifact_tool'
  | 'retry_artifact_kind'
  | 'followup_tool'
  | 'followup_input'
  | 'retry_prompt'
  | 'retry_source_event_type'
  | 'retry_source_tool_call_id'
  | 'retry_tool'
>;

export type RuntimeToolRecoveryActionTaskMetadata = {
  daily_desktop_intent: true;
  desktop_permission_recovery: true;
  desktop_permission_retry?: true;
  recovery_action_kind?: RuntimeToolRecoveryAction['action_kind'];
  recovery_action_id?: string;
  recovery_input: Record<string, unknown>;
  recovery_permission_target: string;
  recovery_risk_level?: string;
  approval_id?: string;
  approval_status?: string;
  action_target?: Record<string, unknown>;
  deferred_tool?: string;
  deferred_input?: Record<string, unknown>;
  deferred_context?: Record<string, unknown>;
  deferred_continuation?: Array<Record<string, unknown>>;
  desktop_execution_policy?: Record<string, unknown>;
  desktop_execution_route?: Record<string, unknown>;
  sandbox_provider?: Record<string, unknown>;
  replan_recovery_action_id?: string;
  replan_request_id?: string;
  recovery_retry_input?: Record<string, unknown>;
  recovery_retry_input_schema?: Record<string, unknown>;
  recovery_retry_input_source?: string;
  recovery_retry_artifact_tool?: string;
  recovery_retry_artifact_kind?: string;
  recovery_followup_input?: Record<string, unknown>;
  recovery_followup_tool?: string;
  recovery_retry_prompt?: string;
  recovery_retry_source_event_type?: string;
  recovery_retry_source_tool_call_id?: string;
  recovery_retry_tool?: string;
  observation_evidence?: Record<string, unknown>;
  observation_retry?: Record<string, unknown>;
  recommended_tools?: string[];
  required_retry_fields?: string[];
  recovery_tool: string;
  task_verification_targets?: Array<Record<string, unknown>>;
  verification_targets?: Array<Record<string, unknown>>;
} & Record<string, unknown>;

export type RuntimeToolRecoveryActionTaskStart = {
  metadata: RuntimeToolRecoveryActionTaskMetadata;
  prompt: string;
  title: string;
};

export type RuntimeToolRecoveryActionRunStartRequest = {
  request_id: string;
  action_id: string;
  title: string;
  continue_to_model: true;
  metadata: Record<string, unknown>;
};

export type RuntimeToolRecoveryActionToolCallSource = {
  group_run_id?: string | null;
  run_id?: string | null;
  source_run_id?: string | null;
  tool_call_id?: string | null;
  tool_name?: string | null;
};

export type RuntimeToolRecoveryActionToolRunStartRequest = {
  tool_call_id: string;
  action_id: string;
  action_kind?: RuntimeToolRecoveryAction['action_kind'];
  title: string;
  continue_to_model: true;
  input_override?: Record<string, unknown>;
  metadata: Record<string, unknown>;
};

export const RUNTIME_TOOL_RECOVERY_TASK_METADATA_KEYS = [
  'daily_desktop_intent',
  'desktop_permission_recovery',
  'desktop_permission_retry',
  'recovery_action_kind',
  'recovery_action_id',
  'recovery_tool',
  'recovery_input',
  'recovery_permission_target',
  'recovery_risk_level',
  'approval_id',
  'approval_status',
  'deferred_tool',
  'deferred_input',
  'deferred_context',
  'deferred_continuation',
  'desktop_execution_policy',
  'desktop_execution_route',
  'sandbox_provider',
  'replan_recovery_action_id',
  'replan_request_id',
  'recovery_retry_tool',
  'recovery_retry_input',
  'recovery_retry_input_schema',
  'recovery_retry_input_source',
  'recovery_retry_artifact_tool',
  'recovery_retry_artifact_kind',
  'recovery_followup_tool',
  'recovery_followup_input',
  'action_target',
  'observation_evidence',
  'observation_retry',
  'required_retry_fields',
  'recommended_tools',
  'verification_targets',
  'task_verification_targets',
  'recovery_retry_prompt',
  'recovery_retry_source_event_type',
  'recovery_retry_source_tool_call_id',
  'source_task_id',
  'source_task_title',
] as const;

export function runtimeToolRecoveryActionPrompt(action: RuntimeToolRecoveryAction): string {
  const tool = String(action.tool || '').trim();
  const input = objectValue(action.input);
  // The selected inputs own the executable request. A saved label can be
  // incomplete or stale after the user edits a recovery parameter.
  const fallbackPrompt = runtimeToolRecoveryExecutableLabel(tool, input);
  if (fallbackPrompt) return fallbackPrompt;
  const prompt = String(action.prompt || '').trim();
  if (isExecutableRecoveryPrompt(prompt)) return prompt;
  const label = String(action.label || '').trim();
  if (isExecutableRecoveryPrompt(label)) return label;
  return prompt || label || tool;
}

export function runtimeToolRecoveryActionTaskMetadata(
  action: RuntimeToolRecoveryAction,
  extra: Record<string, unknown> = {},
): RuntimeToolRecoveryActionTaskMetadata {
  const riskLevel = String(action.risk_level || '').trim()
    || (isLowRiskExecutableRecoveryTool(action.tool) ? 'low' : '');
  return {
    daily_desktop_intent: true,
    ...(action.action_kind === 'retry_original' ? { desktop_permission_retry: true } : {}),
    desktop_permission_recovery: true,
    ...(action.action_kind ? { recovery_action_kind: action.action_kind } : {}),
    ...(action.action_id ? {
      recovery_action_id: action.action_id,
      replan_recovery_action_id: action.action_id,
    } : {}),
    recovery_input: action.input,
    recovery_permission_target: action.permission_target,
    recovery_risk_level: riskLevel,
    recovery_tool: action.tool,
    ...(action.approval_id ? { approval_id: action.approval_id } : {}),
    ...(action.approval_status ? { approval_status: action.approval_status } : {}),
    ...runtimeToolRecoveryObservationMetadata(action),
    ...(action.deferred_tool ? { deferred_tool: action.deferred_tool } : {}),
    ...(Object.keys(action.deferred_input || {}).length ? { deferred_input: action.deferred_input } : {}),
    ...(Object.keys(action.deferred_context || {}).length ? { deferred_context: action.deferred_context } : {}),
    ...(action.deferred_continuation?.length ? { deferred_continuation: action.deferred_continuation } : {}),
    ...(Object.keys(action.desktop_execution_policy || {}).length
      ? { desktop_execution_policy: action.desktop_execution_policy }
      : {}),
    ...(Object.keys(action.desktop_execution_route || {}).length
      ? { desktop_execution_route: action.desktop_execution_route }
      : {}),
    ...(Object.keys(action.sandbox_provider || {}).length
      ? { sandbox_provider: action.sandbox_provider }
      : {}),
    ...(action.replan_request_id ? { replan_request_id: action.replan_request_id } : {}),
    ...(action.retry_tool ? { recovery_retry_tool: action.retry_tool } : {}),
    ...(action.retry_tool || action.retry_input ? { recovery_retry_input: action.retry_input || {} } : {}),
    ...(action.retry_input_schema ? { recovery_retry_input_schema: action.retry_input_schema } : {}),
    ...(action.retry_input_source ? { recovery_retry_input_source: action.retry_input_source } : {}),
    ...(action.retry_artifact_tool ? { recovery_retry_artifact_tool: action.retry_artifact_tool } : {}),
    ...(action.retry_artifact_kind ? { recovery_retry_artifact_kind: action.retry_artifact_kind } : {}),
    ...(action.followup_tool ? { recovery_followup_tool: action.followup_tool } : {}),
    ...(action.followup_input ? { recovery_followup_input: action.followup_input } : {}),
    ...(action.required_retry_fields?.length ? { required_retry_fields: action.required_retry_fields } : {}),
    ...(action.recommended_tools?.length ? { recommended_tools: action.recommended_tools } : {}),
    ...(action.verification_targets?.length ? { verification_targets: action.verification_targets } : {}),
    ...(action.task_verification_targets?.length ? { task_verification_targets: action.task_verification_targets } : {}),
    ...(action.retry_prompt ? { recovery_retry_prompt: action.retry_prompt } : {}),
    ...(action.retry_source_event_type ? { recovery_retry_source_event_type: action.retry_source_event_type } : {}),
    ...(action.retry_source_tool_call_id ? { recovery_retry_source_tool_call_id: action.retry_source_tool_call_id } : {}),
    ...extra,
  };
}

export function runtimeToolRecoveryActionTaskStart(
  action: RuntimeToolRecoveryAction,
  extra: Record<string, unknown> = {},
): RuntimeToolRecoveryActionTaskStart {
  const prompt = runtimeToolRecoveryActionPrompt(action);
  return {
    prompt,
    title: action.label || prompt,
    metadata: runtimeToolRecoveryActionTaskMetadata(action, extra),
  };
}

export function runtimeToolRecoveryActionRunStartRequest(
  action: RuntimeToolRecoveryAction,
  requestId: string,
  extra: Record<string, unknown> = {},
): RuntimeToolRecoveryActionRunStartRequest | null {
  const actionId = String(action.action_id || '').trim();
  const cleanRequestId = String(requestId || action.replan_request_id || '').trim();
  if (!actionId || !cleanRequestId) return null;
  return {
    request_id: cleanRequestId,
    action_id: actionId,
    title: action.label || action.prompt || action.tool,
    continue_to_model: true,
    metadata: {
      permission_target: action.permission_target,
      ...runtimeToolRecoveryApprovalDeferredMetadata(action),
      ...(action.verification_targets?.length ? { verification_targets: action.verification_targets } : {}),
      ...(action.task_verification_targets?.length ? { task_verification_targets: action.task_verification_targets } : {}),
      ...extra,
    },
  };
}

export function runtimeToolRecoveryActionToolRunStartRequest(
  toolCall: RuntimeToolRecoveryActionToolCallSource,
  action: RuntimeToolRecoveryAction,
  extra: Record<string, unknown> = {},
): RuntimeToolRecoveryActionToolRunStartRequest | null {
  const toolCallId = String(toolCall.tool_call_id || '').trim();
  const actionId = String(action.action_id || '').trim();
  if (!toolCallId || !actionId) return null;
  const input = objectValue(action.input);
  return {
    tool_call_id: toolCallId,
    action_id: actionId,
    ...(action.action_kind ? { action_kind: action.action_kind } : {}),
    title: action.label || action.prompt || action.tool,
    continue_to_model: true,
    ...(action.action_kind === 'retry_original' && Object.keys(input).length
      ? { input_override: input }
      : {}),
    metadata: {
      permission_target: action.permission_target,
      ...runtimeToolRecoveryApprovalDeferredMetadata(action),
      source_group_run_id: toolCall.group_run_id || '',
      source_run_id: toolCall.run_id || toolCall.source_run_id || '',
      source_tool_call_id: toolCallId,
      source_tool_name: toolCall.tool_name || '',
      ...(action.verification_targets?.length ? { verification_targets: action.verification_targets } : {}),
      ...(action.task_verification_targets?.length ? { task_verification_targets: action.task_verification_targets } : {}),
      ...extra,
    },
  };
}

function runtimeToolRecoveryApprovalDeferredMetadata(
  action: RuntimeToolRecoveryAction,
): Record<string, unknown> {
  return {
    ...(action.approval_id ? { approval_id: action.approval_id } : {}),
    ...(action.approval_status ? { approval_status: action.approval_status } : {}),
    ...runtimeToolRecoveryObservationMetadata(action),
    ...(action.deferred_tool ? { deferred_tool: action.deferred_tool } : {}),
    ...(Object.keys(action.deferred_input || {}).length ? { deferred_input: action.deferred_input } : {}),
    ...(Object.keys(action.deferred_context || {}).length ? { deferred_context: action.deferred_context } : {}),
    ...(action.deferred_continuation?.length ? { deferred_continuation: action.deferred_continuation } : {}),
    ...(Object.keys(action.desktop_execution_policy || {}).length
      ? { desktop_execution_policy: action.desktop_execution_policy }
      : {}),
    ...(Object.keys(action.desktop_execution_route || {}).length
      ? { desktop_execution_route: action.desktop_execution_route }
      : {}),
    ...(Object.keys(action.sandbox_provider || {}).length
      ? { sandbox_provider: action.sandbox_provider }
      : {}),
  };
}

function runtimeToolRecoveryObservationMetadata(
  action: RuntimeToolRecoveryAction,
): Record<string, unknown> {
  const actionTarget = objectValue(action.action_target);
  const observationEvidence = objectValue(action.observation_evidence);
  const observationRetry = objectValue(action.observation_retry);
  return {
    ...(Object.keys(actionTarget).length ? { action_target: actionTarget } : {}),
    ...(Object.keys(observationEvidence).length ? { observation_evidence: observationEvidence } : {}),
    ...(Object.keys(observationRetry).length ? { observation_retry: observationRetry } : {}),
  };
}

export function runtimeToolRecoveryRetryAction(
  action: RuntimeToolRecoveryAction,
): RuntimeToolRecoveryAction | null {
  const retryTool = String(action.retry_tool || '').trim();
  if (!retryTool) return null;
  const retryInput = objectValue(action.retry_input);
  const prompt = String(action.retry_prompt || '').trim()
    || runtimeToolRecoveryRetryPrompt(retryTool, retryInput);
  if (!prompt) return null;
  return {
    action_id: action.action_id,
    action_kind: 'retry_original',
    approval_required: action.approval_required,
    approval_id: action.approval_id,
    approval_status: action.approval_status,
    auto_start_eligible: action.auto_start_eligible,
    auto_start_reason: action.auto_start_reason,
    auto_start_blockers: action.auto_start_blockers,
    action_target: action.action_target,
    deferred_tool: action.deferred_tool,
    deferred_input: action.deferred_input,
    deferred_context: action.deferred_context,
    deferred_continuation: action.deferred_continuation,
    desktop_execution_policy: action.desktop_execution_policy,
    desktop_execution_route: action.desktop_execution_route,
    sandbox_provider: action.sandbox_provider,
    input: retryInput,
    label: '恢复后重试原操作',
    permission_target: action.permission_target,
    prompt,
    recommended_tools: action.recommended_tools,
    replan_request_id: action.replan_request_id,
    required_retry_fields: action.required_retry_fields,
    risk_level: action.risk_level,
    retry_input: retryInput,
    retry_input_schema: action.retry_input_schema,
    retry_input_source: action.retry_input_source,
    retry_artifact_tool: action.retry_artifact_tool,
    retry_artifact_kind: action.retry_artifact_kind,
    retry_prompt: prompt,
    retry_source_event_type: action.retry_source_event_type,
    retry_source_tool_call_id: action.retry_source_tool_call_id,
    retry_tool: retryTool,
    observation_evidence: action.observation_evidence,
    observation_retry: action.observation_retry,
    task_verification_targets: action.task_verification_targets,
    tool: retryTool,
    verification_targets: action.verification_targets,
  };
}

export function runtimeToolRecoveryActionWithInputPatch(
  action: RuntimeToolRecoveryAction,
  inputPatch: Record<string, unknown>,
): RuntimeToolRecoveryAction {
  const patch = objectValue(inputPatch);
  if (!Object.keys(patch).length) return action;
  const input = {
    ...objectValue(action.input),
    ...patch,
  };
  const retryInput = {
    ...objectValue(action.retry_input),
    ...patch,
  };
  return {
    ...action,
    input,
    ...(action.retry_tool || action.retry_input ? { retry_input: retryInput } : {}),
  };
}

export function runtimeToolRecoveryMissingRequiredFields(
  action: RuntimeToolRecoveryAction,
): string[] {
  const input = objectValue(action.input);
  return (action.required_retry_fields || []).filter((field) => {
    const value = input[field];
    return value === undefined || value === null || value === '';
  });
}

export function runtimeToolRecoveryActionsFromRecords(
  sources: Array<Record<string, unknown>>,
  retryContext: RuntimeToolRecoveryRetryContext = {},
): RuntimeToolRecoveryAction[] {
  const byKey = new Map<string, RuntimeToolRecoveryAction>();
  sources
    .flatMap((source) => runtimeToolRecoveryActionsFromRecord(source, retryContext))
    .forEach((action) => {
      const key = `${action.tool}:${JSON.stringify(action.input)}:${action.permission_target}`;
      if (!byKey.has(key)) byKey.set(key, action);
    });
  return Array.from(byKey.values());
}

export function runtimeToolRecoveryActionsFromRecord(
  source: Record<string, unknown>,
  retryContext: RuntimeToolRecoveryRetryContext = {},
): RuntimeToolRecoveryAction[] {
  const data = objectValue(source.data);
  const rawActions = [
    ...(Array.isArray(source.recovery_actions) ? source.recovery_actions : []),
    ...(Array.isArray(data.recovery_actions) ? data.recovery_actions : []),
  ];
  return rawActions.flatMap((rawAction) => {
    const action = objectValue(rawAction);
    const metadata = objectValue(action.metadata || source.metadata);
    const tool = String(action.tool || '').trim();
    const input = objectValue(action.input);
    const fallbackLabel = runtimeToolRecoveryExecutableLabel(tool, input);
    if (!fallbackLabel) return [];
    const label = String(action.label || fallbackLabel || tool).trim();
    const actionRetryContext = runtimeToolRecoveryRetryContext(action, retryContext);
    const autoStart = runtimeToolRecoveryAutoStartContext(action, source);
    return [{
      action_id: String(action.action_id || action.id || '').trim() || undefined,
      approval_required: Boolean(action.approval_required || action.requires_approval),
      approval_id: String(action.approval_id || source.approval_id || '').trim() || undefined,
      approval_status: String(action.approval_status || source.approval_status || '').trim() || undefined,
      ...autoStart,
      action_target: objectValue(action.action_target || source.action_target),
      deferred_tool: String(action.deferred_tool || source.deferred_tool || '').trim() || undefined,
      deferred_input: objectValue(action.deferred_input || source.deferred_input),
      deferred_context: objectValue(action.deferred_context || source.deferred_context),
      deferred_continuation: recoveryRecordList(
        action.deferred_continuation || source.deferred_continuation,
      ),
      desktop_execution_policy: objectValue(
        action.desktop_execution_policy || source.desktop_execution_policy || metadata.desktop_execution_policy,
      ),
      desktop_execution_route: objectValue(
        action.desktop_execution_route || source.desktop_execution_route || metadata.desktop_execution_route,
      ),
      sandbox_provider: objectValue(
        action.sandbox_provider || source.sandbox_provider || metadata.sandbox_provider,
      ),
      input,
      label,
      metadata,
      permission_target: String(action.permission_target || '').trim(),
      prompt: String(action.prompt || label || fallbackLabel || tool).trim(),
      recommended_tools: recoveryStringList(action.recommended_tools),
      replan_request_id: String(
        action.replan_request_id
        || action.request_id
        || source.replan_request_id
        || source.request_id
        || '',
      ).trim() || undefined,
      required_retry_fields: recoveryStringList(action.required_retry_fields),
      risk_level: String(action.risk_level || '').trim() || undefined,
      observation_evidence: objectValue(action.observation_evidence || source.observation_evidence),
      observation_retry: objectValue(action.observation_retry || source.observation_retry),
      selected: Boolean(action.selected),
      task_verification_targets: recoveryRecordList(
        action.task_verification_targets || source.task_verification_targets || source.verification_targets,
      ),
      verification_targets: recoveryRecordList(
        action.verification_targets || source.verification_targets || source.task_verification_targets,
      ),
      ...actionRetryContext,
      tool,
    }];
  });
}

function runtimeToolRecoveryAutoStartContext(
  action: Record<string, unknown>,
  source: Record<string, unknown>,
): Pick<RuntimeToolRecoveryAction, 'auto_start_eligible' | 'auto_start_reason' | 'auto_start_blockers'> {
  const metadata = objectValue(action.metadata || source.metadata);
  const eligible = optionalBoolean(
    action.auto_start_eligible
    ?? action.runtime_replan_auto_start_eligible
    ?? action.replan_auto_start_eligible
    ?? metadata.auto_start_eligible
    ?? metadata.runtime_replan_auto_start_eligible
    ?? metadata.replan_auto_start_eligible,
  );
  const reason = String(
    action.auto_start_reason
    || action.runtime_replan_auto_start_reason
    || action.replan_auto_start_reason
    || metadata.auto_start_reason
    || metadata.runtime_replan_auto_start_reason
    || metadata.replan_auto_start_reason
    || '',
  ).trim();
  const blockers = recoveryStringList(
    action.auto_start_blockers
    || action.runtime_replan_auto_start_blockers
    || action.replan_auto_start_blockers
    || metadata.auto_start_blockers
    || metadata.runtime_replan_auto_start_blockers
    || metadata.replan_auto_start_blockers,
  ) || [];
  return {
    ...(eligible !== null ? { auto_start_eligible: eligible } : {}),
    ...(reason ? { auto_start_reason: reason } : {}),
    ...(blockers.length ? { auto_start_blockers: blockers } : {}),
  };
}

function optionalBoolean(value: unknown): boolean | null {
  if (value === true || value === false) return value;
  const text = String(value ?? '').trim().toLowerCase();
  if (text === 'true') return true;
  if (text === 'false') return false;
  return null;
}

function recoveryRecordList(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => (
    Boolean(item) && typeof item === 'object' && !Array.isArray(item)
  ));
}

function runtimeToolRecoveryExecutableLabel(tool: string, input: Record<string, unknown>): string {
  const appName = String(input.app_name || '').trim();
  const query = String(input.query || '').trim();
  const url = String(input.url || '').trim();
  const target = String(input.target || '').trim();
  const path = String(input.path || '').trim();
  if (tool === 'app.open' && appName) return `打开${appName}`;
  if (tool === 'app.focus' && appName) return `切到${appName}`;
  if (tool === 'app.focus_window' && appName) return appWindowFocusPrompt(input);
  if (tool === 'app.show' && appName) return `显示${appName}`;
  if (tool === 'app.status' && appName) return `检查${appName}是否打开`;
  const foregroundPrompt = appForegroundActionPrompt(tool, input);
  if (foregroundPrompt) return foregroundPrompt;
  if (tool === 'browser.open_url' && url) return `打开 ${url}`;
  if (tool === 'browser.open_url_and_extract_text' && url) return `打开并读取 ${url}`;
  if (tool === 'browser.open_url_and_screenshot' && url) return `打开 ${url} 并截图`;
  if (tool === 'browser.screenshot') return '截取当前网页';
  if (tool === 'system.settings_open' && target) return `打开${target}`;
  if (tool === 'desktop.open_path' && path) return `打开 ${path}`;
  if (tool === 'desktop.open_path_with_app' && appName && path) return `用${appName}打开 ${path}`;
  if (tool === 'media.apple_music_play' && query) return `在 Apple Music 中播放 ${query}`;
  if (tool === 'media.apple_music_status') return '查看Apple Music播放状态';
  if (tool === 'media.apple_music_open_and_play') return '打开Apple Music并播放';
  if (tool === 'media.apple_music_control') return appleMusicControlRetryPrompt(String(input.action || '').trim());
  if (tool === 'media.music_app_open_and_play' && appName) return musicAppOpenAndPlayPrompt(appName);
  if (tool === 'system.volume') return systemVolumeRetryPrompt(String(input.action || '').trim(), input);
  if (tool === 'system.brightness') return systemBrightnessRetryPrompt(String(input.action || '').trim());
  if (tool === 'clipboard.read') return '读取剪贴板';
  if (tool === 'clipboard.write' && typeof input.text === 'string') return `把 ${input.text} 复制到剪贴板`;
  if (tool === 'screen.capture') return '截图当前屏幕';
  if (tool === 'desktop.permissions') return '检查桌面权限';
  if (tool === 'desktop.inspect_app') return appName ? `检查${appName}界面` : '检查应用界面';
  if (tool === 'desktop.active_window') return '查看当前窗口';
  if (tool === 'desktop.list_apps') return '发现已安装应用';
  if (tool === 'desktop.running_apps') return '列出当前运行的应用';
  if (tool === 'desktop.windows') return appName ? `查看${appName}窗口` : '查看桌面窗口';
  if (tool === 'desktop.ui_elements') {
    const role = String(input.role_filter || '').trim().toLowerCase();
    const labels: Record<string, string> = {
      button: '按钮', text: '文本', menu: '菜单', checkbox: '复选框',
    };
    const label = labels[role];
    return `查看当前界面${label || '控件'}`;
  }
  if (tool === 'desktop.click_ui_element') return desktopUiClickPrompt(input);
  if (tool === 'desktop.type_into_ui_element') return desktopUiTypePrompt(input);
  if (tool === 'desktop.safe_shortcut') return desktopSafeShortcutPrompt(String(input.action || '').trim());
  if (tool === 'desktop.safe_key') return desktopSafeKeyPrompt(input);
  if (tool === 'desktop.safe_scroll') return desktopSafeScrollPrompt(input);
  if (tool === 'desktop.safe_click') return desktopSafeClickPrompt(input);
  if (tool === 'desktop.safe_type_text') return desktopSafeTypeTextPrompt(input);
  if (tool === 'browser.current_page') return '读取当前网页标题和地址';
  if (tool === 'browser.extract_text') return '读取当前网页正文';
  return '';
}

function isLowRiskExecutableRecoveryTool(tool: string): boolean {
  return tool === 'app.open'
    || tool === 'app.focus'
    || tool === 'app.focus_and_safe_click'
    || tool === 'app.focus_and_safe_key'
    || tool === 'app.focus_and_safe_scroll'
    || tool === 'app.focus_and_safe_shortcut'
    || tool === 'app.focus_and_safe_type_text'
    || tool === 'app.focus_window'
    || tool === 'app.open_and_safe_click'
    || tool === 'app.open_and_safe_key'
    || tool === 'app.open_and_safe_scroll'
    || tool === 'app.open_and_safe_shortcut'
    || tool === 'app.open_and_safe_type_text'
    || tool === 'app.show'
    || tool === 'app.status'
    || tool === 'browser.current_page'
    || tool === 'browser.extract_text'
    || tool === 'browser.open_url'
    || tool === 'browser.open_url_and_extract_text'
    || tool === 'browser.open_url_and_screenshot'
    || tool === 'browser.screenshot'
    || tool === 'clipboard.read'
    || tool === 'clipboard.write'
    || tool === 'desktop.active_window'
    || tool === 'desktop.open_path'
    || tool === 'desktop.open_path_with_app'
    || tool === 'desktop.permissions'
    || tool === 'desktop.inspect_app'
    || tool === 'desktop.list_apps'
    || tool === 'desktop.running_apps'
    || tool === 'desktop.safe_click'
    || tool === 'desktop.safe_key'
    || tool === 'desktop.safe_scroll'
    || tool === 'desktop.safe_shortcut'
    || tool === 'desktop.safe_type_text'
    || tool === 'desktop.ui_elements'
    || tool === 'desktop.windows'
    || tool === 'media.apple_music_control'
    || tool === 'media.apple_music_open_and_play'
    || tool === 'media.apple_music_play'
    || tool === 'media.music_app_open_and_play'
    || tool === 'screen.capture'
    || tool === 'system.brightness'
    || tool === 'system.settings_open'
    || tool === 'system.volume';
}

function runtimeToolRecoveryRetryPrompt(tool: string, input: Record<string, unknown>): string {
  const appName = String(input.app_name || '').trim();
  const url = String(input.url || '').trim();
  const query = String(input.query || '').trim();
  const path = String(input.path || '').trim();
  const action = String(input.action || '').trim();
  const target = String(input.target || '').trim();
  const title = String(input.title_contains || input.window_title || '').trim();
  if (tool === 'app.open' && appName) return `打开${appName}`;
  if (tool === 'app.focus' && appName) return `切到${appName}`;
  if (tool === 'app.open_and_safe_type_text' && appName) return `打开${appName}并输入文字`;
  if (tool === 'app.focus_and_safe_type_text' && appName) return `切到${appName}并输入文字`;
  if (tool === 'app.open_and_safe_shortcut' && appName) return `打开${appName}并执行快捷动作`;
  if (tool === 'app.focus_and_safe_shortcut' && appName) return `切到${appName}并执行快捷动作`;
  const foregroundPrompt = appForegroundActionPrompt(tool, input);
  if (foregroundPrompt) return foregroundPrompt;
  if (tool === 'app.focus_window' && appName && title) return `切到${appName} ${title}窗口`;
  if (tool === 'app.show' && appName) return `显示${appName}`;
  if (tool === 'app.hide' && appName) return `隐藏${appName}`;
  if (tool === 'app.minimize' && appName) return `最小化${appName}`;
  if (tool === 'app.quit' && appName) return `退出${appName}`;
  if (tool === 'desktop.quit_app') return '退出当前应用';
  if (tool === 'app.status' && appName) return `检查${appName}是否打开`;
  if (tool === 'browser.open_url' && url) return `打开 ${url}`;
  if (tool === 'browser.open_url_and_extract_text' && url) return `打开并读取 ${url}`;
  if (tool === 'browser.open_url_and_screenshot' && url) return `打开 ${url} 并截图`;
  if (tool === 'browser.current_page') return '查看当前网页';
  if (tool === 'browser.extract_text') return '读取当前网页正文';
  if (tool === 'browser.screenshot') return '截取当前网页';
  if (tool === 'screen.capture') return '截图当前屏幕';
  if (tool === 'desktop.active_window') return '查看当前窗口';
  if (tool === 'desktop.list_apps') return '发现已安装应用';
  if (tool === 'desktop.running_apps') return '查看正在运行的应用';
  if (tool === 'desktop.windows') return appName ? `查看${appName}窗口` : '查看桌面窗口';
  if (tool === 'desktop.permissions') return '检查桌面权限';
  if (tool === 'desktop.inspect_app') return appName ? `检查${appName}界面` : '检查应用界面';
  if (tool === 'desktop.open_path' && path) return `打开 ${path}`;
  if (tool === 'desktop.open_path_with_app' && appName && path) return `用${appName}打开 ${path}`;
  if (tool === 'desktop.safe_shortcut') return desktopSafeShortcutPrompt(action);
  if (tool === 'desktop.safe_key') return desktopSafeKeyPrompt(input);
  if (tool === 'desktop.safe_scroll') return desktopSafeScrollPrompt(input);
  if (tool === 'desktop.safe_click') return desktopSafeClickPrompt(input);
  if (tool === 'desktop.safe_type_text') return desktopSafeTypeTextPrompt(input);
  if (tool === 'desktop.click_ui_element') return desktopUiClickPrompt(input);
  if (tool === 'desktop.type_into_ui_element') return desktopUiTypePrompt(input);
  if (tool === 'desktop.reveal_path' && path) return `在 Finder 中显示 ${path}`;
  if (tool === 'media.apple_music_play' && query) return `播放${query}`;
  if (tool === 'media.apple_music_open_and_play') return '打开Apple Music并播放';
  if (tool === 'media.apple_music_control') return appleMusicControlRetryPrompt(action);
  if (tool === 'media.music_app_open_and_play' && appName) return musicAppOpenAndPlayPrompt(appName);
  if (tool === 'system.settings_open') return target ? `打开${target}` : '打开系统设置';
  if (tool === 'system.volume') return systemVolumeRetryPrompt(action, input);
  if (tool === 'system.brightness') return systemBrightnessRetryPrompt(action);
  if (tool === 'clipboard.write' && typeof input.text === 'string') return `复制${input.text}到剪贴板`;
  return '';
}

function musicAppOpenAndPlayPrompt(appName: string): string {
  const displayName = appName.toLowerCase() === 'music' ? 'Apple Music' : appName;
  return `打开${displayName}并播放`;
}

function appleMusicControlRetryPrompt(action: string): string {
  if (action === 'play') return 'Apple Music 继续播放';
  if (action === 'pause') return '暂停 Apple Music';
  if (action === 'next') return 'Apple Music 下一首';
  if (action === 'previous') return 'Apple Music 上一首';
  if (action === 'toggle') return 'Apple Music 播放暂停';
  return '';
}

function systemVolumeRetryPrompt(action: string, input: Record<string, unknown>): string {
  if (action === 'status') return '当前音量是多少';
  if (action === 'mute') return '静音';
  if (action === 'unmute') return '取消静音';
  if (action === 'up') return '调大音量';
  if (action === 'down') return '调小音量';
  const level = typeof input.level === 'number' ? input.level : Number(input.level);
  if (action === 'set' && Number.isFinite(level)) return `把音量调到 ${level}%`;
  return '';
}

function systemBrightnessRetryPrompt(action: string): string {
  if (action === 'up') return '屏幕亮一点';
  if (action === 'down') return '屏幕暗一点';
  return '';
}

function appForegroundActionPrompt(tool: string, input: Record<string, unknown>): string {
  const appName = String(input.app_name || '').trim();
  if (!appName) return '';
  const isOpen = tool.startsWith('app.open_and_');
  const isFocus = tool.startsWith('app.focus_and_');
  if (!isOpen && !isFocus) return '';
  const prefix = isOpen ? `打开${appName}` : `切到${appName}`;
  let detail = '';
  if (tool.endsWith('safe_type_text')) detail = desktopSafeTypeTextPrompt(input) || '输入文字';
  if (tool.endsWith('safe_shortcut')) detail = desktopSafeShortcutPrompt(String(input.action || '').trim());
  if (tool.endsWith('safe_key')) detail = desktopSafeKeyPrompt(input);
  if (tool.endsWith('safe_scroll')) detail = desktopSafeScrollPrompt(input);
  if (tool.endsWith('safe_click')) detail = desktopSafeClickPrompt(input);
  if (tool.endsWith('click_ui_element')) detail = desktopUiClickPrompt(input);
  if (tool.endsWith('type_into_ui_element')) detail = desktopUiTypePrompt(input);
  return detail ? `${prefix}并${detail}` : '';
}

function appWindowFocusPrompt(input: Record<string, unknown>): string {
  const appName = String(input.app_name || '').trim();
  const title = String(input.window_title || input.title_contains || '').trim();
  if (appName && title) return `切到${appName} ${title}窗口`;
  return appName ? `切到${appName}窗口` : '';
}

function desktopSafeShortcutPrompt(action: string): string {
  if (action === 'copy') return '复制选中内容';
  if (action === 'paste') return '粘贴';
  if (action === 'select_all') return '全选';
  if (action === 'undo') return '撤销';
  if (action === 'redo') return '重做';
  if (action === 'find') return '打开查找';
  if (action === 'new_tab') return '新建标签页';
  if (action === 'close_tab') return '关闭标签页';
  if (action === 'next_tab') return '切到下一个标签页';
  if (action === 'previous_tab') return '切到上一个标签页';
  if (action === 'new_window') return '新建窗口';
  if (action === 'new_document') return '新建文档';
  if (action === 'new_note') return '新建笔记';
  if (action === 'new_reminder') return '新建提醒事项';
  if (action === 'new_event') return '新建日程';
  if (action === 'refresh') return '刷新';
  if (action === 'browser_back') return '返回上一页';
  if (action === 'browser_forward') return '前进一页';
  if (action === 'reopen_closed_tab') return '重新打开关闭的标签页';
  return '';
}

function desktopSafeKeyPrompt(input: Record<string, unknown>): string {
  const action = String(input.action || '').trim();
  const label = desktopSafeKeyLabel(action);
  if (!label) return '';
  const repeatCount = Number(input.repeat_count || 1);
  return Number.isFinite(repeatCount) && repeatCount > 1
    ? `按${label}${repeatCount}次`
    : `按${label}`;
}

function desktopSafeKeyLabel(action: string): string {
  if (action === 'escape') return 'Escape';
  if (action === 'tab') return 'Tab';
  if (action === 'shift_tab') return 'Shift+Tab';
  if (action === 'arrow_up') return '上箭头';
  if (action === 'arrow_down') return '下箭头';
  if (action === 'arrow_left') return '左箭头';
  if (action === 'arrow_right') return '右箭头';
  if (action === 'home') return 'Home';
  if (action === 'end') return 'End';
  if (action === 'page_up') return 'Page Up';
  if (action === 'page_down') return 'Page Down';
  return '';
}

function desktopSafeScrollPrompt(input: Record<string, unknown>): string {
  const direction = String(input.direction || '').trim();
  const label = direction === 'down' ? '向下' : direction === 'up' ? '向上' : '';
  if (!label) return '';
  const pages = Number(input.pages || 1);
  return Number.isFinite(pages) && pages > 1 ? `${label}滚动${pages}页` : `${label}滚动`;
}

function desktopSafeClickPrompt(input: Record<string, unknown>): string {
  const x = input.x;
  const y = input.y;
  if (x === undefined || x === null || y === undefined || y === null) return '';
  return `点击 ${x}, ${y}`;
}

function desktopSafeTypeTextPrompt(input: Record<string, unknown>): string {
  const text = typeof input.text === 'string' ? input.text.trim() : '';
  return text ? `输入${text}` : '';
}

function desktopUiClickPrompt(input: Record<string, unknown>): string {
  const target = String(input.target || '').trim();
  return target ? `点击前台控件${target}` : '';
}

function desktopUiTypePrompt(input: Record<string, unknown>): string {
  const target = String(input.target || '').trim();
  const text = typeof input.text === 'string' ? input.text : '';
  if (target && text) return `在前台控件${JSON.stringify(target)}输入${JSON.stringify(text)}`;
  return target ? `在前台控件${target}输入文字` : '';
}

function isExecutableRecoveryPrompt(value: string): boolean {
  return /^(?:打开|启动|前往|进入|显示|切到|切换到)\s*/.test(value.trim());
}

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function optionalObjectValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined;
}

function recoveryStringList(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const items = value.map((item) => String(item || '').trim()).filter(Boolean);
  return items.length ? items : undefined;
}

function optionalText(value: unknown): string | undefined {
  const text = String(value || '').trim();
  return text || undefined;
}

function runtimeToolRecoveryRetryContext(
  action: Record<string, unknown>,
  fallback: RuntimeToolRecoveryRetryContext,
): RuntimeToolRecoveryRetryContext {
  const retryTool = optionalText(action.retry_tool)
    || optionalText(action.recovery_retry_tool)
    || optionalText(fallback.retry_tool);
  const retryInput = optionalObjectValue(action.retry_input)
    || optionalObjectValue(action.recovery_retry_input)
    || fallback.retry_input;
  const retryInputSchema = optionalObjectValue(action.retry_input_schema)
    || optionalObjectValue(action.recovery_retry_input_schema)
    || fallback.retry_input_schema;
  const retryInputSource = optionalText(action.retry_input_source)
    || optionalText(action.recovery_retry_input_source)
    || fallback.retry_input_source;
  const retryArtifactTool = optionalText(action.retry_artifact_tool)
    || optionalText(action.recovery_retry_artifact_tool)
    || fallback.retry_artifact_tool;
  const retryArtifactKind = optionalText(action.retry_artifact_kind)
    || optionalText(action.recovery_retry_artifact_kind)
    || fallback.retry_artifact_kind;
  const followupTool = optionalText(action.followup_tool)
    || optionalText(action.recovery_followup_tool)
    || fallback.followup_tool;
  const followupInput = optionalObjectValue(action.followup_input)
    || optionalObjectValue(action.recovery_followup_input)
    || fallback.followup_input;
  const requiredRetryFields = recoveryStringList(action.required_retry_fields)
    || fallback.required_retry_fields;
  const recommendedTools = recoveryStringList(action.recommended_tools)
    || fallback.recommended_tools;
  const retryPrompt = optionalText(action.retry_prompt)
    || optionalText(action.recovery_retry_prompt)
    || optionalText(fallback.retry_prompt);
  const retrySourceEventType = optionalText(action.retry_source_event_type)
    || optionalText(action.recovery_retry_source_event_type)
    || optionalText(fallback.retry_source_event_type);
  const retrySourceToolCallId = optionalText(action.retry_source_tool_call_id)
    || optionalText(action.recovery_retry_source_tool_call_id)
    || optionalText(fallback.retry_source_tool_call_id);

  return {
    ...(recommendedTools?.length ? { recommended_tools: recommendedTools } : {}),
    ...(requiredRetryFields?.length ? { required_retry_fields: requiredRetryFields } : {}),
    ...(retryTool ? { retry_tool: retryTool } : {}),
    ...(retryTool || retryInput ? { retry_input: retryInput || {} } : {}),
    ...(retryInputSchema ? { retry_input_schema: retryInputSchema } : {}),
    ...(retryInputSource ? { retry_input_source: retryInputSource } : {}),
    ...(retryArtifactTool ? { retry_artifact_tool: retryArtifactTool } : {}),
    ...(retryArtifactKind ? { retry_artifact_kind: retryArtifactKind } : {}),
    ...(followupTool ? { followup_tool: followupTool } : {}),
    ...(followupInput ? { followup_input: followupInput } : {}),
    ...(retryPrompt ? { retry_prompt: retryPrompt } : {}),
    ...(retrySourceEventType ? { retry_source_event_type: retrySourceEventType } : {}),
    ...(retrySourceToolCallId ? { retry_source_tool_call_id: retrySourceToolCallId } : {}),
  };
}
