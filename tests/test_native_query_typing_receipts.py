"""Queries complete only from frozen goals and real before/after search focus."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_runtime import runtime_goal_contract
from apps.shell.agent.runtime.query_typing_receipts import trusted_query_typing_receipt
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner


def _case():
    goal = "Chrome 点击搜索框输入 yachiyo"
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    payload = runtime_execution_envelope_payload(
        decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
    )
    requests = {
        _r["step_id"]: _r
        for _r in runtime_execution_requests_from_envelope_payload(
            payload, allowed_tools=DAILY_DESKTOP_TOOL_NAMES
        )
    }
    contract = runtime_goal_contract(
        run_id="query-run",
        original_goal=goal,
        runtime_execution_envelope=payload,
        runtime_execution_metadata=None,
        messages=[],
        timeline=[],
    )
    provenance = {
        RUNTIME_EXECUTION_PROVENANCE_KEY: {
            "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
            "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
        },
        "local_desktop_provider": {
            "provider_kind": "local_desktop",
            "provider_id": "local-native-desktop",
        },
    }
    scope = {
        "run_id": "query-run",
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
    }
    click_request = requests["focus-app-search-field"]
    click = {
        **click_request,
        **scope,
        "event": "agent.tool.call",
        "detail": click_request["tool"],
        "tool_call_id": "query-click",
        "input_preview": click_request["input"],
        "result": {"ok": True, "action": click_request["tool"], **provenance, "data": {}},
    }
    field = {
        "role": "AXTextField",
        "name": "搜索",
        "identifier": "search-field",
        "value": "",
        "focused": True,
        "editable": True,
    }
    observation = {
        "ok": True,
        "action": "desktop.ui_elements",
        **provenance,
        "data": {
            "app_name": "Google Chrome",
            "pid": 100,
            "window_id": 200,
            "focused_element": field,
            "elements": [field],
        },
    }
    before = {
        **scope,
        "event": "agent.tool.call",
        "detail": "desktop.ui_elements",
        "tool": "desktop.ui_elements",
        "decision_id": click["decision_id"],
        "tool_plan_id": click["tool_plan_id"],
        "plan_id": click["plan_id"],
        "request_id": (
            f"{click['request_id']}:verify:focus-app-search-field:"
            "runtime-verify:desktop.ui_elements"
        ),
        "step_id": "focus-app-search-field:runtime-verify",
        "tool_call_id": "query-before",
        "source_tool_call_id": click["tool_call_id"],
        "source_step_id": click["step_id"],
        "input_preview": {"app_name": "Google Chrome"},
        "result": deepcopy(observation),
    }
    source_request = requests["type-app-search-query"]
    source = {
        **source_request,
        **scope,
        "event": "agent.tool.call",
        "detail": source_request["tool"],
        "input_preview": source_request["input"],
        "tool_call_id": "query-type",
        "result": {
            "ok": True,
            "action": source_request["tool"],
            **provenance,
            "data": {"character_count": 7},
        },
    }
    verifier = {
        **requests["verify-desktop-result"],
        **scope,
        "tool_call_id": "query-after",
        "source_step_id": source["step_id"],
        "source_tool_call_id": source["tool_call_id"],
    }
    after = deepcopy(observation)
    after["data"]["focused_element"]["value"] = "yachiyo"
    after["data"]["elements"][0]["value"] = "yachiyo"
    timeline = [
        {
            "event": "agent.goal.contract",
            "run_id": "query-run",
            "goal_contract": contract.to_payload(),
        },
        click,
        before,
        source,
    ]
    return source, verifier, after, timeline


def _receipt(source, verifier, after, timeline):
    return trusted_query_typing_receipt(
        "desktop.safe_type_text", source, verifier, after, timeline, run_id="query-run"
    )


def test_actual_frozen_query_and_search_focus_yield_no_send_context():
    source, verifier, after, timeline = _case()
    receipt = _receipt(source, verifier, after, timeline)
    assert receipt["verification_predicate_kind"] == "exact_typed_content_present"
    assert receipt["content_length"] == 7
    assert not any(
        k in receipt
        for k in ("target_window", "target_ui_identity", "composer_required", "target_recipient")
    )


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_goal",
        "changed_goal",
        "source_plan",
        "foreign_decision",
        "foreign_tool_plan",
        "extra_source_input",
        "duplicate_source",
        "duplicate_source_interleaved",
        "extra_click_input",
        "foreign_pre_app",
        "foreign_pre_limit",
        "source_request",
        "source_run",
        "source_bytes",
        "source_actor",
        "source_tool_action",
        "wrong_verifier",
        "wrong_verifier_call",
        "wrong_provider",
        "no_pre",
        "wrong_pre_call",
        "wrong_pre_plan",
        "pre_not_focused",
        "post_not_focused",
        "post_window",
        "post_app",
        "post_identity",
        "post_bytes",
        "duplicate_field",
        "interleaved",
        "pre_ack_only",
        "post_ack_only",
        "not_editable",
        "stale_goal_target",
    ],
)
def test_query_readback_rejects_acknowledgements_unbound_scope_and_target_drift(mutation):
    source, verifier, after, timeline = _case()
    pre = timeline[2]
    if mutation == "missing_goal":
        timeline.pop(0)
    elif mutation == "changed_goal":
        timeline[0]["goal_contract"]["original_goal"] = "只回复 yachiyo"
    elif mutation in {"foreign_decision", "foreign_tool_plan"}:
        key = "decision_id" if mutation == "foreign_decision" else "tool_plan_id"
        source[key] = verifier[key] = "foreign-canonical-identity"
    elif mutation in {"duplicate_source", "duplicate_source_interleaved"}:
        if mutation == "duplicate_source_interleaved":
            timeline.append({"event": "agent.tool.call", "detail": "app.focus"})
        timeline.append(deepcopy(source))
    elif mutation == "extra_source_input":
        source["input_preview"]["submit"] = True
    elif mutation == "extra_click_input":
        timeline[1]["input_preview"]["action"] = "send"
    elif mutation == "foreign_pre_app":
        pre["input_preview"] = {"app_name": "Slack"}
    elif mutation == "foreign_pre_limit":
        pre["input_preview"] = {"limit": 999}
    elif mutation == "source_plan":
        source["plan_id"] = "other"
    elif mutation == "source_request":
        source["request_id"] = "other"
    elif mutation == "source_run":
        source["run_id"] = "other"
    elif mutation == "source_bytes":
        source["input_preview"] = {"text": "foreign"}
    elif mutation == "source_actor":
        source["actor"] = "model"
    elif mutation == "source_tool_action":
        source["result"]["action"] = "app.status"
    elif mutation == "wrong_verifier":
        verifier["request_id"] = "other"
    elif mutation == "wrong_verifier_call":
        verifier["source_tool_call_id"] = "other"
    elif mutation == "wrong_provider":
        after.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    elif mutation == "no_pre":
        timeline.remove(pre)
    elif mutation == "wrong_pre_call":
        pre["source_tool_call_id"] = "other"
    elif mutation == "wrong_pre_plan":
        pre["plan_id"] = "other"
    elif mutation == "pre_not_focused":
        pre["result"]["data"]["focused_element"]["focused"] = False
    elif mutation == "post_not_focused":
        after["data"]["focused_element"]["focused"] = False
    elif mutation == "post_window":
        after["data"]["window_id"] = 999
    elif mutation == "post_app":
        after["data"]["app_name"] = "Slack"
    elif mutation == "post_identity":
        after["data"]["focused_element"]["identifier"] = "other"
    elif mutation == "post_bytes":
        after["data"]["focused_element"]["value"] = "Yachiyo"
    elif mutation == "duplicate_field":
        after["data"]["elements"].append(dict(after["data"]["elements"][0]))
    elif mutation == "interleaved":
        timeline.insert(3, {"event": "agent.tool.call", "detail": "app.focus"})
    elif mutation == "pre_ack_only":
        pre["result"]["data"] = {"postcondition_verified": True}
    elif mutation == "post_ack_only":
        after["data"] = {"postcondition_verified": True, "value": "yachiyo"}
    elif mutation == "not_editable":
        after["data"]["focused_element"].update(role="AXStaticText", editable=False)
    elif mutation == "stale_goal_target":
        source["action_target"] = {"app_name": "Slack", "target": "Message"}
        after["data"]["app_name"] = "Slack"
    assert _receipt(source, verifier, after, timeline) == {}
