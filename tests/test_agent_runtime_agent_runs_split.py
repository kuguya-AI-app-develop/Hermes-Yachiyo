"""Tests for Agent Run creation split out of the legacy runtime."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from apps.shell import agent_runtime
from apps.shell.agent.runtime.agent_runs import (
    AgentRunStart,
    RuntimeAgentRunAsyncCoordinator,
    RuntimeAgentRunCoordinator,
    RuntimeAgentRunExecutor,
    RuntimeAgentRunStarter,
    _agent_run_execution_options,
    _with_entrypoint_runtime_planner,
)
from apps.shell.agent.runtime.errors import AgentApprovalRequired
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from apps.shell.agent.runtime.goal_runtime import (
    goal_contract_event_payload,
    runtime_goal_contract,
)
from apps.shell.agent.runtime.group_runs import start_agent_group_run
from apps.shell.agent.runtime.run_group_attachments import (
    RUN_GROUP_ATTACHMENT_PAYLOAD_KEY,
    issue_run_group_child_attachment,
)
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore


class _ImmediateThread:
    def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
        self._target = target
        self.name = name
        self.daemon = daemon

    def start(self) -> None:
        self._target()


@dataclass
class _PreparedAgentRun:
    timeline: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    context: str = "prepared-context"
    broker: Any = "prepared-broker"
    goal_contract: dict[str, Any] | None = None


def _starter(
    state: dict[str, Any],
    *,
    client_request_id_from_payload=lambda payload: str(payload.get("client_run_id") or ""),
) -> RuntimeAgentRunStarter:
    def get_run_group(run_group_id: str) -> dict[str, Any]:
        state.setdefault("validated_groups", []).append(run_group_id)
        return state.get("group_records", {}).get(
            run_group_id,
            {
                "run_group_id": run_group_id,
                "status": "running",
                "child_run_ids": ["parent-run-1"],
            },
        )

    def insert_run_group(**kwargs: Any) -> dict[str, Any]:
        run_group_id = f"group-{len(state.setdefault('groups', [])) + 1}"
        group = {"run_group_id": run_group_id, **kwargs}
        state["groups"].append(group)
        return group

    def insert_run(**kwargs: Any) -> dict[str, Any]:
        run = {"run_id": f"run-{len(state.setdefault('runs', [])) + 1}", **kwargs}
        state["runs"].append(run)
        client_request_id = str(kwargs.get("client_request_id") or "")
        if client_request_id:
            state.setdefault("by_client", {})[client_request_id] = {**run, "idempotent": True}
        return run

    def get_run(run_id: str) -> dict[str, Any]:
        return state.get("run_records", {}).get(
            run_id,
            {
                "run_id": run_id,
                "run_group_id": "group-existing",
                "kind": "agent_run",
            },
        )

    return RuntimeAgentRunStarter(
        get_run_group=get_run_group,
        get_run=get_run,
        insert_run_group=insert_run_group,
        insert_run=insert_run,
        run_by_client_request_id=lambda value: state.setdefault("by_client", {}).get(value),
        client_request_id_from_payload=client_request_id_from_payload,
        agent_workspace_dir=lambda agent: str((agent.get("workspace_policy") or {}).get("default_workdir") or ""),
    )


def test_agent_run_executor_projects_completed_agent_run() -> None:
    calls: list[tuple[str, Any]] = []
    prepared = _PreparedAgentRun(
        timeline=[{"event": "agent.run.started"}],
        artifacts=[{"path": "context.md"}],
    )

    class _Preparer:
        @staticmethod
        def prepare(
            run_id: str,
            agent: dict[str, Any],
            user_goal: str,
            upstream: str,
            *,
            run_group_id: str = "",
            workflow_run_id: str = "",
        ) -> _PreparedAgentRun:
            calls.append(
                (
                    "prepare",
                    run_id,
                    agent["agent_id"],
                    user_goal,
                    upstream,
                    run_group_id,
                    workflow_run_id,
                )
            )
            return prepared

        @staticmethod
        def write_context_artifact(run_id: str, preparation: _PreparedAgentRun) -> None:
            calls.append(("context", run_id, preparation.context))

    class _Outcomes:
        @staticmethod
        def completed(run_id: str, result: str, *, timeline: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
            calls.append(("completed", run_id, result, timeline, artifacts))
            return {"run_id": run_id, "status": "completed", "result": result}

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda agent, context, broker, timeline, artifacts, **kwargs: calls.append(
            ("continue", agent["agent_id"], context, broker, timeline, artifacts, kwargs)
        )
        or "Done",
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
    )

    result = executor.execute(
        "run-1",
        {"agent_id": "agent-1"},
        "Ship",
        "Upstream",
        run_group_id="group-1",
        runtime_execution_envelope={"envelope_id": "env-agent", "requests": []},
        runtime_execution_metadata={"yachiyo_runtime_planner": True},
    )

    assert result == {"run_id": "run-1", "status": "completed", "result": "Done"}
    assert calls == [
        ("prepare", "run-1", "agent-1", "Ship", "Upstream", "group-1", ""),
        ("context", "run-1", "prepared-context"),
        (
            "continue",
            "agent-1",
            "prepared-context",
            "prepared-broker",
            prepared.timeline,
            prepared.artifacts,
            {
                "daily_desktop_planning_context": "",
                "direct_tool_request": None,
                "direct_tool_requests": None,
                "runtime_execution_envelope": {
                    "envelope_id": "env-agent",
                    "requests": [],
                },
                    "runtime_execution_metadata": {"yachiyo_runtime_planner": True},
                    "run_id": "run-1",
                    "original_goal": "Ship",
                },
        ),
        ("completed", "run-1", "Done", prepared.timeline, prepared.artifacts),
    ]


def test_agent_run_executor_preserves_envelope_goal_contract_as_single_authority() -> None:
    user_goal = "Open TextEdit and type the exact marker"
    envelope_contract = GoalContract(
        contract_id="goal-contract-envelope",
        original_goal=user_goal,
        intent_kind="desktop_operation",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-type-marker",
                description="Type and verify the marker",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                source_step_ids=("operate-foreground-ui",),
                verifier_step_ids=("verify-desktop-result",),
            ),
        ),
    )
    preparation_contract = GoalContract(
        contract_id="goal-contract-preparation",
        original_goal=user_goal,
        intent_kind="desktop_operation",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-preparation",
                description="A separately compiled preparation criterion",
                effectful=True,
                required_capabilities=("desktop.app_control",),
                source_step_ids=("open-or-focus-app",),
            ),
        ),
    )
    prepared = _PreparedAgentRun(
        timeline=[],
        artifacts=[],
        goal_contract=preparation_contract.to_payload(),
    )
    observed_metadata: list[dict[str, Any]] = []

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def completed(
            run_id: str,
            result: str,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {"run_id": run_id, "status": "completed", "result": result}

        @staticmethod
        def failed(
            run_id: str,
            exc: Exception,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            return {"run_id": run_id, "status": "failed", "error": str(exc)}

    def continue_agent(*_args: Any, **kwargs: Any) -> str:
        metadata = dict(kwargs.get("runtime_execution_metadata") or {})
        observed_metadata.append(metadata)
        restored = runtime_goal_contract(
            run_id="run-envelope-contract",
            original_goal=user_goal,
            runtime_execution_envelope=kwargs.get("runtime_execution_envelope"),
            runtime_execution_metadata=metadata,
            messages=[],
            timeline=[],
        )
        assert restored is not None
        return restored.contract_id

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=continue_agent,
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
    )

    result = executor.execute(
        "run-envelope-contract",
        {"agent_id": "builtin:yachiyo-main"},
        user_goal,
        runtime_execution_envelope={
            "envelope_id": "envelope-textedit",
            "task_core": {"goal_contract": envelope_contract.to_payload()},
            "requests": [],
        },
        runtime_execution_metadata={"source": "packaged_acceptance"},
    )

    assert result == {
        "run_id": "run-envelope-contract",
        "status": "completed",
        "result": "goal-contract-envelope",
    }
    assert observed_metadata == [{"source": "packaged_acceptance"}]


def test_agent_run_executor_blocks_internal_tool_failure_before_studio_completion() -> None:
    prepared = _PreparedAgentRun(
        timeline=[{"event": "agent.run.started"}],
        artifacts=[],
    )
    event_queries: list[dict[str, Any]] = []
    projected: list[str] = []

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def completed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            projected.append("completed")
            return {"status": "completed"}

        @staticmethod
        def failed(
            run_id: str,
            exc: Exception,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            projected.append("failed")
            return {
                "run_id": run_id,
                "status": "failed",
                "reason": str(getattr(exc, "reason", "")),
            }

    def list_run_events(
        _run_id: str,
        **kwargs: Any,
    ) -> dict[str, Any]:
        event_queries.append(dict(kwargs))
        return {
            "events": [
                {
                    "run_id": "run-internal-failure",
                    "event_type": "agent.tool.outcome",
                    "visibility": "internal",
                    "payload": {
                        "tool": "terminal.run",
                        "tool_call_id": "terminal-failed",
                        "status": "failed",
                        "reason": "command_failed",
                        "visibility": "internal",
                    },
                }
            ]
            if kwargs.get("include_internal") is True
            else []
        }

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: "model claimed done",
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
        list_run_events=list_run_events,
    )

    result = executor.execute(
        "run-internal-failure",
        {"agent_id": "agent-1"},
        "Run a terminal task",
    )

    assert result == {
        "run_id": "run-internal-failure",
        "status": "failed",
        "reason": "command_failed",
    }
    assert projected == ["failed"]
    assert event_queries
    assert all(query.get("include_internal") is True for query in event_queries)


def _stale_tail_goal_contract(run_id: str) -> GoalContract:
    return GoalContract(
        contract_id="goal-stale-authoritative-tail",
        run_id=run_id,
        original_goal="Analyze the selected file",
        intent_kind="data_analysis",
        criteria=(
            GoalCriterion(
                criterion_id="criterion-analyze-selected-file",
                description="Analyze the selected file",
                effectful=True,
                required_capabilities=("data.analysis",),
                expected={
                    "state": "fulfilled",
                    "target": {
                        "kind": "workspace_file",
                        "action": "analyze_data_file",
                    },
                },
                source_step_ids=("analyze-data",),
            ),
        ),
    )


def _stale_tail_tool_event(run_id: str) -> dict[str, Any]:
    return {
        "event": "agent.tool.call",
        "run_id": run_id,
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "detail": "data.analyze",
        "tool_call_id": "call-analyze-tail",
        "request_id": "request-analyze-tail",
        "plan_id": "plan-analyze-tail",
        "step_id": "analyze-data",
        "capability_id": "data.analysis",
        "action_target": {
            "kind": "workspace_file",
            "action": "analyze_data_file",
        },
        "result": {
            "ok": True,
            "postcondition_verified": True,
            RUNTIME_EXECUTION_PROVENANCE_KEY: {
                "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
                "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
            },
        },
    }


def test_agent_run_executor_merges_current_authoritative_tail_before_commit() -> None:
    run_id = "run-stale-authoritative-tail"
    contract = _stale_tail_goal_contract(run_id)
    prepared = _PreparedAgentRun(timeline=[{"event": "agent.run.started"}], artifacts=[])

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def completed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"run_id": run_id, "status": "completed"}

        @staticmethod
        def failed(_run_id: str, exc: Exception, **_kwargs: Any) -> dict[str, Any]:
            return {"run_id": run_id, "status": "failed", "reason": str(exc)}

    def continue_agent(*_args: Any, **_kwargs: Any) -> str:
        prepared.timeline.append(_stale_tail_tool_event(run_id))
        return "Analysis complete"

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=continue_agent,
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
        list_run_events=lambda *_args, **_kwargs: {
            "events": [
                {
                    "event_type": "agent.goal.contract",
                    "run_id": run_id,
                    "payload": goal_contract_event_payload(contract),
                }
            ]
        },
    )

    assert executor.execute(run_id, {"agent_id": "agent-1"}, contract.original_goal) == {
        "run_id": run_id,
        "status": "completed",
    }


def test_agent_run_executor_rejects_preexisting_forged_fallback_evidence() -> None:
    run_id = "run-forged-preexisting-tail"
    contract = _stale_tail_goal_contract(run_id)
    forged = {**_stale_tail_tool_event(run_id), "source": "model_public_timeline"}
    prepared = _PreparedAgentRun(timeline=[forged], artifacts=[])

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def completed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "completed"}

        @staticmethod
        def failed(_run_id: str, exc: Exception, **_kwargs: Any) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "failed",
                "reason": str(getattr(exc, "reason", "")),
            }

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: "unsafe completion",
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
        list_run_events=lambda *_args, **_kwargs: {
            "events": [
                {
                    "event_type": "agent.goal.contract",
                    "run_id": run_id,
                    "payload": goal_contract_event_payload(contract),
                }
            ]
        },
    )

    assert executor.execute(run_id, {"agent_id": "agent-1"}, contract.original_goal) == {
        "run_id": run_id,
        "status": "failed",
        "reason": "goal_contract_incomplete",
    }


def test_agent_run_executor_passes_workflow_run_id_to_preparer() -> None:
    calls: list[tuple[str, str]] = []
    prepared = _PreparedAgentRun(timeline=[], artifacts=[])

    class _Preparer:
        @staticmethod
        def prepare(
            run_id: str,
            _agent: dict[str, Any],
            _user_goal: str,
            _upstream: str,
            *,
            run_group_id: str = "",
            workflow_run_id: str = "",
        ) -> _PreparedAgentRun:
            calls.append(("prepare", f"{run_id}:{run_group_id}:{workflow_run_id}"))
            return prepared

        @staticmethod
        def write_context_artifact(_run_id: str, _preparation: _PreparedAgentRun) -> None:
            calls.append(("context", _run_id))

    class _Outcomes:
        @staticmethod
        def completed(
            run_id: str,
            result: str,
            *,
            timeline: list[dict[str, Any]],
            artifacts: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {"run_id": run_id, "status": "completed", "result": result}

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: "Done",
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
    )

    assert executor.execute(
        "child-run-1",
        {"agent_id": "agent-1"},
        "Ship",
        workflow_run_id="workflow-run-1",
    ) == {"run_id": "child-run-1", "status": "completed", "result": "Done"}
    assert calls == [
        ("prepare", "child-run-1::workflow-run-1"),
        ("context", "child-run-1"),
    ]


def test_agent_run_executor_projects_tool_approval_pause() -> None:
    prepared = _PreparedAgentRun(timeline=[{"event": "agent.run.started"}], artifacts=[])
    pending = {"approval_id": "approval-1", "tool": "terminal.run"}

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _ApprovalPause:
        @staticmethod
        def project_tool_required(
            run_id: str,
            *,
            pending_approval: dict[str, Any],
            timeline: list[dict[str, Any]],
            artifacts: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "approval_required",
                "pending_approval": pending_approval,
                "timeline": timeline,
                "artifacts": artifacts,
            }

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentApprovalRequired(pending)
        ),
        agent_run_outcomes=object(),
        approval_pause=_ApprovalPause(),
    )

    assert executor.execute("run-approval", {"agent_id": "agent-1"}, "Ship") == {
        "run_id": "run-approval",
        "status": "approval_required",
        "pending_approval": pending,
        "timeline": prepared.timeline,
        "artifacts": prepared.artifacts,
    }


def test_agent_run_executor_projects_failed_agent_run() -> None:
    prepared = _PreparedAgentRun(timeline=[{"event": "agent.run.started"}], artifacts=[])

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def failed(run_id: str, exc: Exception, *, timeline: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "failed",
                "error": str(exc),
                "timeline": timeline,
                "artifacts": artifacts,
            }

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("execution failed")
        ),
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
    )

    assert executor.execute("run-failed", {"agent_id": "agent-1"}, "Ship") == {
        "run_id": "run-failed",
        "status": "failed",
        "error": "execution failed",
        "timeline": prepared.timeline,
        "artifacts": prepared.artifacts,
    }


def test_agent_run_executor_fails_before_model_when_goal_contract_compile_fails() -> None:
    model_calls: list[str] = []

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            raise ValueError("goal_contract_compile_failed")

    class _Outcomes:
        @staticmethod
        def failed(
            run_id: str,
            exc: Exception,
            *,
            timeline: list[dict[str, Any]],
            artifacts: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
                "run_id": run_id,
                "status": "failed",
                "error": str(exc),
                "timeline": timeline,
                "artifacts": artifacts,
            }

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=lambda *_args, **_kwargs: model_calls.append("called")
        or "unsafe completion",
        agent_run_outcomes=_Outcomes(),
        approval_pause=object(),
    )

    assert executor.execute("run-compile-failed", {}, "删除文件") == {
        "run_id": "run-compile-failed",
        "status": "failed",
        "error": "goal_contract_compile_failed",
        "timeline": [],
        "artifacts": [],
    }
    assert model_calls == []


@pytest.mark.parametrize(
    ("mode", "expected_closes"),
    [("completed", 1), ("failed", 1), ("approval_required", 0)],
)
def test_agent_run_executor_releases_browser_target_only_after_terminal_outcome(
    mode: str,
    expected_closes: int,
) -> None:
    class _ClosableBroker:
        def __init__(self) -> None:
            self.closes = 0

        def close_owned_browser_target(self) -> None:
            self.closes += 1

    broker = _ClosableBroker()
    prepared = _PreparedAgentRun(timeline=[], artifacts=[], broker=broker)

    class _Preparer:
        @staticmethod
        def prepare(*_args: Any, **_kwargs: Any) -> _PreparedAgentRun:
            return prepared

        @staticmethod
        def write_context_artifact(*_args: Any) -> None:
            return None

    class _Outcomes:
        @staticmethod
        def completed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "completed"}

        @staticmethod
        def failed(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "failed"}

    class _ApprovalPause:
        @staticmethod
        def project_tool_required(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {"status": "approval_required"}

    def continue_agent(*_args: Any, **_kwargs: Any) -> str:
        if mode == "failed":
            raise RuntimeError("failed")
        if mode == "approval_required":
            raise AgentApprovalRequired({"approval_id": "approval-1"})
        return "done"

    executor = RuntimeAgentRunExecutor(
        preparer=_Preparer(),
        continue_custom_api_agent=continue_agent,
        agent_run_outcomes=_Outcomes(),
        approval_pause=_ApprovalPause(),
    )

    result = executor.execute("run-browser", {"agent_id": "agent-1"}, "Ship")

    assert result["status"] == mode
    assert broker.closes == expected_closes


def test_agent_run_runtime_planner_entrypoint_overlays_stale_desktop_policy() -> None:
    agent = {
        "agent_id": "agent-yachiyo",
        "name": "Yachiyo",
        "tool_policy": {
            "allowed_tools": ["workspace.read"],
            "approval_required": {},
        },
    }

    enriched = _with_entrypoint_runtime_planner(
        agent,
        {
            "runtime_planner_entrypoint": True,
            "user_goal": "能否帮我播放apple Music?",
        },
    )

    allowed = enriched["tool_policy"]["allowed_tools"]
    approval_required = enriched["tool_policy"]["approval_required"]
    assert "_daily_desktop_policy_overlay" not in agent
    assert enriched["_runtime_planner_entrypoint"] is True
    assert enriched["_daily_desktop_policy_overlay"] is True
    assert allowed[:1] == ["workspace.read"]
    assert "desktop.list_apps" in allowed
    assert "app.open" in allowed
    assert "media.music_app_open_and_play" in allowed
    assert approval_required["desktop.hotkey"] is True
    assert approval_required["app.open_and_click_ui_element"] is True


def test_agent_run_direct_request_approval_promotes_temporary_policy() -> None:
    agent = {
        "agent_id": "agent-yachiyo",
        "name": "Yachiyo",
        "tool_policy": {
            "allowed_tools": ["python.run"],
            "approval_required": {},
        },
    }

    enriched = _with_entrypoint_runtime_planner(
        agent,
        {
            "user_goal": "分析 sales.csv",
            "direct_tool_requests": [
                {
                    "request_id": "request-run-analysis",
                    "tool": "python.run",
                    "input": {"code": "print('analysis')"},
                    "approval_required": True,
                }
            ],
        },
    )

    assert agent["tool_policy"]["approval_required"] == {}
    assert enriched["tool_policy"]["allowed_tools"] == ["python.run"]
    assert enriched["tool_policy"]["approval_required"]["python.run"] is True


def test_agent_run_reuses_runtime_envelope_without_replanning_and_preserves_approval(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "apps.shell.agent.runtime.agent_runs.planner_first_direct_decision_and_tool_requests",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime envelope should own entrypoint planning")
        ),
    )
    agent = {
        "agent_id": "agent-yachiyo",
        "name": "Yachiyo",
        "tool_policy": {
            "allowed_tools": ["workspace.read"],
            "approval_required": {},
        },
    }
    payload = {
        "runtime_planner_entrypoint": True,
        "daily_desktop_policy_overlay": True,
        "user_goal": "在当前应用提交表单",
        "runtime_execution_envelope": {
            "envelope_id": "execution-envelope-submit",
            "requests": [
                {
                    "tool_name": "desktop.submit_foreground",
                    "input": {},
                    "step_id": "submit-foreground-ui",
                    "status": "planned",
                    "approval_required": True,
                }
            ],
        },
    }

    enriched = _with_entrypoint_runtime_planner(agent, payload)

    assert enriched["_runtime_planner_entrypoint"] is True
    assert enriched["_daily_desktop_policy_overlay"] is True
    assert "desktop.submit_foreground" in enriched["tool_policy"]["allowed_tools"]
    assert enriched["tool_policy"]["approval_required"]["desktop.submit_foreground"] is True


def test_agent_run_direct_request_tool_name_alias_is_normalized() -> None:
    agent = {
        "agent_id": "agent-yachiyo",
        "name": "Yachiyo",
        "tool_policy": {
            "allowed_tools": ["python.run"],
            "approval_required": {},
        },
    }
    payload = {
        "user_goal": "分析 sales.csv",
        "runtime_execution_envelope": {"envelope_id": "env-agent", "requests": []},
        "metadata": {"yachiyo_runtime_planner": True},
        "direct_tool_request": {
            "request_id": "request-run-analysis",
            "tool_name": "python.run",
            "input": {"code": "print('analysis')"},
            "approval_required": True,
        },
    }

    enriched = _with_entrypoint_runtime_planner(agent, payload)
    options = _agent_run_execution_options(payload)

    assert options["direct_tool_request"]["tool"] == "python.run"
    assert options["direct_tool_requests"][0]["tool"] == "python.run"
    assert options["runtime_execution_envelope"] == {
        "envelope_id": "env-agent",
        "requests": [],
    }
    assert options["runtime_execution_metadata"] == {"yachiyo_runtime_planner": True}
    assert enriched["tool_policy"]["approval_required"]["python.run"] is True


def test_agent_run_runtime_planner_entrypoint_does_not_overlay_howto_question() -> None:
    agent = {
        "agent_id": "agent-yachiyo",
        "name": "Yachiyo",
        "tool_policy": {
            "allowed_tools": ["workspace.read"],
            "approval_required": {},
        },
    }

    enriched = _with_entrypoint_runtime_planner(
        agent,
        {
            "runtime_planner_entrypoint": True,
            "user_goal": "怎么播放 Apple Music？",
        },
    )

    assert enriched is agent
    assert enriched["tool_policy"]["allowed_tools"] == ["workspace.read"]


def test_agent_run_starter_creates_root_group_and_preserves_idempotency() -> None:
    state: dict[str, Any] = {}
    starter = _starter(state)
    agent = {
        "agent_id": "agent-1",
        "name": "Runner",
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    payload = {"agent_id": "agent-1", "user_goal": "Finish", "client_run_id": "client-1"}

    first = starter.start_sync(payload, agent=agent, lock=threading.RLock())
    second = starter.start_sync(payload, agent=agent, lock=threading.RLock())

    assert first.existing is False
    assert first.root_group is True
    assert first.run["kind"] == "agent_run"
    assert first.run["runnable_id"] == "agent-1"
    assert first.run["run_group_id"] == "group-1"
    assert first.run["project_root_group"] is True
    assert first.run["client_request_id"] == "client-1"
    assert state["groups"] == [
        {
            "run_group_id": "group-1",
            "title": "Runner: Finish",
            "source": "agent",
            "workspace_dir": "/tmp/project",
        }
    ]
    assert second.existing is True
    assert second.run["idempotent"] is True
    assert second.run["run_id"] == first.run["run_id"]
    assert len(state["runs"]) == 1


def test_agent_run_starter_rejects_existing_group_without_internal_lineage() -> None:
    state: dict[str, Any] = {}
    starter = _starter(state)

    with pytest.raises(RuntimeError, match="run_group_attachment_authority_required"):
        starter.start_sync(
            {
                "agent_id": "agent-1",
                "user_goal": "Run in group",
                "run_group_id": "group-existing",
            },
            agent={"agent_id": "agent-1", "name": "Runner"},
            lock=threading.RLock(),
        )

    assert state.get("runs") is None


def test_agent_run_starter_accepts_authorized_group_member() -> None:
    state: dict[str, Any] = {
        "group_records": {
            "group-active": {
                "run_group_id": "group-active",
                "status": "running",
                "child_run_ids": ["parent-run-1"],
            }
        },
        "run_records": {
            "parent-run-1": {
                "run_id": "parent-run-1",
                "run_group_id": "group-active",
                "kind": "agent_run",
            }
        },
    }
    starter = _starter(state)
    attachment = issue_run_group_child_attachment(
        run_group_id="group-active",
        parent_run_id="parent-run-1",
        child_kind="agent_run",
        child_runnable_id="agent-2",
        child_identity="group-member:1:agent-2",
    )

    start = starter.start_sync(
        {
            "agent_id": "agent-2",
            "user_goal": "Run as authorized group member",
            "run_group_id": "group-active",
            "client_run_id": attachment.child_identity,
            RUN_GROUP_ATTACHMENT_PAYLOAD_KEY: attachment,
        },
        agent={"agent_id": "agent-2", "name": "Runner"},
        lock=threading.RLock(),
    )

    assert start.root_group is False
    assert start.run["run_group_id"] == "group-active"
    assert start.run["project_root_group"] is False


def test_agent_run_starter_revalidates_attachment_on_group_idempotency_hit() -> None:
    existing = {
        "run_id": "existing-child-run",
        "kind": "agent_run",
        "runnable_id": "agent-2",
        "user_goal": "Run as authorized group member",
        "run_group_id": "group-active",
        "client_request_id": "group-member:1:agent-2",
        "idempotent": True,
    }
    state: dict[str, Any] = {
        "by_client": {existing["client_request_id"]: existing},
        "group_records": {
            "group-active": {
                "run_group_id": "group-active",
                "status": "running",
                "child_run_ids": [
                    "parent-run-1",
                    "other-member-run",
                    "existing-child-run",
                ],
            }
        },
        "run_records": {
            "parent-run-1": {
                "run_id": "parent-run-1",
                "run_group_id": "group-active",
                "kind": "agent_run",
            },
            "other-member-run": {
                "run_id": "other-member-run",
                "run_group_id": "group-active",
                "kind": "agent_run",
            },
        },
    }
    starter = _starter(state)
    replayed_with_forged_parent = issue_run_group_child_attachment(
        run_group_id="group-active",
        parent_run_id="other-member-run",
        child_kind="agent_run",
        child_runnable_id="agent-2",
        child_identity=str(existing["client_request_id"]),
    )

    with pytest.raises(
        RuntimeError,
        match="run_group_attachment_existing_parent_mismatch",
    ):
        starter.start_sync(
            {
                "agent_id": "agent-2",
                "user_goal": existing["user_goal"],
                "run_group_id": "group-active",
                "client_run_id": existing["client_request_id"],
                RUN_GROUP_ATTACHMENT_PAYLOAD_KEY: replayed_with_forged_parent,
            },
            agent={"agent_id": "agent-2", "name": "Runner"},
            lock=threading.RLock(),
        )


def test_agent_run_starter_rejects_idempotent_child_outside_group_membership() -> None:
    existing = {
        "run_id": "existing-child-run",
        "kind": "agent_run",
        "runnable_id": "agent-2",
        "user_goal": "Run as authorized group member",
        "run_group_id": "group-active",
        "client_request_id": "group-member:1:agent-2",
        "idempotent": True,
    }
    state: dict[str, Any] = {
        "by_client": {existing["client_request_id"]: existing},
        "group_records": {
            "group-active": {
                "run_group_id": "group-active",
                "status": "running",
                "child_run_ids": ["parent-run-1"],
            }
        },
        "run_records": {
            "parent-run-1": {
                "run_id": "parent-run-1",
                "run_group_id": "group-active",
                "kind": "agent_run",
            }
        },
    }
    starter = _starter(state)
    marker = issue_run_group_child_attachment(
        run_group_id="group-active",
        parent_run_id="parent-run-1",
        child_kind="agent_run",
        child_runnable_id="agent-2",
        child_identity=str(existing["client_request_id"]),
    )

    with pytest.raises(
        RuntimeError,
        match="run_group_attachment_existing_child_not_member",
    ):
        starter.start_sync(
            {
                "agent_id": "agent-2",
                "user_goal": existing["user_goal"],
                "run_group_id": "group-active",
                "client_run_id": existing["client_request_id"],
                RUN_GROUP_ATTACHMENT_PAYLOAD_KEY: marker,
            },
            agent={"agent_id": "agent-2", "name": "Runner"},
            lock=threading.RLock(),
        )


def test_native_group_run_attaches_authorized_members_through_runtime(tmp_path) -> None:
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )

    class _DeferredThread:
        def __init__(self, *, target: Any, name: str, daemon: bool) -> None:
            self.target = target
            self.name = name
            self.daemon = daemon

        def start(self) -> None:
            return None

    try:
        agent_ids = []
        for name in ("Planner", "Reviewer"):
            agent = service.create_agent(
                {
                    "name": name,
                    "model_mode": "custom_api",
                    "model_config": {
                        "base_url": "https://api.example.test/v1",
                        "model": "demo-model",
                        "api_key": "sk-secret",
                    },
                }
            )
            agent_ids.append(agent["agent_id"])
        service.agent_run_async_coordinator._thread_factory = _DeferredThread
        original_insert_run = service.agent_run_starter._insert_run
        attachment_transaction_states: list[bool] = []

        def traced_insert_run(**kwargs: Any) -> dict[str, Any]:
            attachment_transaction_states.append(
                service._conn.in_managed_transaction
            )
            return original_insert_run(**kwargs)

        service.agent_run_starter._insert_run = traced_insert_run

        started = start_agent_group_run(
            service,
            {"group_id": "group-authorized", "objective": "Prepare report"},
            group={
                "group_id": "group-authorized",
                "name": "Authorized group",
                "members": [{"agent_id": agent_id} for agent_id in agent_ids],
            },
        )

        assert len(started["child_run_ids"]) == 2
        group = service.get_run_group(started["run_group_id"])
        assert group["child_run_ids"] == started["child_run_ids"]
        assert all(
            service.get_run(run_id)["project_root_group"] is False
            for run_id in started["child_run_ids"]
        )
        assert attachment_transaction_states == [False, True]
    finally:
        service.close()


def test_agent_run_starter_persists_explicit_group_aggregator_authority() -> None:
    state: dict[str, Any] = {}
    starter = _starter(state)

    start = starter.start_async(
        {
            "agent_id": "agent-1",
            "user_goal": "Run as first group member",
            "project_root_group": False,
        },
        agent={"agent_id": "agent-1", "name": "Runner"},
    )

    assert start.root_group is False
    assert start.run["run_group_id"] == "group-1"
    assert start.run["project_root_group"] is False
    assert len(state["groups"]) == 1


def test_agent_run_starter_async_claims_client_request_id() -> None:
    state: dict[str, Any] = {}
    starter = _starter(state)
    payload = {
        "agent_id": "agent-1",
        "user_goal": "Run later",
        "client_run_id": "async-client-id",
    }
    first = starter.start_async(
        payload,
        agent={"agent_id": "agent-1", "name": "Runner"},
    )
    second = starter.start_async(
        payload,
        agent={"agent_id": "agent-1", "name": "Runner"},
    )

    assert first.root_group is True
    assert first.run["client_request_id"] == "async-client-id"
    assert second.existing is True
    assert second.run["run_id"] == first.run["run_id"]
    assert len(state["runs"]) == 1


def test_agent_run_coordinator_validates_starts_executes_and_projects_root_group() -> None:
    calls: list[tuple[str, Any]] = []

    class _Starter:
        def start_sync(self, payload: dict[str, Any], *, agent: dict[str, Any], lock: Any) -> AgentRunStart:
            calls.append(("start", payload, agent, lock))
            return AgentRunStart({"run_id": "run-1", "run_group_id": "group-1"}, root_group=True)

    coordinator = RuntimeAgentRunCoordinator(
        get_agent_private=lambda agent_id: calls.append(("agent", agent_id)) or {
            "agent_id": agent_id,
            "name": "Runner",
        },
        validate_agent_run_readiness=lambda agent: calls.append(("readiness", agent["agent_id"])),
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=lambda run_id, agent, user_goal, **kwargs: calls.append(
            ("execute", run_id, agent["agent_id"], user_goal, kwargs)
        )
        or {"run_id": run_id, "status": "completed"},
        project_agent_run_group_if_root=lambda result: calls.append(("project", result["run_id"]))
        or {**result, "group_projected": True},
        lock=object(),
        error_type=agent_runtime.AgentRuntimeError,
    )

    result = coordinator.create_sync({"agent_id": "agent-1", "user_goal": "Ship", "upstream": "Context"})

    assert result == {"run_id": "run-1", "status": "completed", "group_projected": True}
    assert calls[0] == ("agent", "agent-1")
    assert calls[1] == ("readiness", "agent-1")
    assert calls[3] == (
        "execute",
        "run-1",
        "agent-1",
        "Ship",
        {"upstream": "Context", "run_group_id": "group-1"},
    )
    assert calls[4] == ("project", "run-1")


def test_agent_run_coordinator_returns_existing_idempotent_run_without_execution() -> None:
    class _Starter:
        def start_sync(self, payload: dict[str, Any], *, agent: dict[str, Any], lock: Any) -> AgentRunStart:
            return AgentRunStart({"run_id": "existing", "idempotent": True}, root_group=False, existing=True)

    coordinator = RuntimeAgentRunCoordinator(
        get_agent_private=lambda agent_id: {"agent_id": agent_id, "name": "Runner"},
        validate_agent_run_readiness=lambda _agent: None,
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not execute")),
        project_agent_run_group_if_root=lambda result: result,
        lock=object(),
        error_type=agent_runtime.AgentRuntimeError,
    )

    assert coordinator.create_sync({"agent_id": "agent-1", "user_goal": "Ship"}) == {
        "run_id": "existing",
        "idempotent": True,
    }


def test_agent_run_async_coordinator_returns_processing_and_completes_in_background() -> None:
    completions: list[dict[str, Any]] = []

    class _Starter:
        def start_async(
            self,
            payload: dict[str, Any],
            *,
            agent: dict[str, Any],
            lock: Any,
        ) -> AgentRunStart:
            return AgentRunStart(
                {"run_id": "run-1", "kind": "agent_run", "run_group_id": "group-1"},
                root_group=True,
            )

    coordinator = RuntimeAgentRunAsyncCoordinator(
        get_agent_private=lambda agent_id: {"agent_id": agent_id, "name": "Runner"},
        validate_agent_run_readiness=lambda _agent: None,
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=lambda run_id, agent, user_goal, **kwargs: {
            "run_id": run_id,
            "status": "completed",
            "user_goal": user_goal,
            **kwargs,
        },
        project_agent_run_group_if_root=lambda result: {**result, "group_projected": True},
        resolve_runnable=lambda **kwargs: {"id": kwargs["runnable_id"], "kind": "agent"},
        get_run=lambda run_id: {"run_id": run_id, "status": "running"},
        project_agent_run_failure=lambda *_args, **_kwargs: pytest.fail(
            "no failure expected"
        ),
        redact_error=str,
        error_type=agent_runtime.AgentRuntimeError,
        thread_factory=_ImmediateThread,
    )

    result = coordinator.create_async(
        {"agent_id": "agent-1", "user_goal": "Ship", "upstream": "Context"},
        on_complete=completions.append,
    )

    assert result["status"] == "processing"
    assert result["agent_run_id"] == "run-1"
    assert result["runnable"] == {"id": "agent-1", "kind": "agent"}
    assert completions == [
        {
            "run_id": "run-1",
            "status": "completed",
            "user_goal": "Ship",
            "upstream": "Context",
            "run_group_id": "group-1",
            "group_projected": True,
        }
    ]


def test_agent_run_async_coordinator_projects_background_failure() -> None:
    completions: list[dict[str, Any]] = []
    projections: list[dict[str, Any]] = []
    current = {
        "run_id": "run-fail",
        "kind": "agent_run",
        "status": "running",
        "timeline": [{"event": "agent.run.started"}],
        "artifacts": [{"name": "context.md"}],
    }

    class _Starter:
        def start_async(
            self,
            payload: dict[str, Any],
            *,
            agent: dict[str, Any],
            lock: Any,
        ) -> AgentRunStart:
            return AgentRunStart({"run_id": "run-fail", "kind": "agent_run"}, root_group=False)

    def fail_execute(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("secret failure")

    def project_failure(
        run_id: str,
        error: Exception,
        *,
        timeline: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        projections.append(
            {
                "run_id": run_id,
                "error": str(error),
                "timeline": timeline,
                "artifacts": artifacts,
            }
        )
        current.update(status="failed", result=str(error))
        return dict(current)

    coordinator = RuntimeAgentRunAsyncCoordinator(
        get_agent_private=lambda agent_id: {"agent_id": agent_id, "name": "Runner"},
        validate_agent_run_readiness=lambda _agent: None,
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=fail_execute,
        project_agent_run_group_if_root=lambda result: result,
        resolve_runnable=lambda **kwargs: {"id": kwargs["runnable_id"]},
        get_run=lambda _run_id: dict(current),
        project_agent_run_failure=project_failure,
        redact_error=lambda error: str(error).replace("secret", "[redacted]"),
        error_type=agent_runtime.AgentRuntimeError,
        thread_factory=_ImmediateThread,
    )

    result = coordinator.create_async({"agent_id": "agent-1", "user_goal": "Ship"}, on_complete=completions.append)

    assert result["status"] == "processing"
    assert projections == [
        {
            "run_id": "run-fail",
            "error": "[redacted] failure",
            "timeline": [{"event": "agent.run.started"}],
            "artifacts": [{"name": "context.md"}],
        }
    ]
    assert completions == [
        {
            "run_id": "run-fail",
            "kind": "agent_run",
            "status": "failed",
            "timeline": [{"event": "agent.run.started"}],
            "artifacts": [{"name": "context.md"}],
            "result": "[redacted] failure",
        }
    ]


def test_agent_run_async_takeover_projects_failure_before_releasing_lease() -> None:
    order: list[str] = []
    current = {
        "run_id": "run-takeover",
        "kind": "agent_run",
        "status": "running",
        "timeline": [{"event": "tool.completed"}],
        "artifacts": [{"artifact_id": "prior-effect"}],
    }

    class _Starter:
        def start_async(
            self,
            _payload: dict[str, Any],
            *,
            agent: dict[str, Any],
            lock: Any,
        ) -> AgentRunStart:
            return AgentRunStart(
                dict(current),
                root_group=False,
                lease_generation=2,
                lease_owner_token="owner-takeover",
                takeover=True,
            )

        @contextmanager
        def execution_lease_context(self, *_args: Any, **_kwargs: Any) -> Any:
            order.append("lease-enter")
            yield
            order.append("lease-exit")

        def release_async_lease(self, *_args: Any, **_kwargs: Any) -> bool:
            order.append("lease-release")
            return True

    def project_failure(
        _run_id: str,
        error: Exception,
        *,
        timeline: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        order.append("project-failure")
        assert "async_execution_resume_checkpoint_required" in str(error)
        assert timeline == [{"event": "tool.completed"}]
        assert artifacts == [{"artifact_id": "prior-effect"}]
        current.update(status="failed", result=str(error))
        return dict(current)

    completions: list[dict[str, Any]] = []
    coordinator = RuntimeAgentRunAsyncCoordinator(
        get_agent_private=lambda agent_id: {"agent_id": agent_id, "name": "Runner"},
        validate_agent_run_readiness=lambda _agent: None,
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=lambda *_args, **_kwargs: pytest.fail("must not replay"),
        project_agent_run_group_if_root=lambda result: result,
        resolve_runnable=lambda **kwargs: {"id": kwargs["runnable_id"]},
        get_run=lambda _run_id: dict(current),
        project_agent_run_failure=project_failure,
        redact_error=str,
        error_type=agent_runtime.AgentRuntimeError,
        thread_factory=_ImmediateThread,
    )

    result = coordinator.create_async(
        {"agent_id": "agent-1", "user_goal": "Ship"},
        on_complete=completions.append,
    )

    assert result["status"] == "failed"
    assert completions == [result]
    assert order == [
        "lease-enter",
        "project-failure",
        "lease-exit",
        "lease-release",
    ]


def test_agent_run_async_coordinator_marks_run_failed_when_thread_start_raises() -> None:
    projections: list[dict[str, Any]] = []
    current = {
        "run_id": "run-thread-fail",
        "kind": "agent_run",
        "status": "running",
        "timeline": [],
        "artifacts": [],
    }

    class _Starter:
        def start_async(
            self,
            payload: dict[str, Any],
            *,
            agent: dict[str, Any],
            lock: Any,
        ) -> AgentRunStart:
            return AgentRunStart(
                {"run_id": "run-thread-fail", "kind": "agent_run"},
                root_group=False,
            )

    class _FailingThread:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start secret")

    coordinator = RuntimeAgentRunAsyncCoordinator(
        get_agent_private=lambda agent_id: {"agent_id": agent_id, "name": "Runner"},
        validate_agent_run_readiness=lambda _agent: None,
        starter=_Starter(),  # type: ignore[arg-type]
        execute_agent_run=lambda *_args, **_kwargs: {},
        project_agent_run_group_if_root=lambda result: result,
        resolve_runnable=lambda **kwargs: {"id": kwargs["runnable_id"]},
        get_run=lambda _run_id: dict(current),
        project_agent_run_failure=lambda run_id, error, **kwargs: (
            projections.append(
                {"run_id": run_id, "error": str(error), **kwargs}
            )
            or {
                **current,
                "status": "failed",
                "result": str(error),
            }
        ),
        redact_error=lambda error: str(error).replace("secret", "[redacted]"),
        error_type=agent_runtime.AgentRuntimeError,
        thread_factory=_FailingThread,
    )

    with pytest.raises(agent_runtime.AgentRuntimeError, match=r"thread start \[redacted\]"):
        coordinator.create_async({"agent_id": "agent-1", "user_goal": "Ship"})

    assert projections == [
        {
            "run_id": "run-thread-fail",
            "error": "thread start [redacted]",
            "timeline": [],
            "artifacts": [],
        }
    ]


def test_native_runtime_uses_split_agent_run_starter(tmp_path, monkeypatch) -> None:
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    model_calls: list[list[dict[str, Any]]] = []

    def fake_chat(
        _base_url: str,
        _model: str,
        _api_key: str,
        messages: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> dict[str, str]:
        model_calls.append(messages)
        return {"content": "Done"}

    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    try:
        assert agent_runtime.RuntimeAgentRunStarter is RuntimeAgentRunStarter
        assert agent_runtime.RuntimeAgentRunCoordinator is RuntimeAgentRunCoordinator
        assert agent_runtime.RuntimeAgentRunAsyncCoordinator is RuntimeAgentRunAsyncCoordinator
        assert agent_runtime.RuntimeAgentRunExecutor is RuntimeAgentRunExecutor
        assert isinstance(service.agent_run_starter, RuntimeAgentRunStarter)
        assert isinstance(service.agent_run_coordinator, RuntimeAgentRunCoordinator)
        assert isinstance(service.agent_run_async_coordinator, RuntimeAgentRunAsyncCoordinator)
        assert isinstance(service.agent_run_executor, RuntimeAgentRunExecutor)
        agent = service.create_agent(
            {
                "name": "Starter Agent",
                "model_mode": "custom_api",
                "model_config": {
                    "base_url": "https://api.example.test/v1",
                    "model": "demo-model",
                    "api_key": "sk-secret",
                },
            }
        )

        first = service.create_agent_run(
            {"agent_id": agent["agent_id"], "user_goal": "Reply with exactly 'Done'", "client_run_id": "starter-client-1"}
        )
        second = service.create_agent_run(
            {"agent_id": agent["agent_id"], "user_goal": "Reply with exactly 'Done'", "client_run_id": "starter-client-1"}
        )

        assert first["status"] == "completed"
        assert second["idempotent"] is True
        assert second["run_id"] == first["run_id"]
        assert len(model_calls) == 1
    finally:
        service.close()
