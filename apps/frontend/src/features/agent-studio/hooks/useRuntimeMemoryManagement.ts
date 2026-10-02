import type { FutureTaskSpec, MemorySpec } from '../types';
import {
  cancelYachiyoFutureTask,
  deleteYachiyoMemory,
  triggerDueYachiyoFutureTasks,
} from '../../yachiyo-studio/api';

type RuntimeMemoryRefreshOptions = {
  selectedRunId?: string;
  statusMessage?: string;
};

type ConfirmDialogRequest = {
  title: string;
  description: string;
  confirmLabel: string;
  variant?: 'default' | 'danger';
  onConfirm: () => void;
};

type UseRuntimeMemoryManagementOptions = {
  openRunDetail: (runId: string, options?: { revealInHistory?: boolean }) => void;
  runAction: (action: () => Promise<RuntimeMemoryRefreshOptions | void>, label: string) => void;
  showConfirmDialog: (dialog: ConfirmDialogRequest) => void;
};

export function useRuntimeMemoryManagement({
  openRunDetail,
  runAction,
  showConfirmDialog,
}: UseRuntimeMemoryManagementOptions) {
  function requestDeleteMemory(memory: MemorySpec) {
    const memoryLabel = memory.content.trim() || memory.memory_id;
    showConfirmDialog({
      title: `删除 Memory「${memoryLabel.slice(0, 32)}」？`,
      description: '这条长期记忆会从 Agent Runtime 的主动回忆范围中移除；历史 Run 不会被删除。',
      confirmLabel: '删除 Memory',
      variant: 'danger',
      onConfirm: () => void runAction(async () => {
        await deleteYachiyoMemory(memory.memory_id, 'studio_user_delete');
        return { statusMessage: 'Memory 已删除。' };
      }, '删除 Memory'),
    });
  }

  function requestCancelFutureTask(futureTask: FutureTaskSpec) {
    const taskLabel = futureTask.title.trim() || futureTask.future_task_id;
    showConfirmDialog({
      title: `取消 FutureTask「${taskLabel.slice(0, 40)}」？`,
      description: '这个 FutureTask 不会再自动触发；已经开始的 Run 会继续执行。',
      confirmLabel: '取消 FutureTask',
      variant: 'danger',
      onConfirm: () => void runAction(async () => {
        await cancelYachiyoFutureTask(futureTask.future_task_id, 'studio_user_cancel');
        return { statusMessage: 'FutureTask 已取消。' };
      }, '取消 FutureTask'),
    });
  }

  async function triggerDueFutureTaskRuns(): Promise<RuntimeMemoryRefreshOptions> {
    const result = await triggerDueYachiyoFutureTasks();
    const triggered = result.triggered || [];
    const firstRunId = triggered.map((item) => item.run?.run_id || '').find(Boolean) || '';
    const failedCount = triggered.filter((item) => item.error || item.ok === false).length;
    const statusMessage = triggered.length
      ? `已触发 ${triggered.length} 个 FutureTask${failedCount ? `，${failedCount} 个失败` : ''}。`
      : '没有到期 FutureTask。';
    if (firstRunId) {
      openRunDetail(firstRunId, { revealInHistory: true });
      return { selectedRunId: firstRunId, statusMessage };
    }
    return { statusMessage };
  }

  return {
    requestCancelFutureTask,
    requestDeleteMemory,
    triggerDueFutureTaskRuns,
  };
}
