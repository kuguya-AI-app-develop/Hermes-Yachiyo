"""Daily launcher metadata cannot invent an empty rival to an explicit query."""

import pytest

from apps.shell.agent.runtime.model_intent_planning import planner_selection_needs_model_assistance
from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection


@pytest.mark.parametrize(
    "goal",
    ["我想听超时空辉夜姬吧", "我想听 Taylor Swift", "播放超时空辉夜姬", "放点周杰伦", "播点轻音乐"],
)
def test_daily_query_keeps_its_existing_complete_media_plan(goal):
    selection = planner_first_direct_tool_selection(
        goal,
        list(TOOL_DISPATCH_REGISTRY),
        metadata={"daily_desktop_intent": True},
    )
    assert selection.decision.selected_intent.kind == "media_playback"
    assert not planner_selection_needs_model_assistance(selection, goal)
    assert selection.decision.plan.task_core.goal_contract.original_goal == goal
    assert not any(
        candidate.kind == "desktop_operation" and not candidate.inputs.get("operation_hint")
        for candidate in selection.decision.candidate_intents
    )
    assert any(request["tool"].startswith("media.") for request in selection.requests)


@pytest.mark.parametrize(
    "goal",
    [
        "我想听超时空辉夜姬吧，然后截图",
        "我想听超时空辉夜姬吧，然后点击确认",
        "我想听超时空辉夜姬吧，然后删除文件",
        "我想听超时空辉夜姬吧，然后输入 hello",
        "放点周杰伦，然后点击确认",
        "播点轻音乐，然后删除文件",
        "放点周杰伦 and click Confirm",
        "播点轻音乐 and type hello",
    ],
)
def test_an_additional_action_still_needs_its_own_full_plan(goal):
    selection = planner_first_direct_tool_selection(
        goal,
        list(TOOL_DISPATCH_REGISTRY),
        metadata={"daily_desktop_intent": True},
    )
    assert planner_selection_needs_model_assistance(selection, goal)


@pytest.mark.parametrize(
    "goal",
    [
        "不要播放超时空辉夜姬",
        "请说“播放超时空辉夜姬”",
        "解释如何播放超时空辉夜姬",
        "如果有必要，播放超时空辉夜姬",
        "不要放点周杰伦",
        "请说“播点轻音乐”",
        "如果有必要，放点周杰伦",
    ],
)
def test_nonexecuting_media_phrases_remain_nonexecuting(goal):
    selection = planner_first_direct_tool_selection(
        goal,
        list(TOOL_DISPATCH_REGISTRY),
        metadata={"daily_desktop_intent": True},
    )
    assert not any(request["tool"].startswith("media.") for request in selection.requests)
