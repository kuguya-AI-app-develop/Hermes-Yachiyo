"""Exact app-search completion needs correlated AX query and result evidence."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent.runtime.model_intent_planning import planner_selection_needs_model_assistance
from apps.shell.agent.runtime.tool_execution import (
    _tool_result_with_trusted_observation_receipt,
    _trusted_postcondition_observation_receipt_for_verifier,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.entrypoint_tool_selection import planner_first_direct_tool_selection
from apps.shell.yachiyo_agent.runtime_execution import runtime_execution_envelope_from_decision


def _case():
    goal = "切到WeChat，搜索张三"
    selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
    envelope = runtime_execution_envelope_from_decision(
        selection.decision,
        allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        full_plan=True,
    ).model_dump()
    contract = runtime_goal_contract(
        run_id="run-search",
        original_goal=goal,
        runtime_execution_envelope=envelope,
        runtime_execution_metadata=None,
        messages=[],
        timeline=[],
    )
    requests = {request["step_id"]: request for request in envelope["requests"]}
    source_request = requests["submit-app-search"]
    verifier = requests["verify-desktop-result"]
    context = {
        "run_id": "run-search",
        "actor": "native_runtime",
        "visibility": "internal",
        "execution_authority": "runtime_tool_executor",
    }
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
    source = {
        **source_request,
        **context,
        "event": "agent.tool.call",
        "detail": "desktop.search_submit",
        "tool_call_id": "search-call",
        "input_preview": source_request["input"],
        "target_window": {"app_name": "WeChat", "pid": 100, "window_id": 200},
        "result": {
            "ok": True,
            "action": "desktop.search_submit",
            **deepcopy(provenance),
            "data": {"key": "return", "modifiers": []},
        },
    }
    verifier.update(
        context,
        tool="desktop.ui_elements",
        source_tool_call_id="search-call",
        source_step_id="submit-app-search",
        source_request_id=source["request_id"],
    )
    observation = {
        "ok": True,
        "action": "desktop.ui_elements",
        **deepcopy(provenance),
        "data": {
            "app_name": "WeChat",
            "pid": 100,
            "window_id": 200,
            "elements": [
                {"role": "AXTextField", "name": "Search", "value": "张三", "depth": 1},
                {"role": "AXTable", "name": "Search Results", "depth": 1},
                {"role": "AXRow", "name": "张三", "depth": 2},
            ],
        },
    }
    return contract, source, verifier, observation


def _receipt(source, verifier, observation):
    return _trusted_postcondition_observation_receipt_for_verifier(
        verifier,
        observation,
        [source],
        tool_timeline_start=0,
        run_id="run-search",
    )


@pytest.mark.parametrize("private_context", [False, True])
def test_exact_query_and_results_readback_completes_declared_search_criterion(private_context):
    contract, source, verifier, observation = _case()
    assert runtime_goal_assessment(contract, [source]).completed is False
    receipt = _receipt(source, verifier, observation)
    assert receipt["verification_predicate_kind"] == "exact_app_search_result_present"
    event = {
        **verifier,
        "event": "agent.tool.call",
        "detail": "desktop.ui_elements",
        "source": "runtime_native_postcondition_receipt",
        "tool_call_id": "verify-call",
        "input_preview": verifier["input"],
        "result": _tool_result_with_trusted_observation_receipt(observation, receipt),
    }
    if private_context:
        event["result"]["verification_context_trusted"] = True
    assert runtime_goal_assessment(contract, [source, event]).completed is True


@pytest.mark.parametrize(
    "change",
    [
        "wrong_app",
        "wrong_window",
        "wrong_query",
        "typed_only",
        "old_results",
        "unrelated_result_tree",
        "duplicate_query_fields",
        "wrong_provider",
        "foreign_run",
        "foreign_plan",
        "foreign_call",
        "untrusted_observation",
        "other_submit_tool",
        "non_search_target",
        "modifier_dispatch",
        "wrong_declared_step",
    ],
)
def test_search_receipt_rejects_missing_or_foreign_observation(change):
    contract, source, verifier, observation = _case()
    if change == "wrong_app":
        observation["data"]["app_name"] = "Slack"
    elif change == "wrong_window":
        observation["data"]["window_id"] = 201
    elif change == "wrong_query":
        observation["data"]["elements"][0]["value"] = "李四"
    elif change == "typed_only":
        observation["data"]["elements"] = observation["data"]["elements"][:1]
    elif change == "old_results":
        observation["data"]["elements"][2]["name"] = "李四"
    elif change == "unrelated_result_tree":
        observation["data"]["elements"][2]["depth"] = 1
    elif change == "duplicate_query_fields":
        observation["data"]["elements"].append(deepcopy(observation["data"]["elements"][0]))
    elif change == "wrong_provider":
        observation["local_desktop_provider"]["provider_id"] = "other-provider"
    elif change == "foreign_run":
        source["run_id"] = "other-run"
    elif change == "foreign_plan":
        source["plan_id"] = "other-plan"
    elif change == "foreign_call":
        verifier["source_tool_call_id"] = "other-call"
    elif change == "untrusted_observation":
        observation.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
    elif change == "other_submit_tool":
        source["detail"] = "desktop.submit_foreground"
    elif change == "non_search_target":
        source["action_target"]["target"] = "消息"
    elif change == "modifier_dispatch":
        source["result"]["data"]["modifiers"] = ["command"]
    elif change == "wrong_declared_step":
        verifier["depends_on"] = ["type-app-search-query"]
        verifier["source_step_id"] = "type-app-search-query"
        verifier["task_verification_targets"] = []
        verifier["verification_targets"] = []
    observation.update(postcondition_verified=True, verified_observed_state="sent")
    assert _receipt(source, verifier, observation) == {}
    assert runtime_goal_assessment(contract, [source]).completed is False


def test_explicit_field_click_keeps_approval_and_full_search_chain_with_rich_tool_policy():
    goal = "打开 Slack 点击搜索框输入 yachiyo 并搜索"
    selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
    assert not planner_selection_needs_model_assistance(selection, goal)
    steps = selection.decision.plan.tool_plan.steps
    assert [step.step_id for step in steps] == [
        "inspect-app-search-field",
        "focus-app-search-field",
        "type-app-search-query",
        "submit-app-search",
        "verify-desktop-result",
    ]
    assert steps[1].tool_name == "app.open_and_click_ui_element"
    assert steps[1].approval_required is True
    assert steps[2].input_preview == {"text": "yachiyo"}
    assert steps[3].tool_name == "desktop.search_submit"
    assert steps[4].input_preview.get("role_filter", "") == ""
    assert not any("shortcut" in (step.tool_name or "") for step in steps)


def test_search_then_click_keeps_semantic_query_when_app_discovery_refines_scope():
    goal = "打开 Finder 查找 Downloads 然后打开第一个"
    selection = planner_first_direct_tool_selection(goal, DAILY_DESKTOP_TOOL_NAMES)
    envelope = runtime_execution_envelope_from_decision(
        selection.decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True,
    ).model_dump()
    request = next(r for r in envelope["requests"] if r["tool_name"] == "desktop.search_submit")
    assert request["action_target"]["query"] == "Downloads"
    assert request["action_target"]["target"] == "搜索"
    assert request["action_target"]["role_filter"] == "text"
    assert request["action_target"]["selection_query"] == "Finder"
    assert request["input"] == {}


@pytest.mark.parametrize("change", [
    "none", "dispatch", "public", "provider_self_claim", "wrong_query", "wrong_app",
    "wrong_run", "wrong_plan", "wrong_step", "wrong_request", "wrong_call", "missing_provider",
])
def test_search_dependency_requires_exact_runtime_owned_ax_receipt(change):
    from apps.shell.agent.runtime.tool_execution import (
        _approval_dependency_semantic_verification_succeeded,
    )

    _, source, verifier, observation = _case()
    dependency = {**source, "tool": "desktop.search_submit"}
    result = _tool_result_with_trusted_observation_receipt(
        observation, _receipt(source, verifier, observation),
    )
    event = {**verifier, "source": "runtime_native_postcondition_receipt"}
    tool = "desktop.ui_elements"
    if change == "dispatch":
        result = source["result"]
        tool = "desktop.search_submit"
    elif change == "public":
        event["visibility"] = "public"
    elif change == "provider_self_claim":
        event["source"] = "runtime_planner"
    elif change == "wrong_query":
        result["observed_query"] = "李四"
    elif change == "wrong_app":
        result["observed_app_name"] = "Finder"
    elif change == "wrong_run":
        result["run_id"] = "other"
    elif change == "wrong_plan":
        result["plan_id"] = "other"
    elif change in {"wrong_step", "wrong_request", "wrong_call"}:
        key = {"wrong_step": "source_step_id", "wrong_request": "source_request_id",
               "wrong_call": "source_tool_call_id"}[change]
        result[key] = "other"
    elif change == "missing_provider":
        result.pop(RUNTIME_EXECUTION_PROVENANCE_KEY)
        result.pop("local_desktop_provider")
    assert _approval_dependency_semantic_verification_succeeded(
        result, dependency, approval_request={}, event=event, event_payload={}, event_tool=tool,
    ) is (change == "none")


def test_main_chat_search_completes_from_actual_ordered_dispatch_and_ax_results(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from tests.test_chat_api import _make_agent_runtime_service, _make_api, _send_foreground_message

    api, runtime, store = _make_api(tmp_path)
    service = _make_agent_runtime_service(tmp_path)
    runtime.agent_runtime_service = service
    calls = []
    state = {"query": "", "submitted": False}
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: SimpleNamespace(
            get_defaults=lambda: {"chat": ""},
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.openai_compatible_chat_message",
        lambda *a, **kw: pytest.fail("An observed exact native search must not call a model"),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.list_apps",
        lambda query="", limit=20: {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {
                "query": query,
                "apps": [{"name": "WeChat", "path": "/Applications/WeChat.app"}],
                "best_match": {"name": "WeChat", "path": "/Applications/WeChat.app"},
                "count": 1,
                "total_count": 1,
                "truncated": False,
            },
        },
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.running_apps",
        lambda: {
            "ok": True,
            "action": "desktop.running_apps",
            "data": {
                "apps": [{"name": "WeChat", "pid": 100}],
                "count": 1,
            },
        },
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.app_focus",
        lambda app_name: (
            calls.append(("focus", app_name))
            or {
                "ok": True,
                "action": "app.focus",
                "data": {"app_name": app_name},
            }
        ),
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
            },
        },
    )
    monkeypatch.setattr(
        "apps.shell.agent.tools.desktop.desktop_safe_shortcut",
        lambda action: (
            calls.append(("shortcut", action))
            or {
                "ok": True,
                "action": "desktop.safe_shortcut",
                "data": {"shortcut_action": action},
            }
        ),
    )

    def type_text(text):
        calls.append(("type", text))
        state["query"] = text
        return {
            "ok": True,
            "action": "desktop.safe_type_text",
            "data": {
                "character_count": len(text),
                "explicit_user_text": True,
            },
        }

    def submit():
        calls.append(("search_submit", ""))
        state["submitted"] = True
        return {
            "ok": True,
            "action": "desktop.search_submit",
            "data": {
                "key": "return",
                "modifiers": [],
            },
        }

    def ui_elements(**kwargs):
        elements = [{"role": "AXTextField", "name": "Search", "value": state["query"], "depth": 1}]
        if state["submitted"]:
            elements.extend(
                [
                    {"role": "AXTable", "name": "Search Results", "depth": 1},
                    {"role": "AXRow", "name": state["query"], "depth": 2},
                ]
            )
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": "WeChat",
                "pid": 100,
                "window_id": 200,
                "elements": elements,
                "count": len(elements),
            },
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_safe_type_text", type_text)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.desktop_search_submit", submit)
    monkeypatch.setattr("apps.shell.agent.tools.desktop.ui_elements", ui_elements)
    try:
        result = _send_foreground_message(api, "切到WeChat，搜索张三")
        run = service.get_run(result["run_id"])
        assert calls == [
            ("focus", "WeChat"),
            ("shortcut", "find"),
            ("type", "张三"),
            ("search_submit", ""),
        ]
        assert result["status"] == run["status"] == "completed"
        assert run["user_goal"] == "切到WeChat，搜索张三"
        events = service.list_run_events(run["run_id"], include_internal=True)["events"]
        assert "agent.desktop.intent_completed" in [event["event_type"] for event in events]
        assert not any(event["event_type"].startswith("model.request") for event in events)
    finally:
        service.close()
        store.close()


@pytest.mark.parametrize("provider_self_claims", [False, True])
def test_return_dispatch_cannot_replace_independent_query_results_observation(provider_self_claims):
    from apps.shell.agent.runtime.tool_execution import (
        _native_postcondition_receipt_for_verifier,
        _tool_result_with_trusted_exact_dispatch,
    )

    contract, source, verifier, observation = _case()
    raw = deepcopy(source["result"])
    if provider_self_claims:
        raw.update(postcondition_verified=True, verified_observed_state="fulfilled", verified=True)
        raw["data"].update(postcondition_verified=True, verified_observed_state="fulfilled")
    source["result"] = _tool_result_with_trusted_exact_dispatch(
        "desktop.search_submit", source, raw, run_id="run-search"
    )
    assert source["result"]["native_dispatch_verified"] is True
    assert source["result"]["verified_observed_state"] == "dispatched"
    assert source["result"].get("postcondition_verified") is not True
    assert (
        _native_postcondition_receipt_for_verifier(verifier, [source], tool_timeline_start=0) == {}
    )
    assert not runtime_goal_assessment(contract, [source]).completed
    receipt = _receipt(source, verifier, observation)
    assert receipt["verification_predicate_kind"] == "exact_app_search_result_present"
