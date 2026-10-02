"""Tests for main chat model loop runner split out of the legacy runtime."""

from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from typing import Any

import pytest

from apps.shell import agent_runtime
from apps.shell.agent.runtime import main_chat_model_loop
from apps.shell.agent.runtime.errors import (
    AgentApprovalRequired,
    AgentDirectOutcomeUnverified,
)
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCriterion
from apps.shell.agent.runtime.goal_runtime import goal_contract_event_payload, planned_goal_contract_payload
from apps.shell.agent.runtime.main_chat_model_loop import (
    MainChatModelLoopRunner,
    build_runtime_main_chat_model_loop_runner,
)
from apps.shell.agent.runtime.model_intent_planning import (
    ModelIntentClarificationResolution,
    ModelIntentProposal,
    direct_tool_selection_from_model_intent_proposal,
)
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore


class FakeBudget:
    pass


class FakeRuntimeAgentTimeline:
    @staticmethod
    def compiled(**payload: Any) -> dict[str, Any]:
        return {"event": "agent.runtime.compiled", **payload}


class FakeTaskModelEvents:
    @staticmethod
    def model_request_started_payload(**payload: Any) -> dict[str, Any]:
        return {"started": payload}

    @staticmethod
    def model_request_failed_payload(error: str) -> dict[str, Any]:
        return {"error": error}

    @staticmethod
    def model_output_completed_payload(
        content: str,
        *,
        truncated: bool,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        return {"content": content, "truncated": truncated, **metadata}


class FakeToolBrokers:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def for_main_chat(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"broker": kwargs}


class FakeApprovalPause:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def project_tool_required(self, run_id: str, **payload: Any) -> dict[str, Any]:
        self.calls.append({"run_id": run_id, **payload})
        return {"run_id": run_id, "status": "approval_required", **payload}


def _timeline(event: str, detail: str = "", **extra: Any) -> dict[str, Any]:
    return {"event": event, "detail": detail, **extra}


class FakeProfileService:
    def get_defaults(self) -> dict[str, str]:
        return {"chat": "profile-chat"}


def test_main_chat_model_loop_builder_remains_exported_from_legacy_module() -> None:
    assert (
        agent_runtime._build_runtime_main_chat_model_loop_runner
        is build_runtime_main_chat_model_loop_runner
    )


def test_build_runtime_main_chat_model_loop_runner_wires_runtime_dependencies() -> None:
    runner = build_runtime_main_chat_model_loop_runner(
        get_run=lambda _run_id: {"run_id": "run-1", "kind": "main_chat_run", "timeline": []},
        profile_service_factory=FakeProfileService,
        model_profile_config_private=lambda _profile_id: {},
        main_chat_agent_config=lambda **kwargs: {"agent_id": "builtin:yachiyo-main", **kwargs},
        compile_agent_runtime=lambda _agent: {"tool_policy": {}, "workspace_policy": {}},
        run_budget=lambda _run_id, _timeline_value: FakeBudget(),
        check_context_budget=lambda _budget, _messages: None,
        runtime_agent_timeline=FakeRuntimeAgentTimeline(),
        timeline_factory=_timeline,
        update_run=lambda _run_id, **payload: {"run_id": "run-1", **payload},
        append_run_event=lambda _run_id, event_type, payload, **_fence: {
            "event_type": event_type,
            "payload": payload,
        },
        task_model_events=FakeTaskModelEvents(),
        tool_brokers=FakeToolBrokers(),
        continue_custom_api_agent=lambda *_args, **_kwargs: "done",
        main_chat_pending_approval=lambda pending, **payload: {"pending": pending, **payload},
        approval_pause=FakeApprovalPause(),
        terminal_run_or_none=lambda _run_id: None,
        fail_main_chat_run=lambda run_id, _error, **_payload: {
            "run_id": run_id,
            "status": "failed",
        },
    )

    assert isinstance(runner, MainChatModelLoopRunner)
    assert runner._default_profile_id() == "profile-chat"
    assert getattr(runner._run_budget, "__self__", None) is None
    assert getattr(runner._check_context_budget, "__self__", None) is None


def _runner(**overrides: Any) -> tuple[MainChatModelLoopRunner, dict[str, Any]]:
    state: dict[str, Any] = {
        "run": {
            "run_id": "run-1",
            "kind": "main_chat_run",
            "user_goal": "hi",
            "status": "running",
            "updated_at": "version-0",
            "timeline": [],
            "artifacts": [],
            "pending_approval": {},
        },
        "updates": [],
        "events": [],
        "event_fences": [],
        "tool_brokers": FakeToolBrokers(),
        "approval_pause": FakeApprovalPause(),
    }
    version = 0

    def update_run(_run_id: str, **payload: Any) -> dict[str, Any] | None:
        nonlocal version
        state["updates"].append(payload)
        current = state["run"]
        expected_status = payload.get("expected_status")
        expected_updated_at = payload.get("expected_updated_at")
        if expected_status is not None and current.get("status") != expected_status:
            return None
        if (
            expected_updated_at is not None
            and current.get("updated_at") != expected_updated_at
        ):
            return None
        if payload.get("expected_pending_approval_absent") and current.get(
            "pending_approval"
        ):
            return None
        for key, value in payload.items():
            if not key.startswith("expected_"):
                current[key] = value
        version += 1
        current["updated_at"] = f"version-{version}"
        return dict(current)

    def append_run_event(
        _run_id: str,
        event_type: str,
        payload: dict[str, Any],
        **fence: Any,
    ) -> dict[str, Any] | None:
        state["event_fences"].append((event_type, fence))
        current = state["run"]
        if (
            fence.get("expected_status") is not None
            and current.get("status") != fence["expected_status"]
        ):
            return None
        if (
            fence.get("expected_updated_at") is not None
            and current.get("updated_at") != fence["expected_updated_at"]
        ):
            return None
        state["events"].append((event_type, payload))
        return {"event_type": event_type, "payload": payload}

    def fail_main_chat_run(
        run_id: str,
        error: Any,
        **payload: Any,
    ) -> dict[str, Any]:
        current = state["run"]
        if current.get("status") != "running" or current.get("pending_approval"):
            return dict(current)
        failed = update_run(
            run_id,
            status="failed",
            result=str(error),
            timeline=payload.get("timeline") or current.get("timeline") or [],
            artifacts=payload.get("artifacts") or [],
            pending_approval=None,
            expected_status="running",
            expected_updated_at=str(current.get("updated_at") or ""),
            expected_pending_approval_absent=True,
        )
        if failed is None:
            return dict(state["run"])
        for event_type, event_payload in payload.get("run_events") or []:
            append_run_event(
                run_id,
                event_type,
                event_payload,
                expected_status="failed",
                expected_updated_at=str(failed.get("updated_at") or ""),
            )
        return failed

    runner = MainChatModelLoopRunner(
        get_run=lambda _run_id: state["run"],
        default_profile_id=lambda: "profile-chat",
        model_profile_config_private=lambda _profile_id: {
            "base_url": "https://model.local",
            "model": "test-model",
            "api_key": "key",
        },
        main_chat_agent_config=lambda **kwargs: {
            "agent_id": "builtin:yachiyo-main",
            "category": "orchestrator",
            **kwargs,
        },
        compile_agent_runtime=lambda _agent: {
            "tool_policy": {"allowed_tools": ["workspace.read"]},
            "workspace_policy": {"default_workdir": "/tmp/project"},
        },
        run_budget=lambda _run_id, _timeline_value: FakeBudget(),
        check_context_budget=lambda _budget, _messages: None,
        runtime_agent_timeline=FakeRuntimeAgentTimeline(),
        timeline_factory=_timeline,
        update_run=update_run,
        append_run_event=append_run_event,
        task_model_events=FakeTaskModelEvents(),
        tool_brokers=state["tool_brokers"],
        continue_custom_api_agent=lambda *_args, **_kwargs: "done",
        main_chat_pending_approval=lambda pending, **payload: {"pending": pending, **payload},
        approval_pause=state["approval_pause"],
        terminal_run_or_none=lambda _run_id: None,
        fail_main_chat_run=fail_main_chat_run,
        redact_secrets=lambda value: str(value).replace("secret", "[redacted]"),
        model_output_metadata=lambda _value: {"finish_reason": "stop"},
        error_type=agent_runtime.AgentRuntimeError,
    )
    for name, value in overrides.items():
        setattr(runner, name, value)
    return runner, state


def _goal_contract(
    *,
    original_goal: str,
    run_id: str = "",
    description: str = "Complete the requested main chat task",
) -> GoalContract:
    return GoalContract(
        contract_id="contract-main-chat",
        run_id=run_id,
        original_goal=original_goal,
        criteria=(
            GoalCriterion(
                criterion_id="criterion-main-chat",
                description=description,
                response_satisfiable=True,
            ),
        ),
    )


def _effectful_goal_contract(
    *,
    original_goal: str,
    run_id: str = "",
    description: str = "Read the requested project material before summarizing it",
) -> GoalContract:
    return GoalContract(
        contract_id="contract-main-chat",
        run_id=run_id,
        original_goal=original_goal,
        criteria=(
            GoalCriterion(
                criterion_id="criterion-main-chat",
                description=description,
                effectful=True,
                required_capabilities=("workspace.read",),
                source_step_ids=("read-project-material",),
                verifier_step_ids=("verify-project-material",),
            ),
        ),
    )


def test_main_chat_model_loop_persists_contract_before_model_and_runtime_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    user_goal = "Summarize the project README"
    state["run"]["user_goal"] = user_goal
    allowed_tools = ["workspace.list", "workspace.read"]
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": allowed_tools},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    template = _effectful_goal_contract(original_goal=user_goal)
    planner_calls: list[dict[str, Any]] = []

    def planned_contract(
        goal: str,
        *,
        allowed_tools: list[str],
    ) -> dict[str, Any]:
        planner_calls.append({"goal": goal, "allowed_tools": list(allowed_tools)})
        return template.to_payload()

    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        planned_contract,
    )
    continue_calls: list[dict[str, Any]] = []

    def continue_custom_api_agent(
        _agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append({"timeline": list(timeline), **kwargs})
        return "done"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": user_goal}],
    )

    assert result["result"] == "done"
    assert planner_calls == [{"goal": user_goal, "allowed_tools": allowed_tools}]
    expected_payload = goal_contract_event_payload(template.bind_run("run-1"))
    assert state["events"][0] == ("agent.goal.contract", expected_payload)
    assert state["events"][1][0] == "model.request.started"
    forwarded_metadata = continue_calls[0]["runtime_execution_metadata"]
    assert forwarded_metadata["goal_contract"] == expected_payload["goal_contract"]
    assert forwarded_metadata["goal_contract_json"] == expected_payload["goal_contract_json"]
    assert continue_calls[0]["original_goal"] == user_goal
    continue_event_types = [
        event.get("event") or event.get("event_type")
        for event in continue_calls[0]["timeline"]
    ]
    assert continue_event_types.index("agent.goal.contract") < continue_event_types.index(
        "model.request.started"
    )


def test_main_chat_model_plan_replaces_envelope_and_persists_runtime_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    original_goal = "帮我查一下今天的 Python 新闻"
    planning_goal = "搜索网页查找今天的 Python 新闻"
    state["run"]["user_goal"] = original_goal
    allowed_tools = ["browser.search"]
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": allowed_tools},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    selection = direct_tool_selection_from_model_intent_proposal(
        ModelIntentProposal(
            intent_kind="web_research",
            planning_goal=planning_goal,
            rationale="查询当日信息需要网络研究。",
        ),
        original_goal,
        allowed_tools,
    )
    resolver_calls: list[dict[str, Any]] = []

    def resolve_initial_model_plan(**kwargs: Any) -> Any:
        resolver_calls.append(kwargs)
        return selection

    runner._resolve_initial_model_plan = resolve_initial_model_plan
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a Runtime-owned model selection must own the first contract")
        ),
    )
    continue_calls: list[dict[str, Any]] = []

    def continue_custom_api_agent(
        _agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append({"timeline": list(timeline), **kwargs})
        return "已查找。"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": original_goal}],
        runtime_execution_envelope={
            "decision_id": selection.decision.decision_id,
            "requests": [],
        },
    )

    assert result["result"] == "已查找。"
    assert len(resolver_calls) == 1
    assert resolver_calls[0]["original_goal"] == original_goal
    assert resolver_calls[0]["allowed_tools"] == allowed_tools
    forwarded = continue_calls[0]
    envelope = forwarded["runtime_execution_envelope"]
    assert envelope["decision_id"] == selection.decision.decision_id
    assert envelope["plan_id"] == selection.decision.plan.plan_id
    assert [request["tool_name"] for request in envelope["requests"]] == [
        "browser.search"
    ]
    assert [request["tool"] for request in forwarded["direct_tool_requests"]] == [
        "browser.search"
    ]
    assert forwarded["direct_tool_request"] is None
    assert forwarded["runtime_execution_metadata"][
        "runtime_model_assisted_planning"
    ] is True
    assert (
        forwarded["runtime_execution_metadata"]["runtime_model_plan_selection"]
        == selection.event_payload
    )
    event_type, persisted_payload = state["events"][0]
    assert event_type == "agent.goal.contract"
    persisted_contract = persisted_payload["goal_contract"]
    planned_contract = selection.decision.plan.task_core.goal_contract.model_dump(
        mode="json"
    )
    assert persisted_contract["run_id"] == "run-1"
    assert persisted_contract["contract_id"] == planned_contract["contract_id"]
    assert persisted_contract["criteria"] == planned_contract["criteria"]
    assert forwarded["runtime_execution_metadata"]["goal_contract"] == persisted_contract
    assert persisted_contract["original_goal"] == original_goal
    assert persisted_contract["intent_kind"] == "web_research"


def test_main_chat_initial_clarification_exits_before_contract_broker_or_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    original_goal = "帮我整理一下文件"
    question = "请问要整理哪个目录？"
    state["run"]["user_goal"] = original_goal
    runner._resolve_initial_model_plan = lambda **_kwargs: (
        ModelIntentClarificationResolution(
            original_goal=original_goal,
            question=question,
        )
    )
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: pytest.fail(
            "clarification must exit before a GoalContract is created"
        ),
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: pytest.fail(
        "clarification must not execute the Agent/tool loop"
    )

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": original_goal}],
    )

    assert result["status"] == "awaiting_user"
    assert result["result"] == question
    assert result["user_goal"] == original_goal
    assert state["tool_brokers"].calls == []
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.plan.clarification_required"
    ]
    event_payload = state["events"][0][1]
    assert event_payload["question"] == question
    assert event_payload["original_goal"] == original_goal
    assert "rationale" not in event_payload
    assert "planning_goal" not in event_payload
    timeline_events = [event.get("event") for event in result["timeline"]]
    assert timeline_events[-1] == "agent.plan.clarification_required"
    assert "agent.goal.contract" not in timeline_events
    assert "agent.runtime.compiled" not in timeline_events


def test_main_chat_model_loop_restores_contract_without_replanning_for_new_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    user_goal = "Summarize the project README"
    state["run"]["user_goal"] = user_goal
    contract = _effectful_goal_contract(original_goal=user_goal, run_id="run-1")
    state["run"]["timeline"] = [
        _timeline(
            "agent.goal.contract",
            contract.contract_id,
            **goal_contract_event_payload(contract),
        )
    ]
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["terminal.run"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a persisted contract must not be replanned")
        ),
    )
    runner._resolve_initial_model_plan = lambda **_kwargs: pytest.fail(
        "a persisted contract must not invoke the initial model planner"
    )
    continue_calls: list[dict[str, Any]] = []

    def continue_custom_api_agent(
        *_args: Any,
        **kwargs: Any,
    ) -> str:
        continue_calls.append(kwargs)
        return "done"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": user_goal}],
    )

    assert result["result"] == "done"
    assert [event_type for event_type, _payload in state["events"]] == [
        "model.request.started",
        "model.output.completed",
    ]
    forwarded_metadata = continue_calls[0]["runtime_execution_metadata"]
    expected_payload = goal_contract_event_payload(contract)
    assert forwarded_metadata["goal_contract"] == expected_payload["goal_contract"]
    assert forwarded_metadata["goal_contract_json"] == expected_payload["goal_contract_json"]


def test_main_chat_model_loop_missing_user_goal_fails_before_model_or_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    state["run"].pop("user_goal")
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a missing root goal must not be planned")
        ),
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a missing root goal must not reach Runtime execution")
    )

    with pytest.raises(ValueError, match="goal_contract_invalid: user_goal_required"):
        runner.execute("run-1", [{"role": "user", "content": "mutable fallback"}])

    assert state["events"] == []
    assert state["updates"] == []
    assert state["tool_brokers"].calls == []


def test_main_chat_model_loop_damaged_contract_fails_before_model_or_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    state["run"]["timeline"] = [
        {
            "event": "agent.goal.contract",
            "run_id": "run-1",
            "goal_contract_json": "{damaged",
        }
    ]
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a damaged persisted contract must not be replaced")
        ),
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a damaged contract must not reach Runtime execution")
    )

    with pytest.raises(ValueError, match="goal_contract_invalid"):
        runner.execute("run-1", [{"role": "user", "content": "continue"}])

    assert state["events"] == []
    assert state["updates"] == []
    assert state["tool_brokers"].calls == []


def test_main_chat_model_loop_conflicting_candidates_fail_before_model_or_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    user_goal = str(state["run"]["user_goal"])
    canonical = _goal_contract(original_goal=user_goal, run_id="run-1")
    conflicting = _goal_contract(
        original_goal=user_goal,
        description="A conflicting completion criterion",
    )
    state["run"]["timeline"] = [
        _timeline(
            "agent.goal.contract",
            canonical.contract_id,
            **goal_contract_event_payload(canonical),
        )
    ]
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("conflicting candidates must not trigger replanning")
        ),
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("conflicting contracts must not reach Runtime execution")
    )

    with pytest.raises(ValueError, match="goal_contract_conflict"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": user_goal}],
            runtime_execution_envelope={
                "task_core": {"goal_contract": canonical.to_payload()}
            },
            runtime_execution_metadata={"goal_contract": conflicting.to_payload()},
        )

    assert state["events"] == []
    assert state["updates"] == []
    assert state["tool_brokers"].calls == []


def test_first_main_chat_contract_cannot_be_weakened_by_an_external_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner, state = _runner()
    user_goal = str(state["run"]["user_goal"])
    trusted = GoalContract(
        contract_id="contract-main-chat",
        original_goal=user_goal,
        criteria=(
            GoalCriterion(
                criterion_id="criterion-main-chat",
                description="Apply the requested desktop effect",
                effectful=True,
                required_capabilities=("desktop.ui_operation",),
                source_step_ids=("apply-effect",),
                verifier_step_ids=("verify-effect",),
            ),
        ),
    )
    weak = _goal_contract(original_goal=user_goal)
    monkeypatch.setattr(
        main_chat_model_loop,
        "planned_goal_contract_payload",
        lambda *_args, **_kwargs: trusted.to_payload(),
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a weakened contract must not reach Runtime execution")
    )

    with pytest.raises(ValueError, match="goal_contract_conflict"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": user_goal}],
            runtime_execution_envelope={
                "task_core": {"goal_contract": weak.to_payload()}
            },
        )

    assert state["events"] == []
    assert state["updates"] == []
    assert state["tool_brokers"].calls == []


def test_main_chat_model_loop_runner_projects_successful_loop() -> None:
    runner, state = _runner()

    result = runner.execute("run-1", [{"role": "user", "content": "hi"}])

    assert result["status"] == "running"
    assert result["result"] == "done"
    assert state["tool_brokers"].calls == [
        {
            "run_id": "run-1",
            "workspace_policy": {"default_workdir": "/tmp/project"},
        }
    ]
    assert state["updates"][0]["status"] == "running"
    assert state["updates"][-1]["pending_approval"] is None
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.request.started",
        "model.output.completed",
    ]
    assert state["events"][-1][1]["finish_reason"] == "stop"


def test_main_chat_output_projection_rolls_back_when_terminal_event_fails() -> None:
    runner, state = _runner()
    original_append = runner._append_run_event

    @contextmanager
    def transaction_scope():
        run_snapshot = deepcopy(state["run"])
        events_snapshot = list(state["events"])
        fences_snapshot = list(state["event_fences"])
        try:
            yield
        except BaseException:
            state["run"].clear()
            state["run"].update(run_snapshot)
            state["events"][:] = events_snapshot
            state["event_fences"][:] = fences_snapshot
            raise

    def append_run_event(
        run_id: str,
        event_type: str,
        payload: dict[str, Any],
        **fence: Any,
    ) -> dict[str, Any] | None:
        if event_type == "model.output.completed":
            return None
        return original_append(run_id, event_type, payload, **fence)

    runner._transaction_scope = transaction_scope
    runner._append_run_event = append_run_event

    with pytest.raises(agent_runtime.AgentRuntimeError, match="run_event_fence_mismatch"):
        runner.execute("run-1", [{"role": "user", "content": "hi"}])

    assert state["run"]["status"] == "running"
    assert state["run"].get("result") != "done"
    assert state["run"]["timeline"][-1]["event"] == "model.request.started"
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.request.started",
    ]


def test_main_chat_success_cannot_revive_a_run_cancelled_before_projection() -> None:
    runner, state = _runner()

    def cancel_during_terminal_check(_run_id: str) -> None:
        state["run"].update(
            {
                "status": "cancelled",
                "updated_at": "version-cancelled",
                "result": "Run cancelled",
            }
        )
        return None

    runner._terminal_run_or_none = cancel_during_terminal_check

    result = runner.execute("run-1", [{"role": "user", "content": "hi"}])

    assert result["status"] == "cancelled"
    assert result["result"] == "Run cancelled"
    assert "model.output.completed" not in [
        event_type for event_type, _payload in state["events"]
    ]
    assert state["run"]["timeline"][-1]["event"] == "model.request.started"


def test_main_chat_failure_cannot_overwrite_a_concurrent_cancellation() -> None:
    runner, state = _runner()
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("late model failure")
    )

    def cancel_during_terminal_check(_run_id: str) -> None:
        state["run"].update(
            {
                "status": "cancelled",
                "updated_at": "version-cancelled",
                "result": "Run cancelled",
            }
        )
        return None

    runner._terminal_run_or_none = cancel_during_terminal_check

    result = runner.execute("run-1", [{"role": "user", "content": "hi"}])

    assert result["status"] == "cancelled"
    assert result["result"] == "Run cancelled"
    assert "model.request.failed" not in [
        event_type for event_type, _payload in state["events"]
    ]
    assert all(update.get("status") != "failed" for update in state["updates"])


def test_main_chat_model_loop_releases_browser_target_except_during_approval() -> None:
    class _ClosableBroker(dict[str, Any]):
        def __init__(self) -> None:
            super().__init__()
            self.closes = 0

        def close_owned_browser_target(self) -> None:
            self.closes += 1

    class _ClosableToolBrokers:
        def __init__(self, broker: _ClosableBroker) -> None:
            self.broker = broker

        def for_main_chat(self, **_kwargs: Any) -> _ClosableBroker:
            return self.broker

    completed_broker = _ClosableBroker()
    completed_runner, _state = _runner()
    completed_runner._tool_brokers = _ClosableToolBrokers(completed_broker)

    assert completed_runner.execute(
        "run-1",
        [{"role": "user", "content": "hi"}],
    )["status"] == "running"
    assert completed_broker.closes == 1

    approval_broker = _ClosableBroker()
    approval_runner, _state = _runner()
    approval_runner._tool_brokers = _ClosableToolBrokers(approval_broker)
    approval_runner._continue_custom_api_agent = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AgentApprovalRequired({"approval_id": "approval-1", "tool": "terminal.run"})
        )
    )

    assert approval_runner.execute(
        "run-1",
        [{"role": "user", "content": "run command"}],
    )["status"] == "approval_required"
    assert approval_broker.closes == 0


def test_main_chat_model_loop_runner_forwards_runtime_execution_context() -> None:
    continue_calls: list[dict[str, Any]] = []
    runner, _state = _runner()
    envelope = {"envelope_id": "env-main", "requests": [{"tool_name": "app.open"}]}
    metadata = {"yachiyo_runtime_planner": True}

    def continue_custom_api_agent(
        _agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        _timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append(kwargs)
        return "done"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "打开 Apple Music"}],
        runtime_execution_envelope=envelope,
        runtime_execution_metadata=metadata,
    )

    assert result["status"] == "running"
    assert continue_calls[0]["runtime_execution_envelope"] is envelope
    forwarded_metadata = continue_calls[0]["runtime_execution_metadata"]
    assert forwarded_metadata["yachiyo_runtime_planner"] is True
    assert forwarded_metadata["desktop_execution_policy"]["mode"] == "preview_input"


def test_main_chat_model_loop_runner_passes_approval_policy_to_broker() -> None:
    runner, state = _runner()
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {
            "allowed_tools": ["desktop.type_text"],
            "approval_required": {"desktop.type_text": True},
        },
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    result = runner.execute("run-1", [{"role": "user", "content": "输入 hello"}])

    assert result["status"] == "running"
    assert state["tool_brokers"].calls[0]["approvals"] == {"desktop.type_text": True}


def test_main_chat_model_loop_runner_treats_runtime_envelope_as_direct_without_profile() -> None:
    continue_calls: list[dict[str, Any]] = []
    runner, state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    envelope = {
        "requests": [
            {
                "request_id": "open-music",
                "tool_name": "app.open",
                "input": {"app_name": "Music"},
            }
        ]
    }

    def continue_custom_api_agent(
        agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        _timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append({"agent": agent, "kwargs": kwargs})
        return "opened"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "打开 Apple Music"}],
        runtime_execution_envelope=envelope,
    )

    assert result["status"] == "running"
    assert result["result"] == "opened"
    assert continue_calls[0]["agent"]["model_profile_id"] == ""
    assert continue_calls[0]["kwargs"]["runtime_execution_envelope"] is envelope
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed"
    ]


def test_main_chat_model_loop_runner_does_not_read_profile_for_direct_task() -> None:
    runner, state = _runner()
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {
            "allowed_tools": ["desktop.list_apps", "app.open", "desktop.verify"]
        },
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    runner._model_profile_config_private = lambda _profile_id: (_ for _ in ()).throw(
        AssertionError("direct desktop task must not resolve model credentials")
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: "已打开 Calculator。"

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "请打开计算器"}],
    )

    assert result["status"] == "running"
    assert result["result"] == "已打开 Calculator。"
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed"
    ]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"direct_tool_request": {"tool": "app.open"}},
        {"direct_tool_requests": [{"tool": "app.open"}]},
    ],
)
def test_main_chat_model_loop_runner_does_not_read_profile_for_explicit_direct_task(
    request_kwargs: dict[str, Any],
) -> None:
    runner, state = _runner()
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    runner._model_profile_config_private = lambda _profile_id: (_ for _ in ()).throw(
        AssertionError("direct desktop task must not resolve model credentials")
    )
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: "opened"

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "打开计算器"}],
        **request_kwargs,
    )

    assert result["status"] == "running"
    assert result["result"] == "opened"
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed"
    ]


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {
            "direct_tool_request": {
                "tool": "app.open",
                "input": {"app_name": "Music"},
            }
        },
        {
            "direct_tool_requests": [
                {"tool": "app.open", "input": {"app_name": "Music"}}
            ]
        },
        {
            "runtime_execution_envelope": {
                "requests": [
                    {
                        "request_id": "open-music",
                        "tool_name": "app.open",
                        "input": {"app_name": "Music"},
                    }
                ]
            }
        },
    ],
)
def test_main_chat_authoritative_direct_plan_skips_initial_model_assistance_without_profile(
    request_kwargs: dict[str, Any],
) -> None:
    runner, state = _runner()
    state["run"]["user_goal"] = "打开 Music"
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    runner._resolve_initial_model_plan = lambda **_kwargs: (_ for _ in ()).throw(
        AssertionError("an authoritative local plan must skip model-assisted planning")
    )
    runner._model_profile_config_private = lambda _profile_id: (_ for _ in ()).throw(
        AssertionError("an authoritative local plan must not resolve model credentials")
    )

    def continue_custom_api_agent(*_args: Any, **_kwargs: Any) -> str:
        assert state["events"][0][0] == "agent.goal.contract"
        return "opened"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "打开 Music"}],
        runtime_execution_metadata={
            "goal_contract": planned_goal_contract_payload(
                "打开 Music", allowed_tools=["app.open"],
            ),
        },
        **request_kwargs,
    )

    assert result["result"] == "opened"
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed",
    ]


def test_main_chat_direct_model_followup_still_enters_initial_model_planning() -> None:
    planner_calls: list[dict[str, Any]] = []
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["clipboard.read"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def resolve_initial_model_plan(**kwargs: Any) -> None:
        planner_calls.append(kwargs)
        return None

    runner._resolve_initial_model_plan = resolve_initial_model_plan
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("semantic followup must not bypass model readiness")
    )

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "读取剪贴板并总结"}],
            direct_tool_requests=[
                {"tool": "clipboard.read", "continue_to_model": True}
            ],
        )

    assert len(planner_calls) == 1


def test_main_chat_metadata_only_plan_cannot_skip_initial_model_planning() -> None:
    planner_calls: list[dict[str, Any]] = []
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def resolve_initial_model_plan(**kwargs: Any) -> None:
        planner_calls.append(kwargs)
        return None

    runner._resolve_initial_model_plan = resolve_initial_model_plan

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "hi"}],
            runtime_execution_metadata={
                "daily_desktop_intent": True,
                "yachiyo_execution_envelope": {
                    "requests": [
                        {
                            "request_id": "metadata-open-music",
                            "tool_name": "app.open",
                            "input": {"app_name": "Music"},
                        }
                    ]
                },
            },
        )

    assert len(planner_calls) == 1


def test_main_chat_direct_plan_without_allowed_tool_remains_model_first() -> None:
    planner_calls: list[dict[str, Any]] = []
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": []},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def resolve_initial_model_plan(**kwargs: Any) -> None:
        planner_calls.append(kwargs)
        return None

    runner._resolve_initial_model_plan = resolve_initial_model_plan
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("a disallowed direct tool must not bypass model readiness")
    )

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "打开 Music"}],
            direct_tool_request={"tool": "app.open", "input": {"app_name": "Music"}},
        )

    assert len(planner_calls) == 1


@pytest.mark.parametrize(
    "invalid_request",
    [
        {"tool": "desktop.quit_app"},
        {},
    ],
)
def test_main_chat_mixed_invalid_direct_plan_remains_model_first(
    invalid_request: dict[str, Any],
) -> None:
    planner_calls: list[dict[str, Any]] = []
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def resolve_initial_model_plan(**kwargs: Any) -> None:
        planner_calls.append(kwargs)
        return None

    runner._resolve_initial_model_plan = resolve_initial_model_plan

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "打开 Music"}],
            direct_tool_requests=[
                {"tool": "app.open", "input": {"app_name": "Music"}},
                invalid_request,
            ],
        )

    assert len(planner_calls) == 1


@pytest.mark.parametrize(
    "request_kwargs",
    [
        {
            "direct_tool_request": {
                "tool": "clipboard.read",
                "continue_to_model": True,
            }
        },
        {
            "direct_tool_requests": [
                {"tool": "clipboard.read", "continue_to_model": True}
            ]
        },
    ],
)
def test_main_chat_model_loop_runner_requires_profile_for_explicit_model_followup(
    request_kwargs: dict[str, Any],
) -> None:
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["clipboard.read"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("model-followup requests must not bypass profile readiness")
    )

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "读取剪贴板并总结"}],
            **request_kwargs,
        )


def test_main_chat_model_loop_runner_uses_runtime_planner_without_profile_before_legacy(
) -> None:
    assert not hasattr(main_chat_model_loop, "daily_desktop_intent_tool_request")
    assert not hasattr(main_chat_model_loop, "daily_desktop_intent_tool_requests")
    assert not hasattr(main_chat_model_loop, "daily_desktop_intent_candidates")

    continue_calls: list[dict[str, Any]] = []
    runner, state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.open", "desktop.click_ui_element"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def continue_custom_api_agent(
        agent: dict[str, Any],
        _context: str,
        broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append(
            {
                "agent": agent,
                "broker": broker,
                "timeline": timeline,
                "artifacts": artifacts,
                "kwargs": kwargs,
            }
        )
        return "opened"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "打开 PixelForge 并点击导出按钮"}],
    )

    assert result["status"] == "running"
    assert result["result"] == "opened"
    assert continue_calls[0]["agent"]["model_profile_id"] == ""
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed"
    ]
    assert state["tool_brokers"].calls == [
        {
            "run_id": "run-1",
            "workspace_policy": {"default_workdir": "/tmp/project"},
        }
    ]


def test_main_chat_model_loop_runner_keeps_profile_required_for_planner_model_followup() -> None:
    runner, _state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["workspace.list", "workspace.read", "artifact.write"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def continue_custom_api_agent(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("model-followup planner requests should not bypass profile readiness")

    runner._continue_custom_api_agent = continue_custom_api_agent

    with pytest.raises(
        agent_runtime.AgentRuntimeError,
        match="native_agent_not_ready:chat_model_profile_required",
    ):
        runner.execute("run-1", [{"role": "user", "content": "写一份项目总结报告"}])


def test_main_chat_model_loop_runner_treats_discovered_app_followup_as_direct() -> None:
    continue_calls: list[dict[str, Any]] = []
    runner, state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {
            "allowed_tools": [
                "desktop.list_apps",
                "desktop.open_app",
                "desktop.active_window",
            ],
        },
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def continue_custom_api_agent(
        agent: dict[str, Any],
        _context: str,
        broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        artifacts: list[dict[str, Any]],
        **kwargs: Any,
    ) -> str:
        continue_calls.append(
            {
                "agent": agent,
                "broker": broker,
                "timeline": timeline,
                "artifacts": artifacts,
                "kwargs": kwargs,
            }
        )
        return "opened"

    runner._continue_custom_api_agent = continue_custom_api_agent

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "找一个能编辑 PDF 的本机应用并打开它"}],
    )

    assert result["status"] == "running"
    assert result["result"] == "opened"
    assert continue_calls[0]["agent"]["model_profile_id"] == ""
    assert [event_type for event_type, _payload in state["events"]] == [
        "agent.goal.contract",
        "model.output.completed"
    ]


def test_main_chat_model_loop_runner_projects_approval_required_without_bypassing_gate() -> None:
    def raise_approval(*_args: Any, **_kwargs: Any) -> str:
        raise AgentApprovalRequired({"tool": "terminal.run", "approval_id": "approval-1"})

    runner, state = _runner()
    runner._continue_custom_api_agent = raise_approval
    envelope = {"envelope_id": "env-approval", "requests": [{"tool_name": "terminal.run"}]}
    metadata = {"yachiyo_runtime_planner": True}

    result = runner.execute(
        "run-1",
        [{"role": "user", "content": "run command"}],
        runtime_execution_envelope=envelope,
        runtime_execution_metadata=metadata,
    )

    assert result["status"] == "approval_required"
    pending = state["approval_pause"].calls[0]["pending_approval"]
    assert {
        key: value
        for key, value in pending.items()
        if key != "runtime_execution_metadata"
    } == {
        "pending": {"tool": "terminal.run", "approval_id": "approval-1"},
        "model_profile_id": "profile-chat",
        "tool_policy": {"allowed_tools": ["workspace.read"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
        "runtime_execution_envelope": envelope,
    }
    assert pending["runtime_execution_metadata"]["yachiyo_runtime_planner"] is True
    assert (
        pending["runtime_execution_metadata"]["desktop_execution_policy"]["mode"]
        == "preview_input"
    )


def test_main_chat_model_loop_runner_reports_provider_blocker_without_chat_profile() -> None:
    runner, state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.focus"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def fail_after_provider_block(
        _agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> str:
        timeline.append(
            _timeline(
                "agent.tool.skipped",
                "app.focus",
                result={
                    "ok": False,
                    "error": "desktop_execution_policy_blocked",
                    "blocking_conditions": [
                        "loopback_desktop_backend",
                        "real_virtual_desktop_backend_required",
                    ],
                },
            )
        )
        raise agent_runtime.AgentRuntimeError(
            "Agent 缺少可运行的 Chat Profile；请在 Agent Studio 为该岗位选择已测试的文本模型。"
        )

    runner._continue_custom_api_agent = fail_after_provider_block
    envelope = {
        "requests": [
            {
                "request_id": "focus-browser",
                "tool_name": "app.focus",
                "input": {"app_name": "Google Chrome"},
            }
        ]
    }

    with pytest.raises(agent_runtime.AgentRuntimeError, match="隔离桌面 Provider"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "打开 Chrome 后退一下"}],
            runtime_execution_envelope=envelope,
        )

    assert state["updates"][-1]["status"] == "failed"
    assert "Chat Profile" not in state["updates"][-1]["result"]
    event_type, event_payload = state["events"][-1]
    assert event_type == "agent.desktop.permission_recovery"
    assert event_payload["error"] == state["updates"][-1]["result"]
    assert event_payload["status"] == "blocked"
    assert event_payload["blocking_conditions"] == [
        "loopback_desktop_backend",
        "real_virtual_desktop_backend_required",
    ]
    assert event_payload["recovery_actions"] == [
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


def test_main_chat_model_loop_runner_preserves_unrelated_error_after_provider_block() -> None:
    runner, state = _runner()
    runner._default_profile_id = lambda: ""
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["app.focus"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }

    def fail_with_unrelated_error(
        _agent: dict[str, Any],
        _context: str,
        _broker: dict[str, Any],
        timeline: list[dict[str, Any]],
        _artifacts: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> str:
        timeline.append(
            _timeline(
                "agent.tool.skipped",
                "app.focus",
                result={
                    "error": "desktop_execution_policy_blocked",
                    "blocking_conditions": ["sandbox_desktop_provider_required"],
                },
            )
        )
        raise agent_runtime.AgentRuntimeError("planner payload malformed")

    runner._continue_custom_api_agent = fail_with_unrelated_error

    with pytest.raises(agent_runtime.AgentRuntimeError, match="planner payload malformed"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "打开 Chrome 后退一下"}],
            runtime_execution_envelope={
                "requests": [
                    {
                        "request_id": "focus-browser",
                        "tool_name": "app.focus",
                        "input": {"app_name": "Google Chrome"},
                    }
                ]
            },
        )

    assert state["updates"][-1]["result"] == "planner payload malformed"
    assert state["events"][-1][0] == "model.request.failed"


def test_main_chat_model_loop_delegates_terminal_failure_to_main_chat_lifecycle() -> None:
    runner, state = _runner()
    lifecycle_calls: list[dict[str, Any]] = []
    original_update_run = runner._update_run

    def reject_direct_terminal_update(run_id: str, **payload: Any) -> dict[str, Any] | None:
        if payload.get("status") == "failed":
            raise AssertionError("model loop must not own terminal failure projection")
        return original_update_run(run_id, **payload)

    def fail_main_chat_run(
        run_id: str,
        error: Any,
        **payload: Any,
    ) -> dict[str, Any]:
        lifecycle_calls.append({"run_id": run_id, "error": str(error), **payload})
        state["run"].update(status="failed", result=str(error), pending_approval=None)
        return dict(state["run"])

    runner._update_run = reject_direct_terminal_update
    runner._fail_main_chat_run = fail_main_chat_run
    runner._continue_custom_api_agent = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        RuntimeError("model exploded")
    )

    with pytest.raises(RuntimeError, match="model exploded"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "Handle it"}],
        )

    assert len(lifecycle_calls) == 1
    call = lifecycle_calls[0]
    assert call["run_id"] == "run-1"
    assert call["error"] == "model exploded"
    assert call["timeline"][-1]["event"] == "model.request.failed"
    assert call["artifacts"] == []
    assert call["run_events"] == [
        ("model.request.failed", {"error": "model exploded"}),
    ]


def test_main_chat_model_loop_records_unverified_direct_outcome_without_model_failure() -> None:
    runner, state = _runner()
    runner._compile_agent_runtime = lambda _agent: {
        "tool_policy": {"allowed_tools": ["media.system_control"]},
        "workspace_policy": {"default_workdir": "/tmp/project"},
    }
    message = (
        "已发送媒体键尝试切到下一首当前媒体，但无法确认播放状态；"
        "请在播放器中确认后重试。"
    )

    def fail_unverified_direct_outcome(*_args: Any, **_kwargs: Any) -> str:
        raise AgentDirectOutcomeUnverified(
            message,
            tool_name="media.system_control",
            input_preview={"action": "next"},
        )

    runner._continue_custom_api_agent = fail_unverified_direct_outcome

    with pytest.raises(AgentDirectOutcomeUnverified, match="无法确认播放状态"):
        runner.execute(
            "run-1",
            [{"role": "user", "content": "切歌"}],
            direct_tool_request={
                "protocol": "json_fallback",
                "tool": "media.system_control",
                "input": {"action": "next"},
            },
        )

    assert state["updates"][-1]["status"] == "failed"
    assert state["updates"][-1]["result"] == message
    assert state["events"][-1] == (
        "agent.desktop.intent_unverified",
        {
            "error": message,
            "status": "failed",
            "reason": "desktop_verification_missing",
            "tool": "media.system_control",
            "input_preview": {"action": "next"},
        },
    )
    assert not any(
        event_type.startswith("model.request.")
        for event_type, _payload in state["events"]
    )


def test_main_chat_model_loop_runner_forwards_provider_recovery_actions() -> None:
    action = {
        "tool": "desktop.provider_session.start",
        "label": "Configure release provider",
        "input": {"provider_id": "provider-1"},
        "permission_target": "isolated_desktop_provider",
        "risk_level": "medium",
        "approval_required": True,
        "deferred_continuation": [
            {"tool": "app.focus", "input": {"app_name": "Google Chrome"}}
        ],
    }
    failure = main_chat_model_loop._desktop_provider_required_failure(
        [
            _timeline(
                "agent.tool.skipped",
                "app.focus",
                result={
                    "error": "desktop_execution_policy_blocked",
                    "blocking_conditions": ["sandbox_desktop_provider_required"],
                    "recovery_actions": [action],
                },
            )
        ]
    )

    assert failure["recovery_actions"] == [action]
    assert failure["recovery_actions"][0] is not action


def test_main_chat_background_provider_block_does_not_offer_isolated_autostart() -> None:
    failure = main_chat_model_loop._desktop_provider_required_failure(
        [
            _timeline(
                "agent.tool.skipped",
                "desktop.safe_type_text",
                result={
                    "error": "desktop_execution_policy_blocked",
                    "blocking_conditions": ["cua_driver_not_installed"],
                    "desktop_execution_route": {
                        "selected_provider_kind": "background_desktop",
                    },
                },
            )
        ]
    )

    assert "后台操作组件" in failure["summary"]
    assert "未接管你正在使用的桌面" in failure["summary"]
    assert failure["recovery_actions"] == []


def test_native_runtime_installs_main_chat_model_loop_runner(tmp_path) -> None:
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    try:
        assert agent_runtime.MainChatModelLoopRunner is MainChatModelLoopRunner
        assert isinstance(service.main_chat_model_loop, MainChatModelLoopRunner)
        assert getattr(service.main_chat_model_loop._run_budget, "__self__", None) is not service
        assert (
            getattr(
                service.main_chat_model_loop._check_context_budget,
                "__self__",
                None,
            )
            is not service
        )
    finally:
        service.close()
