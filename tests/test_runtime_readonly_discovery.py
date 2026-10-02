"""Read-only desktop questions must never grant app or permission mutation."""

import pytest

from apps.shell.yachiyo_agent import RuntimePlanner

ALL_DESKTOP_TOOLS = [
    "desktop.permissions",
    "desktop.permissions.verify",
    "desktop.running_apps",
    "desktop.active_window",
    "desktop.ui_elements",
    "desktop.capture_screen",
    "app.open",
    "app.status",
    "app.quit",
    "desktop.click_ui_element",
    "terminal.run",
]


@pytest.mark.parametrize(
    ("prompt", "expected_tool"),
    [
        ("为什么不能打开应用", "desktop.permissions"),
        ("为什么无法读取屏幕？", "desktop.permissions"),
        ("为什么我无法控制屏幕", "desktop.permissions"),
        ("现在开了哪些应用", "desktop.running_apps"),
        ("列一下打开的应用", "desktop.running_apps"),
        ("请列出当前运行的应用。", "desktop.running_apps"),
        ("帮我查看现在开着的应用", "desktop.running_apps"),
    ],
)
def test_descriptive_action_words_only_request_passive_observation(
    prompt: str, expected_tool: str
) -> None:
    decision = RuntimePlanner().decision(prompt, allowed_tools=ALL_DESKTOP_TOOLS)

    steps = decision.plan.tool_plan.steps
    assert [(step.tool_name, step.input_preview) for step in steps] == [
        (expected_tool, {})
    ]
    assert not steps[0].approval_required
    assert decision.plan.task_core.goal_contract.original_goal == prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "他说：列一下打开的应用",
        "不要列一下打开的应用",
        "如果有必要，列一下打开的应用",
        "解释为什么不能打开应用",
        "为什么不能打开应用，然后修复权限",
        "不要打开微信",
    ],
)
def test_question_words_do_not_authorize_embedded_or_conditional_actions(
    prompt: str,
) -> None:
    decision = RuntimePlanner().decision(prompt, allowed_tools=ALL_DESKTOP_TOOLS)

    assert all(not step.tool_name for step in decision.plan.tool_plan.steps)


def test_running_app_question_cannot_fall_back_to_an_app_mutation() -> None:
    decision = RuntimePlanner().decision(
        "列一下打开的应用", allowed_tools=["app.open", "app.quit"]
    )

    assert all(not step.tool_name for step in decision.plan.tool_plan.steps)
