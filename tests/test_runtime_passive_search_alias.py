"""A local search alias observes apps without granting an embedded action."""

import pytest

from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent import RuntimePlanner
from apps.shell.yachiyo_agent.runtime_planner import _pure_desktop_discovery_question

TOOLS = [
    "browser.open_url",
    "desktop.running_apps",
    "desktop.list_apps",
    "app.open",
    "app.quit",
    "desktop.safe_click",
    "terminal.run",
]


@pytest.mark.parametrize(
    ("goal", "tool", "inputs"),
    [
        ("搜一下当前运行的应用", "desktop.running_apps", {}),
        ("请搜一下现在运行的应用。", "desktop.running_apps", {}),
        ("帮我搜一下当前开着的软件", "desktop.running_apps", {}),
        ("搜一下系统设置", "desktop.list_apps", {"query": "系统设置", "limit": 20}),
    ],
)
def test_local_search_alias_retains_exact_passive_tool_and_original_goal(goal, tool, inputs):
    decision = RuntimePlanner().decision(goal, allowed_tools=TOOLS)
    assert decision.selected_intent.kind == "desktop_operation"
    steps = decision.plan.tool_plan.steps
    assert [(step.tool_name, step.input_preview) for step in steps] == [(tool, inputs)]
    assert not steps[0].approval_required
    assert decision.plan.task_core.goal_contract.original_goal == goal


@pytest.mark.parametrize("target", ["当前运行的应用", "系统设置"])
@pytest.mark.parametrize(
    "template",
    [
        "不要搜一下{target}",
        "如果有必要，搜一下{target}",
        "他说：搜一下{target}",
        "请解释‘搜一下{target}’的意思",
        "请回复‘搜一下{target}’",
        '请说"搜一下{target}"',
    ],
)
def test_nonexecuting_local_search_alias_has_no_tool_authority(target, template):
    goal = template.format(target=target)
    assert not _pure_desktop_discovery_question(goal)
    decision = RuntimePlanner().decision(goal, allowed_tools=TOOLS)
    assert all(not step.tool_name for step in decision.plan.tool_plan.steps)


@pytest.mark.parametrize("suffix", ["，然后打开Slack", "并删除文件", "，然后修复权限"])
def test_compound_actions_cannot_use_the_passive_search_exception(suffix):
    assert not _pure_desktop_discovery_question("搜一下当前运行的应用" + suffix)


def test_missing_passive_tool_cannot_fall_back_to_an_app_mutation():
    decision = RuntimePlanner().decision(
        "搜一下当前运行的应用", allowed_tools=["app.open", "app.quit"]
    )
    assert all(not step.tool_name for step in decision.plan.tool_plan.steps)


def test_authorized_literal_typing_keeps_the_alias_as_data():
    decision = RuntimePlanner().decision(
        "在 Slack 的 Message 输入框输入“搜一下当前运行的应用”",
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
    )
    steps = decision.plan.tool_plan.steps
    assert not any(step.tool_name == "desktop.running_apps" for step in steps)
    typed = next(step for step in steps if step.tool_name == "app.open_and_type_into_ui_element")
    assert typed.input_preview["text"] == "搜一下当前运行的应用"
    assert typed.input_preview["app_name"] == "Slack"
