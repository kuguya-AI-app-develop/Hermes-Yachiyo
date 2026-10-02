"""Approved-tool resume orchestration."""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections.abc import Mapping
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import replace
from typing import Any

from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.errors import (
    AgentApprovalRequired,
    AgentDirectOutcomeUnverified,
    AgentRuntimeError,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_progress_base_event_type as _approval_resume_base_progress_event_type,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_progress_event_payload as _approval_resume_progress_event_payload,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_progress_event_type as _approval_resume_progress_event_type,
)
from apps.shell.agent.runtime.event_scopes import (
    runtime_replan_base_event_type as _approval_resume_replan_event_type,
)
from apps.shell.agent.runtime.outcome_evaluator import (
    evaluate_goal_contract_outcome,
    evaluate_main_chat_outcome,
)
from apps.shell.agent.runtime.outcome_loop import (
    OutcomeLoopCoordinator,
    OutcomeLoopDisposition,
)
from apps.shell.agent.runtime.recovery_lineage import (
    RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY,
    rehydrate_private_recovery_context,
)
from apps.shell.agent.runtime.tool_approvals import (
    ToolApprovalClaimProjection,
    ToolApprovalContinuationHandoff,
    ToolApprovalContinuationOutcome,
    ToolApprovalCustomApiContinuationRequest,
    ToolApprovalExecutionFailureProjection,
    ToolApprovalExecutionFollowup,
    ToolApprovalExecutionRequest,
    ToolApprovalResumeContext,
    approval_request_fingerprint,
)
from apps.shell.agent.runtime.tool_brokers import (
    close_owned_browser_target_best_effort,
)
from apps.shell.agent.runtime.tool_capabilities import capability_ids_for_tool
from apps.shell.agent.runtime.tool_execution import (
    RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY,
    RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_REQUEST_KEY,
    _RUNTIME_PRIVATE_EXACT_SUBMIT_RESULT_KEY,
    _RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY,
    _bind_exact_workspace_file_readback_verifier,
    _private_exact_submit_dispatch_receipt_for_verifier,
    _private_exact_submit_dispatch_receipt_from_result,
    _post_action_verification_predicate_kind,
    _post_action_verification_request,
    _remaining_requests_include_post_action_verification,
    append_replan_request_event_for_tool_result,
    rehydrate_private_prepared_submit_context,
)
from apps.shell.agent.runtime.tool_outcomes import from_tool_result
from apps.shell.yachiyo_agent.policy import READ_ONLY_OBSERVATION_TOOLS
from packages.security import redact_api_error_text, sanitize_sensitive_value

_NON_RESUMABLE_REMAINING_REQUEST_STATUSES = {
    "blocked",
    "cancelled",
    "canceled",
    "completed",
    "denied",
    "expired",
    "failed",
    "rejected",
    "recovered",
    "skipped",
}
_APPROVAL_RESUME_RESULT_IDENTITY_KEYS = (
    "approval_id",
    "approval_generation_id",
    "approval_claim_id",
    "approval_request_fingerprint",
    "run_id",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "step_id",
    "request_id",
    "tool_call_id",
    "tool",
    "provider_kind",
    "provider_id",
    "materialization_binding_id",
    "materialized_content_sha256",
)


class _ToolApprovalProjectionConflict(Exception):
    pass


class _ToolApprovalContinuationProjectionConflict(Exception):
    def __init__(self, current: Any) -> None:
        self.current = current
        super().__init__("approval continuation projection lost its Run CAS")


class ApprovalResumeCoordinator:
    """Executes the approved tool portion of a paused run resume."""

    def __init__(
        self,
        *,
        call_agent_tool: Any,
        fatal_tool_failure_detail: Any,
        append_tool_result_message: Any,
        run_tool_requests: Any,
        timeline_factory: Any,
        claim_pending_approval: Any | None = None,
        approve_tool_run: Any | None = None,
        continue_custom_api_agent: Any | None = None,
        append_run_event: Any | None = None,
        event_buffer_scope: Any | None = None,
        transaction_scope: Any | None = None,
        get_current_run: Any | None = None,
    ) -> None:
        self._call_agent_tool = call_agent_tool
        self._fatal_tool_failure_detail = fatal_tool_failure_detail
        self._append_tool_result_message = append_tool_result_message
        self._run_tool_requests = run_tool_requests
        self._timeline = timeline_factory
        self._claim_pending_approval = claim_pending_approval
        self._approve_tool_run = approve_tool_run
        self._continue_custom_api_agent = continue_custom_api_agent
        self._append_run_event = append_run_event
        self._event_buffer_scope = event_buffer_scope
        self._transaction_scope = transaction_scope
        self._get_current_run = get_current_run
        self._outcome_loop = OutcomeLoopCoordinator()

    def _append_resume_run_event(
        self,
        context: ToolApprovalResumeContext,
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        **event_fields: Any,
    ) -> Any:
        if self._append_run_event is None:
            return None
        pending_events = getattr(
            context,
            "_approval_resume_pending_events",
            None,
        )
        if isinstance(pending_events, list):
            pending_events.append(
                (
                    run_id,
                    event_type,
                    deepcopy(payload),
                    deepcopy(event_fields),
                )
            )
            return {"buffered": True}
        return self._append_run_event(
            run_id,
            event_type,
            payload,
            **event_fields,
        )

    def _flush_resume_run_events(
        self,
        context: ToolApprovalResumeContext,
        projected: Any,
    ) -> None:
        pending_events = getattr(
            context,
            "_approval_resume_pending_events",
            None,
        )
        if not isinstance(pending_events, list) or not pending_events:
            return
        if self._append_run_event is None:
            pending_events.clear()
            return
        status = (
            str(projected.get("status") or "").strip()
            if isinstance(projected, Mapping)
            else ""
        )
        updated_at = (
            str(projected.get("updated_at") or "").strip()
            if isinstance(projected, Mapping)
            else ""
        )
        fence: dict[str, str] = {}
        if status and updated_at:
            if supports_keyword(self._append_run_event, "expected_status"):
                fence["expected_status"] = status
            if supports_keyword(self._append_run_event, "expected_updated_at"):
                fence["expected_updated_at"] = updated_at
        for run_id, event_type, payload, event_fields in list(pending_events):
            appended = self._append_run_event(
                run_id,
                event_type,
                payload,
                **fence,
                **event_fields,
            )
            if fence and appended is None:
                raise AgentRuntimeError("run_event_fence_mismatch")
        pending_events.clear()

    @staticmethod
    def _flush_runtime_event_batch(batch: Any, projected: Any) -> None:
        if batch is None or not hasattr(batch, "flush"):
            return
        status = (
            str(projected.get("status") or "").strip()
            if isinstance(projected, Mapping)
            else ""
        )
        updated_at = (
            str(projected.get("updated_at") or "").strip()
            if isinstance(projected, Mapping)
            else ""
        )
        batch.flush(
            expected_status=status,
            expected_updated_at=updated_at,
        )

    def _flush_all_resume_events(
        self,
        context: ToolApprovalResumeContext,
        runtime_event_batch: Any,
        projected: Any,
    ) -> None:
        self._flush_runtime_event_batch(runtime_event_batch, projected)
        self._flush_resume_run_events(context, projected)

    def claim_and_project_approved_tool(
        self,
        run_id: str,
        pending: dict[str, Any],
        context: ToolApprovalResumeContext,
        *,
        resumed_detail: str,
        running_result: str,
        expected_approval_id: str = "",
    ) -> dict[str, Any] | None:
        if self._claim_pending_approval is None or self._approve_tool_run is None:
            raise AgentRuntimeError(
                "Approval resume coordinator is missing approval projection callbacks"
            )
        pending_id = str(pending.get("approval_id") or "").strip()
        expected_id = str(expected_approval_id or "").strip() or pending_id
        if not expected_id:
            raise AgentRuntimeError("approval_expected_id_required")
        if pending_id != expected_id:
            raise AgentRuntimeError("approval_generation_mismatch")
        claim_kwargs = (
            {"expected_approval_id": expected_id}
            if supports_keyword(
                self._claim_pending_approval,
                "expected_approval_id",
            )
            else {}
        )
        working_context = _approval_resume_working_context(context)
        scope = (
            self._transaction_scope()
            if self._transaction_scope is not None
            else nullcontext()
        )
        try:
            with scope:
                if not self._claim_pending_approval(run_id, pending, **claim_kwargs):
                    return None
                projection = ToolApprovalClaimProjection.from_context(
                    run_id,
                    working_context,
                    resumed_detail=resumed_detail,
                    running_result=running_result,
                )
                projected = projection.project(self._approve_tool_run)
                if projected is None:
                    current = (
                        self._get_current_run(run_id)
                        if self._get_current_run is not None
                        else None
                    )
                    if not isinstance(current, Mapping) or str(
                        current.get("status") or ""
                    ).strip().lower() not in {
                        "cancelled",
                        "canceled",
                        "completed",
                        "failed",
                    }:
                        raise _ToolApprovalProjectionConflict
            if projected is None:
                _reset_approval_resume_context(working_context, context)
                return None
            _commit_approval_resume_context(context, working_context)
            return projected
        except _ToolApprovalProjectionConflict:
            _reset_approval_resume_context(working_context, context)
            return None
        except BaseException:
            _reset_approval_resume_context(working_context, context)
            raise

    def execute_approved_tool(self, context: ToolApprovalResumeContext) -> None:
        _approval_resume_validate_materialization_binding(context)
        task_progress_start = len(context.timeline)
        request = ToolApprovalExecutionRequest.from_context(context)
        # A structurally valid canonical event is still unusable after its
        # exact approval claim loses authority. Validate the active generation
        # before any callback can observe the stored result.
        request._assert_active()
        replayed, tool_result = _approval_resume_existing_canonical_result(context)
        prepared_submit_context: dict[str, Any] = {}
        private_exact_submit_result: dict[str, Any] = {}
        private_recovery_context: dict[str, Any] = {}
        if not replayed:
            # Fingerprint + winning approval generation are the final CAS gate
            # before any process-private authority can be re-minted.
            private_recovery_context = _approval_resume_private_recovery_context(
                context.tool_request,
                context,
            )
            persisted_prepared_submit_receipt = context.tool_request.get(
                RUNTIME_PERSISTED_PREPARED_SUBMIT_RECEIPT_KEY
            )
            if persisted_prepared_submit_receipt is not None:
                # ``request._assert_active`` validates the exact persisted
                # fingerprint and the winning approval claim before any
                # process-private authority is minted.
                request._assert_active()

                def observe_private_target() -> Any:
                    from .prepared_submit_resume_observation import (
                        observe_actual_prepared_submit_target,
                    )

                    return observe_actual_prepared_submit_target(
                        context.broker,
                        request=context.tool_request,
                        run_id=context.run_id,
                        allowed_tools=context.allowed_tools,
                        budget=context.budget,
                        assert_active=request._assert_active,
                    )

                prepared_submit_context = (
                    rehydrate_private_prepared_submit_context(
                        context.tool_request,
                        context.timeline,
                        run_id=context.run_id,
                        goal_contract=context.goal_contract,
                        observe_private_target=observe_private_target,
                    )
                )
                if not prepared_submit_context:
                    raise AgentRuntimeError(
                        "approval_resume_prepared_submit_receipt_invalid"
                    )

            def call_approved_tool(
                tool_request: dict[str, Any],
                allowed_tools: list[str],
                broker: Any,
                timeline: list[dict[str, Any]],
                **kwargs: Any,
            ) -> Any:
                execution_request = dict(tool_request)
                if private_recovery_context:
                    execution_request[
                        RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY
                    ] = private_recovery_context
                if prepared_submit_context:
                    execution_request[
                        _RUNTIME_PRIVATE_PREPARED_SUBMIT_REQUEST_KEY
                    ] = prepared_submit_context
                raw_result = self._call_agent_tool(
                    execution_request,
                    allowed_tools,
                    broker,
                    timeline,
                    **kwargs,
                )
                if not isinstance(raw_result, Mapping):
                    return raw_result
                clean_result = dict(raw_result)
                private_result = clean_result.pop(
                    _RUNTIME_PRIVATE_EXACT_SUBMIT_RESULT_KEY,
                    None,
                )
                if private_result is not None:
                    if not (
                        prepared_submit_context
                        and isinstance(private_result, Mapping)
                    ):
                        raise AgentRuntimeError(
                            "approval_resume_exact_submit_result_invalid"
                        )
                    private_exact_submit_result.clear()
                    private_exact_submit_result.update(private_result)
                return clean_result

            tool_result = request.execute(
                call_approved_tool,
                record_executed_result=lambda result: (
                    self._record_durable_approved_tool_receipt(context, result)
                ),
            )
        fatal_failure = self._fatal_tool_failure_detail(
            context.tool_name,
            context.tool_request,
            tool_result,
        )
        if fatal_failure:
            failure = ToolApprovalExecutionFailureProjection.from_context(
                context,
                tool_result,
                fatal_failure,
            )
            context.timeline.append(failure.timeline_event(self._timeline))
            self._record_task_progress_after_resume(
                context,
                tool_timeline_start=task_progress_start,
            )
            self._record_replan_request_after_resume_failure(
                context,
                tool_result,
                tool_timeline_start=task_progress_start,
            )
            raise AgentRuntimeError(failure.detail)
        if not replayed:
            self._record_canonical_approved_tool_result(
                context,
                tool_result,
                tool_timeline_start=task_progress_start,
            )
        if _approval_resume_tool_result_blocks_plan_continuation(tool_result):
            context.remaining_requests = []
            self._record_replan_request_after_resume_result(
                context,
                tool_result,
                tool_timeline_start=task_progress_start,
            )
        else:
            context.remaining_requests = _approval_resume_remaining_requests_after_tool(
                context,
                tool_result,
            )
            _bind_exact_workspace_file_readback_verifier(
                context.tool_request,
                context.remaining_requests,
                run_id=context.run_id,
            )
            if prepared_submit_context:
                exact_submit_receipt = (
                    _private_exact_submit_dispatch_receipt_from_result(
                        context.tool_request,
                        tool_result,
                        context.remaining_requests,
                        context.timeline,
                        prepared_submit_context,
                        dict(
                            private_exact_submit_result.get("pre_revalidation")
                            or {}
                        ),
                        dict(
                            private_exact_submit_result.get("post_revalidation")
                            or {}
                        ),
                        tool_timeline_start=task_progress_start,
                        run_id=context.run_id,
                    )
                )
                context.remaining_requests = (
                    _approval_resume_attach_private_exact_submit_receipt(
                        context.remaining_requests,
                        exact_submit_receipt,
                        run_id=context.run_id,
                    )
                )
        followup = ToolApprovalExecutionFollowup.from_context(
            context,
            tool_result,
        )
        # The approved call may be the source of a dependent recovery
        # verifier. Re-check the claim after its side effect and mint only
        # short-lived execution copies; persisted continuation state remains
        # public and fingerprintable.
        request._assert_active()
        followup = replace(
            followup,
            remaining_requests=[
                _approval_resume_recovery_execution_request(item, context)
                for item in context.remaining_requests
            ],
        )
        try:
            followup.apply(
                self._append_tool_result_message,
                self._run_tool_requests,
            )
        finally:
            self._record_task_progress_after_resume(
                context,
                tool_timeline_start=task_progress_start,
            )

    def _record_canonical_approved_tool_result(
        self,
        context: ToolApprovalResumeContext,
        tool_result: Any,
        *,
        tool_timeline_start: int,
    ) -> None:
        identity = _approval_resume_result_identity(context)
        _approval_resume_validate_fresh_tool_result_events(
            context.timeline[tool_timeline_start:],
            identity,
            tool_result,
        )
        payload = {
            "approval_resume_result_canonical": True,
            "approved": True,
            "actor": "native_runtime",
            "execution_authority": "runtime_tool_executor",
            "execution_mode": "approved_result_canonical_projection",
            **identity,
            "input_preview": deepcopy(context.input_preview),
            "result": deepcopy(tool_result),
        }
        payload = {
            key: value
            for key, value in payload.items()
            if value not in (None, "", [], {}) or key in {"result", "input_preview"}
        }
        context.timeline.append(
            self._timeline(
                "agent.tool.call",
                str(identity.get("tool") or context.tool_name or "").strip(),
                visibility="internal",
                sensitivity="private",
                **payload,
            )
        )
        if self._append_run_event is None:
            return
        if not (
            supports_keyword(self._append_run_event, "visibility")
            and supports_keyword(self._append_run_event, "sensitivity")
        ):
            # A legacy three-argument callback would persist this private
            # result as a public event. The Run timeline is still projected
            # with the authoritative internal fact at continuation commit.
            return
        scope = {
            "visibility": "internal",
            "sensitivity": "private",
        }
        if supports_keyword(self._append_run_event, "actor"):
            scope["actor"] = "native_runtime"
        self._append_resume_run_event(
            context,
            context.run_id,
            "agent.tool.call",
            deepcopy(payload),
            **scope,
        )
        outcome = from_tool_result(
            str(identity.get("tool") or context.tool_name or "").strip(),
            tool_result,
            capabilities=capability_ids_for_tool(
                str(identity.get("tool") or context.tool_name or "").strip()
            ),
        )
        outcome_payload = {
            **outcome.to_event_payload(),
            **identity,
            "approved": True,
            "visibility": "internal",
        }
        outcome_payload = {
            key: value
            for key, value in outcome_payload.items()
            if value not in (None, "")
        }
        self._append_resume_run_event(
            context,
            context.run_id,
            "agent.tool.outcome",
            outcome_payload,
            **scope,
        )

    def _record_durable_approved_tool_receipt(
        self,
        context: ToolApprovalResumeContext,
        tool_result: Any,
    ) -> None:
        if self._append_run_event is None:
            return
        batch = getattr(
            context,
            "_approval_resume_runtime_event_batch",
            None,
        )
        if batch is None and self._get_current_run is None:
            # Standalone helper calls have no proof that an approval claim won.
            return
        payload = _approval_resume_durable_receipt_payload(context, tool_result)
        append_event = (
            batch.append_durable
            if batch is not None and hasattr(batch, "append_durable")
            else None
        )
        event_fields: dict[str, Any] = {}
        if append_event is not None or supports_keyword(
            self._append_run_event,
            "visibility",
        ):
            event_fields["visibility"] = "internal"
        if append_event is not None or supports_keyword(
            self._append_run_event,
            "sensitivity",
        ):
            event_fields["sensitivity"] = "private"
        if append_event is not None or supports_keyword(
            self._append_run_event,
            "actor",
        ):
            event_fields["actor"] = "native_runtime"
        for _attempt in range(3):
            current = None
            if self._get_current_run is not None:
                try:
                    current = self._get_current_run(context.run_id)
                except (KeyError, RuntimeError):
                    current = None
            fence: dict[str, str] = {}
            if isinstance(current, Mapping):
                status = str(current.get("status") or "").strip()
                updated_at = str(current.get("updated_at") or "").strip()
                if status and updated_at:
                    target = append_event or self._append_run_event
                    if append_event is not None or supports_keyword(
                        target,
                        "expected_status",
                    ):
                        fence["expected_status"] = status
                    if append_event is not None or supports_keyword(
                        target,
                        "expected_updated_at",
                    ):
                        fence["expected_updated_at"] = updated_at
            if append_event is not None:
                appended = append_event(
                    "agent.tool.executed_after_claim",
                    payload,
                    **event_fields,
                    **fence,
                )
            else:
                appended = self._append_run_event(
                    context.run_id,
                    "agent.tool.executed_after_claim",
                    payload,
                    **event_fields,
                    **fence,
                )
            if not fence or appended is not None:
                return
        raise AgentRuntimeError("approval_resume_receipt_fence_mismatch")

    def _record_task_progress_after_resume(
        self,
        context: ToolApprovalResumeContext,
        *,
        tool_timeline_start: int,
    ) -> None:
        for event_type, detail, payload in _approval_resume_task_progress_events(
            context.timeline,
            tool_timeline_start=tool_timeline_start,
        ):
            scoped_event_type = _approval_resume_progress_event_type(event_type, payload)
            event_payload = _approval_resume_progress_event_payload(
                payload,
                event_type,
                scoped_event_type,
            )
            context.timeline.append(self._timeline(scoped_event_type, detail, **event_payload))
            if self._append_run_event is not None:
                self._append_resume_run_event(
                    context,
                    context.run_id,
                    scoped_event_type,
                    event_payload,
                )

    def _record_replan_request_after_resume_failure(
        self,
        context: ToolApprovalResumeContext,
        tool_result: Any,
        *,
        tool_timeline_start: int,
    ) -> None:
        append_replan_request_event_for_tool_result(
            tool_request={**context.tool_request, "tool": context.tool_name},
            tool_event={
                "event": "agent.tool.failed",
                "detail": context.tool_name,
                "result": tool_result if isinstance(tool_result, Mapping) else {},
            },
            timeline=context.timeline,
            timeline_factory=self._timeline,
            append_run_event=(
                lambda run_id, event_type, payload, **event_fields: (
                    self._append_resume_run_event(
                        context,
                        run_id,
                        event_type,
                        payload,
                        **event_fields,
                    )
                )
                if self._append_run_event is not None
                else None
            ),
            runtime_tool_timeline_start=tool_timeline_start,
            run_id=context.run_id,
        )

    def _record_replan_request_after_resume_result(
        self,
        context: ToolApprovalResumeContext,
        tool_result: Any,
        *,
        tool_timeline_start: int,
    ) -> None:
        result = tool_result if isinstance(tool_result, Mapping) else {}
        append_replan_request_event_for_tool_result(
            tool_request={**context.tool_request, "tool": context.tool_name},
            tool_event={
                "event": _approval_resume_tool_result_event_type(result),
                "detail": context.tool_name,
                "result": result,
            },
            timeline=context.timeline,
            timeline_factory=self._timeline,
            append_run_event=(
                lambda run_id, event_type, payload, **event_fields: (
                    self._append_resume_run_event(
                        context,
                        run_id,
                        event_type,
                        payload,
                        **event_fields,
                    )
                )
                if self._append_run_event is not None
                else None
            ),
            runtime_tool_timeline_start=tool_timeline_start,
            run_id=context.run_id,
        )

    def continue_custom_api_agent_after_approved_tool(
        self,
        agent: dict[str, Any],
        context: ToolApprovalResumeContext,
    ) -> str:
        if self._continue_custom_api_agent is None:
            raise AgentRuntimeError(
                "Approval resume coordinator is missing custom API continuation"
            )
        resume_timeline_start = len(context.timeline)
        planned_tool_call_ids = [
            str(request.get("tool_call_id") or "")
            for request in [context.tool_request, *context.remaining_requests]
            if isinstance(request, Mapping)
        ]
        try:
            handoff = self.continuation_handoff_after_approved_tool(agent, context)
        except AgentApprovalRequired:
            # Approval pauses are control flow, not recoverable runtime errors.
            # AgentApprovalRequired subclasses AgentRuntimeError, so it must be
            # re-raised before the generic replan fallback below.
            raise
        except AgentRuntimeError:
            if not _approval_resume_has_pending_replan_request(context):
                raise
            handoff = ToolApprovalContinuationHandoff.from_context(
                agent,
                context,
                resume_after_approved_tool=False,
            )
        fresh_replan_pending = _approval_resume_events_have_pending_replan_request(
            context.timeline[resume_timeline_start:]
        )
        if (
            handoff.resume_after_approved_tool
            and fresh_replan_pending
        ):
            # A recoverable approved-tool result can request a fresh replan
            # without raising AgentRuntimeError (for example provider_required
            # or a policy-blocked desktop action). Only old replan requests are
            # skipped by the normal successful approval continuation.
            handoff = ToolApprovalContinuationHandoff.from_context(
                agent,
                context,
                resume_after_approved_tool=False,
            )
        outcome_results = self._outcome_loop.decide_terminal_batch(
            timeline=context.timeline,
            start_index=resume_timeline_start,
            run_id=context.run_id,
            allowed_tools=context.allowed_tools,
            planned_tool_call_ids=planned_tool_call_ids,
        )
        terminal_failure = next(
            (
                result
                for result in outcome_results
                if result.disposition is OutcomeLoopDisposition.FAILED
            ),
            None,
        )
        if terminal_failure is not None and not fresh_replan_pending:
            raise AgentRuntimeError(
                terminal_failure.reason or "approved_tool_outcome_failed"
            )
        action_required = next(
            (
                result
                for result in outcome_results
                if result.disposition is OutcomeLoopDisposition.AWAIT_USER
            ),
            None,
        )
        if action_required is not None:
            permission_recovery = (
                self._record_permission_recovery_after_approved_tool(
                    context,
                    action_required,
                )
            )
            if permission_recovery:
                raise AgentDirectOutcomeUnverified(
                    _approval_resume_permission_recovery_message(
                        permission_recovery
                    ),
                    reason=action_required.reason or "permission_required",
                    tool_name=action_required.outcome.tool_name,
                    input_preview=dict(
                        permission_recovery.get("input_preview") or {}
                    ),
                    tool_call_id=action_required.source_tool_call_id,
                )
            raise AgentRuntimeError(
                action_required.reason or "approved_tool_user_action_required"
            )
        blocking_dispositions = {
            OutcomeLoopDisposition.REPLAN_MODEL,
            OutcomeLoopDisposition.PARTIAL,
            OutcomeLoopDisposition.FAILED,
        }
        if any(
            result.disposition in blocking_dispositions
            for result in outcome_results
        ):
            handoff = ToolApprovalContinuationHandoff.from_context(
                agent,
                context,
                resume_after_approved_tool=False,
            )
        else:
            direct_result = _daily_desktop_resume_result_after_remaining_tools(
                context,
                resume_timeline_start=resume_timeline_start,
            )
            if direct_result:
                self._record_daily_desktop_completion(
                    context,
                    direct_result,
                    resume_timeline_start=resume_timeline_start,
                )
                return direct_result
        request = ToolApprovalCustomApiContinuationRequest.from_handoff(handoff)
        return request.execute(self._continue_custom_api_agent)

    def _record_permission_recovery_after_approved_tool(
        self,
        context: ToolApprovalResumeContext,
        action_required: Any,
    ) -> dict[str, Any]:
        payload = _approval_resume_permission_recovery_payload(
            context,
            action_required,
        )
        if not payload:
            return {}
        tool_call_id = str(payload.get("tool_call_id") or "").strip()
        duplicate = bool(
            tool_call_id
            and any(
                isinstance(event, Mapping)
                and str(
                    event.get("event") or event.get("event_type") or ""
                ).strip()
                == "agent.desktop.permission_recovery"
                and str(event.get("tool_call_id") or "").strip()
                == tool_call_id
                for event in context.timeline
            )
        )
        if duplicate:
            return payload
        tool_name = str(payload.get("tool") or context.tool_name or "").strip()
        context.timeline.append(
            self._timeline(
                "agent.desktop.permission_recovery",
                tool_name,
                **deepcopy(payload),
            )
        )
        if self._append_run_event is not None:
            self._append_resume_run_event(
                context,
                context.run_id,
                "agent.desktop.permission_recovery",
                deepcopy(payload),
            )
        return payload

    def _record_daily_desktop_completion(
        self,
        context: ToolApprovalResumeContext,
        result_text: str,
        *,
        resume_timeline_start: int = 0,
    ) -> None:
        if any(
            isinstance(event, Mapping)
            and str(event.get("event") or event.get("event_type") or "").strip()
            == "agent.desktop.intent_completed"
            for event in context.timeline
        ):
            return
        action_events = _daily_desktop_successful_action_events(context.timeline)
        fresh_verification_events = _daily_desktop_successful_verification_events(
            context.timeline[max(0, int(resume_timeline_start or 0)) :]
        )
        included_event_ids = {
            id(event) for event in [*action_events, *fresh_verification_events]
        }
        completion_events = [
            event for event in context.timeline if id(event) in included_event_ids
        ]
        tools = [
            str(event.get("detail") or event["result"].get("tool") or "").strip()
            for event in completion_events
            if str(event.get("detail") or event["result"].get("tool") or "").strip()
        ]
        steps = [
            _daily_desktop_completion_step(event)
            for event in completion_events
        ]
        payload = {
            "tool": context.tool_name,
            "source": "runtime_planner",
            "tools": tools,
            "steps": steps,
            "result": result_text,
            "summary": result_text,
        }
        verification_evidence = (
            _daily_desktop_native_receipt_verification_evidence(
                context,
                fresh_verification_events,
            )
        )
        if verification_evidence:
            payload.update(verification_evidence)
            payload["verification_evidence"] = deepcopy(
                verification_evidence
            )
        primary_action = next(
            (
                event
                for event in reversed(action_events)
                if str(event.get("detail") or "").strip() == context.tool_name
            ),
            action_events[-1] if action_events else None,
        )
        if primary_action is not None:
            primary_step = _daily_desktop_completion_step(primary_action)
            for key in (
                "request_id",
                "source_request_id",
                "step_id",
                "source_step_id",
                "runtime_stage",
                "runtime_role",
                "verification_target",
                "verification_targets",
                "task_verification_targets",
            ):
                value = primary_step.get(key)
                if value not in (None, "", [], {}):
                    payload[key] = deepcopy(value)
        context.timeline.append(
            self._timeline(
                "agent.desktop.intent_completed",
                tools[-1] if tools else context.tool_name,
                **payload,
            )
        )
        if self._append_run_event is not None:
            self._append_resume_run_event(
                context,
                context.run_id,
                "agent.desktop.intent_completed",
                payload,
            )

    def continuation_handoff_after_approved_tool(
        self,
        agent: dict[str, Any],
        context: ToolApprovalResumeContext,
    ) -> ToolApprovalContinuationHandoff:
        self.execute_approved_tool(context)
        conflicting_artifact = _approval_resume_conflicting_pending_artifact(context)
        if conflicting_artifact:
            approved_path = _approval_resume_request_path(context.tool_request)
            payload = {
                "source": "approval_resume",
                "status": "superseded",
                "approved_tool": context.tool_name,
                "approved_path": approved_path,
                "pending_tool": "artifact.write",
                "pending_path": conflicting_artifact["path"],
            }
            payload = {
                key: value
                for key, value in payload.items()
                if value not in (None, "", [], {})
            }
            context.timeline.append(
                self._timeline(
                    "agent.model.followup_context",
                    "Approved model write superseded conflicting pending artifact",
                    **payload,
                )
            )
            if self._append_run_event is not None:
                self._append_resume_run_event(
                    context,
                    context.run_id,
                    "agent.model.followup_context",
                    payload,
                )
        return ToolApprovalContinuationHandoff.from_context(agent, context)

    def continue_and_project_after_approved_tool(
        self,
        *,
        agent: dict[str, Any],
        context: ToolApprovalResumeContext,
        project_completed: Any,
        project_required: Any,
        project_failed: Any,
        prepare_required: Any | None = None,
        redact_error: Any = redact_api_error_text,
    ) -> dict[str, Any]:
        working_context = _approval_resume_working_context(context)
        setattr(working_context, "_approval_resume_pending_events", [])
        event_scope = (
            self._event_buffer_scope(context.run_id)
            if self._event_buffer_scope is not None
            else nullcontext(None)
        )
        with event_scope as runtime_event_batch:
            setattr(
                working_context,
                "_approval_resume_runtime_event_batch",
                runtime_event_batch,
            )
            try:
                result_text = self.continue_custom_api_agent_after_approved_tool(
                    agent,
                    working_context,
                )
                outcome = ToolApprovalContinuationOutcome.completed(result_text)
            except AgentApprovalRequired as exc:
                outcome = ToolApprovalContinuationOutcome.approval_required(
                    exc.pending_approval,
                    prepare_required=prepare_required,
                )
            except Exception as exc:
                outcome = ToolApprovalContinuationOutcome.failed(
                    exc,
                    redact_error=redact_error,
                )
        # Tool execution may have external side effects, but its mutable local
        # projections and replayable events are tentative until the final Run
        # CAS wins. Keep them isolated and flush them in the same UoW as that
        # authoritative projection.
        scope = (
            self._transaction_scope()
            if self._transaction_scope is not None
            else nullcontext()
        )
        setattr(
            working_context,
            "_approval_resume_flush_events",
            lambda projected: self._flush_all_resume_events(
                working_context,
                runtime_event_batch,
                projected,
            ),
        )
        try:
            with scope:
                projected = outcome.project(
                    working_context,
                    project_completed=project_completed,
                    project_required=project_required,
                    project_failed=project_failed,
                )
                if getattr(
                    working_context,
                    "_approval_resume_projection_state",
                    "",
                ) == "cas_lost":
                    raise _ToolApprovalContinuationProjectionConflict(projected)
                if (
                    outcome.kind == "approval_required"
                    and not _approval_required_projection_matches(
                        projected,
                        outcome.pending_approval,
                    )
                ):
                    raise _ToolApprovalContinuationProjectionConflict(projected)
                self._flush_all_resume_events(
                    working_context,
                    runtime_event_batch,
                    projected,
                )
            _commit_approval_resume_context(context, working_context)
            return projected
        except _ToolApprovalContinuationProjectionConflict as exc:
            if runtime_event_batch is not None and hasattr(
                runtime_event_batch,
                "discard",
            ):
                runtime_event_batch.discard()
            _reset_approval_resume_context(working_context, context)
            return exc.current
        except BaseException:
            if runtime_event_batch is not None and hasattr(
                runtime_event_batch,
                "discard",
            ):
                runtime_event_batch.discard()
            _reset_approval_resume_context(working_context, context)
            raise
        finally:
            if outcome.kind != "approval_required":
                close_owned_browser_target_best_effort(context.broker)

    def resume_approved_tool_run(
        self,
        *,
        run_id: str,
        pending: dict[str, Any],
        context: ToolApprovalResumeContext,
        agent: dict[str, Any],
        resumed_detail: str,
        running_result: str,
        project_completed: Any,
        project_required: Any,
        project_failed: Any,
        get_current_run: Any,
        expected_approval_id: str = "",
        project_running: Any | None = None,
        prepare_required: Any | None = None,
        project_result: Any | None = None,
        redact_error: Any = redact_api_error_text,
    ) -> dict[str, Any]:
        running = self.claim_and_project_approved_tool(
            run_id,
            pending,
            context,
            resumed_detail=resumed_detail,
            running_result=running_result,
            expected_approval_id=expected_approval_id,
        )
        if running is None:
            return get_current_run(run_id)
        if project_running is not None:
            running = project_running(running)
        result = self.continue_and_project_after_approved_tool(
            agent=agent,
            context=context,
            project_completed=project_completed,
            project_required=project_required,
            project_failed=project_failed,
            prepare_required=prepare_required,
            redact_error=redact_error,
        )
        return project_result(result) if project_result is not None else result


def _approval_resume_permission_recovery_payload(
    context: ToolApprovalResumeContext,
    action_required: Any,
) -> dict[str, Any]:
    """Project only a structured, provider-backed permission gate publicly."""

    outcome = getattr(action_required, "outcome", None)
    user_action = getattr(outcome, "user_action", None)
    if not (
        getattr(user_action, "required", False) is True
        and str(getattr(user_action, "kind", "") or "").strip()
        == "permission"
    ):
        return {}
    raw = getattr(outcome, "raw", None)
    if not isinstance(raw, Mapping):
        return {}
    nested = raw.get("data")
    sources = [
        source
        for source in (
            nested if isinstance(nested, Mapping) else None,
            raw,
        )
        if isinstance(source, Mapping)
    ]
    if not _approval_resume_has_raw_permission_signal(sources):
        return {}

    tool_name = str(
        getattr(outcome, "tool_name", "") or context.tool_name or ""
    ).strip()
    permission_targets = _approval_resume_safe_text_list(
        [
            *list(getattr(user_action, "targets", ()) or ()),
            *(
                item
                for source in sources
                for key in ("permission_targets", "missing_permissions")
                for item in _approval_resume_text_items(source.get(key))
            ),
        ],
        limit=24,
    )
    affected_tools = _approval_resume_safe_text_list(
        [
            *(
                item
                for source in sources
                for item in _approval_resume_text_items(
                    source.get("affected_tools")
                )
            ),
            tool_name,
        ],
        limit=24,
    )
    recovery_hints = _approval_resume_safe_text_list(
        [
            *list(getattr(outcome, "recovery_hints", ()) or ()),
            *(
                item
                for source in sources
                for item in _approval_resume_text_items(
                    source.get("recovery_hints")
                )
            ),
        ],
        limit=24,
    )
    safe_input = sanitize_sensitive_value(
        dict(context.input_preview or {}),
        max_depth=3,
        text_limit=400,
        max_items=24,
    )
    if not isinstance(safe_input, dict):
        safe_input = {}
    tool_call_id = str(
        getattr(action_required, "source_tool_call_id", "")
        or context.tool_request.get("tool_call_id")
        or ""
    ).strip()
    payload: dict[str, Any] = {
        "tool": sanitize_sensitive_value(tool_name, text_limit=160),
        "source": "approval_resume",
        "status": "permission_recovery_available",
        # Preserve a machine-readable signal even when the provider omitted
        # target names. Consumer snapshots must not reinterpret this as an
        # ordinary tool failure.
        "permission_error": True,
        "input_preview": safe_input,
        "permission_targets": permission_targets,
        "affected_tools": affected_tools,
        "recovery_hints": recovery_hints,
        "tool_call_id": sanitize_sensitive_value(tool_call_id, text_limit=160),
    }
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", [], {})
        or key
        in {
            "input_preview",
            "permission_error",
            "permission_targets",
            "recovery_hints",
        }
    }


def _approval_resume_has_raw_permission_signal(
    sources: list[Mapping[str, Any]],
) -> bool:
    return any(
        source.get("permission_error") is True
        or str(source.get("status") or "").strip().lower()
        == "permission_required"
        or bool(
            _approval_resume_text_items(source.get("missing_permissions"))
        )
        or bool(
            _approval_resume_text_items(source.get("permission_targets"))
        )
        for source in sources
    )


def _approval_resume_text_items(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (set, frozenset)):
        values = sorted(value, key=lambda item: str(item))
    elif isinstance(value, (list, tuple)):
        values = value
    else:
        return []
    return [
        item.strip()
        for item in values
        if isinstance(item, str) and item.strip()
    ]


def _approval_resume_safe_text_list(
    values: Any,
    *,
    limit: int,
) -> list[str]:
    safe: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        item = sanitize_sensitive_value(value, text_limit=240)
        if isinstance(item, str) and item and item not in safe:
            safe.append(item)
        if len(safe) >= limit:
            break
    return safe


def _approval_resume_permission_recovery_message(
    payload: Mapping[str, Any],
) -> str:
    targets = _approval_resume_safe_text_list(
        _approval_resume_text_items(payload.get("permission_targets")),
        limit=4,
    )
    if targets:
        return f"此操作需要先完成系统权限授权（{', '.join(targets)}），然后重试。"
    return "此操作需要先完成系统权限授权，然后重试。"


def _approval_resume_working_context(
    context: ToolApprovalResumeContext,
) -> ToolApprovalResumeContext:
    """Isolate all mutable continuation state until its Run UoW commits."""

    return replace(
        context,
        timeline=deepcopy(context.timeline),
        artifacts=deepcopy(context.artifacts),
        allowed_tools=deepcopy(context.allowed_tools),
        messages=deepcopy(context.messages),
        tool_request=deepcopy(context.tool_request),
        input_preview=deepcopy(context.input_preview),
        remaining_requests=deepcopy(context.remaining_requests),
        runtime_execution_envelope=deepcopy(context.runtime_execution_envelope),
        runtime_execution_metadata=deepcopy(context.runtime_execution_metadata),
    )


def _approval_resume_private_recovery_context(
    tool_request: Mapping[str, Any],
    context: ToolApprovalResumeContext,
) -> dict[str, Any]:
    """Validate persisted lineage before minting one process-local capability."""

    recovery_marker_keys = (
        "recovery_link_kind",
        "recovery_action",
        "recovery_scope_id",
        "replan_recovery_identity",
        "recovery_source_tool",
        "recovery_origin_tool_call_id",
        "root_source_tool_call_id",
        "root_source_step_id",
        "root_verifier_step_id",
        "root_plan_id",
        "recovery_suggested_tool",
    )
    claims_recovery = (
        str(tool_request.get("source") or "").strip()
        == "runtime_internal_recovery"
        or any(key in tool_request for key in recovery_marker_keys)
    )
    if not claims_recovery:
        return {}
    required_text_keys = (
        "goal_contract_id",
        "goal_criterion_id",
        "goal_subgoal_id",
        "source_tool_call_id",
        "recovery_source_tool",
        "recovery_action",
        "recovery_scope_id",
        "replan_recovery_identity",
        "tool_call_id",
        "plan_id",
        "source_step_id",
        "recovery_suggested_tool",
        "root_source_tool_call_id",
        "root_source_step_id",
        "root_verifier_step_id",
        "root_plan_id",
        "recovery_origin_tool_call_id",
    )
    if not (
        str(tool_request.get("source") or "").strip()
        == "runtime_internal_recovery"
        and str(tool_request.get("recovery_link_kind") or "").strip()
        == "coordinator_action"
        and tool_request.get("root_goal_unchanged") is True
        and all(str(tool_request.get(key) or "").strip() for key in required_text_keys)
    ):
        raise AgentRuntimeError("approval_resume_recovery_lineage_invalid")
    private_context = rehydrate_private_recovery_context(
        tool_request,
        context.timeline,
        run_id=context.run_id,
        goal_contract=context.goal_contract,
    )
    if not private_context:
        raise AgentRuntimeError("approval_resume_recovery_lineage_invalid")
    return private_context


def _approval_resume_recovery_execution_request(
    tool_request: Mapping[str, Any],
    context: ToolApprovalResumeContext,
) -> dict[str, Any]:
    """Build an ephemeral request without mutating persisted continuation state."""

    execution_request = dict(tool_request)
    execution_request.pop(RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY, None)
    execution_request.pop("recovery_context_trusted", None)
    private_context = _approval_resume_private_recovery_context(
        execution_request,
        context,
    )
    if private_context:
        execution_request[RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY] = private_context
    return execution_request


def _commit_approval_resume_context(
    context: ToolApprovalResumeContext,
    working_context: ToolApprovalResumeContext,
) -> None:
    context.timeline[:] = working_context.timeline
    context.artifacts[:] = working_context.artifacts
    context.messages[:] = working_context.messages
    context.remaining_requests[:] = working_context.remaining_requests


def _reset_approval_resume_context(
    working_context: ToolApprovalResumeContext,
    context: ToolApprovalResumeContext,
) -> None:
    working_context.timeline[:] = deepcopy(context.timeline)
    working_context.artifacts[:] = deepcopy(context.artifacts)
    working_context.messages[:] = deepcopy(context.messages)
    working_context.remaining_requests[:] = deepcopy(context.remaining_requests)
    pending_events = getattr(
        working_context,
        "_approval_resume_pending_events",
        None,
    )
    if isinstance(pending_events, list):
        pending_events.clear()


def _approval_resume_result_identity(
    context: ToolApprovalResumeContext,
) -> dict[str, str]:
    request = context.tool_request
    approval_id = str(
        context.approval_id or request.get("approval_id") or ""
    ).strip()
    request_fingerprint = str(
        context.approval_request_fingerprint or ""
    ).strip() or approval_request_fingerprint(request)
    broker_type = type(context.broker)
    provider_id = (
        f"{str(broker_type.__module__ or '').strip()}."
        f"{str(broker_type.__qualname__ or broker_type.__name__).strip()}"
    ).strip(".")
    identity = {
        "approval_id": approval_id,
        "approval_generation_id": approval_id,
        "approval_request_fingerprint": request_fingerprint,
        "run_id": str(context.run_id or "").strip(),
        "decision_id": str(request.get("decision_id") or "").strip(),
        "plan_id": str(request.get("plan_id") or "").strip(),
        "tool_plan_id": str(request.get("tool_plan_id") or "").strip(),
        "step_id": str(
            request.get("step_id") or request.get("planner_step_id") or ""
        ).strip(),
        "request_id": str(
            request.get("request_id")
            or request.get("planner_request_id")
            or ""
        ).strip(),
        "tool_call_id": str(request.get("tool_call_id") or "").strip(),
        "tool": str(context.tool_name or request.get("tool") or "").strip(),
        "provider_kind": "runtime_tool_broker",
        "provider_id": provider_id,
        "materialization_binding_id": str(
            request.get("materialization_binding_id") or ""
        ).strip(),
        "materialized_content_sha256": str(
            request.get("materialized_content_sha256") or ""
        ).strip(),
    }
    claim_source = json.dumps(
        {
            key: identity.get(key, "")
            for key in (
                "approval_generation_id",
                "approval_request_fingerprint",
                "run_id",
                "decision_id",
                "plan_id",
                "tool_plan_id",
                "step_id",
                "request_id",
                "tool_call_id",
                "tool",
                "provider_kind",
                "provider_id",
            )
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    identity["approval_claim_id"] = hashlib.sha256(
        claim_source.encode("utf-8")
    ).hexdigest()
    return identity


def _approval_resume_validate_materialization_binding(
    context: ToolApprovalResumeContext,
) -> None:
    request = context.tool_request
    binding_id = str(request.get("materialization_binding_id") or "").strip()
    expected_sha256 = str(request.get("materialized_content_sha256") or "").strip()
    if not binding_id and not expected_sha256:
        return
    if not binding_id or len(expected_sha256) != 64:
        raise AgentRuntimeError("approval_resume_materialization_identity_invalid")
    raw_input = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    tool_name = str(request.get("tool") or context.tool_name or "").strip()
    body_key = {
        "artifact.write": "content",
        "clipboard.write": "text",
        "notes.create": "body",
    }.get(tool_name)
    if body_key is None and tool_name in {
        "app.focus_and_safe_type_text",
        "app.focus_and_type_into_ui_element",
        "app.open_and_safe_type_text",
        "app.open_and_type_into_ui_element",
        "desktop.safe_type_text",
        "desktop.type_into_ui_element",
        "desktop.type_text",
    }:
        body_key = "text"
    body = raw_input.get(body_key) if body_key else None
    if isinstance(body, str):
        actual_sha256 = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if actual_sha256 != expected_sha256:
            raise AgentRuntimeError("approval_resume_materialization_hash_mismatch")
        return
    dependencies = set(
        str(value or "").strip()
        for value in request.get("depends_on", [])
        if str(value or "").strip()
    )
    if not dependencies:
        raise AgentRuntimeError("approval_resume_materialization_source_missing")
    expected_run_id = str(context.run_id or "").strip()
    expected_decision_id = str(request.get("decision_id") or "").strip()
    expected_plan_id = str(request.get("plan_id") or "").strip()
    for event in reversed(context.timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != "agent.tool.call":
            continue
        if str(event.get("materialization_binding_id") or "").strip() != binding_id:
            continue
        if str(event.get("materialized_content_sha256") or "").strip() != expected_sha256:
            continue
        step_id = str(event.get("step_id") or event.get("planner_step_id") or "").strip()
        if step_id not in dependencies:
            continue
        event_run_id = str(event.get("run_id") or "").strip()
        if event_run_id and event_run_id != expected_run_id:
            continue
        if expected_decision_id and str(event.get("decision_id") or "").strip() != (
            expected_decision_id
        ):
            continue
        if expected_plan_id and str(event.get("plan_id") or "").strip() != expected_plan_id:
            continue
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        if result.get("ok") is True and not result.get("approval_required"):
            return
    raise AgentRuntimeError("approval_resume_materialization_source_missing")


def _approval_resume_durable_receipt_payload(
    context: ToolApprovalResumeContext,
    tool_result: Any,
) -> dict[str, Any]:
    identity = _approval_resume_result_identity(context)
    outcome = from_tool_result(
        str(identity.get("tool") or context.tool_name or "").strip(),
        tool_result,
        capabilities=capability_ids_for_tool(
            str(identity.get("tool") or context.tool_name or "").strip()
        ),
    )
    safe_outcome = sanitize_sensitive_value(
        outcome.to_event_payload(),
        max_depth=3,
        text_limit=240,
        max_items=20,
    )
    safe_preview = sanitize_sensitive_value(
        _task_progress_result_preview(
            tool_result if isinstance(tool_result, Mapping) else {}
        ),
        max_depth=2,
        text_limit=240,
        max_items=16,
    )
    digest_source = json.dumps(
        {
            "outcome": safe_outcome,
            "preview": safe_preview,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload = {
        **identity,
        "receipt_kind": "executed_after_claim",
        "result_status": outcome.status.value,
        "result_reason": sanitize_sensitive_value(
            outcome.reason,
            text_limit=160,
        ),
        "result_preview": safe_preview,
        "result_sha256": hashlib.sha256(
            digest_source.encode("utf-8")
        ).hexdigest(),
        "effects": sanitize_sensitive_value(
            list(outcome.effects),
            max_depth=1,
            text_limit=160,
            max_items=12,
        ),
        "external_effect_possible": context.tool_name
        not in READ_ONLY_OBSERVATION_TOOLS,
        "completion_evidence": False,
        "goal_evidence": False,
    }
    return {
        key: value
        for key, value in payload.items()
        if value not in (None, "", [], {})
        or key
        in {
            "completion_evidence",
            "external_effect_possible",
            "goal_evidence",
            "result_preview",
        }
    }


def _approval_resume_event_identity(event: Mapping[str, Any]) -> dict[str, str]:
    return {
        key: str(
            (
                event.get("tool") or event.get("detail")
                if key == "tool"
                else event.get(key)
            )
            or ""
        ).strip()
        for key in _APPROVAL_RESUME_RESULT_IDENTITY_KEYS
    }


def _approval_resume_existing_canonical_result(
    context: ToolApprovalResumeContext,
) -> tuple[bool, Any]:
    expected = _approval_resume_result_identity(context)
    for event in reversed(context.timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        if event.get("approval_resume_result_canonical") is not True:
            continue
        actual = _approval_resume_event_identity(event)
        if not _approval_resume_canonical_result_correlates(expected, actual):
            continue
        _approval_resume_validate_canonical_result_authority(event)
        _approval_resume_validate_canonical_result_completeness(
            expected,
            event,
        )
        _approval_resume_validate_exact_result_identity(expected, actual)
        if "result" not in event:
            raise AgentRuntimeError("approval_resume_result_missing")
        return True, deepcopy(event.get("result"))
    return False, None


def _approval_resume_canonical_result_correlates(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> bool:
    # A plan and even a planner step may legitimately contain more than one
    # sequential approval.  They are routing context, not a result-instance
    # identity.  Only a matching approval generation/claim/fingerprint or
    # exact request/call may select a canonical result for strict validation.
    return any(
        expected.get(key)
        and actual.get(key)
        and expected.get(key) == actual.get(key)
        for key in (
            "approval_id",
            "approval_generation_id",
            "approval_claim_id",
            "approval_request_fingerprint",
            "tool_call_id",
            "request_id",
        )
    )


def _approval_resume_validate_canonical_result_authority(
    event: Mapping[str, Any],
) -> None:
    if not (
        event.get("approved") is True
        and str(event.get("actor") or "").strip() == "native_runtime"
        and str(event.get("execution_authority") or "").strip()
        == "runtime_tool_executor"
        and str(event.get("execution_mode") or "").strip()
        == "approved_result_canonical_projection"
        and str(event.get("visibility") or "").strip() == "internal"
        and str(event.get("sensitivity") or "").strip() == "private"
    ):
        raise AgentRuntimeError("approval_resume_result_authority_mismatch")


def _approval_resume_validate_canonical_result_completeness(
    expected: Mapping[str, str],
    event: Mapping[str, Any],
) -> None:
    required = (
        "approval_id",
        "approval_generation_id",
        "approval_claim_id",
        "approval_request_fingerprint",
        "run_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "step_id",
        "request_id",
        "tool_call_id",
        "tool",
        "provider_kind",
        "provider_id",
    )
    if any(
        not expected.get(key) or not str(event.get(key) or "").strip()
        for key in required
    ):
        raise AgentRuntimeError("approval_resume_result_identity_mismatch")


def _approval_resume_validate_exact_result_identity(
    expected: Mapping[str, str],
    actual: Mapping[str, str],
) -> None:
    if any(
        expected.get(key) and actual.get(key) != expected.get(key)
        for key in _APPROVAL_RESUME_RESULT_IDENTITY_KEYS
    ):
        raise AgentRuntimeError("approval_resume_result_identity_mismatch")


def _approval_resume_validate_fresh_tool_result_events(
    events: list[dict[str, Any]],
    expected: Mapping[str, str],
    tool_result: Any,
) -> None:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        actual = _approval_resume_event_identity(event)
        same_tool = bool(
            expected.get("tool") and actual.get("tool") == expected.get("tool")
        )
        same_approval = bool(
            expected.get("approval_id")
            and actual.get("approval_id") == expected.get("approval_id")
        )
        if not (
            same_tool
            or same_approval
            or event.get("approved") is True
            or event.get("approval_resume_result_canonical") is True
        ):
            continue
        if any(
            expected.get(key)
            and actual.get(key)
            and actual.get(key) != expected.get(key)
            for key in _APPROVAL_RESUME_RESULT_IDENTITY_KEYS
        ):
            raise AgentRuntimeError("approval_resume_result_identity_mismatch")
        if "result" in event and event.get("result") != tool_result:
            raise AgentRuntimeError("approval_resume_result_mismatch")


def _approval_resume_conflicting_pending_artifact(
    context: ToolApprovalResumeContext,
) -> dict[str, str]:
    request = context.tool_request
    if str(request.get("protocol") or "").strip() != "tool_calls":
        return {}
    approved_tool = str(context.tool_name or request.get("tool") or "").strip()
    if approved_tool not in {"artifact.write", "workspace.write_patch"}:
        return {}
    approved_step_id = str(
        request.get("step_id") or request.get("planner_step_id") or ""
    ).strip()
    approved_request_id = str(
        request.get("request_id")
        or request.get("planner_request_id")
        or request.get("intent_id")
        or request.get("planner_intent_id")
        or ""
    ).strip()
    approved_path = _approval_resume_request_path(request)
    approved_normalized_path = _approval_resume_normalized_path(approved_path)
    for event in reversed(context.timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.model.followup_context"
        ):
            continue
        payload = _timeline_payload(event)
        if str(payload.get("status") or "").strip() == "superseded":
            return {}
        pending_items = payload.get("pending_execution_requests")
        if not isinstance(pending_items, list):
            pending_items = payload.get("pending_plan_steps")
        if not isinstance(pending_items, list):
            return {}
        pending_artifacts = [
            item
            for item in pending_items
            if isinstance(item, Mapping)
            and str(item.get("tool_name") or item.get("tool") or "").strip()
            == "artifact.write"
        ]
        if not pending_artifacts:
            return {}
        for item in pending_artifacts:
            pending_step_id = str(
                item.get("step_id") or item.get("planner_step_id") or ""
            ).strip()
            pending_request_id = str(
                item.get("request_id")
                or item.get("planner_request_id")
                or item.get("intent_id")
                or item.get("planner_intent_id")
                or ""
            ).strip()
            pending_path = _approval_resume_request_path(item)
            same_request = bool(
                approved_request_id and pending_request_id == approved_request_id
            )
            same_step = bool(approved_step_id and pending_step_id == approved_step_id)
            same_path = bool(
                approved_normalized_path
                and _approval_resume_normalized_path(pending_path)
                == approved_normalized_path
            )
            if same_request or same_step or same_path:
                return {
                    "tool": "artifact.write",
                    "path": pending_path,
                }
        return {}
    return {}


def _approval_required_projection_matches(
    projected: Any,
    pending_approval: Any,
) -> bool:
    if not isinstance(projected, Mapping):
        return False
    if str(projected.get("status") or "").strip() != "approval_required":
        return False
    expected = (
        str(pending_approval.get("approval_id") or "").strip()
        if isinstance(pending_approval, Mapping)
        else ""
    )
    if not expected or "pending_approval" not in projected:
        # The missing key is retained for compatibility with lightweight
        # projection callbacks used outside the persisted runtime.
        return True
    projected_pending = projected.get("pending_approval")
    if not isinstance(projected_pending, Mapping):
        return False
    return str(projected_pending.get("approval_id") or "").strip() == expected


def _approval_resume_request_path(request: Mapping[str, Any]) -> str:
    raw_input = (
        request.get("input")
        if isinstance(request.get("input"), Mapping)
        else request.get("input_preview")
        if isinstance(request.get("input_preview"), Mapping)
        else {}
    )
    return str(
        raw_input.get("path")
        or raw_input.get("artifact_path")
        or raw_input.get("target_path")
        or ""
    ).strip()


def _approval_resume_normalized_path(value: Any) -> str:
    clean = str(value or "").strip().replace("\\", "/")
    if not clean:
        return ""
    normalized = posixpath.normpath(clean)
    return "" if normalized == "." else normalized


def _approval_resume_task_progress_events(
    timeline: list[dict[str, Any]],
    *,
    tool_timeline_start: int,
) -> list[tuple[str, str, dict[str, Any]]]:
    task_context = _latest_task_core_context(timeline)
    task_core = task_context.get("task_core")
    if not isinstance(task_core, Mapping):
        return []
    todos = [
        todo
        for todo in task_core.get("todos", [])
        if isinstance(todo, Mapping) and str(todo.get("step_id") or "").strip()
    ]
    if not todos:
        return []
    checkpoints = [
        checkpoint
        for checkpoint in task_core.get("checkpoints", [])
        if isinstance(checkpoint, Mapping)
    ]
    plan_steps = _latest_plan_steps(
        timeline,
        decision_id=str(task_context.get("decision_id") or "").strip(),
        plan_id=str(task_context.get("plan_id") or "").strip(),
    )
    checkpoints_by_step: dict[str, list[Mapping[str, Any]]] = {}
    for checkpoint in checkpoints:
        step_id = str(checkpoint.get("after_step_id") or "").strip()
        if step_id:
            checkpoints_by_step.setdefault(step_id, []).append(checkpoint)
    tool_events = _approval_resume_deduped_tool_events(
        timeline[tool_timeline_start:]
    )
    if not tool_events:
        return []

    event_index = 0
    events: list[tuple[str, str, dict[str, Any]]] = []
    for todo in todos:
        step_id = str(todo.get("step_id") or "").strip()
        if _latest_task_update_status(
            timeline,
            "agent.task.todo.updated",
            "step_id",
            step_id,
            decision_id=str(task_context.get("decision_id") or "").strip(),
        ) in {"completed", "skipped"}:
            continue
        step = plan_steps.get(step_id, {})
        tool_name = str(step.get("tool_name") or todo.get("tool_name") or "").strip()
        if not tool_name:
            continue
        tool_event: dict[str, Any] | None = None
        while event_index < len(tool_events):
            candidate = tool_events[event_index]
            event_index += 1
            if str(candidate.get("detail") or "").strip() == tool_name:
                tool_event = candidate
                break
        if tool_event is None:
            continue
        result = tool_event.get("result") if isinstance(tool_event.get("result"), Mapping) else {}
        todo_status = _task_todo_status_for_tool_result(
            str(tool_event.get("event") or ""),
            result,
        )
        checkpoint_status = _task_checkpoint_status_for_todo_status(
            todo_status,
            result,
        )
        source_event = {
            "event": str(tool_event.get("event") or "").strip(),
            "detail": str(tool_event.get("detail") or "").strip(),
        }
        base_payload = {
            "source": "runtime_planner",
            "core_id": str(task_context.get("core_id") or "").strip(),
            "workspace_id": str(task_context.get("workspace_id") or "").strip(),
            "decision_id": str(task_context.get("decision_id") or "").strip(),
            "plan_id": str(task_context.get("plan_id") or "").strip(),
            "step_id": step_id,
            "tool": tool_name,
            "source_event": source_event,
            "result_preview": _task_progress_result_preview(result),
        }
        for key in (
            "task_id",
            "run_group_id",
            "group_run_id",
            "group_id",
            "workflow_id",
            "workflow_run_id",
            "workflow_node_id",
            "workflow_node_label",
        ):
            value = str(task_context.get(key) or "").strip()
            if value:
                base_payload[key] = value
        events.append(_todo_progress_event(timeline, todo, base_payload, todo_status))
        for checkpoint in checkpoints_by_step.get(step_id, []):
            events.append(
                _checkpoint_progress_event(
                    timeline,
                    checkpoint,
                    base_payload,
                    checkpoint_status,
                )
            )
    return events


def _approval_resume_attach_private_exact_submit_receipt(
    requests: list[dict[str, Any]],
    receipt: Mapping[str, Any],
    *,
    run_id: str,
) -> list[dict[str, Any]]:
    """Pass one opaque exact-submit receipt to its one bound verifier."""

    if not receipt:
        raise AgentRuntimeError("approval_resume_exact_submit_receipt_invalid")
    source_tool_call_id = str(
        receipt.get("source_tool_call_id") or ""
    ).strip()
    if not source_tool_call_id:
        raise AgentRuntimeError("approval_resume_exact_submit_receipt_invalid")
    receipt_map = {source_tool_call_id: receipt}
    matching_indexes = [
        index
        for index, request in enumerate(requests)
        if _private_exact_submit_dispatch_receipt_for_verifier(
            request,
            receipt_map,
            run_id=run_id,
        )
    ]
    if len(matching_indexes) != 1:
        raise AgentRuntimeError("approval_resume_exact_submit_verifier_invalid")
    matching_index = matching_indexes[0]
    attached = [dict(request) for request in requests]
    attached[matching_index][
        RUNTIME_PRIVATE_EXACT_SUBMIT_RECEIPT_REQUEST_KEY
    ] = receipt
    return attached


def _approval_resume_remaining_requests_after_tool(
    context: ToolApprovalResumeContext,
    tool_result: Any,
) -> list[dict[str, Any]]:
    existing = [
        dict(request)
        for request in context.remaining_requests
        if isinstance(request, Mapping)
    ]
    existing = _resumable_remaining_requests(existing)
    existing = _approval_resume_bind_goal_contract_identity(
        existing,
        context.goal_contract,
    )
    existing = [
        request
        for request in existing
        if not _approval_resume_exact_approved_request_duplicate(
            request,
            context.tool_request,
        )
    ]
    existing_verifier_index = _approval_resume_exact_verifier_index(
        existing,
        context.tool_request,
    )
    if existing_verifier_index is not None:
        verifier = existing[existing_verifier_index]
        ordered = [
            verifier,
            *existing[:existing_verifier_index],
            *existing[existing_verifier_index + 1 :],
        ]
        source_step_id = str(
            context.tool_request.get("step_id")
            or context.tool_request.get("planner_step_id")
            or ""
        ).strip()
        if _remaining_requests_include_post_action_verification(
            ordered,
            source_tool_name=context.tool_name,
            allowed_tools=context.allowed_tools,
            source_step_id=source_step_id,
            source_request_id=str(
                context.tool_request.get("request_id") or ""
            ).strip(),
            source_tool_call_id=str(
                context.tool_request.get("tool_call_id") or ""
            ).strip(),
            source_plan_id=str(
                context.tool_request.get("plan_id") or ""
            ).strip(),
            source_tool_plan_id=str(
                context.tool_request.get("tool_plan_id") or ""
            ).strip(),
            verification_predicate_kind=_post_action_verification_predicate_kind(
                context.tool_name
            ),
            bind_source_identity=True,
        ):
            return _approval_resume_bind_goal_contract_identity(
                ordered,
                context.goal_contract,
            )
    post_action_verification = _post_action_verification_request(
        context.tool_name,
        context.tool_request,
        tool_result if isinstance(tool_result, Mapping) else {},
        allowed_tools=context.allowed_tools,
        remaining_requests=[],
        active_window_target=None,
        timeline=context.timeline,
    )
    if existing:
        if not post_action_verification:
            return existing
        # The approved action executes outside the ordinary request runner.
        # Verify that exact action before continuing with later actions. A
        # single verifier must not be stretched across heterogeneous effects
        # such as focus + type/copy/click, and a later approval must only be
        # offered after its ancestor's own postcondition has been observed.
        return _approval_resume_bind_goal_contract_identity(
            [post_action_verification, *existing],
            context.goal_contract,
        )
    if post_action_verification:
        return _approval_resume_bind_goal_contract_identity(
            [post_action_verification],
            context.goal_contract,
        )
    if not _approved_workspace_patch_step(context, tool_result):
        return []
    verification = _pending_verification_request_after_patch(
        context.timeline,
        allowed_tools=context.allowed_tools,
    )
    return _approval_resume_bind_goal_contract_identity(
        [verification] if verification else [],
        context.goal_contract,
    )


def _approval_resume_bind_goal_contract_identity(
    requests: list[dict[str, Any]],
    goal_contract: Any,
) -> list[dict[str, Any]]:
    """Restore exact criterion identity from the persisted immutable contract."""

    contract_id = str(getattr(goal_contract, "contract_id", "") or "").strip()
    criteria = tuple(getattr(goal_contract, "criteria", ()) or ())
    if not contract_id or not criteria:
        return requests
    bound: list[dict[str, Any]] = []
    for request in requests:
        item = dict(request)
        existing_contract_id = str(item.get("goal_contract_id") or "").strip()
        if existing_contract_id and existing_contract_id != contract_id:
            raise AgentRuntimeError("approval_resume_goal_contract_conflict")
        request_step_ids = {
            str(item.get("step_id") or item.get("planner_step_id") or "").strip(),
            str(item.get("source_step_id") or "").strip(),
            *(
                str(value or "").strip()
                for value in item.get("verification_target_step_ids", [])
                if str(value or "").strip()
            ),
        }
        request_step_ids.discard("")
        matching = [
            criterion
            for criterion in criteria
            if request_step_ids
            & {
                *tuple(getattr(criterion, "source_step_ids", ()) or ()),
                *tuple(getattr(criterion, "verifier_step_ids", ()) or ()),
            }
        ]
        if len(matching) > 1:
            raise AgentRuntimeError("approval_resume_goal_criterion_ambiguous")
        if matching:
            criterion_id = str(
                getattr(matching[0], "criterion_id", "") or ""
            ).strip()
            existing_criterion_id = str(
                item.get("goal_criterion_id") or ""
            ).strip()
            if existing_criterion_id and existing_criterion_id != criterion_id:
                raise AgentRuntimeError("approval_resume_goal_criterion_conflict")
            item["goal_contract_id"] = contract_id
            item["goal_criterion_id"] = criterion_id
            item["root_goal_unchanged"] = True
        elif existing_contract_id:
            # A pre-bound request must still name a criterion from this exact
            # contract even when it is an auxiliary dependency step.
            existing_criterion_id = str(item.get("goal_criterion_id") or "").strip()
            if not any(
                str(getattr(criterion, "criterion_id", "") or "").strip()
                == existing_criterion_id
                for criterion in criteria
            ):
                raise AgentRuntimeError("approval_resume_goal_criterion_conflict")
        bound.append(item)
    return bound


def _approval_resume_exact_approved_request_duplicate(
    candidate: Mapping[str, Any],
    approved_request: Mapping[str, Any],
) -> bool:
    """Drop only a replay of the same approved action generation."""

    if approval_request_fingerprint(candidate) != approval_request_fingerprint(
        approved_request
    ):
        return False
    for key in ("request_id", "planner_request_id", "tool_call_id"):
        approved_identity = str(approved_request.get(key) or "").strip()
        if approved_identity and approved_identity == str(candidate.get(key) or "").strip():
            return True
    approved_plan_id = str(approved_request.get("plan_id") or "").strip()
    approved_step_id = str(
        approved_request.get("step_id")
        or approved_request.get("planner_step_id")
        or ""
    ).strip()
    candidate_step_id = str(
        candidate.get("step_id") or candidate.get("planner_step_id") or ""
    ).strip()
    return bool(
        approved_plan_id
        and approved_step_id
        and approved_plan_id == str(candidate.get("plan_id") or "").strip()
        and approved_step_id == candidate_step_id
    )


def _approval_resume_exact_verifier_index(
    requests: list[dict[str, Any]],
    approved_request: Mapping[str, Any],
) -> int | None:
    source_step_id = str(
        approved_request.get("step_id")
        or approved_request.get("planner_step_id")
        or ""
    ).strip()
    if not source_step_id:
        return None
    source_request_id = str(approved_request.get("request_id") or "").strip()
    for index, request in enumerate(requests):
        source = str(request.get("source") or "").strip()
        runtime_stage = str(request.get("runtime_stage") or "").strip()
        runtime_role = str(request.get("runtime_role") or "").strip()
        if not (
            source in {"runtime_post_action_auto_verify", "runtime_verification"}
            or runtime_stage == "verify"
            or runtime_role == "verify_result"
        ):
            continue
        verifier_request_id = str(request.get("source_request_id") or "").strip()
        if (
            source_request_id
            and verifier_request_id
            and verifier_request_id != source_request_id
        ):
            continue
        if _approval_resume_verifier_target_step_ids(request) == {source_step_id}:
            return index
    return None


def _approval_resume_verifier_target_step_ids(request: Mapping[str, Any]) -> set[str]:
    step_ids = {
        str(request.get("source_step_id") or "").strip(),
        *(
            str(value or "").strip()
            for value in request.get("depends_on") or []
            if not isinstance(value, Mapping)
        ),
    }
    for key in ("verification_targets", "task_verification_targets"):
        values = request.get(key)
        if not isinstance(values, list):
            continue
        step_ids.update(
            str(value.get("step_id") or "").strip()
            for value in values
            if isinstance(value, Mapping)
        )
    desktop_loop = request.get("desktop_loop")
    if isinstance(desktop_loop, Mapping):
        target_ids = desktop_loop.get("verification_target_step_ids")
        if isinstance(target_ids, list):
            step_ids.update(str(value or "").strip() for value in target_ids)
    return {value for value in step_ids if value}


def _approval_resume_tool_result_blocks_plan_continuation(tool_result: Any) -> bool:
    if not isinstance(tool_result, Mapping):
        return True
    if tool_result.get("approval_required"):
        return False
    if tool_result.get("ok") is False or tool_result.get("error"):
        return True
    status = str(tool_result.get("status") or "").strip().lower()
    return status in {
        "blocked",
        "cancelled",
        "canceled",
        "error",
        "failed",
        "failure",
        "handoff_required",
        "preview_required",
        "provider_required",
        "rejected",
        "unavailable",
    }


def _approval_resume_tool_result_event_type(result: Mapping[str, Any]) -> str:
    if result.get("blocked_by_desktop_execution_policy"):
        return "agent.tool.skipped"
    status = str(result.get("status") or "").strip().lower()
    if status in {"handoff_required", "preview_required", "provider_required"}:
        return "agent.tool.skipped"
    return "agent.tool.failed"


def _resumable_remaining_requests(
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    return [
        request
        for request in requests
        if str(request.get("status") or "").strip()
        not in _NON_RESUMABLE_REMAINING_REQUEST_STATUSES
    ]


def _approval_resume_has_pending_replan_request(context: ToolApprovalResumeContext) -> bool:
    return _approval_resume_events_have_pending_replan_request(context.timeline)


def _approval_resume_events_have_pending_replan_request(events: Any) -> bool:
    for event in reversed(list(events or [])):
        if not isinstance(event, Mapping):
            continue
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        if _approval_resume_replan_event_type(event_type) != "agent.replan.requested":
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        status = str(event.get("status") or payload.get("status") or "requested").strip()
        if status in {"", "requested", "pending"}:
            return True
    return False


def _daily_desktop_resume_result_after_remaining_tools(
    context: ToolApprovalResumeContext,
    *,
    resume_timeline_start: int = 0,
) -> str:
    if not _daily_desktop_resume_context(context):
        return ""
    current_events = context.timeline[max(0, int(resume_timeline_start or 0)) :]
    if _approval_resume_events_have_pending_replan_request(current_events):
        return ""
    if not _daily_desktop_successful_action_events(current_events):
        return ""
    goal_gate = evaluate_goal_contract_outcome(
        {"run_id": context.run_id},
        context.timeline,
    )
    if goal_gate is not None and not goal_gate.allows_completion:
        return ""
    if not evaluate_main_chat_outcome({}, current_events).allows_completion:
        return ""
    phrases: list[str] = []
    seen: set[str] = set()
    for event in _daily_desktop_successful_action_events(context.timeline):
        result = event["result"]
        tool_name = str(event.get("detail") or result.get("tool") or "").strip()
        input_preview = (
            event.get("input_preview")
            if isinstance(event.get("input_preview"), Mapping)
            else {}
        )
        phrase = _daily_desktop_tool_result_phrase(
            tool_name,
            result,
            input_preview=input_preview,
        )
        if not phrase or phrase in seen:
            continue
        seen.add(phrase)
        phrases.append(phrase)
    return " ".join(phrases).strip()


def _daily_desktop_successful_action_events(events: Any) -> list[Mapping[str, Any]]:
    successful: list[Mapping[str, Any]] = []
    for event in _approval_resume_deduped_tool_events(events or []):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != "agent.tool.call":
            continue
        if str(event.get("source") or "").strip() == "runtime_post_action_auto_verify":
            continue
        if str(event.get("runtime_stage") or "").strip() in {
            "discover",
            "observe",
            "verify",
        }:
            continue
        result = event.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            continue
        tool_name = str(
            event.get("detail")
            or result.get("tool")
            or result.get("action")
            or ""
        ).strip()
        if tool_name in READ_ONLY_OBSERVATION_TOOLS:
            continue
        successful.append(event)
    return successful


def _approval_resume_deduped_tool_events(events: Any) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    for event in events or []:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event") or event.get("event_type") or "").strip()
        if event_type not in {
            "agent.tool.call",
            "agent.tool.failed",
            "agent.tool.skipped",
        }:
            continue
        if event.get("approval_resume_result_canonical") is True and any(
            _approval_resume_canonical_event_duplicates(event, previous)
            for previous in reversed(deduped)
        ):
            continue
        deduped.append(event)
    return deduped


def _approval_resume_canonical_event_duplicates(
    canonical: Mapping[str, Any],
    previous: Mapping[str, Any],
) -> bool:
    if previous.get("approval_resume_result_canonical") is True:
        return False
    if str(previous.get("event") or previous.get("event_type") or "").strip() != (
        "agent.tool.call"
    ):
        return False
    canonical_tool = str(
        canonical.get("tool") or canonical.get("detail") or ""
    ).strip()
    previous_tool = str(
        previous.get("tool") or previous.get("detail") or ""
    ).strip()
    if not canonical_tool or canonical_tool != previous_tool:
        return False
    if canonical.get("result") != previous.get("result"):
        return False
    for key in _APPROVAL_RESUME_RESULT_IDENTITY_KEYS:
        canonical_value = str(canonical.get(key) or "").strip()
        previous_value = str(previous.get(key) or "").strip()
        if canonical_value and previous_value and canonical_value != previous_value:
            return False
    return True


def _daily_desktop_successful_verification_events(
    events: Any,
) -> list[Mapping[str, Any]]:
    successful: list[Mapping[str, Any]] = []
    for event in events or []:
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or event.get("event_type") or "").strip() != (
            "agent.tool.call"
        ):
            continue
        is_verification = (
            str(event.get("source") or "").strip()
            == "runtime_post_action_auto_verify"
            or str(event.get("runtime_stage") or "").strip() == "verify"
            or str(event.get("runtime_role") or "").strip() == "verify_result"
        )
        result = event.get("result")
        if not is_verification or not isinstance(result, Mapping):
            continue
        if result.get("ok") is True:
            successful.append(event)
    return successful


def _daily_desktop_native_receipt_verification_evidence(
    context: ToolApprovalResumeContext,
    verification_events: Any,
) -> dict[str, Any]:
    """Project only an exact Runtime-owned receipt as public verification.

    A successful observation or an ``ok`` tool result is deliberately
    insufficient.  The verifier must be the one declared by the immutable
    GoalContract and must bind to the exact approved action invocation.
    """

    source_request = context.tool_request
    source_tool = str(context.tool_name or source_request.get("tool") or "").strip()
    source_request_id = str(source_request.get("request_id") or "").strip()
    source_tool_call_id = str(source_request.get("tool_call_id") or "").strip()
    source_step_id = str(
        source_request.get("step_id")
        or source_request.get("planner_step_id")
        or ""
    ).strip()
    source_plan_id = str(source_request.get("plan_id") or "").strip()
    contract = context.goal_contract
    contract_id = str(getattr(contract, "contract_id", "") or "").strip()
    contract_run_id = str(getattr(contract, "run_id", "") or "").strip()
    if not all(
        (
            context.run_id,
            source_tool,
            source_request_id,
            source_tool_call_id,
            source_step_id,
            source_plan_id,
            contract_id,
            contract_run_id,
        )
    ) or contract_run_id != context.run_id:
        return {}

    source_contract_id = str(source_request.get("goal_contract_id") or "").strip()
    source_criterion_id = str(source_request.get("goal_criterion_id") or "").strip()
    if source_contract_id and source_contract_id != contract_id:
        return {}

    candidates = {
        id(event)
        for event in verification_events or []
        if isinstance(event, Mapping)
    }
    if not candidates:
        return {}
    timeline = [event for event in context.timeline if isinstance(event, Mapping)]
    for verifier_index in range(len(timeline) - 1, -1, -1):
        verifier_event = timeline[verifier_index]
        if id(verifier_event) not in candidates:
            continue
        evidence = _daily_desktop_exact_native_receipt_evidence(
            context,
            verifier_event,
            timeline[:verifier_index],
            contract_id=contract_id,
            source_criterion_id=source_criterion_id,
            source_tool=source_tool,
            source_request_id=source_request_id,
            source_tool_call_id=source_tool_call_id,
            source_step_id=source_step_id,
            source_plan_id=source_plan_id,
        )
        if evidence:
            return evidence
    return {}


def _daily_desktop_exact_native_receipt_evidence(
    context: ToolApprovalResumeContext,
    verifier_event: Mapping[str, Any],
    prior_events: list[Mapping[str, Any]],
    *,
    contract_id: str,
    source_criterion_id: str,
    source_tool: str,
    source_request_id: str,
    source_tool_call_id: str,
    source_step_id: str,
    source_plan_id: str,
) -> dict[str, Any]:
    event_type = str(
        verifier_event.get("event") or verifier_event.get("event_type") or ""
    ).strip()
    verifier_tool = str(
        verifier_event.get("detail") or verifier_event.get("tool") or ""
    ).strip()
    verifier_step_id = str(
        verifier_event.get("step_id")
        or verifier_event.get("planner_step_id")
        or ""
    ).strip()
    verifier_request_id = str(verifier_event.get("request_id") or "").strip()
    verifier_tool_call_id = str(verifier_event.get("tool_call_id") or "").strip()
    result = (
        verifier_event.get("result")
        if isinstance(verifier_event.get("result"), Mapping)
        else {}
    )
    if not (
        event_type == "agent.tool.call"
        and verifier_tool
        and verifier_step_id
        and verifier_request_id
        and verifier_tool_call_id
        and verifier_request_id != source_request_id
        and verifier_tool_call_id != source_tool_call_id
        and str(verifier_event.get("source") or "").strip()
        == "runtime_native_postcondition_receipt"
        and str(verifier_event.get("actor") or "").strip() == "native_runtime"
        and str(verifier_event.get("execution_authority") or "").strip()
        == "runtime_tool_executor"
        and str(verifier_event.get("execution_mode") or "").strip()
        == "native_postcondition_receipt_projection"
        and str(verifier_event.get("visibility") or "").strip() == "internal"
        and str(verifier_event.get("run_id") or "").strip() == context.run_id
        and str(verifier_event.get("plan_id") or "").strip() == source_plan_id
        and str(verifier_event.get("source_request_id") or "").strip()
        == source_request_id
        and str(verifier_event.get("source_tool_call_id") or "").strip()
        == source_tool_call_id
        and str(verifier_event.get("source_step_id") or "").strip()
        == source_step_id
        and result.get("ok") is True
        and result.get("postcondition_verified") is True
        and result.get("verification_satisfied_by_native_receipt") is True
        and not result.get("approval_required")
        and result.get("permission_error") is not True
        and result.get("verification_failed") is not True
        and str(result.get("action") or "").strip() == verifier_tool
        and str(result.get("source_tool") or "").strip() == source_tool
        and str(result.get("source_tool_call_id") or "").strip()
        == source_tool_call_id
        and str(result.get("source_step_id") or "").strip() == source_step_id
    ):
        return {}

    expected_predicate = _post_action_verification_predicate_kind(source_tool)
    claimed_predicate = str(
        result.get("verification_predicate_kind")
        or verifier_event.get("verification_predicate_kind")
        or ""
    ).strip()
    if claimed_predicate != expected_predicate:
        return {}

    matching_criteria = [
        item
        for item in tuple(getattr(context.goal_contract, "criteria", ()) or ())
        if source_step_id in tuple(getattr(item, "source_step_ids", ()) or ())
        and verifier_step_id in tuple(getattr(item, "verifier_step_ids", ()) or ())
    ]
    if len(matching_criteria) != 1:
        return {}
    criterion_id = str(
        getattr(matching_criteria[0], "criterion_id", "") or ""
    ).strip()
    if not criterion_id or (
        source_criterion_id and source_criterion_id != criterion_id
    ):
        return {}
    for claimed_contract_id in (
        verifier_event.get("goal_contract_id"),
        result.get("goal_contract_id"),
    ):
        clean_claim = str(claimed_contract_id or "").strip()
        if clean_claim and clean_claim != contract_id:
            return {}
    for claimed_criterion_id in (
        verifier_event.get("goal_criterion_id"),
        result.get("goal_criterion_id"),
    ):
        clean_claim = str(claimed_criterion_id or "").strip()
        if clean_claim and clean_claim != criterion_id:
            return {}
    if (
        "root_goal_unchanged" in verifier_event
        and verifier_event.get("root_goal_unchanged") is not True
    ):
        return {}

    source_event = next(
        (
            event
            for event in reversed(prior_events)
            if _daily_desktop_exact_source_action_event(
                event,
                context=context,
                contract_id=contract_id,
                criterion_id=criterion_id,
                source_tool=source_tool,
                source_request_id=source_request_id,
                source_tool_call_id=source_tool_call_id,
                source_step_id=source_step_id,
                source_plan_id=source_plan_id,
            )
        ),
        None,
    )
    if source_event is None:
        return {}
    source_result = (
        source_event.get("result")
        if isinstance(source_event.get("result"), Mapping)
        else {}
    )
    source_providers = _daily_desktop_provider_identities(source_event, source_result)
    verifier_providers = _daily_desktop_provider_identities(verifier_event, result)
    if len(source_providers) != 1 or (
        verifier_providers and verifier_providers != source_providers
    ):
        return {}

    provider_kind, provider_id = next(iter(source_providers))
    verification_result = {
        "ok": True,
        "postcondition_verified": True,
        "verification_satisfied_by_native_receipt": True,
    }
    return {
        "verification_status": "verified",
        "verification_tool": verifier_tool,
        "verification_result": verification_result,
        "verification_step_id": verifier_step_id,
        "verification_request_id": verifier_request_id,
        "verification_tool_call_id": verifier_tool_call_id,
        "verification_source_request_id": source_request_id,
        "verification_source_tool_call_id": source_tool_call_id,
        "verification_source_step_id": source_step_id,
        "verification_plan_id": source_plan_id,
        "verification_provider_kind": provider_kind,
        "verification_provider_id": provider_id,
        "goal_contract_id": contract_id,
        "goal_criterion_id": criterion_id,
        "receipt_status": "satisfied",
        "verification_satisfied_by_native_receipt": True,
    }


def _daily_desktop_exact_source_action_event(
    event: Mapping[str, Any],
    *,
    context: ToolApprovalResumeContext,
    contract_id: str,
    criterion_id: str,
    source_tool: str,
    source_request_id: str,
    source_tool_call_id: str,
    source_step_id: str,
    source_plan_id: str,
) -> bool:
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    return bool(
        str(event.get("event") or event.get("event_type") or "").strip()
        == "agent.tool.call"
        and event.get("approval_resume_result_canonical") is not True
        and event.get("approved") is True
        and str(event.get("detail") or event.get("tool") or "").strip()
        == source_tool
        and str(event.get("request_id") or "").strip() == source_request_id
        and str(event.get("tool_call_id") or "").strip() == source_tool_call_id
        and str(event.get("step_id") or event.get("planner_step_id") or "").strip()
        == source_step_id
        and str(event.get("plan_id") or "").strip() == source_plan_id
        and str(event.get("run_id") or "").strip() == context.run_id
        and str(event.get("actor") or "").strip() == "native_runtime"
        and str(event.get("execution_authority") or "").strip()
        == "runtime_tool_executor"
        and result.get("ok") is True
        and not result.get("approval_required")
        and (
            not str(event.get("goal_contract_id") or "").strip()
            or str(event.get("goal_contract_id") or "").strip() == contract_id
        )
        and (
            not str(event.get("goal_criterion_id") or "").strip()
            or str(event.get("goal_criterion_id") or "").strip() == criterion_id
        )
        and (
            "root_goal_unchanged" not in event
            or event.get("root_goal_unchanged") is True
        )
    )


def _daily_desktop_provider_identities(
    event: Mapping[str, Any],
    result: Mapping[str, Any],
) -> set[tuple[str, str]]:
    identities: set[tuple[str, str]] = set()
    sources: list[Mapping[str, Any]] = [event, result]
    for container in (event, result):
        for key in (
            "desktop_execution_evidence",
            "desktop_execution_provider",
            "desktop_execution_route",
            "local_desktop_provider",
            "sandbox_provider",
        ):
            value = container.get(key)
            if isinstance(value, Mapping):
                sources.append(value)
    for source in sources:
        provider_kind = str(
            source.get("provider_kind")
            or source.get("selected_provider_kind")
            or ""
        ).strip()
        provider_id = str(
            source.get("provider_id")
            or source.get("selected_provider_id")
            or ""
        ).strip()
        if provider_kind and provider_id:
            identities.add((provider_kind, provider_id))
    return identities


def _daily_desktop_completion_step(event: Mapping[str, Any]) -> dict[str, Any]:
    result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
    step = {
        "tool": str(event.get("detail") or result.get("tool") or "").strip(),
        "input_preview": dict(event.get("input_preview"))
        if isinstance(event.get("input_preview"), Mapping)
        else {},
        "result": dict(result),
    }
    for key in (
        "request_id",
        "source_request_id",
        "step_id",
        "source_step_id",
        "runtime_stage",
        "runtime_role",
        "verification_target",
        "verification_targets",
        "task_verification_targets",
    ):
        value = event.get(key)
        if value not in (None, "", [], {}):
            step[key] = deepcopy(value)
    return step


def _daily_desktop_resume_context(context: ToolApprovalResumeContext) -> bool:
    source = str(context.tool_request.get("source") or "").strip()
    if source not in {"daily_desktop_intent", "runtime_planner", "daily_desktop_metadata"}:
        return False
    return _daily_desktop_tool_name(context.tool_name)


def _daily_desktop_tool_name(tool_name: str) -> bool:
    clean = str(tool_name or "").strip()
    return bool(
        clean == "terminal.run"
        or clean.startswith("app.")
        or clean.startswith("desktop.")
        or clean.startswith("media.")
        or clean.startswith("browser.")
    )


def _daily_desktop_tool_result_phrase(
    tool_name: str,
    result: Mapping[str, Any],
    *,
    input_preview: Mapping[str, Any] | None = None,
) -> str:
    clean_tool = str(tool_name or result.get("action") or "").strip()
    reported_action = str(result.get("action") or "").strip()
    action = reported_action or clean_tool
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if clean_tool == "terminal.run":
        command = str((input_preview or {}).get("command") or "").strip()
        command_text = f"：{command}" if command else ""
        stdout = str(result.get("stdout") or "").strip()
        if stdout:
            output = " ".join(stdout.split())[:500]
            return f"已运行命令{command_text}。\n输出：{output}"
        return f"已运行命令{command_text}。"
    if clean_tool == "browser.open_url" or action == "browser.open_url":
        url = _daily_desktop_public_phrase_value(
            data.get("url")
            or data.get("final_url")
            or result.get("url")
            or (input_preview or {}).get("url"),
            limit=500,
        )
        return f"已打开网页：{url}。" if url else "已发送打开网页指令。"
    if clean_tool == "browser.click" or action == "browser.click":
        label = _daily_desktop_public_phrase_value(
            data.get("matched_label")
            or data.get("label")
            or data.get("accessible_name")
            or result.get("matched_label")
            or result.get("label"),
        )
        selector = _daily_desktop_public_phrase_value(
            data.get("selector")
            or result.get("selector")
            or (input_preview or {}).get("selector"),
        )
        point = _daily_desktop_browser_point_label(selector)
        if point:
            return f"已点击网页位置：{point}。"
        target = label or selector
        return f"已点击网页元素：{target}。" if target else "已发送网页点击指令。"
    if clean_tool == "browser.type_text" or action == "browser.type_text":
        length = data.get("length")
        if not isinstance(length, int):
            length = result.get("length")
        if not isinstance(length, int):
            text = (input_preview or {}).get("text")
            length = len(text) if isinstance(text, str) else None
        selector = _daily_desktop_public_phrase_value(
            data.get("selector")
            or result.get("selector")
            or (input_preview or {}).get("selector"),
        )
        point = _daily_desktop_browser_point_label(selector)
        target = _daily_desktop_browser_target_label(selector)
        if isinstance(length, int) and length >= 0:
            if point:
                return f"已在网页位置：{point} 输入文字（{length} 个字符）。"
            if target:
                return f"已在网页{target}输入文字（{length} 个字符）。"
            return f"已向网页元素输入文字（{length} 个字符）。"
        if point:
            return f"已在网页位置：{point} 输入文字。"
        if target:
            return f"已在网页{target}输入文字。"
        return "已向网页元素输入文字。"
    if clean_tool in {
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
    } or action in {
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
    }:
        composite_action = clean_tool if clean_tool.startswith("app.") else action
        app_name = str(data.get("app_name") or result.get("app_name") or "").strip()
        key = str(data.get("key") or result.get("key") or "").strip()
        modifiers = data.get("modifiers") if isinstance(data.get("modifiers"), list) else []
        hotkey = _daily_desktop_hotkey_text(key, modifiers) or key
        verb = "打开" if composite_action == "app.open_and_hotkey" else "聚焦"
        target = f" {app_name} " if app_name else "应用"
        return f"已{verb}{target}并发送快捷键：{hotkey}。"
    if clean_tool in {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
    } or action in {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
    }:
        composite_action = clean_tool if clean_tool.startswith("app.") else action
        app_name = str(data.get("app_name") or result.get("app_name") or "").strip()
        label = str(data.get("matched_label") or data.get("target") or "").strip()
        x = data.get("x")
        y = data.get("y")
        point = f"（{x}, {y}）" if x not in (None, "") and y not in (None, "") else ""
        verb = "打开" if composite_action.startswith("app.open") else "切到"
        target = f" {app_name} " if app_name else "应用"
        control = (
            f"“{label}”{point}"
            if label and composite_action.startswith("app.open")
            else f"前台控件：{label}{point}"
            if label
            else f"前台控件{point}"
        )
        return f"已{verb}{target}并点击{control}。"
    if clean_tool == "desktop.click_ui_element" or action == "desktop.click_ui_element":
        label = str(
            data.get("matched_label")
            or data.get("target")
            or result.get("matched_label")
            or result.get("target")
            or ""
        ).strip()
        return f"已点击前台控件：{label}。" if label else "已点击前台控件。"
    if clean_tool in {
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.type_into_ui_element",
    } or action in {
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.type_into_ui_element",
    }:
        app_name = str(
            data.get("app_name")
            or data.get("expected_app_name")
            or result.get("app_name")
            or ""
        ).strip()
        label = str(data.get("matched_label") or data.get("target") or "").strip()
        character_count = data.get("character_count")
        if app_name and label:
            location = f"{app_name} 的 {label}"
        elif app_name:
            location = app_name
        elif label:
            location = f"前台控件 {label}"
        else:
            location = "前台控件"
        count_text = ""
        if isinstance(character_count, int) and character_count >= 0:
            count_text = f"（{character_count} 个字符）"
        prefix = f"已在 {location}" if app_name else f"已在{location}"
        return f"{prefix} 输入文字{count_text}。"
    if clean_tool in {"desktop.hotkey", "desktop.shortcut"} or action == "desktop.hotkey":
        key = str(data.get("key") or result.get("key") or "").strip()
        modifiers = data.get("modifiers") if isinstance(data.get("modifiers"), list) else []
        combo = _daily_desktop_hotkey_text(key, modifiers)
        return f"已发送快捷键：{combo or key}。"
    if clean_tool == "desktop.safe_type_text" or action == "desktop.safe_type_text":
        character_count = data.get("character_count")
        if not isinstance(character_count, int):
            text = str(data.get("text") or result.get("text") or "")
            character_count = len(text) if text else None
        if isinstance(character_count, int) and character_count >= 0:
            return f"已向前台输入文字（{character_count} 个字符）。"
        return "已向前台输入文字。"
    if clean_tool == "desktop.safe_shortcut" or action == "desktop.safe_shortcut":
        shortcut_action = str(
            data.get("shortcut_action") or result.get("shortcut_action") or ""
        ).strip()
        label = {
            "copy": "已发送复制快捷键。",
            "paste": "已发送粘贴快捷键。",
            "select_all": "已发送全选快捷键。",
            "undo": "已发送撤销快捷键。",
            "redo": "已发送重做快捷键。",
            "find": "已发送打开查找快捷键。",
        }.get(shortcut_action)
        return label or "已发送快捷动作。"
    if clean_tool in {"app.open", "desktop.open_app"} or action in {"app.open", "desktop.open_app"}:
        app_name = str(data.get("app_name") or result.get("app_name") or "").strip()
        return f"已打开 {app_name}。" if app_name else "已打开应用。"
    if clean_tool in {"app.focus", "desktop.focus_app"} or action in {"app.focus", "desktop.focus_app"}:
        app_name = str(data.get("app_name") or result.get("app_name") or "").strip()
        return f"已聚焦 {app_name}。" if app_name else ""
    if clean_tool == "app.quit" or action == "app.quit":
        app_name = str(data.get("app_name") or result.get("app_name") or "").strip()
        return f"已退出 {app_name}。" if app_name else "已退出应用。"
    if clean_tool == "desktop.quit_app" or action == "desktop.quit_app":
        return "已请求退出当前应用。"
    if clean_tool == "desktop.search_submit" or action == "desktop.search_submit":
        return "已提交前台搜索。"
    if clean_tool == "desktop.submit_foreground" or action == "desktop.submit_foreground":
        submit_action = str(
            data.get("submit_action") or result.get("submit_action") or ""
        ).strip()
        if submit_action == "send":
            return "已向前台发送“发送”指令。"
        if submit_action == "confirm":
            return "已向前台发送确认指令。"
        if submit_action == "submit":
            return "已向前台发送提交指令。"
        return "已向前台发送提交按键。"
    if reported_action:
        return str(result.get("summary") or "").strip()
    return ""


def _daily_desktop_public_phrase_value(
    value: Any,
    *,
    limit: int = 240,
) -> str:
    clean = " ".join(str(value or "").split()).strip()
    return clean[: max(1, int(limit or 1))]


def _daily_desktop_browser_point_label(selector: Any) -> str:
    clean = str(selector or "").strip()
    if not clean.startswith("point="):
        return ""
    coordinates = clean.removeprefix("point=").split(",")
    if len(coordinates) != 2:
        return ""
    labels: list[str] = []
    for coordinate in coordinates:
        try:
            value = float(coordinate.strip())
        except (TypeError, ValueError):
            return ""
        if not math.isfinite(value):
            return ""
        labels.append(str(int(value)) if value.is_integer() else f"{value:g}")
    return ", ".join(labels)


def _daily_desktop_browser_target_label(selector: Any) -> str:
    clean = " ".join(str(selector or "").split()).strip().lower()
    search_markers = (
        'input[type="search"]',
        'input[name="q"]',
        'textarea[name="q"]',
        'aria-label*="搜索"',
        'placeholder*="搜索"',
        'aria-label*="search"',
        'placeholder*="search"',
    )
    if any(marker in clean for marker in search_markers):
        return "搜索框"
    return ""


def _daily_desktop_hotkey_text(key: str, modifiers: list[Any]) -> str:
    modifier_labels = {
        "command": "Command",
        "control": "Control",
        "option": "Option",
        "shift": "Shift",
    }
    parts = [
        modifier_labels.get(str(item).strip().lower(), str(item).strip())
        for item in modifiers
        if str(item).strip()
    ]
    clean_key = str(key or "").strip()
    parts.append(clean_key.upper() if len(clean_key) == 1 else clean_key)
    return "+".join(part for part in parts if part)


def _approved_workspace_patch_step(
    context: ToolApprovalResumeContext,
    tool_result: Any,
) -> bool:
    if str(context.tool_name or "").strip() != "workspace.write_patch":
        return False
    result = tool_result if isinstance(tool_result, Mapping) else {}
    if result.get("ok") is not True:
        return False
    step_id = str(context.tool_request.get("step_id") or "").strip()
    capability_id = str(context.tool_request.get("capability_id") or "").strip()
    return step_id == "apply-code-changes" or capability_id == "file.workspace_write"


def _pending_verification_request_after_patch(
    timeline: list[dict[str, Any]],
    *,
    allowed_tools: list[str],
) -> dict[str, Any]:
    if "terminal.run" not in {str(tool or "").strip() for tool in allowed_tools}:
        return {}
    for event in reversed(timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or "").strip() != "agent.model.followup_context":
            continue
        payload = _timeline_payload(event)
        steps = payload.get("pending_plan_steps")
        if not isinstance(steps, list):
            continue
        for step in steps:
            if not isinstance(step, Mapping):
                continue
            if str(step.get("step_id") or "").strip() != "verify-code-changes":
                continue
            if str(step.get("tool_name") or "").strip() != "terminal.run":
                continue
            depends_on = [
                str(item or "").strip()
                for item in step.get("depends_on", [])
                if str(item or "").strip()
            ] if isinstance(step.get("depends_on"), list) else []
            if "apply-code-changes" not in depends_on:
                continue
            raw_input = (
                step.get("input_preview")
                if isinstance(step.get("input_preview"), Mapping)
                else {}
            )
            command = str(raw_input.get("command") or "").strip()
            if not command:
                continue
            request = {
                "protocol": "json_fallback",
                "tool": "terminal.run",
                "input": {"command": command},
                "source": "runtime_planner",
                "planning_reason": "planner_followup_verify_code_changes",
                "continue_to_model": True,
                "step_id": "verify-code-changes",
                "capability_id": "terminal.execution",
            }
            return request
    return {}


def _latest_task_core_context(timeline: list[dict[str, Any]]) -> dict[str, Any]:
    for event in reversed(timeline):
        if not isinstance(event, Mapping):
            continue
        if str(event.get("event") or "").strip() != "agent.task_core.created":
            continue
        payload = _timeline_payload(event)
        task_core = (
            payload.get("task_core")
            if isinstance(payload.get("task_core"), Mapping)
            else {}
        )
        workspace = (
            task_core.get("workspace")
            if isinstance(task_core.get("workspace"), Mapping)
            else {}
        )
        return {
            "task_core": task_core,
            "decision_id": str(payload.get("decision_id") or "").strip(),
            "plan_id": str(payload.get("plan_id") or "").strip(),
            "core_id": str(payload.get("core_id") or task_core.get("core_id") or "").strip(),
            "workspace_id": str(workspace.get("workspace_id") or "").strip(),
            **{
                key: str(payload.get(key) or "").strip()
                for key in (
                    "task_id",
                    "run_group_id",
                    "group_run_id",
                    "group_id",
                    "workflow_id",
                    "workflow_run_id",
                    "workflow_node_id",
                    "workflow_node_label",
                )
                if str(payload.get(key) or "").strip()
            },
        }
    return {}


def _latest_plan_steps(
    timeline: list[dict[str, Any]],
    *,
    decision_id: str,
    plan_id: str,
) -> dict[str, Mapping[str, Any]]:
    steps: dict[str, Mapping[str, Any]] = {}
    for event in timeline:
        if not isinstance(event, Mapping):
            continue
        event_name = str(event.get("event") or "").strip()
        payload = _timeline_payload(event)
        if not _same_plan(payload, decision_id=decision_id, plan_id=plan_id):
            continue
        if event_name == "agent.plan.created":
            plan = payload.get("plan") if isinstance(payload.get("plan"), Mapping) else {}
            tool_plan = (
                plan.get("tool_plan")
                if isinstance(plan.get("tool_plan"), Mapping)
                else {}
            )
            for step in tool_plan.get("steps", []):
                if not isinstance(step, Mapping):
                    continue
                step_id = str(step.get("step_id") or "").strip()
                if step_id:
                    steps[step_id] = step
        elif event_name == "agent.plan.step":
            step = payload.get("step") if isinstance(payload.get("step"), Mapping) else {}
            step_id = str(step.get("step_id") or "").strip()
            if step_id:
                steps[step_id] = step
    return steps


def _todo_progress_event(
    timeline: list[dict[str, Any]],
    todo: Mapping[str, Any],
    base_payload: Mapping[str, Any],
    status: str,
) -> tuple[str, str, dict[str, Any]]:
    step_id = str(base_payload.get("step_id") or "").strip()
    previous_status = _latest_task_update_status(
        timeline,
        "agent.task.todo.updated",
        "step_id",
        step_id,
        decision_id=str(base_payload.get("decision_id") or "").strip(),
    ) or str(todo.get("status") or "pending")
    todo_payload = deepcopy(dict(todo))
    todo_payload["status"] = status
    payload = {
        **dict(base_payload),
        "todo_id": str(todo.get("todo_id") or "").strip(),
        "status": status,
        "previous_status": previous_status,
        "todo": todo_payload,
    }
    return (
        "agent.task.todo.updated",
        str(todo.get("title") or step_id),
        payload,
    )


def _checkpoint_progress_event(
    timeline: list[dict[str, Any]],
    checkpoint: Mapping[str, Any],
    base_payload: Mapping[str, Any],
    status: str,
) -> tuple[str, str, dict[str, Any]]:
    step_id = str(base_payload.get("step_id") or "").strip()
    checkpoint_id = str(checkpoint.get("checkpoint_id") or "").strip()
    previous_status = _latest_task_update_status(
        timeline,
        "agent.task.checkpoint.updated",
        "checkpoint_id",
        checkpoint_id,
        decision_id=str(base_payload.get("decision_id") or "").strip(),
    ) or str(checkpoint.get("status") or "planned")
    checkpoint_payload = deepcopy(dict(checkpoint))
    checkpoint_payload["status"] = status
    payload = {
        **dict(base_payload),
        "checkpoint_id": checkpoint_id,
        "status": status,
        "previous_status": previous_status,
        "checkpoint": checkpoint_payload,
    }
    return (
        "agent.task.checkpoint.updated",
        str(checkpoint.get("title") or step_id),
        payload,
    )


def _latest_task_update_status(
    timeline: list[dict[str, Any]],
    event_type: str,
    identity_key: str,
    identity: str,
    *,
    decision_id: str,
) -> str:
    clean_identity = str(identity or "").strip()
    if not clean_identity:
        return ""
    for event in reversed(timeline):
        if not isinstance(event, Mapping):
            continue
        if (
            _approval_resume_base_progress_event_type(str(event.get("event") or "").strip())
            != event_type
        ):
            continue
        payload = _timeline_payload(event)
        if (
            decision_id
            and str(payload.get("decision_id") or "").strip() != decision_id
        ):
            continue
        if str(payload.get(identity_key) or "").strip() != clean_identity:
            continue
        return str(payload.get("status") or "").strip()
    return ""


def _same_plan(
    payload: Mapping[str, Any],
    *,
    decision_id: str,
    plan_id: str,
) -> bool:
    if decision_id and str(payload.get("decision_id") or "").strip() != decision_id:
        return False
    if plan_id and str(payload.get("plan_id") or "").strip() != plan_id:
        return False
    return True


def _timeline_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
    return {**dict(event), **dict(payload)}


def _task_todo_status_for_tool_result(
    event_type: str,
    result: Mapping[str, Any],
) -> str:
    if result.get("approval_required"):
        return "blocked"
    if str(event_type or "").strip() == "agent.tool.skipped":
        return "skipped" if result.get("blocked_by_user_goal") else "blocked"
    if result.get("ok") is False or result.get("error"):
        return "blocked"
    for key in ("returncode", "exit_code"):
        if key not in result:
            continue
        try:
            if int(result.get(key) or 0) != 0:
                return "blocked"
        except (TypeError, ValueError):
            return "blocked"
    return "completed"


def _task_checkpoint_status_for_todo_status(
    todo_status: str,
    result: Mapping[str, Any],
) -> str:
    if result.get("approval_required"):
        return "waiting_approval"
    if todo_status == "completed":
        return "completed"
    return "blocked"


def _task_progress_result_preview(result: Mapping[str, Any]) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key in (
        "ok",
        "action",
        "summary",
        "error",
        "hint",
        "returncode",
        "exit_code",
        "blocked_by_user_goal",
        "approval_required",
    ):
        if key in result:
            preview[key] = result.get(key)
    stderr = str(result.get("stderr") or "").strip()
    if stderr:
        preview["stderr"] = stderr[:500]
    return preview
