"""A named media query keeps its complete plan and its user's action scope."""

import pytest

from apps.shell.agent.runtime.model_intent_planning import planner_selection_needs_model_assistance
from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection


def selection(goal):
    return planner_first_direct_tool_selection(goal, list(TOOL_DISPATCH_REGISTRY))


@pytest.mark.parametrize("goal", [
    "打开 Spotify 播放 Taylor Swift",
    "用 Spotify 播放 Taylor Swift",
])
def test_named_query_runs_the_complete_search_and_play_plan_without_a_model(goal):
    selected = selection(goal)
    assert selected.decision.selected_intent.kind == "media_playback"
    assert not planner_selection_needs_model_assistance(selected, goal)
    assert selected.decision.plan.task_core.goal_contract.original_goal == goal
    assert [(request["tool"], request["input"]) for request in selected.requests] == [
        ("desktop.list_apps", {"query": "Spotify", "limit": 20}),
        ("app.open_and_safe_shortcut", {
            "app_name": "Spotify", "action": "find",
            "selection_source": "desktop.list_apps", "query": "Spotify",
        }),
        ("desktop.safe_type_text", {"text": "Taylor Swift"}),
        ("desktop.search_submit", {}),
        ("media.music_app_open_and_play", {
            "app_name": "Spotify", "selection_source": "desktop.list_apps", "query": "Spotify",
        }),
        ("desktop.ui_elements", {"role_filter": "", "limit": 80}),
    ]


@pytest.mark.parametrize("goal", [
    "不要打开 Spotify 播放 Taylor Swift",
    "不要播放 Taylor Swift",
    "请说‘打开 Spotify 播放 Taylor Swift’",
    "解释如何打开 Spotify 播放 Taylor Swift",
    "如果有必要，打开 Spotify 播放 Taylor Swift",
])
def test_nonexecuting_media_text_does_not_reenter_through_the_unsanitized_goal(goal):
    selected = selection(goal)
    assert not any(request["tool"].startswith("media.") for request in selected.requests)


@pytest.mark.parametrize("goal", [
    "打开 Spotify 播放 Taylor Swift，然后截图",
    "打开 Spotify 播放 Taylor Swift，然后删除所有文件",
    "打开 Spotify 然后输入‘播放 Taylor Swift’",
])
def test_a_media_phrase_does_not_complete_other_requested_actions(goal):
    selected = selection(goal)
    assert planner_selection_needs_model_assistance(selected, goal)


def test_an_explicit_search_field_click_keeps_its_ui_action():
    selected = selection("打开 Spotify 点击搜索框")
    assert selected.decision.selected_intent.kind == "desktop_operation"
    assert any(request["tool"] == "app.focus_and_click_ui_element"
               for request in selected.requests)


def test_unqualified_named_app_play_keeps_its_existing_media_tool():
    goal = "打开 Spotify 播放音乐"
    selected = selection(goal)
    assert not planner_selection_needs_model_assistance(selected, goal)
    assert [(request["tool"], request["input"]) for request in selected.requests] == [
        ("media.music_app_open_and_play", {"app_name": "Spotify"}),
    ]
