"""Run control compatibility facade methods for NativeRunEngine."""

from __future__ import annotations

from typing import Any

from packages.security import redact_api_error_text

from apps.shell.agent.runtime.tool_approvals import ToolApprovalResumeContext


class RuntimeRunControlFacadeMixin:
    """Keeps legacy cancellation and approval methods while delegating to services."""

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        run = self.get_run(run_id) if str(run_id).startswith("main_chat_run_") else {}
        if run.get("kind") == "main_chat_run":
            from .main_chat_delegation import MainChatDelegationCoordinator

            MainChatDelegationCoordinator(self).cancel_children(run_id)
        return self.run_cancellation_coordinator.cancel(run_id)

    def _cancel_workflow_run_projection(
        self,
        run_id: str,
        run: dict[str, Any],
        timeline: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
        return self.workflow_cancellation.project_cancelled_workflow_run(run_id, run, timeline)

    def _cancel_run_once(self, run_id: str) -> dict[str, Any]:
        return self.run_cancellation.cancel_once(run_id)

    def _tool_approval_resume_context(
        self,
        run: dict[str, Any],
        pending: dict[str, Any],
        *,
        runtime: dict[str, Any],
        skills: list[dict[str, Any]] | None = None,
    ) -> ToolApprovalResumeContext:
        return self.tool_approval_resume.context(
            run,
            pending,
            runtime=runtime,
            skills=skills,
        )

    def approve_run_approval(
        self,
        run_id: str,
        expected_approval_id: str = "",
    ) -> dict[str, Any]:
        expected_id = str(expected_approval_id or "").strip()
        if expected_id:
            return self.approval_execution.approve_run_approval(
                run_id,
                expected_approval_id=expected_id,
            )
        return self.approval_execution.approve_run_approval(run_id)

    def _approve_run_approval_once(self, run: dict[str, Any]) -> dict[str, Any]:
        expected_id = _run_pending_approval_id(run)
        if expected_id:
            return self.approval_resume_dispatcher.approve_once(
                run,
                expected_approval_id=expected_id,
            )
        return self.approval_resume_dispatcher.approve_once(run)

    def _resume_approved_tool_run(
        self,
        *,
        run_id: str,
        pending: dict[str, Any],
        resume_context: ToolApprovalResumeContext,
        agent: dict[str, Any],
        resumed_detail: str,
        running_result: str,
        project_completed: Any,
        expected_approval_id: str = "",
        project_running: Any | None = None,
        project_required: Any | None = None,
        project_result: Any | None = None,
        project_failed: Any | None = None,
        redact_error: Any = redact_api_error_text,
    ) -> dict[str, Any]:
        resume_kwargs = {
            "run_id": run_id,
            "pending": pending,
            "context": resume_context,
            "agent": agent,
            "resumed_detail": resumed_detail,
            "running_result": running_result,
            "project_completed": project_completed,
            "project_required": self._project_approval_resume_required,
            "project_failed": project_failed or self._project_approval_resume_failed,
            "get_current_run": self.get_run,
            "project_running": project_running,
            "prepare_required": project_required,
            "project_result": project_result,
            "redact_error": redact_error,
        }
        expected_id = str(expected_approval_id or "").strip()
        if expected_id:
            resume_kwargs["expected_approval_id"] = expected_id
        return self.approval_resume.resume_approved_tool_run(
            **resume_kwargs,
        )

    def _project_agent_approval_resume_running(self, running: dict[str, Any]) -> dict[str, Any]:
        return self.approval_resume_projection.project_agent_running(running)

    def _project_agent_approval_resume_completed(
        self,
        context: ToolApprovalResumeContext,
        result_text: str,
    ) -> dict[str, Any]:
        return self.approval_resume_projection.project_agent_completed(context, result_text)

    def _project_main_chat_approval_resume_completed(
        self,
        context: ToolApprovalResumeContext,
        result_text: str,
    ) -> dict[str, Any]:
        return self.approval_resume_projection.project_main_chat_completed(context, result_text)

    def _project_approval_resume_required(
        self,
        context: ToolApprovalResumeContext,
        pending_approval: dict[str, Any],
    ) -> dict[str, Any]:
        return self.approval_resume_projection.project_required(context, pending_approval)

    def _project_approval_resume_failed(
        self,
        context: ToolApprovalResumeContext,
        safe_error: str,
    ) -> dict[str, Any]:
        return self.approval_resume_projection.project_failed(context, safe_error)

    def _project_main_chat_approval_resume_failed(
        self,
        context: ToolApprovalResumeContext,
        safe_error: str,
    ) -> dict[str, Any]:
        return self.approval_resume_projection.project_main_chat_failed(
            context,
            safe_error,
        )

    def _approve_main_chat_run_approval(
        self,
        run: dict[str, Any],
        *,
        expected_approval_id: str = "",
    ) -> dict[str, Any]:
        expected_id = (
            str(expected_approval_id or "").strip()
            or _run_pending_approval_id(run)
        )
        if expected_id:
            return self.tool_approval_resume.approve_main_chat_run(
                run,
                expected_approval_id=expected_id,
            )
        return self.tool_approval_resume.approve_main_chat_run(run)

    def _approve_workflow_run_approval(
        self,
        run: dict[str, Any],
        *,
        expected_approval_id: str = "",
    ) -> dict[str, Any]:
        expected_id = (
            str(expected_approval_id or "").strip()
            or _run_pending_approval_id(run)
        )
        if expected_id:
            return self.workflow_approval_execution.approve_workflow_run(
                run,
                expected_approval_id=expected_id,
            )
        return self.workflow_approval_execution.approve_workflow_run(run)

    def _project_cancelled_workflow_group_if_root(
        self,
        run: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return self.run_transition_projection.project_cancelled_workflow_group_if_root(run, result)

    def _project_child_run_transition(self, result: dict[str, Any]) -> dict[str, Any]:
        return self.run_transition_projection.project_child_run_transition(result)

    def _project_agent_run_group_if_root(self, result: dict[str, Any]) -> dict[str, Any]:
        return self.run_transition_projection.project_agent_run_group_if_root(result)

    def reject_run_approval(
        self,
        run_id: str,
        reason: str = "",
        expected_approval_id: str = "",
    ) -> dict[str, Any]:
        expected_id = str(expected_approval_id or "").strip()
        if expected_id:
            return self.approval_transitions.reject(
                run_id,
                reason,
                expected_approval_id=expected_id,
            )
        return self.approval_transitions.reject(run_id, reason)

    def timeout_run_approval(
        self,
        run_id: str,
        reason: str = "approval_wait_timeout",
        expected_approval_id: str = "",
    ) -> dict[str, Any]:
        expected_id = str(expected_approval_id or "").strip()
        if expected_id:
            return self.approval_transitions.timeout(
                run_id,
                reason,
                expected_approval_id=expected_id,
            )
        return self.approval_transitions.timeout(run_id, reason)

    def _update_agent_run_group_if_root(self, run: dict[str, Any]) -> None:
        self.agent_run_group_projection.update_if_root(run)


def _run_pending_approval_id(run: dict[str, Any]) -> str:
    pending = run.get("pending_approval")
    if not isinstance(pending, dict):
        return ""
    return str(pending.get("approval_id") or "").strip()
