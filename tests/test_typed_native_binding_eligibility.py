"""Literal native target bindings do not reject semantic model plans."""

import pytest

from apps.shell.agent.runtime.model_intent_planning import (
    ModelIntentProposal,
    direct_tool_selection_from_model_intent_proposal,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner


@pytest.mark.parametrize("goal", [
    "微信给张三说你好", "微信给张三说剪贴板内容", "总结当前页面然后发给张三",
])
def test_model_semantic_communication_plan_keeps_original_verification_path(goal):
    decision = RuntimePlanner().decision_from_model_intent_hint(
        goal, goal, "communication", allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    assert not any(s.step_id.startswith(("inspect-typed-draft-", "verify-typed-draft-"))
                   for s in decision.plan.tool_plan.steps)
    assert decision.plan.task_core.goal_contract.original_goal == goal


@pytest.mark.parametrize("goal", [
    "微信给张三说剪贴板内容", "微信给张三说当前页面内容",
])
def test_context_body_is_not_granted_literal_native_typing_authority(goal):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert not any(s.step_id.startswith("inspect-typed-draft-")
                   for s in decision.plan.tool_plan.steps)


def test_actual_model_semantic_send_preserves_existing_unverified_path(
    tmp_path, monkeypatch,
):
    from tests.test_agent_runtime import FakeDefaultProfileService
    from tests.test_native_typed_draft_target import _fixture

    monkeypatch.setattr("apps.shell.agent.runtime.tooling.MAX_AGENT_TOOL_ITERATIONS", 10)
    _, service, store, state = _fixture(tmp_path, monkeypatch)
    goal = "在WeChat输入你好并发送"
    state["selected"] = True
    metadata = {"allow_user_foreground_takeover": True}
    selection = direct_tool_selection_from_model_intent_proposal(
        ModelIntentProposal(
            intent_kind="desktop_operation", planning_goal=goal, action_evidence=goal,
        ),
        goal, DAILY_DESKTOP_TOOL_NAMES, metadata=metadata,
    )
    assert not any(str(r.get("step_id") or "").startswith("inspect-typed-draft-")
                   for r in selection.requests)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service", lambda: FakeDefaultProfileService(),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *_args, **_kwargs: {"content": "已完成请求"},
    )
    monkeypatch.setattr(
        service.main_chat_model_loop, "_resolve_initial_model_plan", lambda **_kwargs: selection,
    )
    try:
        run = service.start_main_chat_run(
            task_id="model-communication", session_id="model-session", user_goal=goal,
            metadata=metadata,
        )
        from apps.shell.agent.runtime.errors import AgentRuntimeError

        # The identical pre-wrapper baseline stops at independent UI verify.
        # Preserve that limitation without introducing opaque native binding
        # failures or granting a model's completion claim send authority.
        with pytest.raises(AgentRuntimeError, match="工具循环超过上限"):
            service.execute_main_chat_model_loop(
                run["run_id"], [{"role": "user", "content": goal}],
                runtime_execution_metadata=metadata,
                tool_policy={"allowed_tools": DAILY_DESKTOP_TOOL_NAMES},
            )
        failed = service.get_run(run["run_id"])
        assert failed["status"] == "failed"
        assert "typed_draft_target_or_recipient_unverified" not in failed["result"]
        assert state["sent"] == 0
    finally:
        service.close()
        store.close()
