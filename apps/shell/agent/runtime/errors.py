"""Runtime exception types shared across Agent runtime modules."""

from __future__ import annotations

from typing import Any


class AgentRuntimeError(RuntimeError):
    """Raised when an Agent Studio operation cannot be completed."""


class AgentWorkspaceBoundaryError(AgentRuntimeError):
    """Raised when a workspace request is rejected by its configured boundary."""


class AgentDelegationProposed(AgentRuntimeError):
    """A model proposal awaiting runtime goal and permission binding."""

    def __init__(self, proposal: str) -> None:
        self.proposal = proposal
        super().__init__("native_delegation_proposed")


class AgentDirectOutcomeUnverified(AgentRuntimeError):
    """Raised when a direct action ran but its requested outcome is unverified."""

    def __init__(
        self,
        message: str,
        *,
        reason: str = "desktop_verification_missing",
        tool_name: str = "",
        input_preview: dict[str, Any] | None = None,
        tool_call_id: str = "",
    ) -> None:
        self.reason = str(reason or "desktop_verification_missing").strip()
        self.tool_name = str(tool_name or "").strip()
        self.input_preview = dict(input_preview or {})
        self.tool_call_id = str(tool_call_id or "").strip()
        super().__init__(message)


class AgentApprovalRequired(AgentRuntimeError):  # noqa: N818 - existing runtime API name.
    """Raised internally when a run must pause for user approval."""

    def __init__(self, pending_approval: dict[str, Any]) -> None:
        self.pending_approval = pending_approval
        super().__init__(f"等待审批：{pending_approval.get('tool') or 'tool'}")
