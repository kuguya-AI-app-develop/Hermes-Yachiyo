"""Tests for Agent Run preparation split out of the legacy runtime."""

from __future__ import annotations

from pathlib import Path
from typing import Any
import pytest

from apps.shell import agent_runtime
from apps.shell.agent.runtime.agent_preparation import RuntimeAgentRunPreparer
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore


class FakeTimeline:
    def started(self, agent_name: str, *, backend: str, runtime: str) -> dict[str, Any]:
        return {
            "event": "agent.started",
            "agent_name": agent_name,
            "backend": backend,
            "runtime": runtime,
        }

    def compiled(self, *, allowed_tools: list[str]) -> dict[str, Any]:
        return {"event": "agent.runtime.compiled", "allowed_tools": allowed_tools}


class FakeRunEvents:
    def __init__(self) -> None:
        self.started_calls: list[dict[str, Any]] = []

    def started(self, run_id: str, **payload: Any) -> None:
        self.started_calls.append({"run_id": run_id, **payload})


class FakeTraceEvents:
    def memory_retrieved_payload(self, memories: list[dict[str, Any]]) -> dict[str, Any]:
        return {"memory_count": len(memories), "memories": memories}


class FakeMemoryStore:
    def __init__(self, memories: list[dict[str, Any]]) -> None:
        self.memories = memories
        self.query_calls: list[Any] = []

    def query(self, query: Any) -> list[dict[str, Any]]:
        self.query_calls.append(query)
        return self.memories


class FakeBroker:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def artifact_write(self, path: str, content: str) -> dict[str, Any]:
        self.writes.append((path, content))
        return {"path": path, "bytes": len(content)}


class FakeSharedToolBrokers:
    def __init__(self) -> None:
        self.broker = FakeBroker()
        self.calls: list[dict[str, Any]] = []

    def for_run(
        self,
        *,
        run_id: str,
        workspace_policy: dict[str, Any],
        approvals: dict[str, bool] | None = None,
        default_runnable_id: str = "",
        skills: list[dict[str, Any]] | None = None,
        foreground_lock_key: str = "",
        foreground_lock_owner: str = "",
    ) -> FakeBroker:
        call = {
            "run_id": run_id,
            "workspace_policy": workspace_policy,
            "default_runnable_id": default_runnable_id,
            "skills": skills,
        }
        if approvals:
            call["approvals"] = approvals
        if foreground_lock_key:
            call["foreground_lock_key"] = foreground_lock_key
        if foreground_lock_owner:
            call["foreground_lock_owner"] = foreground_lock_owner
        self.calls.append(call)
        return self.broker


def _timeline(event: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"event": event, "detail": detail, **extra}


def _preparer(
    tmp_path: Path,
    state: dict[str, Any],
    *,
    tool_brokers: Any | None = None,
) -> RuntimeAgentRunPreparer:
    run_events = state.setdefault("run_events", [])
    memory_store_calls = state.setdefault("memory_store_calls", [])
    future_task_store_calls = state.setdefault("future_task_store_calls", [])

    def compile_agent_runtime(_agent: dict[str, Any]) -> dict[str, Any]:
        return {
            "runtime": "oha_agent",
            "tool_policy": {"allowed_tools": ["workspace.read", "artifact.write"]},
            "workspace_policy": {"default_workdir": "/tmp/project"},
        }

    def load_agent_skills(skill_ids: list[str]) -> list[dict[str, Any]]:
        state["skill_ids"] = skill_ids
        return [{"skill_id": "skill-1", "name": "Brief Reader"}]

    def agent_context(
        _agent: dict[str, Any],
        user_goal: str,
        upstream: str = "",
        *,
        skills: list[dict[str, Any]] | None = None,
    ) -> str:
        state["context_args"] = {
            "user_goal": user_goal,
            "upstream": upstream,
            "skills": skills,
        }
        return "model visible context"

    def memory_store(**kwargs: Any) -> FakeMemoryStore:
        memory_store_calls.append(kwargs)
        return FakeMemoryStore([{"memory_id": "memory-1", "content": "Remember this"}])

    def future_task_store(**kwargs: Any) -> object:
        future_task_store_calls.append(kwargs)
        return object()

    def tool_broker_factory(workspace_policy: dict[str, Any], artifact_root: Path, **kwargs: Any) -> FakeBroker:
        broker = FakeBroker()
        state["broker_args"] = {
            "workspace_policy": workspace_policy,
            "artifact_root": artifact_root,
            **kwargs,
        }
        state["broker"] = broker
        return broker

    return RuntimeAgentRunPreparer(
        agent_artifacts_dir=tmp_path / "artifacts",
        normalize_execution_backend=lambda backend, *, model_mode: f"{backend or model_mode}-normalized",
        compile_agent_runtime=compile_agent_runtime,
        load_agent_skills=load_agent_skills,
        agent_context=agent_context,
        memory_store=memory_store,
        future_task_store=future_task_store,
        tool_broker_factory=tool_broker_factory,
        runtime_agent_timeline=FakeTimeline(),
        runtime_agent_run_events=state.setdefault("run_event_recorder", FakeRunEvents()),
        runtime_trace_events=FakeTraceEvents(),
        append_run_event=lambda run_id, event_type, payload: run_events.append((run_id, event_type, payload)),
        timeline_factory=_timeline,
        memory_context_limit=12,
        tool_brokers=tool_brokers,
    )


def test_agent_run_preparer_builds_started_timeline_context_and_broker(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    preparer = _preparer(tmp_path, state)

    preparation = preparer.prepare(
        "run-1",
        {
            "agent_id": "agent-1",
            "name": "Prep Agent",
            "model_mode": "custom_api",
            "skill_ids": ["skill-1"],
        },
        "Reply with exactly 'Done'",
        "Parent context",
    )

    assert preparation.backend == "custom_api-normalized"
    assert preparation.runtime["runtime"] == "oha_agent"
    assert preparation.timeline == [
        {
            "event": "agent.started",
            "agent_name": "Prep Agent",
            "backend": "custom_api-normalized",
            "runtime": "oha_agent",
        },
        {"event": "agent.runtime.compiled", "allowed_tools": ["workspace.read", "artifact.write"]},
    ]
    assert state["run_event_recorder"].started_calls == [
        {
            "run_id": "run-1",
            "agent_id": "agent-1",
            "agent_name": "Prep Agent",
            "backend": "custom_api-normalized",
            "runtime": "oha_agent",
        }
    ]
    assert state["skill_ids"] == ["skill-1"]
    assert state["context_args"] == {
        "user_goal": "Reply with exactly 'Done'",
        "upstream": "Parent context",
        "skills": [{"skill_id": "skill-1", "name": "Brief Reader"}],
    }
    assert preparation.artifact_root == tmp_path / "artifacts" / "run-1"
    assert preparation.context == "model visible context"
    assert state["memory_store_calls"] == [{"source_run_id": "run-1"}]
    assert state["future_task_store_calls"] == [
        {"source_run_id": "run-1", "default_runnable_id": "agent-1"}
    ]
    assert state["broker_args"]["workspace_policy"] == {"default_workdir": "/tmp/project"}
    assert state["broker_args"]["artifact_root"] == tmp_path / "artifacts" / "run-1"
    assert state["broker_args"]["skills"] == [{"skill_id": "skill-1", "name": "Brief Reader"}]
    assert preparation.artifacts == []


def test_preparer_uses_entrypoint_contract_before_deterministic_recompilation(tmp_path, monkeypatch):
    state = {}
    preparer = _preparer(tmp_path, state)
    goal = "Read exactly README.md and then LICENSE"
    contract = GoalContract(contract_id="entrypoint-goal", original_goal=goal, criteria=(
        GoalCriterion(criterion_id="read-both", description="Read both requested inputs",
                      source_step_ids=("read-first", "read-second")),
    ))

    def reject_second_compile(*args, **kwargs):
        raise AssertionError("The entrypoint plan must retain its original authority")

    monkeypatch.setattr("apps.shell.agent.runtime.agent_preparation.planned_goal_contract_payload",
                        reject_second_compile)
    result = preparer.prepare("run-1", {"agent_id": "agent-1", "name": "Reader"}, goal,
                              runtime_execution_envelope={"task_core": {"goal_contract": contract.to_payload()}})
    assert result.goal_contract == contract.bind_run("run-1").to_payload()
    assert state["context_args"]["user_goal"] == goal


@pytest.mark.parametrize("mutation", ["wrong_goal", "invalid", "response_only_effect"])
def test_preparer_rejects_conflicting_or_invalid_envelope_before_broker(tmp_path, mutation):
    state = {}
    preparer = _preparer(tmp_path, state)
    goal = "Read exactly README.md"
    contract = GoalContract(contract_id="entrypoint-goal", original_goal=goal, criteria=(
        GoalCriterion(criterion_id="read", description="Read the requested input",
                      source_step_ids=("read",)),
    )).to_payload()
    if mutation == "wrong_goal": contract["original_goal"] = "Read LICENSE"
    elif mutation == "invalid": contract["criteria"] = []
    else: contract["criteria"][0]["response_satisfiable"] = True
    with pytest.raises(ValueError, match="goal_contract_(?:conflict|invalid)"):
        preparer.prepare("run-1", {"agent_id": "agent-1", "name": "Reader"}, goal,
                         runtime_execution_envelope={"task_core": {"goal_contract": contract}})
    assert "broker_args" not in state


def test_agent_run_preparer_can_use_shared_tool_broker_factory(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    tool_brokers = FakeSharedToolBrokers()
    preparer = _preparer(tmp_path, state, tool_brokers=tool_brokers)

    preparation = preparer.prepare(
        "run-1",
        {
            "agent_id": "agent-1",
            "name": "Prep Agent",
            "skill_ids": ["skill-1"],
        },
        "Reply with exactly 'Done'",
    )

    assert preparation.broker is tool_brokers.broker
    assert preparation.artifact_root == tmp_path / "artifacts" / "run-1"
    assert tool_brokers.calls == [
        {
            "run_id": "run-1",
            "workspace_policy": {"default_workdir": "/tmp/project"},
            "default_runnable_id": "agent-1",
            "skills": [{"skill_id": "skill-1", "name": "Brief Reader"}],
        }
    ]
    assert state["memory_store_calls"] == []
    assert state["future_task_store_calls"] == []
    assert "broker_args" not in state


def test_agent_run_preparer_scopes_shared_tool_broker_to_run_group(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    tool_brokers = FakeSharedToolBrokers()
    preparer = _preparer(tmp_path, state, tool_brokers=tool_brokers)

    preparation = preparer.prepare(
        "run-1",
        {
            "agent_id": "agent-1",
            "name": "Prep Agent",
            "skill_ids": ["skill-1"],
        },
        "Reply with exactly 'Done'",
        run_group_id="group-1",
    )

    assert preparation.broker is tool_brokers.broker
    assert tool_brokers.calls == [
        {
            "run_id": "run-1",
            "workspace_policy": {"default_workdir": "/tmp/project"},
            "default_runnable_id": "agent-1",
            "skills": [{"skill_id": "skill-1", "name": "Brief Reader"}],
            "foreground_lock_key": "group-1",
            "foreground_lock_owner": "group-1:run-1",
        }
    ]


def test_agent_run_preparer_scopes_shared_tool_broker_to_workflow_run(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    tool_brokers = FakeSharedToolBrokers()
    preparer = _preparer(tmp_path, state, tool_brokers=tool_brokers)

    preparation = preparer.prepare(
        "child-run-1",
        {
            "agent_id": "agent-1",
            "name": "Prep Agent",
            "skill_ids": ["skill-1"],
        },
        "Reply with exactly 'Done'",
        workflow_run_id="workflow-run-1",
    )

    assert preparation.broker is tool_brokers.broker
    assert tool_brokers.calls == [
        {
            "run_id": "child-run-1",
            "workspace_policy": {"default_workdir": "/tmp/project"},
            "default_runnable_id": "agent-1",
            "skills": [{"skill_id": "skill-1", "name": "Brief Reader"}],
            "foreground_lock_key": "workflow:workflow-run-1",
            "foreground_lock_owner": "workflow:workflow-run-1:child-run-1",
        }
    ]


def test_agent_run_preparer_writes_observable_context_artifact(tmp_path: Path) -> None:
    state: dict[str, Any] = {}
    preparer = _preparer(tmp_path, state)
    preparation = preparer.prepare(
        "run-1",
        {"agent_id": "agent-1", "name": "Prep Agent", "skill_ids": ["skill-1"]},
        "Reply with exactly 'Done'",
    )

    artifact = preparer.write_context_artifact("run-1", preparation)

    assert artifact == {"path": "agent-context.md", "bytes": len("model visible context")}
    assert state["memory_store_calls"] == [{"source_run_id": "run-1"}, {}]
    assert state["run_events"] == [
        (
            "run-1",
            "memory.retrieved",
            {
                "memory_count": 1,
                "memories": [{"memory_id": "memory-1", "content": "Remember this"}],
            },
        ),
        (
            "run-1",
            "agent.artifact.write",
            {
                "kind": "agent_artifact",
                "artifact": {"path": "agent-context.md", "bytes": len("model visible context")},
                "path": "agent-context.md",
                "bytes": len("model visible context"),
            },
        ),
    ]
    assert state["broker"].writes == [("agent-context.md", "model visible context")]
    assert preparation.artifacts == [{"kind": "context", "path": "agent-context.md", "bytes": 21}]
    assert preparation.timeline[-1] == {
        "event": "agent.artifact.write",
        "detail": "agent-context.md",
        "artifact": {"path": "agent-context.md", "bytes": 21},
    }


def test_native_runtime_uses_split_agent_run_preparer(tmp_path: Path) -> None:
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    try:
        assert agent_runtime.RuntimeAgentRunPreparer is RuntimeAgentRunPreparer
        assert isinstance(service.agent_run_preparer, RuntimeAgentRunPreparer)
    finally:
        service.close()
