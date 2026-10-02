"""Main Chat compatibility facade methods for NativeRunEngine."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_continuation_requests_from_envelope_payload,
)


def _preferred_runtime_execution_mapping(
    primary: Any,
    fallback: Any,
) -> dict[str, Any] | None:
    candidates = [value for value in (primary, fallback) if isinstance(value, dict)]
    for expected_rank in (2, 1, 0):
        for value in candidates:
            requests = value.get("requests")
            nested = value.get("yachiyo_execution_envelope")
            nested_requests = (
                nested.get("requests") if isinstance(nested, dict) else None
            )
            rank = (
                2
                if (isinstance(requests, list) and requests)
                or (isinstance(nested_requests, list) and nested_requests)
                else 1
                if value
                else 0
            )
            if rank == expected_rank:
                return deepcopy(value)
    return None


class RuntimeMainChatFacadeMixin:
    """Keeps daily Chat runtime methods while delegating to split services."""

    def prepare_main_chat_delegation(
        self, run_id: str, *, selected_ids: set[str], group_scope: bool = False, direct_group: bool = False,
        tool_policy: dict[str, Any] | None = None,
        workspace_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        from .main_chat_delegation import MainChatDelegationCoordinator

        agent = self._main_chat_agent_config(
            model_profile_id="", tool_policy=tool_policy, workspace_policy=workspace_policy,
        )
        return MainChatDelegationCoordinator(self).prepare(
            run_id, parent_runtime=self._compile_agent_runtime(agent), selected_ids=selected_ids, group_scope=group_scope, direct_group=direct_group,
        )

    def start_main_chat_delegation(
        self, run_id: str, proposals: list[dict[str, Any]], *, task_id: str, group: bool, upstream: str,
    ) -> list[dict[str, Any]]:
        from .main_chat_delegation import MainChatDelegationCoordinator

        return MainChatDelegationCoordinator(self).start(
            run_id, proposals, task_id=task_id, group=group, upstream=upstream,
        )

    def verify_main_chat_delegation(self, run_id: str) -> dict[str, Any]:
        from .main_chat_delegation import MainChatDelegationCoordinator

        return MainChatDelegationCoordinator(self).verify(run_id)

    def bind_main_chat_delegation_summary(self, run_id: str, task: Any) -> dict[str, Any]:
        from .main_chat_delegation import MainChatDelegationCoordinator

        return MainChatDelegationCoordinator(self).bind_summary(run_id, task)

    def verify_main_chat_delegation_summary(self, run_id: str) -> dict[str, Any]:
        from .main_chat_delegation import MainChatDelegationCoordinator

        return MainChatDelegationCoordinator(self).verify_summary(run_id)

    def prepare_main_chat_delegation_proposal(
        self, run_id: str, proposal: dict[str, Any], *,
        tool_policy: dict[str, Any] | None = None,
        workspace_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        from .main_chat_delegation import MainChatDelegationCoordinator

        parent = self._compile_agent_runtime(self._main_chat_agent_config(
            model_profile_id="", tool_policy=tool_policy, workspace_policy=workspace_policy,
        ))
        return MainChatDelegationCoordinator(self).prepare_same_goal(
            run_id, proposal, parent_runtime=parent,
        )

    def bind_main_chat_supplied_images(
        self, run_id: str, messages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self.main_chat_runs.bind_supplied_images(run_id, messages)

    def start_main_chat_run(
        self,
        *,
        task_id: str,
        session_id: str,
        user_goal: str,
        client_run_id: str = "",
        metadata: dict[str, Any] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        return self.main_chat_runs.start(
            task_id=task_id,
            session_id=session_id,
            user_goal=user_goal,
            client_run_id=client_run_id,
            metadata=metadata,
            runtime_execution_envelope=runtime_execution_envelope,
            direct_tool_request=direct_tool_request,
            direct_tool_requests=direct_tool_requests,
        )

    def latest_awaiting_user_main_chat_run(
        self,
        session_id: str,
    ) -> dict[str, Any] | None:
        return self.task_run_links.latest_awaiting_user_main_chat_for_session(
            session_id
        )

    def call_main_chat_model(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        *,
        profile_id: str = "",
        capability: str = "chat",
    ) -> str:
        return self.main_chat_model.call(
            run_id,
            messages,
            profile_id=profile_id,
            capability=capability,
        )

    def _main_chat_workspace_policy(self, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.main_chat_config.workspace_policy(policy)

    def _main_chat_tool_policy(self, policy: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.main_chat_config.tool_policy(policy)

    def _main_chat_agent_config(
        self,
        *,
        model_profile_id: str,
        tool_policy: dict[str, Any] | None = None,
        workspace_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.main_chat_config.agent_config(
            model_profile_id=model_profile_id,
            tool_policy=tool_policy,
            workspace_policy=workspace_policy,
        )

    @staticmethod
    def _main_chat_pending_approval(
        pending_approval: dict[str, Any],
        *,
        model_profile_id: str,
        tool_policy: dict[str, Any],
        workspace_policy: dict[str, Any],
        runtime_execution_envelope: dict[str, Any] | None = None,
        runtime_execution_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            **pending_approval,
            "resume_kind": "main_chat",
            "model_profile_id": str(model_profile_id or "").strip(),
            "tool_policy": tool_policy,
            "workspace_policy": workspace_policy,
        }
        effective_runtime_execution_envelope = _preferred_runtime_execution_mapping(
            runtime_execution_envelope,
            pending_approval.get("runtime_execution_envelope"),
        )
        if isinstance(effective_runtime_execution_envelope, dict):
            payload["runtime_execution_envelope"] = effective_runtime_execution_envelope
            if not payload.get("remaining_tool_requests"):
                payload["remaining_tool_requests"] = (
                    runtime_execution_continuation_requests_from_envelope_payload(
                        effective_runtime_execution_envelope,
                        after_request=(
                            pending_approval.get("tool_request")
                            if isinstance(pending_approval.get("tool_request"), dict)
                            else None
                        ),
                        allowed_tools=tool_policy.get("allowed_tools") or [],
                    )
                )
        effective_runtime_execution_metadata = _preferred_runtime_execution_mapping(
            runtime_execution_metadata,
            pending_approval.get("runtime_execution_metadata"),
        )
        if isinstance(effective_runtime_execution_metadata, dict):
            payload["runtime_execution_metadata"] = effective_runtime_execution_metadata
        return payload

    def execute_main_chat_model_loop(
        self,
        run_id: str,
        messages: list[dict[str, Any]],
        *,
        profile_id: str = "",
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
        runtime_execution_metadata: dict[str, Any] | None = None,
        tool_policy: dict[str, Any] | None = None,
        workspace_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.main_chat_model_loop.execute(
            run_id,
            messages,
            profile_id=profile_id,
            direct_tool_request=direct_tool_request,
            direct_tool_requests=direct_tool_requests,
            runtime_execution_envelope=runtime_execution_envelope,
            runtime_execution_metadata=runtime_execution_metadata,
            tool_policy=tool_policy,
            workspace_policy=workspace_policy,
        )

    def complete_main_chat_run(self, run_id: str, result: str) -> dict[str, Any]:
        return self.main_chat_runs.complete(run_id, result)

    def fail_main_chat_run(self, run_id: str, error: Any) -> dict[str, Any]:
        return self.main_chat_runs.fail(run_id, error)
