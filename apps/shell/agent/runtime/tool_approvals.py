"""Tool approval resume contexts and projections."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from apps.shell.agent.runtime import goal_runtime
from apps.shell.agent.runtime.budget import RunBudget
from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.runtime.events import tool_input_preview as _tool_input_preview
from apps.shell.agent.runtime.goal_contract import GoalContract
from apps.shell.agent.runtime.recovery_lineage import (
    RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY,
)
from apps.shell.agent.runtime.tool_requests import (
    normalize_tool_iteration,
    normalize_tool_name,
    normalize_tool_request_input,
)
from packages.security import redact_api_error_text

_PENDING_APPROVAL_CONTEXT_TEXT_KEYS = (
    "source_run_id",
    "source_runnable_id",
    "source_runnable_name",
    "member_agent_id",
    "member_agent_name",
    "agent_id",
    "agent_name",
    "workflow_id",
    "workflow_run_id",
    "workflow_node_id",
    "workflow_node_label",
    "group_id",
    "group_run_id",
    "run_group_id",
    "core_id",
    "workspace_id",
    "task_id",
    "source",
    "planning_reason",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "step_id",
    "planner_step_id",
    "capability_id",
    "replan_request_id",
    "replan_trigger",
    "materialized_from_request_id",
    "materialization_source_request_id",
    "materialization_source_step_id",
    "materialization_binding_id",
    "materialized_content_sha256",
    "materialization_authority",
    "goal_contract_id",
    "goal_criterion_id",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
)
_PENDING_APPROVAL_CONTEXT_BOOL_KEYS = (
    "requires_observation",
    "requires_post_action_verification",
)
_PENDING_APPROVAL_CONTEXT_LIST_KEYS = (
    "replan_triggers",
    "replan_signal_ids",
)
_PENDING_APPROVAL_CONTEXT_MAPPING_KEYS = (
    "runtime_execution_envelope",
    "yachiyo_execution_envelope",
    "runtime_execution_metadata",
)

APPROVAL_REQUEST_FINGERPRINT_KEY = "approval_request_fingerprint"
_APPROVAL_PRIVATE_RUNTIME_KEYS = frozenset(
    {
        RUNTIME_PRIVATE_RECOVERY_CONTEXT_KEY,
        "recovery_context_trusted",
        "_runtime_private_select_all_copy",
        "_runtime_private_clipboard_target",
        "_runtime_private_typed_observation",
        "_runtime_private_copy_observation",
        "_runtime_private_clipboard_observation",
        "_runtime_private_typed_raw_result",
        "_runtime_private_clipboard_source_request",
        "_runtime_private_prepared_submit_context",
        "_runtime_private_exact_submit_receipt",
        "_runtime_private_exact_file_readback",
    }
)


def _persistable_approval_value(value: Any) -> Any:
    """Copy approval state without process-private execution authority."""

    if isinstance(value, Mapping):
        return {
            str(key): _persistable_approval_value(item)
            for key, item in value.items()
            if str(key) not in _APPROVAL_PRIVATE_RUNTIME_KEYS
        }
    if isinstance(value, list):
        return [_persistable_approval_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_persistable_approval_value(item) for item in value)
    return deepcopy(value)


def approval_request_fingerprint(tool_request: Mapping[str, Any]) -> str:
    """Return a deterministic identity for the exact executable request."""

    normalized = _persistable_approval_value(tool_request)
    normalize_tool_request_input(normalized)
    normalized["tool"] = normalize_tool_name(normalized.get("tool"))
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def ensure_pending_approval_request_fingerprint(
    pending: dict[str, Any],
) -> str:
    """Bind a private approval projection to its exact executable request."""

    tool_request = pending.get("tool_request")
    if not isinstance(tool_request, Mapping) or not tool_request:
        return ""
    request_tool = normalize_tool_name(tool_request.get("tool"))
    pending_tool = normalize_tool_name(pending.get("tool"))
    if pending_tool and request_tool and pending_tool != request_tool:
        raise AgentRuntimeError("approval_request_projection_mismatch")
    normalized_request = deepcopy(dict(tool_request))
    normalize_tool_request_input(normalized_request)
    request_input = (
        normalized_request.get("input")
        if isinstance(normalized_request.get("input"), Mapping)
        else {}
    )
    pending_input = pending.get("input")
    if isinstance(pending_input, Mapping):
        normalized_pending_input = {
            "tool": request_tool,
            "input": deepcopy(dict(pending_input)),
        }
        normalize_tool_request_input(normalized_pending_input)
        if dict(normalized_pending_input.get("input") or {}) != dict(request_input):
            raise AgentRuntimeError("approval_request_projection_mismatch")
    computed = approval_request_fingerprint(tool_request)
    stored = str(pending.get(APPROVAL_REQUEST_FINGERPRINT_KEY) or "").strip()
    if stored and stored != computed:
        raise AgentRuntimeError("approval_request_fingerprint_mismatch")
    pending[APPROVAL_REQUEST_FINGERPRINT_KEY] = computed
    return computed


class ToolPendingApprovalBuilder:
    """Builds private pending-approval payloads for tool approvals."""

    def __init__(self, *, approval_id_factory: Any, now: Any) -> None:
        self._approval_id_factory = approval_id_factory
        self._now = now

    def build(
        self,
        tool_request: dict[str, Any],
        *,
        messages: list[dict[str, Any]],
        next_iteration: int,
        remaining_tool_requests: list[dict[str, Any]],
    ) -> dict[str, Any]:
        persisted_tool_request = _persistable_approval_value(tool_request)
        persisted_remaining = _persistable_approval_value(
            list(remaining_tool_requests)
        )
        raw_input = (
            persisted_tool_request.get("input")
            if isinstance(persisted_tool_request.get("input"), dict)
            else {}
        )
        context = _pending_approval_context(persisted_tool_request)
        input_preview = _pending_approval_input_preview(
            _tool_input_preview(raw_input),
            context,
        )
        pending = {
            "approval_id": str(self._approval_id_factory()),
            "tool": normalize_tool_name(tool_request.get("tool")),
            "input": deepcopy(raw_input),
            "input_preview": input_preview,
            "requested_at": str(self._now()),
            "messages": _persistable_approval_value(messages),
            "tool_request": persisted_tool_request,
            "remaining_tool_requests": persisted_remaining,
            "next_iteration": normalize_tool_iteration(next_iteration),
            **context,
        }
        ensure_pending_approval_request_fingerprint(pending)
        return pending


@dataclass
class ToolApprovalResumeContext:
    """Private execution context needed to resume an approved tool call."""

    run_id: str
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    broker: Any
    allowed_tools: list[str]
    budget: RunBudget
    messages: list[dict[str, Any]]
    tool_request: dict[str, Any]
    tool_name: str
    input_preview: dict[str, Any]
    remaining_requests: list[dict[str, Any]]
    next_iteration: int
    approval_id: str = ""
    assert_resume_active: Any | None = None
    runtime_execution_envelope: dict[str, Any] | None = None
    runtime_execution_metadata: dict[str, Any] | None = None
    goal_contract: GoalContract | None = None
    approval_request_fingerprint: str = ""

    @classmethod
    def from_run(
        cls,
        run: dict[str, Any],
        pending: dict[str, Any],
        *,
        broker: Any,
        allowed_tools: list[str],
        budget: RunBudget | None = None,
        budget_factory: Any | None = None,
        assert_resume_active: Any | None = None,
    ) -> "ToolApprovalResumeContext":
        run_id = str(run.get("run_id") or "").strip()
        original_goal = run.get("user_goal")
        if not run_id or not isinstance(original_goal, str) or not original_goal.strip():
            raise AgentRuntimeError("approval_resume_goal_contract_missing")
        messages = (
            _persistable_approval_value(pending.get("messages"))
            if isinstance(pending.get("messages"), list)
            else []
        )
        tool_request = (
            _persistable_approval_value(pending.get("tool_request"))
            if isinstance(pending.get("tool_request"), dict)
            else {}
        )
        normalize_tool_request_input(tool_request)
        if not messages or not tool_request:
            raise AgentRuntimeError("Run 待审批上下文不完整，无法恢复")
        private_pending = deepcopy(pending)
        private_pending["tool_request"] = deepcopy(tool_request)
        request_fingerprint = ensure_pending_approval_request_fingerprint(private_pending)
        timeline = [
            deepcopy(event)
            for event in run.get("timeline") or []
            if isinstance(event, dict)
        ]
        artifacts = [
            deepcopy(item)
            for item in run.get("artifacts") or []
            if isinstance(item, dict)
        ]
        remaining = pending.get("remaining_tool_requests")
        remaining_requests = (
            [
                normalize_tool_request_input(_persistable_approval_value(item))
                for item in remaining
                if isinstance(item, dict)
            ]
            if isinstance(remaining, list)
            else []
        )
        allowed_tool_names = list(allowed_tools)
        next_iteration = normalize_tool_iteration(pending.get("next_iteration"))
        tool_name = str(tool_request.get("tool") or pending.get("tool") or "").strip()
        runtime_execution_envelope = _pending_approval_runtime_mapping(
            pending,
            tool_request,
            "runtime_execution_envelope",
        )
        runtime_execution_metadata = _pending_approval_runtime_mapping(
            pending,
            tool_request,
            "runtime_execution_metadata",
        )
        goal_contract = _approval_resume_goal_contract(
            run,
            pending,
            tool_request,
            run_id=run_id,
            original_goal=original_goal,
            timeline=timeline,
        )
        run_budget = budget
        if run_budget is None:
            if budget_factory is None:
                raise AgentRuntimeError("Run 待审批预算上下文不完整，无法恢复")
            run_budget = budget_factory(run_id, timeline)
        return cls(
            run_id=run_id,
            timeline=timeline,
            artifacts=artifacts,
            broker=broker,
            allowed_tools=allowed_tool_names,
            budget=run_budget,
            messages=messages,
            tool_request=tool_request,
            tool_name=tool_name,
            input_preview=_pending_approval_resume_input_preview(pending, tool_request),
            remaining_requests=remaining_requests,
            next_iteration=next_iteration,
            approval_id=str(pending.get("approval_id") or "").strip(),
            approval_request_fingerprint=request_fingerprint,
            assert_resume_active=assert_resume_active,
            runtime_execution_envelope=runtime_execution_envelope,
            runtime_execution_metadata=runtime_execution_metadata,
            goal_contract=goal_contract,
        )


def _pending_approval_context(tool_request: dict[str, Any]) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for key in _PENDING_APPROVAL_CONTEXT_TEXT_KEYS:
        value = str(tool_request.get(key) or "").strip()
        if value:
            context[key] = value
    for key in _PENDING_APPROVAL_CONTEXT_BOOL_KEYS:
        value = tool_request.get(key)
        if isinstance(value, bool):
            context[key] = value
    for key in _PENDING_APPROVAL_CONTEXT_LIST_KEYS:
        value = tool_request.get(key)
        if isinstance(value, list):
            items = [str(item).strip() for item in value if str(item).strip()]
            if items:
                context[key] = items
    for key in _PENDING_APPROVAL_CONTEXT_MAPPING_KEYS:
        value = tool_request.get(key)
        if isinstance(value, dict):
            context[key] = deepcopy(value)
    return context


def _pending_approval_runtime_mapping(
    pending: dict[str, Any],
    tool_request: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    """Restore private execution authority without trusting public previews."""

    candidates = [
        source.get(key)
        for source in (pending, tool_request)
        if isinstance(source.get(key), dict)
    ]
    for expected_rank in (2, 1, 0):
        for value in candidates:
            if _runtime_execution_mapping_rank(value) == expected_rank:
                return deepcopy(value)
    return None


def _approval_resume_goal_contract(
    run: dict[str, Any],
    pending: dict[str, Any],
    tool_request: dict[str, Any],
    *,
    run_id: str,
    original_goal: str,
    timeline: list[dict[str, Any]],
) -> GoalContract:
    runtime_execution_envelope = _approval_resume_runtime_contract_container(
        pending,
        tool_request,
        "runtime_execution_envelope",
    )
    runtime_execution_metadata = _approval_resume_runtime_contract_container(
        pending,
        tool_request,
        "runtime_execution_metadata",
    )
    has_persisted_contract = bool(
        _timeline_has_goal_contract(timeline)
        or _container_has_goal_contract(run, allow_direct_contract=True)
        or _container_has_goal_contract(runtime_execution_envelope)
        or _container_has_goal_contract(runtime_execution_metadata)
    )
    try:
        contract = goal_runtime.runtime_goal_contract(
            run_id=run_id,
            original_goal=original_goal,
            goal_contract_template=run,
            runtime_execution_envelope=runtime_execution_envelope,
            runtime_execution_metadata=runtime_execution_metadata,
            # Mutable pending messages are continuation state, never root-goal
            # authority.
            messages=(),
            timeline=timeline,
        )
    except (TypeError, ValueError) as exc:
        raise AgentRuntimeError("approval_resume_goal_contract_invalid") from exc
    if not has_persisted_contract or not isinstance(contract, GoalContract):
        raise AgentRuntimeError("approval_resume_goal_contract_missing")
    return contract


def _approval_resume_runtime_contract_container(
    pending: dict[str, Any],
    tool_request: dict[str, Any],
    key: str,
) -> dict[str, Any] | None:
    candidates: list[dict[str, Any]] = []
    for source in (pending, tool_request):
        if key not in source or source.get(key) is None:
            continue
        value = source.get(key)
        if not isinstance(value, dict):
            raise AgentRuntimeError("approval_resume_goal_contract_invalid")
        candidates.append(deepcopy(value))
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    return {
        "runtime_execution_envelope": candidates[0],
        "metadata": candidates[1],
    }


def _timeline_has_goal_contract(timeline: list[dict[str, Any]]) -> bool:
    for event in timeline:
        payload = event.get("payload")
        flattened = (
            {**payload, **event}
            if isinstance(payload, dict)
            else event
        )
        if str(
            flattened.get("event") or flattened.get("event_type") or ""
        ).strip() == "agent.goal.contract":
            return True
    return False


def _container_has_goal_contract(
    container: Any,
    *,
    allow_direct_contract: bool = False,
) -> bool:
    if not isinstance(container, dict):
        return False
    if "goal_contract" in container or "goal_contract_json" in container:
        return True
    contract_keys = {"contract_id", "original_goal", "criteria"}
    if allow_direct_contract and contract_keys.intersection(container):
        return True
    return any(
        _container_has_goal_contract(container.get(key))
        for key in (
            "task_core",
            "plan",
            "runtime_plan",
            "runtime_execution_envelope",
            "metadata",
        )
    )


def _runtime_execution_mapping_rank(value: Any) -> int:
    if not isinstance(value, dict):
        return -1
    requests = value.get("requests")
    nested = value.get("yachiyo_execution_envelope")
    nested_requests = nested.get("requests") if isinstance(nested, dict) else None
    if (isinstance(requests, list) and requests) or (
        isinstance(nested_requests, list) and nested_requests
    ):
        return 2
    return 1 if value else 0


def _approval_handoff_has_runtime_authority(
    handoff: "ToolApprovalContinuationHandoff",
) -> bool:
    return bool(
        _runtime_execution_mapping_rank(handoff.runtime_execution_envelope) == 2
        or (
            isinstance(handoff.runtime_execution_metadata, dict)
            and bool(handoff.runtime_execution_metadata)
        )
    )


def _pending_approval_input_preview(
    input_preview: Any,
    context: dict[str, Any],
) -> Any:
    if isinstance(input_preview, dict):
        preview = dict(input_preview)
        if preview.get("app_name"):
            preview.pop("query", None)
            preview.pop("selection_source", None)
        # The resumed tool call must keep the same planner, workflow and
        # verification identity. The top-level pending payload is private;
        # public approval snapshots project only the raw executable input and
        # expose any useful trace identity through dedicated fields.
        preview.update(deepcopy(context))
        return preview
    return input_preview


def _pending_approval_resume_input_preview(
    pending: dict[str, Any],
    tool_request: dict[str, Any],
) -> dict[str, Any]:
    pending_preview = pending.get("input_preview")
    if isinstance(pending_preview, dict):
        computed = deepcopy(pending_preview)
    else:
        raw_input = tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
        computed = _tool_input_preview(raw_input)
    computed = _pending_approval_input_preview(
        computed,
        _pending_approval_context(tool_request),
    )
    return dict(computed) if isinstance(computed, dict) else {}


@dataclass(frozen=True)
class ToolApprovalClaimProjection:
    """Running projection after an approved tool claim succeeds."""

    run_id: str
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    tool_name: str
    input_preview: dict[str, Any]
    resumed_detail: str
    running_result: str
    expected_approval_id: str = ""

    @classmethod
    def from_context(
        cls,
        run_id: str,
        context: ToolApprovalResumeContext,
        *,
        resumed_detail: str,
        running_result: str,
    ) -> "ToolApprovalClaimProjection":
        return cls(
            run_id=run_id,
            timeline=context.timeline,
            artifacts=context.artifacts,
            tool_name=context.tool_name,
            input_preview=context.input_preview,
            resumed_detail=resumed_detail,
            running_result=running_result,
            expected_approval_id=str(context.approval_id or "").strip(),
        )

    def project(self, approve_tool_run: Any) -> dict[str, Any]:
        kwargs = {
            "timeline": self.timeline,
            "artifacts": self.artifacts,
            "tool_name": self.tool_name,
            "input_preview": self.input_preview,
            "resumed_detail": self.resumed_detail,
            "running_result": self.running_result,
        }
        if self.expected_approval_id and supports_keyword(
            approve_tool_run,
            "expected_approval_id",
        ):
            kwargs["expected_approval_id"] = self.expected_approval_id
        return approve_tool_run(
            self.run_id,
            **kwargs,
        )


@dataclass(frozen=True)
class ToolApprovalExecutionRequest:
    """Approved tool call request assembled from resume context."""

    tool_request: dict[str, Any]
    allowed_tools: list[str]
    broker: Any
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    run_id: str
    budget: RunBudget
    approval_id: str = ""
    assert_resume_active: Any | None = None
    approval_request_fingerprint: str = ""

    @classmethod
    def from_context(
        cls,
        context: ToolApprovalResumeContext,
    ) -> "ToolApprovalExecutionRequest":
        return cls(
            tool_request=context.tool_request,
            allowed_tools=context.allowed_tools,
            broker=context.broker,
            timeline=context.timeline,
            artifacts=context.artifacts,
            run_id=context.run_id,
            budget=context.budget,
            approval_id=str(context.approval_id or "").strip(),
            approval_request_fingerprint=(
                str(context.approval_request_fingerprint or "").strip()
                or approval_request_fingerprint(context.tool_request)
            ),
            assert_resume_active=context.assert_resume_active,
        )

    def execute(
        self,
        call_agent_tool: Any,
        *,
        record_executed_result: Any | None = None,
    ) -> Any:
        self._assert_active()
        result = call_agent_tool(
            self.tool_request,
            self.allowed_tools,
            self.broker,
            self.timeline,
            artifacts=self.artifacts,
            approved=True,
            run_id=self.run_id,
            budget=self.budget,
        )
        if record_executed_result is not None:
            record_executed_result(result)
        self._assert_active()
        return result

    def _assert_active(self) -> None:
        if self.approval_request_fingerprint and (
            approval_request_fingerprint(self.tool_request)
            != self.approval_request_fingerprint
        ):
            raise AgentRuntimeError("approval_request_fingerprint_mismatch")
        if self.assert_resume_active is None or not self.approval_id:
            return
        self.assert_resume_active(self.run_id, self.approval_id)


@dataclass(frozen=True)
class ToolApprovalContinuationHandoff:
    """Continuation payload after an approved tool call has been executed."""

    agent: dict[str, Any]
    user_goal: str
    broker: Any
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    messages: list[dict[str, Any]]
    start_iteration: int
    run_id: str
    budget: RunBudget
    resume_after_approved_tool: bool = True
    runtime_execution_envelope: dict[str, Any] | None = None
    runtime_execution_metadata: dict[str, Any] | None = None

    @classmethod
    def from_context(
        cls,
        agent: dict[str, Any],
        context: ToolApprovalResumeContext,
        *,
        resume_after_approved_tool: bool = True,
    ) -> "ToolApprovalContinuationHandoff":
        return cls(
            agent=agent,
            user_goal=(
                context.goal_contract.original_goal
                if context.goal_contract is not None
                else ""
            ),
            broker=context.broker,
            timeline=context.timeline,
            artifacts=context.artifacts,
            messages=context.messages,
            start_iteration=context.next_iteration,
            run_id=context.run_id,
            budget=context.budget,
            resume_after_approved_tool=resume_after_approved_tool,
            runtime_execution_envelope=deepcopy(
                context.runtime_execution_envelope
            ),
            runtime_execution_metadata=deepcopy(
                context.runtime_execution_metadata
            ),
        )


@dataclass(frozen=True)
class ToolApprovalCustomApiContinuationRequest:
    """Custom API continuation call assembled from approved-tool handoff."""

    handoff: ToolApprovalContinuationHandoff

    @classmethod
    def from_handoff(
        cls,
        handoff: ToolApprovalContinuationHandoff,
    ) -> "ToolApprovalCustomApiContinuationRequest":
        return cls(handoff=handoff)

    def execute(self, continue_custom_api_agent: Any) -> str:
        kwargs = {
            "messages": self.handoff.messages,
            "start_iteration": self.handoff.start_iteration,
            "run_id": self.handoff.run_id,
            "budget": self.handoff.budget,
        }
        if supports_keyword(
            continue_custom_api_agent,
            "resume_after_approved_tool",
        ):
            kwargs["resume_after_approved_tool"] = (
                self.handoff.resume_after_approved_tool
            )
        if (
            self.handoff.user_goal
            and supports_keyword(continue_custom_api_agent, "original_goal")
        ):
            kwargs["original_goal"] = self.handoff.user_goal
        for key, value in (
            (
                "runtime_execution_envelope",
                self.handoff.runtime_execution_envelope,
            ),
            (
                "runtime_execution_metadata",
                self.handoff.runtime_execution_metadata,
            ),
        ):
            if value is None or (isinstance(value, dict) and not value):
                continue
            if supports_keyword(continue_custom_api_agent, key):
                kwargs[key] = deepcopy(value)
                continue
            if _approval_handoff_has_runtime_authority(self.handoff):
                raise AgentRuntimeError(
                    "approval_resume_runtime_authority_unsupported"
                )
        return continue_custom_api_agent(
            self.handoff.agent,
            self.handoff.user_goal,
            self.handoff.broker,
            self.handoff.timeline,
            self.handoff.artifacts,
            **kwargs,
        )


@dataclass(frozen=True)
class ToolApprovalContinuationOutcome:
    """Projection outcome after continuing a run with an approved tool result."""

    kind: str
    result_text: str = ""
    pending_approval: Any = None
    safe_error: str = ""

    @classmethod
    def completed(cls, result_text: str) -> "ToolApprovalContinuationOutcome":
        return cls(kind="completed", result_text=result_text)

    @classmethod
    def approval_required(
        cls,
        pending_approval: dict[str, Any],
        *,
        prepare_required: Any | None = None,
    ) -> "ToolApprovalContinuationOutcome":
        pending_next = pending_approval
        if prepare_required is not None:
            pending_next = prepare_required(pending_next)
        return cls(kind="approval_required", pending_approval=pending_next)

    @classmethod
    def failed(
        cls,
        error: Any,
        *,
        redact_error: Any = redact_api_error_text,
    ) -> "ToolApprovalContinuationOutcome":
        return cls(kind="failed", safe_error=redact_error(error))

    def project(
        self,
        context: ToolApprovalResumeContext,
        *,
        project_completed: Any,
        project_required: Any,
        project_failed: Any,
    ) -> dict[str, Any]:
        if self.kind == "completed":
            return project_completed(context, self.result_text)
        if self.kind == "approval_required":
            return project_required(context, self.pending_approval)
        if self.kind == "failed":
            return project_failed(context, self.safe_error)
        raise AgentRuntimeError(f"Unknown approved-tool continuation outcome: {self.kind}")


@dataclass(frozen=True)
class ToolApprovalExecutionFailureProjection:
    """Fatal failure projection for an approved tool execution."""

    tool_name: str
    input_preview: dict[str, Any]
    tool_result: Any
    detail: str

    @classmethod
    def from_context(
        cls,
        context: ToolApprovalResumeContext,
        tool_result: Any,
        detail: str,
    ) -> "ToolApprovalExecutionFailureProjection":
        return cls(
            tool_name=context.tool_name or "tool",
            input_preview=context.input_preview,
            tool_result=tool_result,
            detail=detail,
        )

    def timeline_event(self, timeline_factory: Any) -> dict[str, Any]:
        return timeline_factory(
            "agent.tool.failed",
            self.tool_name,
            input_preview=self.input_preview,
            result=self.tool_result,
            status="failed",
        )


@dataclass(frozen=True)
class ToolApprovalExecutionFollowup:
    """Follow-up execution after an approved tool succeeds."""

    messages: list[dict[str, Any]]
    tool_request: dict[str, Any]
    tool_result: Any
    remaining_requests: list[dict[str, Any]]
    allowed_tools: list[str]
    broker: Any
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    next_iteration: int
    run_id: str
    budget: RunBudget

    @classmethod
    def from_context(
        cls,
        context: ToolApprovalResumeContext,
        tool_result: Any,
    ) -> "ToolApprovalExecutionFollowup":
        return cls(
            messages=context.messages,
            tool_request=context.tool_request,
            tool_result=tool_result,
            remaining_requests=context.remaining_requests,
            allowed_tools=context.allowed_tools,
            broker=context.broker,
            timeline=context.timeline,
            artifacts=context.artifacts,
            next_iteration=context.next_iteration,
            run_id=context.run_id,
            budget=context.budget,
        )

    def apply(
        self,
        append_tool_result_message: Any,
        run_tool_requests: Any,
    ) -> None:
        append_tool_result_message(self.messages, self.tool_request, self.tool_result)
        run_tool_requests(
            self.remaining_requests,
            self.allowed_tools,
            self.broker,
            self.messages,
            self.timeline,
            self.artifacts,
            next_iteration=self.next_iteration,
            run_id=self.run_id,
            budget=self.budget,
        )


@dataclass(frozen=True)
class ToolApprovalTransitionContext:
    """Shared public context for tool approval reject/timeout transitions."""

    tool_name: str
    input_preview: Any

    @classmethod
    def from_pending(cls, pending: dict[str, Any]) -> "ToolApprovalTransitionContext":
        tool_request = (
            pending.get("tool_request")
            if isinstance(pending.get("tool_request"), dict)
            else {}
        )
        return cls(
            tool_name=str(tool_request.get("tool") or pending.get("tool") or "").strip(),
            input_preview=_tool_input_preview(
                tool_request.get("input") if isinstance(tool_request.get("input"), dict) else {}
            ),
        )
