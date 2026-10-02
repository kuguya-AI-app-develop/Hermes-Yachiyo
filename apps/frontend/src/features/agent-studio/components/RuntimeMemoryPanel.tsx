import type { FutureTaskSpec, MemorySpec } from '../types';

type RuntimeMemoryPanelProps = {
  busy: boolean;
  formatRunDate: (value?: string) => string;
  futureTasks: FutureTaskSpec[];
  memories: MemorySpec[];
  onCancelFutureTask: (futureTask: FutureTaskSpec) => void;
  onDeleteMemory: (memory: MemorySpec) => void;
  onOpenRunDetail: (runId: string) => void;
  onTriggerDueFutureTasks: () => void;
};

export function RuntimeMemoryPanel({
  busy,
  formatRunDate,
  futureTasks,
  memories,
  onCancelFutureTask,
  onDeleteMemory,
  onOpenRunDetail,
  onTriggerDueFutureTasks,
}: RuntimeMemoryPanelProps) {
  return (
    <section className="agent-studio-grid agent-runtime-grid" data-testid="agent-runtime-memory">
      <aside className="agent-studio-panel agent-runtime-panel">
        <div className="section-heading-row">
          <h2>Long-term Memory</h2>
          <span className="agent-section-count">{memories.filter((memory) => !memory.deleted_at).length} active</span>
        </div>
        <div className="runtime-management-list" data-testid="agent-memory-list">
          {memories.map((memory) => {
            const sourceRunId = memory.source_run_id || '';
            return (
              <article
                className={memory.deleted_at ? 'runtime-management-row muted' : 'runtime-management-row'}
                data-memory-id={memory.memory_id}
                data-memory-kind={memory.kind}
                data-memory-scope={memory.scope}
                data-testid="agent-memory-item"
                key={memory.memory_id}
              >
                <div className="runtime-management-main">
                  <div className="runtime-management-title">
                    <strong>{memory.kind || 'memory'}</strong>
                    <span>{memory.scope || 'local'}</span>
                    {memory.pinned ? <em>pinned</em> : null}
                    {memory.user_confirmed ? <em>confirmed</em> : null}
                    {memory.deleted_at ? <em>deleted</em> : null}
                  </div>
                  <p>{memory.content || 'Empty memory'}</p>
                  <div className="runtime-management-meta">
                    <code>{memory.memory_id}</code>
                    <span>{formatRunDate(memory.updated_at || memory.created_at)}</span>
                    {typeof memory.confidence === 'number' ? <span>{Math.round(memory.confidence * 100)}% confidence</span> : null}
                    {sourceRunId ? (
                      <button type="button" disabled={busy} onClick={() => onOpenRunDetail(sourceRunId)}>
                        Source Run
                      </button>
                    ) : null}
                  </div>
                </div>
                {!memory.deleted_at ? (
                  <div className="runtime-management-actions">
                    <button
                      type="button"
                      className="danger-action"
                      data-testid="agent-memory-delete"
                      disabled={busy}
                      onClick={() => onDeleteMemory(memory)}
                    >
                      删除
                    </button>
                  </div>
                ) : null}
              </article>
            );
          })}
          {!memories.length ? <div className="empty-state inline-empty">暂无长期记忆。</div> : null}
        </div>
      </aside>
      <section className="agent-studio-panel agent-runtime-panel">
        <div className="section-heading-row">
          <h2>Future Tasks</h2>
          <div className="studio-heading-actions">
            <span className="agent-section-count">{futureTasks.filter((task) => task.status === 'scheduled').length} scheduled</span>
            <button
              type="button"
              data-testid="agent-future-task-trigger-due"
              disabled={busy}
              onClick={onTriggerDueFutureTasks}
            >
              触发到期
            </button>
          </div>
        </div>
        <div className="runtime-management-list" data-testid="agent-future-task-list">
          {futureTasks.map((futureTask) => {
            const lastRunId = futureTask.last_run_id || '';
            return (
              <article
                className={`runtime-management-row ${futureTask.status === 'scheduled' ? 'scheduled' : ''}`}
                data-future-task-id={futureTask.future_task_id}
                data-future-task-status={futureTask.status}
                data-testid="agent-future-task-item"
                key={futureTask.future_task_id}
              >
                <div className="runtime-management-main">
                  <div className="runtime-management-title">
                    <strong>{futureTask.title || 'FutureTask'}</strong>
                    <em className={`run-status-pill ${futureTaskStatusTone(futureTask.status)}`}>{futureTaskStatusLabel(futureTask.status)}</em>
                  </div>
                  <p>{futureTask.prompt || '暂无 Prompt'}</p>
                  <div className="runtime-management-meta">
                    <span>到期 {formatEpochDate(futureTask.scheduled_at_epoch)}</span>
                    {futureTask.cron ? <span>{futureTask.cron}</span> : null}
                    {futureTask.runnable_name || futureTask.runnable_id ? <span>{futureTask.runnable_name || futureTask.runnable_id}</span> : null}
                    {typeof futureTask.run_count === 'number' ? <span>{futureTask.run_count} 次运行</span> : null}
                    <code>{futureTask.future_task_id}</code>
                  </div>
                  {futureTask.status === 'cancelled' && futureTask.trigger_in_flight ? (
                    <div className="agent-inline-note warn">后续触发已取消，当前 Run 继续执行。</div>
                  ) : null}
                  {futureTask.error ? <div className="agent-inline-note warn">{futureTask.error}</div> : null}
                </div>
                <div className="runtime-management-actions">
                  {lastRunId ? (
                    <button type="button" data-testid="agent-future-task-open-run" disabled={busy} onClick={() => onOpenRunDetail(lastRunId)}>
                      打开 Run
                    </button>
                  ) : null}
                  {['scheduled', 'claimed', 'executing'].includes(futureTask.status) ? (
                    <button
                      type="button"
                      className="danger-action"
                      data-testid="agent-future-task-cancel"
                      disabled={busy}
                      onClick={() => onCancelFutureTask(futureTask)}
                    >
                      取消
                    </button>
                  ) : null}
                </div>
              </article>
            );
          })}
          {!futureTasks.length ? <div className="empty-state inline-empty">暂无 FutureTask。</div> : null}
        </div>
      </section>
    </section>
  );
}

function formatEpochDate(value?: number): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '未知时间';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(Number(value) * 1000);
}

function futureTaskStatusLabel(status?: string): string {
  if (status === 'scheduled') return '已排程';
  if (status === 'claimed') return '准备触发';
  if (status === 'executing') return '正在触发';
  if (status === 'triggered') return '已触发';
  if (status === 'cancelled') return '已取消';
  if (status === 'failed') return '失败';
  return status || '未知';
}

function futureTaskStatusTone(status?: string): string {
  if (['scheduled', 'claimed', 'executing'].includes(status || '')) return 'running';
  if (status === 'triggered') return 'ready';
  if (status === 'failed' || status === 'cancelled') return 'danger';
  return '';
}
