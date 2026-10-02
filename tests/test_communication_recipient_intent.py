"""Recipient operands cannot manufacture artifact intent or lose send gates."""

import pytest

from apps.shell.agent.runtime.model_intent_planning import (
    planner_selection_needs_model_assistance,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
    planner_first_direct_tool_selection,
)


def _selection(goal):
    return planner_first_direct_tool_selection(
        goal,
        DAILY_DESKTOP_TOOL_NAMES,
        metadata={"runtime_planner_request_trace": True},
    )


@pytest.mark.parametrize("recipient", ["文件传输助手", "文件项目组"])
def test_unchanged_clipboard_send_keeps_native_recipient_and_send_approval(recipient):
    goal = f"把剪贴板内容发给微信{recipient}"
    selection = _selection(goal)
    assert selection.decision.selected_intent.kind == "communication"
    assert not planner_selection_needs_model_assistance(selection, goal)
    requests = selection.requests
    assert [request["tool"] for request in requests] == [
        "app.focus", "desktop.safe_shortcut", "desktop.safe_type_text",
        "desktop.search_submit", "desktop.safe_shortcut", "desktop.submit_foreground",
        "desktop.ui_elements",
    ]
    assert requests[2]["input"] == {"text": recipient}
    assert requests[4]["input"] == {"action": "paste"}
    assert requests[5]["input"] == {"action": "send"}
    send_step = next(step for step in selection.decision.plan.tool_plan.steps
                     if step.step_id == "send-communication-message")
    assert send_step.approval_required is True
    assert send_step.depends_on == ["paste-communication-message"]
    assert selection.decision.selected_intent.user_goal == goal


@pytest.mark.parametrize("goal", [
    "把剪贴板内容整理成文件再发给微信文件传输助手",
    "把剪贴板内容总结成周报再发给微信文件传输助手",
    "把剪贴板内容发给微信文件传输助手，然后删除源文件",
])
def test_recipient_disambiguation_does_not_authorize_unplanned_effects(goal):
    selection = _selection(goal)
    assert planner_selection_needs_model_assistance(selection, goal)
    assert selection.decision.selected_intent.user_goal == goal
    assert not any(request.get("tool") == "file.organize" for request in selection.requests)
