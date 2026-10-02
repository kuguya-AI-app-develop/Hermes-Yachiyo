"""Agent run preparation helpers for observable runtime execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from apps.shell.agent.repositories.memories import MemoryQuery
from apps.shell.agent.runtime.foreground_lock_scope import foreground_lock_broker_kwargs
from apps.shell.agent.runtime.goal_runtime import planned_goal_contract_payload, runtime_goal_contract


@dataclass
class AgentRunPreparation:
    backend: str
    runtime: dict[str, Any]
    timeline: list[dict[str, Any]]
    artifact_root: Path
    skills: list[dict[str, Any]]
    context: str
    goal_contract: dict[str, Any]
    broker: Any
    artifacts: list[dict[str, Any]]


class RuntimeAgentRunPreparer:
    """Builds the model-visible context and first observable artifacts for an Agent Run."""

    def __init__(
        self,
        *,
        agent_artifacts_dir: Path,
        normalize_execution_backend: Callable[..., str],
        compile_agent_runtime: Callable[[dict[str, Any]], dict[str, Any]],
        load_agent_skills: Callable[[list[str]], list[dict[str, Any]]],
        agent_context: Callable[..., str],
        memory_store: Callable[..., Any],
        future_task_store: Callable[..., Any],
        runtime_agent_timeline: Any,
        runtime_agent_run_events: Any,
        runtime_trace_events: Any,
        append_run_event: Callable[[str, str, dict[str, Any]], Any],
        timeline_factory: Callable[..., dict[str, Any]],
        memory_context_limit: int,
        tool_broker_factory: Callable[..., Any] | None = None,
        tool_brokers: Any | None = None,
    ) -> None:
        self._agent_artifacts_dir = agent_artifacts_dir
        self._normalize_execution_backend = normalize_execution_backend
        self._compile_agent_runtime = compile_agent_runtime
        self._load_agent_skills = load_agent_skills
        self._agent_context = agent_context
        self._memory_store = memory_store
        self._future_task_store = future_task_store
        self._tool_broker_factory = tool_broker_factory
        self._tool_brokers = tool_brokers
        self._runtime_agent_timeline = runtime_agent_timeline
        self._runtime_agent_run_events = runtime_agent_run_events
        self._runtime_trace_events = runtime_trace_events
        self._append_run_event = append_run_event
        self._timeline_factory = timeline_factory
        self._memory_context_limit = memory_context_limit

    def prepare(
        self,
        run_id: str,
        agent: dict[str, Any],
        user_goal: str,
        upstream: str = "",
        run_group_id: str = "",
        workflow_run_id: str = "",
        runtime_execution_envelope: dict[str, Any] | None = None,
    ) -> AgentRunPreparation:
        backend = self._normalize_execution_backend(
            agent.get("execution_backend"),
            model_mode=str(agent.get("model_mode") or "profile"),
        )
        runtime = self._compile_agent_runtime(agent)
        timeline = [
            self._runtime_agent_timeline.started(
                str(agent["name"]),
                backend=backend,
                runtime=runtime["runtime"],
            )
        ]
        self._runtime_agent_run_events.started(
            run_id,
            agent_id=str(agent.get("agent_id") or ""),
            agent_name=str(agent.get("name") or ""),
            backend=backend,
            runtime=runtime["runtime"],
        )
        timeline.append(
            self._runtime_agent_timeline.compiled(
                allowed_tools=runtime["tool_policy"].get("allowed_tools") or [],
            )
        )
        artifact_root = self._agent_artifacts_dir / run_id
        skills = self._load_agent_skills(agent.get("skill_ids") or [])
        model_goal_context = str(
            agent.get("_runtime_agent_goal_context") or user_goal
        ).strip()
        context = self._agent_context(
            agent,
            model_goal_context,
            upstream,
            skills=skills,
        )
        if runtime_execution_envelope:
            # The entrypoint planner already compiled this immutable goal.
            # Resolve it against the original objective before constructing a
            # broker; a second deterministic compilation can reject a valid
            # compound plan or introduce a conflicting completion contract.
            contract = runtime_goal_contract(
                run_id=run_id, original_goal=user_goal,
                runtime_execution_envelope=runtime_execution_envelope,
                runtime_execution_metadata=None, messages=[], timeline=[],
            )
            if contract is None:
                raise ValueError("goal_contract_missing")
            goal_contract = contract.to_payload()
        else:
            goal_contract = planned_goal_contract_payload(
                user_goal,
                allowed_tools=runtime["tool_policy"].get("allowed_tools") or [],
            )
        default_runnable_id = str(agent.get("agent_id") or "")
        approval_required = runtime["tool_policy"].get("approval_required") or {}
        if self._tool_brokers is not None:
            broker_kwargs: dict[str, Any] = foreground_lock_broker_kwargs(
                run_id=run_id,
                run_group_id=run_group_id,
                workflow_run_id=workflow_run_id,
            )
            if approval_required:
                broker_kwargs["approvals"] = approval_required
            broker = self._tool_brokers.for_run(
                run_id=run_id,
                workspace_policy=runtime["workspace_policy"],
                default_runnable_id=default_runnable_id,
                skills=skills,
                **broker_kwargs,
            )
        else:
            if self._tool_broker_factory is None:
                raise RuntimeError(
                    "Tool broker factory is required when shared tool brokers are not configured"
                )
            broker_kwargs: dict[str, Any] = {
                "skills": skills,
                "memory_store": self._memory_store(source_run_id=run_id),
                "future_task_store": self._future_task_store(
                    source_run_id=run_id,
                    default_runnable_id=default_runnable_id,
                ),
            }
            if approval_required:
                broker_kwargs["approvals"] = approval_required
            broker = self._tool_broker_factory(
                runtime["workspace_policy"],
                artifact_root,
                **broker_kwargs,
            )
        return AgentRunPreparation(
            backend=backend,
            runtime=runtime,
            timeline=timeline,
            artifact_root=artifact_root,
            skills=skills,
            context=context,
            goal_contract=goal_contract,
            broker=broker,
            artifacts=[],
        )

    def write_context_artifact(self, run_id: str, preparation: AgentRunPreparation) -> dict[str, Any]:
        retrieved_memories = self._memory_store().query(
            MemoryQuery(limit=self._memory_context_limit),
        )
        self._append_run_event(
            run_id,
            "memory.retrieved",
            self._runtime_trace_events.memory_retrieved_payload(retrieved_memories),
        )
        artifact = preparation.broker.artifact_write("agent-context.md", preparation.context)
        preparation.artifacts.append({"kind": "context", **artifact})
        preparation.timeline.append(
            self._timeline_factory("agent.artifact.write", "agent-context.md", artifact=artifact)
        )
        self._append_run_event(
            run_id,
            "agent.artifact.write",
            {"kind": "agent_artifact", "artifact": artifact, **artifact},
        )
        return artifact
