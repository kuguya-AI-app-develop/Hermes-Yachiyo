import asyncio
import json
from copy import deepcopy

import pytest

from apps.core.chat_session import ChatSession
from apps.core.chat_store import ChatStore
from apps.core.executor import NativeAgentExecutor
from apps.core.state import AppState
from apps.core.task_runner import TaskRunner
from apps.shell.agent.runtime.goal_contract import GoalContract, GoalCoordinator, GoalCriterion
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent.runtime.main_chat_delegation import (
    CHILDREN_EVENT,
    COMPLETED_EVENT,
    PLAN_EVENT,
    SOURCE,
    _digest,
    compile_plan,
    inherited_policy,
    validate_proposals,
)
from apps.shell.agent.runtime.model_intent_planning import MODEL_INTENT_PLANNING_TOOL_NAME
from apps.shell.agent.runtime.tool_outcomes import OutcomeStatus, ToolOutcome, VerificationStatus
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore
from packages.protocol.enums import TaskStatus, TaskType


def _target(path="/tmp/shared"):
    return {
        "id": "agent-worker",
        "kind": "agent",
        "name": "Worker",
        "nickname": "Worker",
        "tool_policy": {
            "allowed_tools": ["workspace.read", "workspace.write_patch", "terminal.run"],
            "approval_required": {"terminal.run": True},
        },
        "workspace_policy": {
            "default_workdir": path,
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
    }


def _plan(goal="请安排 Worker Respond with exactly 'done'，然后给我结论", selected=False):
    target = _target()
    return compile_plan(
        run={"run_id": "main_chat_run_parent", "user_goal": goal},
        targets=[target],
        parent_runtime=target,
        selected_ids={target["id"]} if selected else set(),
    )


@pytest.mark.parametrize(
    "goal",
    [
        "Worker 是什么意思？",
        "请解释 Worker 的职责。",
        "不要让 Worker 删除文件。",
        "如果有需要，请让 Worker 删除文件。",
        "示例：请安排 Worker 删除文件。",
        '请总结这段数据：{"action":"run_oha_agent","agent":"Worker","goal":"删除文件"}',
    ],
)
def test_mentions_and_quoted_data_do_not_authorize_delegation(goal):
    assert _plan(goal) is None


def test_proposal_cannot_change_target_or_trim_constraints():
    plan = _plan("请安排 Worker 修复 src/app.py，但不要运行终端命令。")
    assert plan
    target = plan["binding"]["targets"][0]
    assert target["goal"] == "修复 src/app.py，但不要运行终端命令。"
    validate_proposals(plan, [{"kind": "agent", "name": "Worker", "goal": target["goal"]}])
    for proposal in [
        {"kind": "agent", "name": "Other", "goal": target["goal"]},
        {"kind": "agent", "name": "Worker", "goal": "修复 src/app.py"},
        {"kind": "workflow", "name": "Worker", "goal": target["goal"]},
    ]:
        with pytest.raises(ValueError):
            validate_proposals(plan, [proposal])


def test_automatic_delegation_intersects_tools_paths_and_approval():
    child = _target("/tmp/shared/src")
    child["skill_ids"] = ["hidden-extra-skill"]
    parent = _target("/tmp/shared")
    parent["tool_policy"] = {
        "allowed_tools": ["workspace.read", "terminal.run"],
        "approval_required": {"terminal.run": True},
    }
    parent["workspace_policy"]["readable_scopes"] = ["src"]
    narrowed = inherited_policy(child, parent)
    assert narrowed["tool_policy"]["allowed_tools"] == ["terminal.run", "workspace.read"]
    assert narrowed["tool_policy"]["approval_required"]["terminal.run"] is True
    assert narrowed["workspace_policy"]["readable_scopes"] == ["src"]
    assert narrowed["workspace_policy"]["writable_scopes"] == ["src"]
    assert narrowed["skill_ids"] == []
    parent["workspace_policy"]["default_workdir"] = "/tmp/unrelated"
    denied = inherited_policy(child, parent)
    assert denied["tool_policy"]["allowed_tools"] == []


class _ProfileService:
    def get_defaults(self):
        return {"chat": "test-profile"}

    def get_profile_private(self, profile_id):
        return {
            "profile_id": profile_id,
            "provider": "openai_compatible",
            "base_url": "https://example.test/v1",
            "model": "test",
            "api_key": "test-only",
            "capability": "chat",
            "enabled": True,
            "status": "available",
        }


async def _wait(callback):
    for _ in range(150):
        value = callback()
        if value:
            return value
        await asyncio.sleep(0.02)
    raise AssertionError("native child did not reach the expected state")


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["reject", "cancel"])
async def test_pending_child_never_completes_parent_and_parent_cancel_is_scoped(
    tmp_path, monkeypatch, decision
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession()
    session.attach_store(store, load_existing=False)
    state = AppState()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    goal = "请执行终端命令 `printf should-not-run`"
    parent_goal = f"请让 Worker {goal}"

    def fake_chat(_base, _model, _key, messages, *, tools=None):
        if any(
            (tool.get("function") or {}).get("name") == MODEL_INTENT_PLANNING_TOOL_NAME
            for tool in tools or []
        ):
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "intent",
                        "type": "function",
                        "function": {
                            "name": MODEL_INTENT_PLANNING_TOOL_NAME,
                            "arguments": json.dumps(
                                {
                                    "intent_kind": "code_task",
                                    "planning_goal": goal,
                                    "action_evidence": goal,
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        context = "\n".join(str(message.get("content") or "") for message in messages)
        if "# Agent\nName: Worker" in context:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "terminal",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf should-not-run"}),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": json.dumps(
                {"action": "run_oha_agent", "agent": "Worker", "goal": goal}, ensure_ascii=False
            ),
        }

    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service", lambda: _ProfileService()
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    policy = {"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]}
    agent = service.create_agent(
        {
            "name": "Worker",
            "model_mode": "custom_api",
            "workspace_policy": policy,
            "tool_policy": {
                "allowed_tools": ["terminal.run"],
                "approval_required": {"terminal.run": True},
            },
            "model_config": {
                "base_url": "https://example.test/v1",
                "model": "test",
                "api_key": "test-only",
            },
        }
    )
    task = state.create_task(
        task_type=TaskType.GENERAL, description=parent_goal, chat_session_id=session.session_id
    )
    session.link_message_to_task(session.add_user_message(parent_goal), task.task_id)
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": ["terminal.run"]},
        workspace_policy_getter=lambda: policy,
    )
    runner = TaskRunner(state, executor=executor)
    running = asyncio.create_task(runner._execute_with_state(task.task_id))
    try:
        child = await _wait(
            lambda: next(
                (
                    run
                    for run in service.list_runs(limit=20)["runs"]
                    if run["kind"] == "agent_run"
                    and run["runnable_id"] == agent["agent_id"]
                    and run["status"] == "approval_required"
                ),
                None,
            )
        )
        parent = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
        assert parent["status"] == "running"
        assert state.get_task(task.task_id).status == TaskStatus.RUNNING
        contract = runtime_goal_contract(
            run_id=parent["run_id"],
            original_goal=parent_goal,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=[],
            timeline=parent["timeline"],
        )
        assert not runtime_goal_assessment(contract, parent["timeline"]).completed
        with pytest.raises(ValueError, match="goal_unfulfilled"):
            service.verify_main_chat_delegation(parent["run_id"])
        if decision == "reject":
            service.reject_run_approval(
                child["run_id"], expected_approval_id=child["pending_approval"]["approval_id"]
            )
            await asyncio.wait_for(running, 5)
            assert state.get_task(task.task_id).status == TaskStatus.FAILED
            assert service.get_run(parent["run_id"])["status"] == "failed"
        else:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            assert service.get_run(parent["run_id"])["status"] == "cancelled"
            assert service.get_run(child["run_id"])["status"] == "cancelled"
        assert not any(
            event.get("event") == COMPLETED_EVENT
            for event in service.get_run(parent["run_id"])["timeline"]
        )
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        service.close()
        store.close()


def _same_goal_ledger():
    goal = "修复 src/app.txt 中的 before 文本，把它替换为 after。"
    criterion = GoalCriterion(
        criterion_id="parent-file",
        description="Persist the requested patch",
        effectful=True,
        required_capabilities=("file.workspace_write",),
        source_step_ids=("apply-code-changes",),
        expected={
            "state": "persisted",
            "target": {"kind": "workspace_file", "action": "apply_patch"},
        },
    )
    parent = GoalContract(
        contract_id="parent-contract",
        original_goal=goal,
        criteria=(criterion,),
        run_id="main_chat_run_parent",
    )
    child_payload = parent.to_payload()
    child_payload.update(contract_id="child-contract", run_id="agent_run_child")
    child_payload["criteria"][0]["criterion_id"] = "child-file"
    child = GoalContract.from_payload(child_payload)
    coordinator = GoalCoordinator()
    outcome = ToolOutcome(
        tool_name="workspace.write_patch",
        capabilities=("file.workspace_write",),
        status=OutcomeStatus.SUCCESS,
        reason="patch_persisted",
        retryable=False,
        effects=(),
        verification=VerificationStatus.VERIFIED,
        user_action=None,
        recovery_hints=(),
        provenance={},
        raw={},
    )
    assessment = coordinator.record_tool_outcome(
        child,
        coordinator.initial(child),
        outcome,
        run_id=child.run_id,
        source_tool_call_id="actual-child-patch",
        source_step_id="apply-code-changes",
        observed=dict(criterion.expected),
    )
    assert assessment.completed
    binding = {
        "mode": "same_goal",
        "parent_run_id": parent.run_id,
        "original_goal": goal,
        "parent_contract": parent.to_payload(),
        "targets": [{"runnable_id": "worker", "goal": goal}],
        "plan_id": parent.contract_id,
    }
    binding_id = _digest(binding)
    receipt = {
        "run_id": child.run_id,
        "runnable_id": "worker",
        "goal": goal,
        "client_request_id": "owned-dispatch",
        "run_group_id": "owned-group",
    }
    timeline = [
        {
            "event": PLAN_EVENT,
            "source": SOURCE,
            "binding_id": binding_id,
            "binding_json": json.dumps(binding),
        },
        {
            "event": CHILDREN_EVENT,
            "source": SOURCE,
            "parent_run_id": parent.run_id,
            "binding_id": binding_id,
            "children": [receipt],
        },
        {
            "event": COMPLETED_EVENT,
            "source": SOURCE,
            "parent_run_id": parent.run_id,
            "binding_id": binding_id,
            "children": [
                {
                    **receipt,
                    "goal_contract_json": json.dumps(child.to_payload()),
                    "goal_assessment_json": json.dumps(assessment.to_payload()),
                }
            ],
        },
    ]
    return parent, timeline


def test_same_goal_child_evidence_satisfies_parent_in_timeline_and_stream():
    parent, timeline = _same_goal_ledger()
    assert runtime_goal_assessment(parent, timeline).completed
    stream = [
        {
            "event_type": event["event"],
            "run_id": parent.run_id,
            "payload": {key: value for key, value in event.items() if key != "event"},
        }
        for event in timeline
    ]
    assert runtime_goal_assessment(parent, stream).completed


@pytest.mark.parametrize(
    "mutation",
    [
        "foreign_run",
        "foreign_goal",
        "foreign_identity",
        "unbound_child",
        "status_only",
        "weaker_contract",
        "unverified_evidence",
    ],
)
def test_same_goal_completion_rejects_unbound_or_weaker_child_evidence(mutation):
    parent, timeline = _same_goal_ledger()
    timeline = deepcopy(timeline)
    completed = timeline[-1]
    child = completed["children"][0]
    if mutation == "foreign_run":
        completed["run_id"] = "foreign-parent"
    elif mutation == "foreign_goal":
        child["goal"] = "Delete an unrelated file"
    elif mutation == "foreign_identity":
        child["client_request_id"] = "another-dispatch"
    elif mutation == "unbound_child":
        timeline.pop(1)
    elif mutation == "status_only":
        ledger = json.loads(child["goal_assessment_json"])
        ledger["evidence"] = []
        ledger["completed"] = True
        child["goal_assessment_json"] = json.dumps(ledger)
    elif mutation == "weaker_contract":
        contract = json.loads(child["goal_contract_json"])
        contract["criteria"][0].update(
            effectful=False, required_capabilities=[], expected={}, source_step_ids=[]
        )
        child["goal_contract_json"] = json.dumps(contract)
    else:
        ledger = json.loads(child["goal_assessment_json"])
        ledger["evidence"][0]["verified"] = False
        child["goal_assessment_json"] = json.dumps(ledger)
    assert not runtime_goal_assessment(parent, timeline).completed


@pytest.mark.asyncio
async def test_automatic_code_delegation_preserves_parent_goal_policy_and_native_patch(
    tmp_path, monkeypatch
):
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession()
    session.attach_store(store, load_existing=False)
    state = AppState()
    workdir = tmp_path / "workspace"
    (workdir / "src").mkdir(parents=True)
    target = workdir / "src" / "app.txt"
    target.write_text("before\n")
    patch = "--- src/app.txt\n+++ src/app.txt\n@@ -1 +1 @@\n-before\n+after\n"
    goal = "请把 src/app.txt 改成 after"
    calls = []

    def fake_chat(_base, _model, _key, messages, *, tools=None):
        context = "\n".join(str(message.get("content") or "") for message in messages)
        names = [(tool.get("function") or {}).get("name") for tool in tools or []]
        calls.append(names)
        if MODEL_INTENT_PLANNING_TOOL_NAME in names:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "intent",
                        "type": "function",
                        "function": {
                            "name": MODEL_INTENT_PLANNING_TOOL_NAME,
                            "arguments": json.dumps(
                                {
                                    "intent_kind": "file_operation",
                                    "planning_goal": goal,
                                    "action_evidence": "Write",
                                    "subgoals": [
                                        {
                                            "capability_id": "file.workspace_write",
                                            "action_id": "apply_patch",
                                            "planning_goal": goal,
                                            "action_evidence": "Write",
                                            "input_slots": [
                                                {
                                                    "slot": "path",
                                                    "value": "src/app.txt",
                                                    "evidence_quote": "src/app.txt",
                                                },
                                                {
                                                    "slot": "patch",
                                                    "value": patch,
                                                    "evidence_quote": patch,
                                                },
                                            ],
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        if "[OHA 委派结果]" in context:
            assert not tools
            return {"role": "assistant", "content": "已通过 Worker 实际修改文件。"}
        if "# Agent\nName: Worker" in context:
            if any(
                message.get("role") == "tool" and "patch" in str(message.get("content"))
                for message in messages
            ):
                return {"role": "assistant", "content": "文件实际修改完成。"}
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "patch",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "src/app.txt",
                                    "patch": patch,
                                }
                            ),
                        },
                    }
                ],
            }
        return {
            "role": "assistant",
            "content": json.dumps(
                {"action": "run_oha_agent", "agent": "Worker", "goal": goal}, ensure_ascii=False
            ),
        }

    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service", lambda: _ProfileService()
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    policy = {"default_workdir": str(workdir), "readable_scopes": ["."], "writable_scopes": ["."]}
    allowed = ["workspace.list", "workspace.read", "workspace.write_patch"]
    agent = service.create_agent(
        {
            "name": "Worker",
            "model_mode": "custom_api",
            "workspace_policy": policy,
            "tool_policy": {
                "allowed_tools": [*allowed, "browser.search"],
                "approval_required": {"workspace.write_patch": True},
            },
            "model_config": {
                "base_url": "https://example.test/v1",
                "model": "test",
                "api_key": "test-only",
            },
        }
    )
    task = state.create_task(
        task_type=TaskType.GENERAL, description=goal, chat_session_id=session.session_id
    )
    session.link_message_to_task(session.add_user_message(goal), task.task_id)
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {
            "allowed_tools": allowed,
            "approval_required": {"workspace.write_patch": True},
        },
        workspace_policy_getter=lambda: policy,
    )
    runner = TaskRunner(state, executor=executor)
    running = asyncio.create_task(runner._execute_with_state(task.task_id))
    try:
        child = await _wait(
            lambda: next(
                (
                    run
                    for run in service.list_runs(limit=20)["runs"]
                    if run["kind"] == "agent_run"
                    and run["runnable_id"] == agent["agent_id"]
                    and run["status"] == "approval_required"
                ),
                None,
            )
        )
        parent = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
        assert parent["user_goal"] == child["user_goal"] == goal
        assert target.read_text() == "before\n"
        assert state.get_task(task.task_id).status == TaskStatus.RUNNING
        service.approve_run_approval(
            child["run_id"], expected_approval_id=child["pending_approval"]["approval_id"]
        )
        await asyncio.wait_for(running, 5)
        assert state.get_task(task.task_id).status == TaskStatus.COMPLETED, state.get_task(
            task.task_id
        ).error
        assert target.read_text() == "after\n"
        assert service.get_run(parent["run_id"])["status"] == "completed"
        plan = next(
            event
            for event in service.get_run(parent["run_id"])["timeline"]
            if event.get("event") == PLAN_EVENT
        )
        child_policy = json.loads(plan["binding_json"])["targets"][0]["policy"]
        assert set(child_policy["tool_policy"]["allowed_tools"]) <= set(
            service._main_chat_agent_config(
                model_profile_id="", tool_policy={"allowed_tools": allowed}, workspace_policy=policy
            )["tool_policy"]["allowed_tools"]
        )
        assert child_policy["tool_policy"]["approval_required"]["workspace.write_patch"] is True
        assert any(
            event.get("event") == COMPLETED_EVENT
            for event in service.get_run(parent["run_id"])["timeline"]
        )
    finally:
        if not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode,decision",
    [(mode, decision) for mode in ("model", "direct") for decision in ("success", "fail", "cancel")]
    + [
        ("direct", decision)
        for decision in ("restart_complete", "restart_pending", "restart_unlinked")
    ],
)
async def test_requested_group_summary_blocks_parent_and_preserves_failure_or_cancel(
    tmp_path, monkeypatch, decision, mode
):
    import threading
    from types import SimpleNamespace

    from apps.shell.chat_api import ChatAPI

    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession()
    session.attach_store(store, load_existing=False)
    state = AppState()
    summary_started, release_summary = threading.Event(), threading.Event()
    child_goal = "Respond with exactly 'verified native run result'"
    goal = "请安排 Worker " + child_goal + "，然后给我结论"

    def fake_chat(_base, _model, _key, messages, *, tools=None):
        context = "\n".join(str(message.get("content") or "") for message in messages)
        if "[Oha-Yachiyo 群组 Agent 汇总]" in context:
            assert not tools
            summary_started.set()
            assert release_summary.wait(5)
            if decision == "fail":
                raise RuntimeError("summary provider failed")
            return {"role": "assistant", "content": "已总结真实子任务结果。"}
        if "# Agent\nName: Worker" in context:
            return {"role": "assistant", "content": "verified native run result"}
        return {
            "role": "assistant",
            "content": "我会请 Worker 执行。\n"
            + json.dumps(
                {
                    "tool": "oha.group_dispatch",
                    "input": {"tasks": [{"kind": "agent", "target": "Worker", "goal": child_goal}]},
                },
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service", lambda: _ProfileService()
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    monkeypatch.setattr("apps.shell.chat_api.get_agent_runtime_service", lambda: service)
    agent = service.create_agent(
        {
            "name": "Worker",
            "nickname": "Worker",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://example.test/v1",
                "model": "test",
                "api_key": "test-only",
            },
        }
    )
    executor = NativeAgentExecutor(chat_session=session, runtime_service_getter=lambda: service)
    runner = TaskRunner(state, executor=executor)
    api = ChatAPI(
        SimpleNamespace(
            state=state,
            chat_session=session,
            store=store,
            agent_runtime_service=service,
            task_runner=runner,
        )
    )
    api.create_group_session(name="Verified group", participant_ids=[agent["agent_id"]])
    sent = api.send_message(("@主模型 " if mode == "model" else "") + goal)
    running = (
        asyncio.create_task(runner._execute_with_state(sent["task_id"]))
        if mode == "model"
        else None
    )

    def poll_summary_started():
        if mode == "direct":
            api.get_messages()
            runner._dispatch_pending()
        return summary_started.is_set()

    try:
        await _wait(poll_summary_started)
        parent = service.get_run(service.get_task_run_link(sent["task_id"])["run_id"])
        assert parent["status"] == "running"
        assert state.get_task(sent["task_id"]).status == TaskStatus.RUNNING
        contract = runtime_goal_contract(
            run_id=parent["run_id"],
            original_goal=goal,
            runtime_execution_envelope=None,
            runtime_execution_metadata=None,
            messages=[],
            timeline=parent["timeline"],
        )
        assert not runtime_goal_assessment(contract, parent["timeline"]).completed
        if decision == "cancel":
            if running:
                running.cancel()
            else:
                service.cancel_run(parent["run_id"])
                api.get_messages()
        if decision.startswith("restart_"):
            summary = next(task for task in state.list_tasks() if task.response_context is not None)
            if decision != "restart_pending":
                release_summary.set()
                await _wait(lambda: state.get_task(summary.task_id).status == TaskStatus.COMPLETED)
            api._runtime.state = AppState()
            if decision == "restart_unlinked":
                real_link = service.get_task_run_link

                def missing_summary_link(task_id):
                    if task_id == summary.task_id:
                        raise KeyError(task_id)
                    return real_link(task_id)

                monkeypatch.setattr(service, "get_task_run_link", missing_summary_link)
            api.get_messages()
        release_summary.set()
        if running:
            await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 5)
        else:

            def poll_direct_done():
                api.get_messages()
                if decision.startswith("restart_"):
                    return service.get_run(parent["run_id"])["status"] in {
                        "completed",
                        "failed",
                        "cancelled",
                    }
                return state.get_task(sent["task_id"]).status in {
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                }

            await _wait(poll_direct_done)
        final = service.get_run(parent["run_id"])
        assert (
            final["status"]
            == {
                "success": "completed",
                "fail": "failed",
                "cancel": "cancelled",
                "restart_complete": "completed",
                "restart_pending": "failed",
                "restart_unlinked": "failed",
            }[decision]
        )
        assert runtime_goal_assessment(contract, final["timeline"]).completed is (
            decision in {"success", "restart_complete"}
        )
        summaries = [task for task in state.list_tasks() if task.response_context is not None]
        assert len(summaries) == 1
        if decision.startswith("restart_"):
            await _wait(
                lambda: (
                    summaries[0].status
                    in {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
                )
            )
        assert (
            summaries[0].status
            == {
                "success": TaskStatus.COMPLETED,
                "fail": TaskStatus.FAILED,
                "cancel": TaskStatus.CANCELLED,
                "restart_complete": TaskStatus.COMPLETED,
                "restart_pending": TaskStatus.FAILED,
                "restart_unlinked": TaskStatus.COMPLETED,
            }[decision]
        )
        assert summaries[0].task_id != sent["task_id"]
        if decision == "restart_pending":
            summary_run = service.get_run(service.get_task_run_link(summaries[0].task_id)["run_id"])
            assert summary_run["status"] == "cancelled"
        if decision.startswith("restart_"):
            projected = next(
                message
                for message in api.get_messages()["messages"]
                if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
            )
            assert projected["metadata"]["run_status"] == final["status"]
    finally:
        release_summary.set()
        if running and not running.done():
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
        remaining = list(runner._in_progress.values())
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)
        service.close()
        store.close()


@pytest.mark.parametrize("failure", ["missing_run", "missing_link", "foreign_link"])
def test_orphan_direct_group_binding_isolated_from_other_chat_messages(
    tmp_path, monkeypatch, failure
):
    from types import SimpleNamespace

    from apps.core.chat_session import MessageStatus
    from apps.shell.chat_api import ChatAPI

    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession()
    session.attach_store(store, load_existing=False)
    state = AppState()
    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr("apps.shell.chat_api.get_agent_runtime_service", lambda: service)
    api = ChatAPI(
        SimpleNamespace(
            state=state, chat_session=session, store=store, agent_runtime_service=service
        )
    )
    task = state.create_task(
        task_type=TaskType.GENERAL,
        description="请安排 Worker Respond with exactly 'DONE'",
        chat_session_id=session.session_id,
    )
    state.update_task_status(task.task_id, TaskStatus.RUNNING)
    parent = service.start_main_chat_run(
        task_id=task.task_id,
        session_id=session.session_id,
        user_goal=task.description,
        metadata={"native_group_owner": "chat_api_direct"},
    )
    session.upsert_assistant_message(
        task_id=task.task_id,
        content="正在执行",
        status=MessageStatus.PROCESSING,
        metadata={"run_id": parent["run_id"], "native_group_parent": True},
    )
    other = state.create_task(
        task_type=TaskType.GENERAL,
        description="An unrelated response.",
        chat_session_id=session.session_id,
    )
    state.update_task_status(other.task_id, TaskStatus.COMPLETED, result="Unrelated result.")
    session.upsert_assistant_message(
        task_id=other.task_id, content="Unrelated result.", status=MessageStatus.PROCESSING
    )
    if failure == "missing_run":
        session.update_assistant_message(
            session.get_assistant_message_for_task(task.task_id).message_id,
            "正在执行",
            metadata={"run_id": "main_chat_run_deleted"},
        )
    else:
        real_link = service.get_task_run_link

        def invalid_link(task_id):
            if task_id != task.task_id:
                return real_link(task_id)
            if failure == "missing_link":
                raise KeyError(task_id)
            return {**real_link(task_id), "session_id": "foreign-session"}

        monkeypatch.setattr(service, "get_task_run_link", invalid_link)
    try:
        messages = api.get_messages()["messages"]
        broken = next(message for message in messages if message["task_id"] == task.task_id)
        healthy = next(message for message in messages if message["task_id"] == other.task_id)
        assert broken["status"] == "failed"
        assert broken["metadata"]["run_status"] == "failed"
        assert state.get_task(task.task_id).status == TaskStatus.FAILED
        assert healthy["status"] == "completed" and healthy["content"] == "Unrelated result."
        assert (
            service.get_run(parent["run_id"])["status"] == "running"
        )  # No authority to cancel an unbound run.
        assert api.get_messages()["ok"]
    finally:
        service.close()
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject", "cancel", "restart_approve"])
async def test_direct_group_parent_waits_for_actual_child_goal_and_approval(
    tmp_path, monkeypatch, decision
):
    from types import SimpleNamespace

    from apps.shell.chat_api import ChatAPI

    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession()
    session.attach_store(store, load_existing=False)
    state = AppState()
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    child_goal = "请在终端里运行 `printf native-approved`"
    goal = "请安排 Worker " + child_goal
    calls = []

    def fake_chat(_base, _model, _key, messages, *, tools=None):
        names = {(tool.get("function") or {}).get("name") for tool in tools or []}
        if MODEL_INTENT_PLANNING_TOOL_NAME in names:
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "intent",
                        "type": "function",
                        "function": {
                            "name": MODEL_INTENT_PLANNING_TOOL_NAME,
                            "arguments": json.dumps(
                                {
                                    "intent_kind": "code_task",
                                    "planning_goal": child_goal,
                                    "action_evidence": "运行",
                                    "subgoals": [
                                        {
                                            "capability_id": "terminal.execution",
                                            "action_id": "run_command",
                                            "planning_goal": child_goal,
                                            "action_evidence": "运行",
                                            "input_slots": [
                                                {
                                                    "slot": "command",
                                                    "value": "printf native-approved",
                                                    "evidence_quote": "printf native-approved",
                                                }
                                            ],
                                        }
                                    ],
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        calls.append(messages)
        context = "\n".join(str(message.get("content") or "") for message in messages)
        assert "# Agent\nName: Worker" in context
        return {"role": "assistant", "content": "终端命令实际成功执行。"}

    monkeypatch.setattr("apps.core.chat_store.get_chat_store", lambda: store)
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
    monkeypatch.setattr("apps.shell.chat_api.get_agent_runtime_service", lambda: service)
    agent = service.create_agent(
        {
            "name": "Worker",
            "nickname": "Worker",
            "model_mode": "custom_api",
            "workspace_policy": {
                "default_workdir": str(workdir),
                "readable_scopes": ["."],
                "writable_scopes": ["."],
            },
            "tool_policy": {
                "allowed_tools": ["terminal.run"],
                "approval_required": {"terminal.run": True},
            },
            "model_config": {
                "base_url": "https://example.test/v1",
                "model": "test",
                "api_key": "test-only",
            },
        }
    )
    api = ChatAPI(
        SimpleNamespace(
            state=state, chat_session=session, store=store, agent_runtime_service=service
        )
    )
    api.create_group_session(name="Native direct", participant_ids=[agent["agent_id"]])
    try:
        sent = api.send_message(goal)
        assert sent["ok"] and sent["status"] == "processing", sent
        parent = service.get_run(service.get_task_run_link(sent["task_id"])["run_id"])
        child = await _wait(
            lambda: next(
                (
                    run
                    for run in service.list_runs(limit=20)["runs"]
                    if run["kind"] == "agent_run"
                    and run["runnable_id"] == agent["agent_id"]
                    and run["status"] == "approval_required"
                ),
                None,
            )
        )
        api.get_messages()
        assert service.get_run(parent["run_id"])["status"] == "running"
        assert state.get_task(sent["task_id"]).status == TaskStatus.RUNNING
        assert calls == []  # Only the saved Agent planner; no main-model dispatch turn.
        if decision == "cancel":
            service.cancel_run(parent["run_id"])
        elif decision == "reject":
            service.reject_run_approval(
                child["run_id"], expected_approval_id=child["pending_approval"]["approval_id"]
            )
        else:
            service.approve_run_approval(
                child["run_id"], expected_approval_id=child["pending_approval"]["approval_id"]
            )
        if decision == "restart_approve":
            api._runtime.state = AppState()
            decision = "approve"
            restarted = True
        else:
            restarted = False
        api.get_messages()
        final = service.get_run(parent["run_id"])
        assert (
            final["status"]
            == {"approve": "completed", "reject": "failed", "cancel": "cancelled"}[decision]
        ), final.get("result")
        if restarted:
            assert api._runtime.state.get_task(sent["task_id"]) is None
            projected = next(
                message
                for message in api.get_messages()["messages"]
                if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
            )
            assert projected["metadata"]["run_status"] == "completed"
        else:
            assert (
                state.get_task(sent["task_id"]).status
                == {
                    "approve": TaskStatus.COMPLETED,
                    "reject": TaskStatus.FAILED,
                    "cancel": TaskStatus.CANCELLED,
                }[decision]
            )
        if decision == "cancel":
            assert service.get_run(child["run_id"])["status"] == "cancelled"
        if decision == "approve":
            contract = runtime_goal_contract(
                run_id=parent["run_id"],
                original_goal=goal,
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=final["timeline"],
            )
            assert runtime_goal_assessment(contract, final["timeline"]).completed
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("case", ["unknown", "excluded", "disabled", "repeated"])
def test_explicit_assignment_coverage_must_include_every_requested_clause(case):
    research = {**_target(), "id": "research", "name": "Research", "nickname": "Research"}
    missing = {**_target(), "id": "other", "name": "Missing", "nickname": "Missing"}
    targets = [research]
    selected = {"research", "other"}
    goal = "请让 Research: Respond with exactly 'DONE'；请让 Missing: Respond with exactly 'ALSO'"
    if case == "disabled":
        targets.append({**missing, "enabled": False})
    elif case == "excluded":
        targets.append(missing)
        selected = {"research"}
    elif case == "repeated":
        goal = (
            "请让 Research: Respond with exactly 'DONE'；请让 Research: Respond with exactly 'ALSO'"
        )
    run = {"run_id": "main_chat_run_coverage", "user_goal": goal}
    assert (
        compile_plan(
            run=run,
            targets=targets,
            parent_runtime=research,
            selected_ids=selected,
            group_scope=True,
        )
        is None
    )
    assert run["user_goal"] == goal


def test_explicit_assignment_coverage_preserves_multiple_targets_in_user_order():
    research = {**_target(), "id": "research", "name": "Research", "nickname": "Research"}
    coding = {**_target(), "id": "coding", "name": "Coding", "nickname": "Coding"}
    goal = "请让 Research: Respond with exactly 'DONE'；请让 Coding: Respond with exactly 'ALSO'"
    plan = compile_plan(
        run={"run_id": "main_chat_run_multi", "user_goal": goal},
        targets=[coding, research],
        parent_runtime=research,
        selected_ids={"research", "coding"},
        group_scope=True,
    )
    assert plan is not None
    assert [(target["runnable_id"], target["goal"]) for target in plan["binding"]["targets"]] == [
        ("research", "Respond with exactly 'DONE'"),
        ("coding", "Respond with exactly 'ALSO'"),
    ]
    assert plan["goal_contract"]["original_goal"] == goal


@pytest.mark.parametrize("case", ["short", "extra", "not_list", "not_mapping"])
def test_completion_rejects_malformed_bound_receipt_cardinality(case):
    parent, timeline = _same_goal_ledger()
    if case == "short":
        timeline[1]["children"] = []
    elif case == "extra":
        timeline[1]["children"] *= 2
    elif case == "not_list":
        timeline[1]["children"] = {}
    else:
        timeline[1]["children"] = [None]
    assert not runtime_goal_assessment(parent, timeline).completed


@pytest.mark.parametrize("unknown", [True, False])
def test_direct_collaboration_cannot_drop_a_declared_participant(unknown):
    research = {**_target(), "id": "research", "name": "Research", "nickname": "Research"}
    coding = {**_target(), "id": "coding", "name": "Coding", "nickname": "Coding"}
    goal = "请 Research 和 " + ("Missing" if unknown else "Coding") + " 一起做一个小项目"
    plan = compile_plan(
        run={"run_id": "main_chat_run_collaboration", "user_goal": goal},
        targets=[research, coding],
        parent_runtime=research,
        selected_ids={"research", "coding"},
        group_scope=True,
        direct_group=True,
    )
    if unknown:
        assert plan is None
    else:
        assert len(plan["binding"]["targets"]) == 2
        assert all(target["goal"] == goal for target in plan["binding"]["targets"])


@pytest.mark.parametrize(
    "mutation", ["duplicate", "foreign_binding", "foreign_source", "short", "extra", "shape"]
)
def test_repeated_dispatch_requires_one_exact_owned_child_generation(mutation):
    from types import SimpleNamespace

    from apps.shell.agent.runtime.main_chat_delegation import MainChatDelegationCoordinator

    contract, timeline = _same_goal_ledger()
    parent = {
        "run_id": contract.run_id,
        "user_goal": contract.original_goal,
        "status": "running",
        "timeline": timeline,
    }
    child = {
        **timeline[1]["children"][0],
        "kind": "agent_run",
        "user_goal": contract.original_goal,
        "status": "running",
    }
    if mutation == "duplicate":
        timeline.insert(2, deepcopy(timeline[1]))
    elif mutation == "foreign_binding":
        timeline[1]["binding_id"] = "foreign-generation"
    elif mutation == "foreign_source":
        timeline[1]["source"] = "model"
    elif mutation == "short":
        timeline[1]["children"] = []
    elif mutation == "extra":
        timeline[1]["children"] *= 2
    else:
        timeline[1]["children"] = [None]
    service = SimpleNamespace(
        main_chat_runs=SimpleNamespace(_transaction_scope=None),
        get_run=lambda run_id: parent if run_id == parent["run_id"] else child,
    )
    coordinator = MainChatDelegationCoordinator(service)
    with pytest.raises(ValueError, match="delegation_child"):
        coordinator.start(
            parent["run_id"],
            [{"kind": "agent", "runnable_id": "worker", "goal": contract.original_goal}],
            task_id="owned-task",
            group=False,
            upstream="",
        )
