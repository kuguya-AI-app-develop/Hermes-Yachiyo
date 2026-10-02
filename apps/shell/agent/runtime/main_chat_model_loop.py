"""Tool-capable main chat model loop orchestration."""

from __future__ import annotations

from contextlib import AbstractContextManager, nullcontext
from typing import Any, Callable

from apps.shell.agent.runtime.callbacks import supports_keyword
from apps.shell.agent.runtime.errors import (
    AgentApprovalRequired,
    AgentDirectOutcomeUnverified,
    AgentRuntimeError,
)
from apps.shell.agent.runtime.events import redact_secrets
from apps.shell.agent.runtime.goal_runtime import (
    goal_contract_event_payload,
    planned_goal_contract_payload,
    runtime_goal_contract,
    supplied_image_goal_contract_payload,
)
from apps.shell.agent.runtime.model_messages import (
    message_visible_content_text,
    messages_require_model_first,
    model_output_metadata,
)
from apps.shell.agent.runtime.model_intent_planning import (
    ModelIntentClarificationResolution,
    goal_contract_payload_from_model_selection,
)
from apps.shell.agent.runtime.tool_brokers import (
    close_owned_browser_target_best_effort,
)
from apps.shell.agent.runtime.supplied_images import (
    SUPPLIED_IMAGE_EVENT,
    persisted_supplied_image_binding,
    supplied_image_binding_from_messages,
)
from apps.shell.yachiyo_agent.daily_desktop import (
    daily_desktop_requests_can_complete_without_model,
)
from apps.shell.yachiyo_agent.desktop_execution_policy import (
    with_daily_entrypoint_desktop_execution_policy,
)
from apps.shell.yachiyo_agent.discovered_app_followups import (
    planner_discovered_app_followup_can_direct_execute,
)
from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
    planner_first_direct_tool_selection,
)
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)


def build_runtime_main_chat_model_loop_runner(
    *,
    get_run: Callable[[str], dict[str, Any]],
    profile_service_factory: Callable[[], Any],
    model_profile_config_private: Callable[[str], dict[str, Any]],
    main_chat_agent_config: Callable[..., dict[str, Any]],
    compile_agent_runtime: Callable[[dict[str, Any]], dict[str, Any]],
    run_budget: Callable[[str, list[dict[str, Any]]], Any],
    check_context_budget: Callable[[Any, list[dict[str, Any]]], None],
    runtime_agent_timeline: Any,
    timeline_factory: Callable[..., dict[str, Any]],
    update_run: Callable[..., dict[str, Any] | None],
    append_run_event: Callable[..., dict[str, Any] | None],
    task_model_events: Any,
    tool_brokers: Any,
    continue_custom_api_agent: Callable[..., str],
    main_chat_pending_approval: Callable[..., dict[str, Any]],
    approval_pause: Any,
    terminal_run_or_none: Callable[[str], dict[str, Any] | None],
    fail_main_chat_run: Callable[..., dict[str, Any]],
    transaction_scope: Callable[[], AbstractContextManager[Any]] | None = None,
    resolve_initial_model_plan: Callable[..., Any] | None = None,
) -> "MainChatModelLoopRunner":
    return MainChatModelLoopRunner(
        get_run=get_run,
        default_profile_id=lambda: str(
            profile_service_factory().get_defaults().get("chat") or ""
        ).strip(),
        model_profile_config_private=model_profile_config_private,
        main_chat_agent_config=main_chat_agent_config,
        compile_agent_runtime=compile_agent_runtime,
        run_budget=run_budget,
        check_context_budget=check_context_budget,
        runtime_agent_timeline=runtime_agent_timeline,
        timeline_factory=timeline_factory,
        update_run=update_run,
        append_run_event=append_run_event,
        task_model_events=task_model_events,
        tool_brokers=tool_brokers,
        continue_custom_api_agent=continue_custom_api_agent,
        main_chat_pending_approval=main_chat_pending_approval,
        approval_pause=approval_pause,
        terminal_run_or_none=terminal_run_or_none,
        fail_main_chat_run=fail_main_chat_run,
        transaction_scope=transaction_scope,
        resolve_initial_model_plan=resolve_initial_model_plan,
        redact_secrets=redact_secrets,
        model_output_metadata=model_output_metadata,
        error_type=AgentRuntimeError,
    )


class MainChatModelLoopRunner:
    """Orchestrates the daily Chat model loop while preserving approval gates."""

    def __init__(
        self,
        *,
        get_run: Callable[[str], dict[str, Any]],
        default_profile_id: Callable[[], str],
        model_profile_config_private: Callable[[str], dict[str, Any]],
        main_chat_agent_config: Callable[..., dict[str, Any]],
        compile_agent_runtime: Callable[[dict[str, Any]], dict[str, Any]],
        run_budget: Callable[[str, list[dict[str, Any]]], Any],
        check_context_budget: Callable[[Any, list[dict[str, Any]]], None],
        runtime_agent_timeline: Any,
        timeline_factory: Callable[..., dict[str, Any]],
        update_run: Callable[..., dict[str, Any] | None],
        append_run_event: Callable[..., dict[str, Any] | None],
        task_model_events: Any,
        tool_brokers: Any,
        continue_custom_api_agent: Callable[..., str],
        main_chat_pending_approval: Callable[..., dict[str, Any]],
        approval_pause: Any,
        terminal_run_or_none: Callable[[str], dict[str, Any] | None],
        fail_main_chat_run: Callable[..., dict[str, Any]],
        redact_secrets: Callable[[Any], str],
        model_output_metadata: Callable[[Any], dict[str, Any]],
        error_type: type[Exception],
        transaction_scope: Callable[[], AbstractContextManager[Any]] | None = None,
        resolve_initial_model_plan: Callable[..., Any] | None = None,
    ) -> None:
        self._get_run = get_run
        self._default_profile_id = default_profile_id
        self._model_profile_config_private = model_profile_config_private
        self._main_chat_agent_config = main_chat_agent_config
        self._compile_agent_runtime = compile_agent_runtime
        self._run_budget = run_budget
        self._check_context_budget = check_context_budget
        self._runtime_agent_timeline = runtime_agent_timeline
        self._timeline = timeline_factory
        self._update_run = update_run
        self._append_run_event = append_run_event
        self._task_model_events = task_model_events
        self._tool_brokers = tool_brokers
        self._continue_custom_api_agent = continue_custom_api_agent
        self._main_chat_pending_approval = main_chat_pending_approval
        self._approval_pause = approval_pause
        self._terminal_run_or_none = terminal_run_or_none
        self._fail_main_chat_run = fail_main_chat_run
        self._redact_secrets = redact_secrets
        self._model_output_metadata = model_output_metadata
        self._error_type = error_type
        self._transaction_scope = transaction_scope
        self._resolve_initial_model_plan = resolve_initial_model_plan

    def execute(
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
        run = self._get_run(run_id)
        if str(run.get("kind") or "") != "main_chat_run":
            raise self._error_type("Run 不是主聊天 Native Run")
        runtime_execution_metadata = with_daily_entrypoint_desktop_execution_policy(
            runtime_execution_metadata,
            surface="chat",
        )
        default_profile_id = str(profile_id or self._default_profile_id() or "").strip()
        agent = self._main_chat_agent_config(
            model_profile_id=default_profile_id,
            tool_policy=tool_policy,
            workspace_policy=workspace_policy,
        )
        runtime = self._compile_agent_runtime(agent)
        timeline = [event for event in run.get("timeline") or [] if isinstance(event, dict)]
        allowed_tools = runtime["tool_policy"].get("allowed_tools") or []
        user_goal = str(run.get("user_goal") or "").strip()
        if not user_goal:
            raise ValueError("goal_contract_invalid: user_goal_required")
        current_image_binding = supplied_image_binding_from_messages(
            run_id=run_id, original_goal=user_goal, messages=messages
        )
        persisted_image_binding = persisted_supplied_image_binding(
            run_id=run_id, original_goal=user_goal, timeline=timeline
        )
        newly_bound_images = None
        if current_image_binding is not None:
            if persisted_image_binding and persisted_image_binding != current_image_binding:
                return self._fail_main_chat_run(run_id, "supplied_image_binding_conflict")
            if persisted_image_binding is None:
                newly_bound_images = current_image_binding
                timeline.append(self._timeline(
                    SUPPLIED_IMAGE_EVENT, "User image input bound", visibility="internal", **current_image_binding
                ))
        image_goal_template = supplied_image_goal_contract_payload(
            run_id=run_id, original_goal=user_goal, timeline=timeline
        )
        if image_goal_template:
            if direct_tool_request or direct_tool_requests:
                return self._fail_main_chat_run(run_id, "supplied_image_goal_has_external_tool_request")
            agent = self._main_chat_agent_config(
                model_profile_id=default_profile_id,
                tool_policy={"allowed_tools": [], "response_only": True},
                workspace_policy=workspace_policy,
            )
            runtime = self._compile_agent_runtime(agent)
            allowed_tools = runtime["tool_policy"].get("allowed_tools") or []
        budget = self._run_budget(run_id, timeline)
        self._check_context_budget(budget, messages)
        authoritative_direct_daily_desktop_intent = (
            self._authoritative_runtime_plan_can_complete_without_model(
                allowed_tools,
                direct_tool_request=direct_tool_request,
                direct_tool_requests=direct_tool_requests,
                runtime_execution_envelope=runtime_execution_envelope,
            )
        )
        has_persisted_goal_contract = _timeline_has_goal_contract_event(timeline)
        goal_contract_template = image_goal_template or None
        model_assisted_selection = None
        if not has_persisted_goal_contract:
            if (
                self._resolve_initial_model_plan is not None
                and not authoritative_direct_daily_desktop_intent
                and not image_goal_template
            ):
                try:
                    initial_plan_resolution = self._resolve_initial_model_plan(
                        agent=agent,
                        original_goal=user_goal,
                        allowed_tools=list(allowed_tools),
                        runtime_execution_metadata=runtime_execution_metadata,
                        runtime_execution_envelope=runtime_execution_envelope,
                        run_id=run_id,
                        timeline=timeline,
                        budget=budget,
                    )
                except Exception as exc:
                    # Intent planning calls the same provider as execution.
                    # A rejected planning turn must not leave a live run behind.
                    safe_error = self._redact_secrets(exc)
                    failed_run = self._fail_main_chat_run(
                        run_id,
                        safe_error,
                        timeline=[
                            *timeline,
                            self._timeline("model.request.failed", safe_error),
                        ],
                        run_events=[(
                            "model.request.failed",
                            self._task_model_events.model_request_failed_payload(safe_error),
                        )],
                    )
                    if str(failed_run.get("status") or "") != "failed":
                        return failed_run
                    raise
                if isinstance(
                    initial_plan_resolution,
                    ModelIntentClarificationResolution,
                ):
                    return self._project_initial_planning_clarification(
                        run_id,
                        user_goal=user_goal,
                        resolution=initial_plan_resolution,
                        timeline=timeline,
                    )
                model_assisted_selection = initial_plan_resolution
            if model_assisted_selection is not None:
                goal_contract_template = goal_contract_payload_from_model_selection(
                    model_assisted_selection,
                    user_goal,
                )
                replacement_envelope = runtime_execution_envelope_payload(
                    model_assisted_selection.decision,
                    allowed_tools=allowed_tools,
                    full_plan=True,
                    metadata=runtime_execution_metadata,
                )
                if not replacement_envelope:
                    raise ValueError("model_intent_plan_envelope_missing")
                runtime_execution_envelope = replacement_envelope
                direct_tool_request = None
                direct_tool_requests = runtime_execution_requests_from_envelope_payload(
                    replacement_envelope,
                    allowed_tools=allowed_tools,
                )
                runtime_execution_metadata = {
                    **runtime_execution_metadata,
                    "runtime_model_assisted_planning": True,
                    "runtime_model_plan_selection": dict(
                        model_assisted_selection.event_payload
                    ),
                }
            elif not image_goal_template and not authoritative_direct_daily_desktop_intent:
                goal_contract_template = planned_goal_contract_payload(
                    user_goal,
                    allowed_tools=allowed_tools,
                ) or None
        goal_contract = runtime_goal_contract(
            run_id=run_id,
            original_goal=user_goal,
            goal_contract_template=goal_contract_template,
            runtime_execution_envelope=runtime_execution_envelope,
            runtime_execution_metadata=runtime_execution_metadata,
            messages=messages,
            timeline=timeline,
        )
        if goal_contract is None:
            raise ValueError("goal_contract_invalid: contract_required")
        contract_payload = goal_contract_event_payload(goal_contract)
        timeline.append(
            self._runtime_agent_timeline.compiled(
                detail="Main chat NativeRunEngine compiled tools and workspace policy",
                allowed_tools=allowed_tools,
            )
        )
        direct_daily_desktop_intent = (
            authoritative_direct_daily_desktop_intent
            or self._will_handle_daily_desktop_intent(
                messages,
                allowed_tools,
                direct_tool_request=direct_tool_request,
                direct_tool_requests=direct_tool_requests,
                runtime_execution_envelope=runtime_execution_envelope,
            )
        )
        if not default_profile_id and not direct_daily_desktop_intent:
            raise self._error_type("native_agent_not_ready:chat_model_profile_required")
        model_config = (
            self._model_profile_config_private(default_profile_id)
            if default_profile_id and not direct_daily_desktop_intent
            else {}
        )
        if not has_persisted_goal_contract:
            timeline.append(
                self._timeline(
                    "agent.goal.contract",
                    goal_contract.contract_id,
                    **contract_payload,
                )
            )
            contract_run, committed = self._cas_from_running(
                run_id,
                status="running",
                timeline=timeline,
            )
            if not committed:
                return contract_run
            if newly_bound_images and self._append_run_event(
                run_id,
                SUPPLIED_IMAGE_EVENT,
                {**newly_bound_images, "visibility": "internal"},
                visibility="internal",
                **_run_event_fence(contract_run, status="running"),
            ) is None:
                return self._get_run(run_id)
            if self._append_run_event(
                run_id,
                "agent.goal.contract",
                contract_payload,
                **_run_event_fence(contract_run, status="running"),
            ) is None:
                return self._get_run(run_id)
        runtime_execution_metadata = {
            **runtime_execution_metadata,
            "goal_contract": contract_payload["goal_contract"],
            "goal_contract_json": contract_payload["goal_contract_json"],
        }
        if not direct_daily_desktop_intent:
            timeline.append(
                self._timeline(
                    "model.request.started",
                    str(model_config.get("model") or ""),
                    profile_id=default_profile_id,
                    capability="chat",
                )
            )
        started_run, committed = self._cas_from_running(
            run_id,
            status="running",
            timeline=timeline,
        )
        if not committed:
            return started_run
        if not direct_daily_desktop_intent:
            if self._append_run_event(
                run_id,
                "model.request.started",
                self._task_model_events.model_request_started_payload(
                    profile_id=default_profile_id,
                    model=str(model_config.get("model") or ""),
                    capability="chat",
                    message_count=len(messages),
                ),
                **_run_event_fence(started_run, status="running"),
            ) is None:
                return self._get_run(run_id)
        broker_kwargs: dict[str, Any] = {}
        approval_required = runtime["tool_policy"].get("approval_required") or {}
        if approval_required:
            broker_kwargs["approvals"] = approval_required
        broker = self._tool_brokers.for_main_chat(
            run_id=run_id,
            workspace_policy=runtime["workspace_policy"],
            **broker_kwargs,
        )
        artifacts = [item for item in run.get("artifacts") or [] if isinstance(item, dict)]
        preserve_browser_target = False
        model_execution_succeeded = False
        try:
            original_goal_kwargs = (
                {"original_goal": user_goal}
                if supports_keyword(
                    self._continue_custom_api_agent,
                    "original_goal",
                )
                else {}
            )
            result_text = self._continue_custom_api_agent(
                agent,
                "",
                broker,
                timeline,
                artifacts,
                messages=messages,
                direct_tool_request=direct_tool_request,
                direct_tool_requests=direct_tool_requests,
                runtime_execution_envelope=runtime_execution_envelope,
                runtime_execution_metadata=runtime_execution_metadata,
                run_id=run_id,
                budget=budget,
                **original_goal_kwargs,
            )
            model_execution_succeeded = True
        except AgentApprovalRequired as exc:
            preserve_browser_target = True
            pending = self._main_chat_pending_approval(
                exc.pending_approval,
                model_profile_id=default_profile_id,
                tool_policy=runtime["tool_policy"],
                workspace_policy=runtime["workspace_policy"],
                runtime_execution_envelope=runtime_execution_envelope,
                runtime_execution_metadata=runtime_execution_metadata,
            )
            return self._approval_pause.project_tool_required(
                run_id,
                pending_approval=pending,
                timeline=timeline,
                artifacts=artifacts,
            )
        except Exception as exc:
            terminal = self._terminal_run_or_none(run_id)
            if terminal is not None:
                return terminal
            direct_partial = (
                _direct_clipboard_copy_partial(timeline)
                if (
                    direct_daily_desktop_intent
                    and _is_missing_chat_profile_error(exc)
                )
                else {}
            )
            provider_blocker = (
                _desktop_provider_required_failure(timeline)
                if (
                    direct_daily_desktop_intent
                    and not default_profile_id
                    and _is_missing_chat_profile_error(exc)
                    and not direct_partial
                )
                else {}
            )
            safe_error = str(
                direct_partial.get("summary")
                or provider_blocker.get("summary")
                or self._redact_secrets(exc)
            )
            direct_outcome_unverified = isinstance(
                exc,
                AgentDirectOutcomeUnverified,
            ) or bool(direct_partial)
            failure_event = (
                "agent.desktop.permission_recovery"
                if provider_blocker
                else "agent.desktop.intent_unverified"
                if direct_outcome_unverified
                else "model.request.failed"
            )
            unverified_payload = (
                {
                    "status": "failed",
                    "reason": (
                        str(direct_partial.get("reason") or "")
                        if direct_partial
                        else exc.reason
                    ),
                    **(
                        {
                            "tool_call_id": str(
                                direct_partial.get("tool_call_id") or ""
                            )
                        }
                        if direct_partial.get("tool_call_id")
                        else {"tool_call_id": exc.tool_call_id}
                        if isinstance(exc, AgentDirectOutcomeUnverified)
                        and exc.tool_call_id
                        else {}
                    ),
                    **(
                        {"tool": str(direct_partial.get("tool") or "")}
                        if direct_partial.get("tool")
                        else {"tool": exc.tool_name}
                        if isinstance(exc, AgentDirectOutcomeUnverified)
                        and exc.tool_name
                        else {}
                    ),
                    **(
                        {
                            "input_preview": dict(
                                direct_partial.get("input_preview") or {}
                            )
                        }
                        if direct_partial
                        else {"input_preview": exc.input_preview}
                        if isinstance(exc, AgentDirectOutcomeUnverified)
                        and exc.input_preview
                        else {}
                    ),
                }
                if direct_outcome_unverified
                else {}
            )
            next_timeline = [
                *timeline,
                self._timeline(
                    failure_event,
                    safe_error,
                    **(
                        {
                            "status": "blocked",
                            "recovery_actions": provider_blocker["recovery_actions"],
                            "blocking_conditions": provider_blocker[
                                "blocking_conditions"
                            ],
                        }
                        if provider_blocker
                        else unverified_payload
                    ),
                ),
            ]
            event_payload = (
                {
                    "error": safe_error,
                    "status": "blocked",
                    "recovery_actions": provider_blocker["recovery_actions"],
                    "blocking_conditions": provider_blocker["blocking_conditions"],
                }
                if provider_blocker
                else {
                    "error": safe_error,
                    **unverified_payload,
                }
                if direct_outcome_unverified
                else self._task_model_events.model_request_failed_payload(safe_error)
            )
            failed_run = self._fail_main_chat_run(
                run_id,
                safe_error,
                timeline=next_timeline,
                artifacts=artifacts,
                run_events=[(failure_event, event_payload)],
            )
            timeline[:] = [
                event
                for event in failed_run.get("timeline") or next_timeline
                if isinstance(event, dict)
            ]
            if str(failed_run.get("status") or "").strip().lower() != "failed":
                return failed_run
            if provider_blocker:
                raise self._error_type(safe_error) from exc
            if direct_partial:
                raise AgentDirectOutcomeUnverified(
                    safe_error,
                    reason=str(direct_partial.get("reason") or "direct_partial"),
                    tool_name=str(direct_partial.get("tool") or ""),
                    input_preview=dict(direct_partial.get("input_preview") or {}),
                    tool_call_id=str(direct_partial.get("tool_call_id") or ""),
                ) from exc
            raise
        finally:
            if not preserve_browser_target and not model_execution_succeeded:
                close_owned_browser_target_best_effort(broker)
        try:
            terminal = self._terminal_run_or_none(run_id)
            if terminal is not None:
                return terminal

            next_timeline = [
                *timeline,
                self._timeline(
                    "model.output.ready",
                    result_text[:500],
                    output_chars=len(result_text),
                    truncated=bool(getattr(result_text, "output_truncated", False)),
                ),
            ]
            scope = (
                self._transaction_scope()
                if self._transaction_scope is not None
                else nullcontext()
            )
            with scope:
                projected, committed = self._cas_from_running(
                    run_id,
                    status="running",
                    result=result_text,
                    timeline=next_timeline,
                    artifacts=artifacts,
                    pending_approval=None,
                )
                if not committed:
                    return projected
                _require_run_event(
                    self._append_run_event(
                        run_id,
                        "model.output.completed",
                        self._task_model_events.model_output_completed_payload(
                            str(result_text),
                            truncated=bool(
                                getattr(result_text, "output_truncated", False)
                            ),
                            metadata=self._model_output_metadata(result_text),
                        ),
                        **_run_event_fence(projected, status="running"),
                    )
                )
            timeline[:] = next_timeline
            return projected
        finally:
            close_owned_browser_target_best_effort(broker)

    def _project_initial_planning_clarification(
        self,
        run_id: str,
        *,
        user_goal: str,
        resolution: ModelIntentClarificationResolution,
        timeline: list[dict[str, Any]],
    ) -> dict[str, Any]:
        if resolution.original_goal != user_goal:
            raise ValueError("model_intent_clarification_goal_conflict")
        validated = ModelIntentClarificationResolution(
            original_goal=user_goal,
            question=self._redact_secrets(resolution.question),
        )
        event_payload = {
            "question": validated.question,
            "original_goal": user_goal,
            "status": "awaiting_user",
            "source": "runtime_model_intent_planner",
            "visibility": "internal",
        }
        next_timeline = [
            *timeline,
            self._timeline(
                "agent.plan.clarification_required",
                validated.question,
                **event_payload,
            ),
        ]
        projected, committed = self._cas_from_running(
            run_id,
            status="awaiting_user",
            result=validated.question,
            timeline=next_timeline,
            pending_approval=None,
        )
        if not committed:
            return projected
        if self._append_run_event(
            run_id,
            "agent.plan.clarification_required",
            event_payload,
            **_run_event_fence(projected, status="awaiting_user"),
        ) is None:
            return self._get_run(run_id)
        timeline[:] = next_timeline
        return projected

    def _cas_from_running(
        self,
        run_id: str,
        **fields: Any,
    ) -> tuple[dict[str, Any], bool]:
        current = self._get_run(run_id)
        if not _run_accepts_model_loop_projection(current):
            return current, False
        updated = self._update_run(
            run_id,
            **fields,
            expected_status="running",
            expected_updated_at=str(current.get("updated_at") or ""),
            expected_pending_approval_absent=True,
        )
        if updated is None:
            return self._get_run(run_id), False
        return updated, True

    @staticmethod
    def _authoritative_runtime_plan_can_complete_without_model(
        allowed_tools: list[str],
        *,
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
    ) -> bool:
        allowed = {
            str(tool or "").strip()
            for tool in allowed_tools
            if str(tool or "").strip()
        }
        explicit_requests: list[Any] = list(direct_tool_requests or [])
        if direct_tool_request is not None:
            explicit_requests.append(direct_tool_request)
        if explicit_requests and any(
            not isinstance(request, dict)
            or str(request.get("tool") or "").strip() not in allowed
            for request in explicit_requests
        ):
            return False
        requests = [
            request for request in explicit_requests if isinstance(request, dict)
        ]
        requests.extend(
            runtime_execution_requests_from_envelope_payload(
                runtime_execution_envelope,
                allowed_tools=allowed_tools,
            )
        )
        if not requests or any(
            bool(request.get("continue_to_model")) for request in requests
        ):
            return False
        return daily_desktop_requests_can_complete_without_model(requests)

    @staticmethod
    def _will_handle_daily_desktop_intent(
        messages: list[dict[str, Any]],
        allowed_tools: list[str],
        *,
        direct_tool_request: dict[str, Any] | None = None,
        direct_tool_requests: list[dict[str, Any]] | None = None,
        runtime_execution_envelope: dict[str, Any] | None = None,
    ) -> bool:
        allowed = {
            str(tool or "").strip()
            for tool in allowed_tools
            if str(tool or "").strip()
        }
        explicit_requests: list[Any] = list(direct_tool_requests or [])
        if direct_tool_request is not None:
            explicit_requests.append(direct_tool_request)
        if explicit_requests:
            if any(
                not isinstance(request, dict)
                or str(request.get("tool") or "").strip() not in allowed
                for request in explicit_requests
            ):
                return False
            return daily_desktop_requests_can_complete_without_model(
                [
                    request
                    for request in explicit_requests
                    if isinstance(request, dict)
                ]
            )
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope,
            allowed_tools=allowed_tools,
        )
        if requests and not any(
            bool(request.get("continue_to_model"))
            for request in requests
            if isinstance(request, dict)
        ):
            return True
        if daily_desktop_requests_can_complete_without_model(requests):
            return True
        if messages_require_model_first(messages):
            return False
        intent_text = _latest_user_intent_text(messages)
        if not intent_text:
            return False
        try:
            selection = planner_first_direct_tool_selection(
                intent_text,
                allowed_tools,
            )
            planned_requests = selection.requests
            if planned_requests:
                if not any(
                    bool(request.get("continue_to_model"))
                    for request in planned_requests
                    if isinstance(request, dict)
                ):
                    return True
                if daily_desktop_requests_can_complete_without_model(planned_requests):
                    return True
                return planner_discovered_app_followup_can_direct_execute(
                    selection.event_payload,
                    planned_requests,
                    allowed_tools,
                    allow_open_path=True,
                )
        except Exception:
            pass
        return False


def _run_accepts_model_loop_projection(run: dict[str, Any]) -> bool:
    return (
        str(run.get("status") or "").strip().lower() == "running"
        and not run.get("pending_approval")
    )


def _direct_clipboard_copy_partial(
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Project a source-unverified copy/read chain without a model fallback."""

    read_index = -1
    read_event: dict[str, Any] = {}
    for index in range(len(timeline) - 1, -1, -1):
        candidate = timeline[index]
        if str(candidate.get("event") or "").strip() != "agent.tool.call":
            continue
        if str(candidate.get("detail") or candidate.get("tool") or "").strip() != (
            "clipboard.read"
        ):
            continue
        result = (
            candidate.get("result")
            if isinstance(candidate.get("result"), dict)
            else {}
        )
        if result.get("ok") is True:
            read_index = index
            read_event = candidate
            break
    if read_index < 0:
        return {}

    read_plan_id = str(read_event.get("plan_id") or "").strip()
    read_step_id = str(
        read_event.get("step_id") or read_event.get("planner_step_id") or ""
    ).strip()
    if not read_plan_id or not read_step_id:
        return {}
    copy_event: dict[str, Any] = {}
    for candidate in reversed(timeline[:read_index]):
        if str(candidate.get("event") or "").strip() != "agent.tool.call":
            continue
        if str(candidate.get("detail") or candidate.get("tool") or "").strip() not in {
            "desktop.safe_shortcut",
            "desktop.shortcut",
        }:
            continue
        if str(candidate.get("plan_id") or "").strip() != read_plan_id:
            continue
        input_preview = (
            candidate.get("input_preview")
            if isinstance(candidate.get("input_preview"), dict)
            else {}
        )
        result = (
            candidate.get("result")
            if isinstance(candidate.get("result"), dict)
            else {}
        )
        if (
            str(input_preview.get("action") or "").strip().lower() == "copy"
            and result.get("ok") is True
            and result.get("postcondition_verified") is not True
        ):
            copy_event = candidate
            break
    if not copy_event:
        return {}

    read_result = (
        read_event.get("result")
        if isinstance(read_event.get("result"), dict)
        else {}
    )
    read_data = (
        read_result.get("data")
        if isinstance(read_result.get("data"), dict)
        else {}
    )
    if (
        read_result.get("clipboard_source_verified") is True
        or read_data.get("clipboard_source_verified") is True
    ):
        return {}
    observed_text = str(read_data.get("text") or "")
    preview = f"当前剪贴板内容：{observed_text}。" if observed_text else ""
    if read_data.get("truncated") is True and preview:
        preview = preview.rstrip("。") + "（已截断预览）。"
    return {
        "reason": "clipboard_copy_source_unverified",
        "tool": "clipboard.read",
        "tool_call_id": str(read_event.get("tool_call_id") or "").strip(),
        "input_preview": {},
        "summary": (
            "已读取剪贴板，但无法确认内容来自当前选区，"
            f"因此没有把任务标记为完成。{preview}"
        ),
    }


def _run_event_fence(
    run: dict[str, Any],
    *,
    status: str,
) -> dict[str, str]:
    return {
        "expected_status": status,
        "expected_updated_at": str(run.get("updated_at") or ""),
    }


def _require_run_event(event: Any) -> None:
    if event is None:
        raise AgentRuntimeError("run_event_fence_mismatch")


def _latest_user_intent_text(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if str(message.get("role") or "") != "user":
            continue
        content = message_visible_content_text(message).strip()
        if content:
            return content
    return ""


def _timeline_has_goal_contract_event(timeline: list[dict[str, Any]]) -> bool:
    for event in timeline:
        payload = event.get("payload")
        event_type = str(
            event.get("event")
            or event.get("event_type")
            or (
                payload.get("event") or payload.get("event_type")
                if isinstance(payload, dict)
                else ""
            )
            or ""
        ).strip()
        if event_type == "agent.goal.contract":
            return True
    return False


_DESKTOP_PROVIDER_BLOCKING_CONDITIONS = {
    "cua_driver_not_installed",
    "desktop_backend_not_release_ready",
    "desktop_execution_provider_adapter_unavailable",
    "desktop_execution_provider_unavailable",
    "desktop_provider_authentication_required",
    "desktop_provider_missing_required_tools",
    "loopback_desktop_backend",
    "real_virtual_desktop_backend_required",
    "sandbox_desktop_provider_required",
    "sandbox_keyboard_mouse_provider_required",
}


def _desktop_provider_required_failure(
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    for event in reversed(timeline):
        if str(event.get("event") or "").strip() != "agent.tool.skipped":
            continue
        result = event.get("result") if isinstance(event.get("result"), dict) else {}
        if str(result.get("error") or "").strip() != "desktop_execution_policy_blocked":
            continue
        blockers = sorted(
            {
                str(value or "").strip()
                for value in result.get("blocking_conditions") or []
                if str(value or "").strip() in _DESKTOP_PROVIDER_BLOCKING_CONDITIONS
            }
        )
        if not blockers:
            continue
        recovery_actions = [
            dict(action)
            for action in result.get("recovery_actions") or []
            if isinstance(action, dict)
        ]
        route = result.get("desktop_execution_route")
        route = route if isinstance(route, dict) else {}
        execution_provider = result.get("desktop_execution_provider")
        execution_provider = (
            execution_provider if isinstance(execution_provider, dict) else {}
        )
        sandbox_provider = result.get("sandbox_provider")
        sandbox_provider = sandbox_provider if isinstance(sandbox_provider, dict) else {}
        provider_kind = str(
            route.get("selected_provider_kind")
            or route.get("provider_kind")
            or execution_provider.get("provider_kind")
            or sandbox_provider.get("provider_kind")
            or ""
        ).strip()
        background_provider = provider_kind == "background_desktop"
        if not recovery_actions and not background_provider:
            recovery_actions = [
                {
                    "tool": "desktop.provider_session.start",
                    "label": "Start isolated desktop provider",
                    "input": {"diagnostic_route": "/yachiyo/studio/tools"},
                    "planning_reason": "desktop_provider_session_recovery",
                    "permission_target": "isolated_desktop_provider",
                    "risk_level": "medium",
                    "approval_required": True,
                    "approval_status": "pending",
                }
            ]
        real_provider_required = bool(
            {"loopback_desktop_backend", "real_virtual_desktop_backend_required"}
            .intersection(blockers)
        )
        return {
            "blocking_conditions": blockers,
            "recovery_actions": recovery_actions,
            "summary": (
                "后台操作组件尚未安装、授权或连接成功；任务已暂停，未接管你正在使用的桌面。"
                "请在工具中心完成后台操作设置后重试。"
                if background_provider
                else "桌面任务需要真实的隔离桌面 Provider；当前开发 Provider 不能执行此前台操作。"
                "请在 Agent Studio 配置 Provider 后重试，或选择受监督执行。"
                if real_provider_required
                else "桌面任务需要隔离桌面 Provider，当前环境尚未就绪。"
                "请在 Agent Studio 配置 Provider 后重试，或选择受监督执行。"
            ),
        }
    return {}


def _is_missing_chat_profile_error(error: Exception) -> bool:
    detail = str(error or "").strip()
    return bool(
        "缺少可运行的 Chat Profile" in detail
        or detail == "native_agent_not_ready:chat_model_profile_required"
    )
