"""A dispatched search click needs a correlated independent URL observation."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.browser_navigation_receipts import (
    approved_navigation_projection_duplicates,
)
from apps.shell.agent.runtime.events import RUNTIME_EXECUTION_PROVENANCE_KEY
from apps.shell.agent.runtime.tool_execution import (
    _trusted_postcondition_observation_receipt_for_verifier,
)


@pytest.fixture
def navigation_case():
    ownership = {
        "target_id": "owned-target",
        "target_owned_by_run": True,
        "browser_profile_isolated_from_user": True,
    }
    provenance = {RUNTIME_EXECUTION_PROVENANCE_KEY: {"source": "local_tool_broker", "version": 1}}
    route = {"selected_provider_kind": "browser_target", "selected_provider_id": "browser_target"}
    source = {
        "event": "agent.tool.call",
        "detail": "browser.click",
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "approved": True,
        "run_id": "run",
        "plan_id": "plan",
        "decision_id": "decision",
        "tool_plan_id": "tool-plan",
        "step_id": "click",
        "request_id": "click-request",
        "tool_call_id": "click-call",
        "desktop_execution_route": route,
        "input_preview": {"selector": "search-result=1", "click_count": 1},
        "result": {
            "ok": True,
            "action": "browser.click",
            **provenance,
            "data": {
                **ownership,
                "selector": "search-result=1",
                "click_count": 1,
                "tag": "A",
                "link_target": "",
                "source_url": "https://search.test/?q=yachiyo",
                "navigation_url": "https://example.test/yachiyo",
            },
        },
    }
    request = {
        "tool": "browser.current_page",
        "input": {},
        "run_id": "run",
        "plan_id": "plan",
        "decision_id": "decision",
        "tool_plan_id": "tool-plan",
        "runtime_stage": "verify",
        "source_tool_call_id": "click-call",
        "source_request_id": "click-request",
        "depends_on": ["click"],
        "desktop_execution_route": route,
    }
    result = {
        "ok": True,
        "action": "browser.current_page",
        **provenance,
        "data": {**ownership, "url": "https://example.test/yachiyo"},
    }
    return source, request, result


def _receipt(case):
    source, request, result = case
    return _trusted_postcondition_observation_receipt_for_verifier(
        request,
        result,
        [source],
        tool_timeline_start=0,
        run_id="run",
    )


def test_selected_link_matches_independent_owned_page(navigation_case):
    receipt = _receipt(navigation_case)
    assert receipt["verified_observed_state"] == "open"
    assert receipt["source_request_id"] == "click-request"
    assert receipt["source_tool_call_id"] == "click-call"
    assert receipt["observed_navigation_url"] == "https://example.test/yachiyo"
    assert receipt["observed_browser_target_id"] == "owned-target"


@pytest.mark.parametrize(
    "mutation",
    [
        "unobserved",
        "redirect",
        "wrong_target",
        "source_unowned",
        "observer_unowned",
        "source_user_profile",
        "observer_user_profile",
        "same_url",
        "missing_href",
        "javascript_href",
        "credential_url",
        "invalid_url",
        "url_whitespace",
        "new_tab",
        "button",
        "non_search_selector",
        "zero_result",
        "extra_input",
        "changed_selector",
        "double_click",
        "boolean_source_count",
        "boolean_result_count",
        "wrong_observer",
        "observer_input",
        "source_failure",
        "observer_failure",
        "fallback",
        "permission",
        "source_truncated",
        "observer_truncated",
        "wrong_run",
        "wrong_plan",
        "wrong_decision",
        "wrong_tool_plan",
        "wrong_call",
        "wrong_step",
        "source_model_provenance",
        "observer_model_provenance",
        "wrong_provider",
        "unapproved_source",
        "foreign_source_actor",
        "foreign_source_executor",
        "missing_decision",
        "missing_tool_plan",
    ],
)
def test_acknowledgement_foreign_state_and_scope_cannot_prove_navigation(navigation_case, mutation):
    source, request, result = deepcopy(navigation_case)
    sd, vd = source["result"]["data"], result["data"]
    if mutation == "unobserved":
        vd["url"] = sd["source_url"]
    elif mutation == "redirect":
        vd["url"] = "https://other.test/"
    elif mutation == "wrong_target":
        vd["target_id"] = "foreign"
    elif mutation == "source_unowned":
        sd["target_owned_by_run"] = False
    elif mutation == "observer_unowned":
        vd["target_owned_by_run"] = False
    elif mutation == "source_user_profile":
        sd["browser_profile_isolated_from_user"] = False
    elif mutation == "observer_user_profile":
        vd["browser_profile_isolated_from_user"] = False
    elif mutation == "same_url":
        sd["source_url"] = sd["navigation_url"]
    elif mutation == "missing_href":
        sd.pop("navigation_url")
    elif mutation == "javascript_href":
        sd["navigation_url"] = vd["url"] = "javascript:alert(1)"
    elif mutation == "credential_url":
        sd["navigation_url"] = vd["url"] = "https://user:pw@example.test/"
    elif mutation == "invalid_url":
        sd["navigation_url"] = vd["url"] = "https://example.test:invalid/"
    elif mutation == "url_whitespace":
        sd["navigation_url"] = vd["url"] = " https://example.test/"
    elif mutation == "new_tab":
        sd["link_target"] = "_blank"
    elif mutation == "button":
        sd["tag"] = "BUTTON"
    elif mutation == "non_search_selector":
        source["input_preview"]["selector"] = "text=Open"
    elif mutation == "zero_result":
        source["input_preview"]["selector"] = "search-result=0"
    elif mutation == "extra_input":
        source["input_preview"]["fallback_x"] = 10
    elif mutation == "changed_selector":
        sd["selector"] = "search-result=2"
    elif mutation == "double_click":
        sd["click_count"] = 2
    elif mutation == "boolean_source_count":
        source["input_preview"]["click_count"] = True
    elif mutation == "boolean_result_count":
        sd["click_count"] = True
    elif mutation == "wrong_observer":
        request["tool"] = "desktop.ui_elements"
    elif mutation == "observer_input":
        request["input"] = {"target_id": "foreign"}
    elif mutation == "source_failure":
        source["result"]["ok"] = False
    elif mutation == "observer_failure":
        result["ok"] = False
    elif mutation == "fallback":
        source["result"]["fallback_used"] = True
    elif mutation == "permission":
        result["permission_error"] = True
    elif mutation == "source_truncated":
        sd["truncated"] = True
    elif mutation == "observer_truncated":
        vd["truncated"] = True
    elif mutation == "wrong_run":
        source["run_id"] = "foreign"
    elif mutation == "wrong_plan":
        source["plan_id"] = "foreign"
    elif mutation == "wrong_decision":
        source["decision_id"] = "foreign"
    elif mutation == "wrong_tool_plan":
        source["tool_plan_id"] = "foreign"
    elif mutation == "wrong_call":
        request["source_tool_call_id"] = "foreign"
    elif mutation == "wrong_step":
        source["step_id"] = "foreign"
    elif mutation == "source_model_provenance":
        source["result"][RUNTIME_EXECUTION_PROVENANCE_KEY]["source"] = "model"
    elif mutation == "observer_model_provenance":
        result[RUNTIME_EXECUTION_PROVENANCE_KEY]["source"] = "model"
    elif mutation == "wrong_provider":
        request["desktop_execution_route"] = {
            "selected_provider_kind": "foreign",
            "selected_provider_id": "foreign",
        }
    elif mutation == "unapproved_source":
        source["approved"] = False
    elif mutation == "foreign_source_actor":
        source["actor"] = "model"
    elif mutation == "foreign_source_executor":
        source["execution_authority"] = "model"
    elif mutation == "missing_decision":
        request.pop("decision_id")
        source.pop("decision_id")
    elif mutation == "missing_tool_plan":
        request.pop("tool_plan_id")
        source.pop("tool_plan_id")
    source["result"]["postcondition_verified"] = True
    result["postcondition_verified"] = (
        True  # Provider flags cannot override an independent mismatch.
    )
    assert _receipt((source, request, result)) == {}


def _canonical(source):
    event = deepcopy(source)
    event.update(
        approval_resume_result_canonical=True, execution_mode="approved_result_canonical_projection"
    )
    event.pop("desktop_execution_route")
    event["input_preview"].update(plan_id="plan", step_id="click")
    return event


def test_exact_approved_projection_uses_original_native_source(navigation_case):
    source, request, result = navigation_case
    canonical = _canonical(source)
    assert approved_navigation_projection_duplicates(canonical, [source, canonical])
    receipt = _trusted_postcondition_observation_receipt_for_verifier(
        request,
        result,
        [source, canonical],
        tool_timeline_start=2,
        run_id="run",
    )
    assert receipt["verified_observed_state"] == "open"


@pytest.mark.parametrize(
    "mutation",
    [
        "result",
        "run",
        "plan",
        "decision",
        "request",
        "call",
        "step",
        "tool_plan",
        "missing",
        "duplicate",
        "unapproved",
        "foreign_actor",
        "foreign_executor",
        "boolean_count",
        "changed_selector",
        "fallback_input",
    ],
)
def test_changed_approved_projection_never_falls_back(navigation_case, mutation):
    source, request, result = deepcopy(navigation_case)
    canonical = _canonical(source)
    timeline = [source, canonical]
    if mutation == "result":
        canonical["result"]["data"]["navigation_url"] = "https://foreign.test/"
    elif mutation in {"run", "plan", "decision", "request", "call", "step", "tool_plan"}:
        canonical[
            {
                "run": "run_id",
                "plan": "plan_id",
                "decision": "decision_id",
                "request": "request_id",
                "call": "tool_call_id",
                "step": "step_id",
                "tool_plan": "tool_plan_id",
            }[mutation]
        ] = "foreign"
    elif mutation == "missing":
        canonical.pop("decision_id")
    elif mutation == "duplicate":
        timeline.insert(0, deepcopy(source))
    elif mutation == "unapproved":
        source["approved"] = False
    elif mutation == "foreign_actor":
        source["actor"] = "model"
    elif mutation == "foreign_executor":
        source["execution_authority"] = "model"
    elif mutation == "boolean_count":
        canonical["input_preview"]["click_count"] = True
    elif mutation == "changed_selector":
        canonical["input_preview"]["selector"] = "search-result=2"
    elif mutation == "fallback_input":
        canonical["input_preview"]["fallback_x"] = 10
    assert not approved_navigation_projection_duplicates(canonical, timeline)


def test_search_navigation_verifier_is_frozen_in_original_goal():
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
    from apps.shell.yachiyo_agent.daily_desktop import (
        daily_desktop_entrypoint_runtime_plan,
        daily_desktop_requests_can_complete_without_model,
    )
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    goal = "打开 Chrome 搜索 yachiyo 然后打开第一个结果"
    tools = list(TOOL_DISPATCH_REGISTRY)
    plan = daily_desktop_entrypoint_runtime_plan(goal, allowed_tools=tools)
    (criterion,) = plan.decision.plan.task_core.goal_contract.criteria
    assert plan.decision.plan.task_core.goal_contract.original_goal == goal
    assert criterion.source_step_ids == ["click-web-search-result"]
    assert criterion.verifier_step_ids == ["verify-web-search-navigation"]
    assert [request["tool"] for request in plan.executable_requests] == [
        "browser.open_url",
        "browser.click",
        "browser.current_page",
    ]
    assert plan.executable_requests[1]["approval_required"] is True
    assert daily_desktop_requests_can_complete_without_model(plan.executable_requests)
    selection = planner_first_direct_tool_selection(goal, tools)
    assert not planner_selection_needs_model_assistance(selection, goal)


@pytest.mark.parametrize("mutation", ["source", "step", "input", "dependency", "approval", "stage"])
def test_only_declared_readback_can_skip_model_followup(mutation):
    from apps.shell.yachiyo_agent.daily_desktop import (
        daily_desktop_requests_can_complete_without_model,
    )

    request = {
        "tool": "browser.current_page",
        "input": {},
        "runtime_stage": "verify",
        "source": "runtime_verification",
        "step_id": "verify-web-search-navigation",
        "depends_on": ["click-web-search-result"],
        "continue_to_model": True,
    }
    if mutation == "source":
        request["source"] = "model"
    elif mutation == "step":
        request["step_id"] = "foreign"
    elif mutation == "input":
        request["input"] = {"target_id": "foreign"}
    elif mutation == "dependency":
        request["depends_on"] = ["foreign"]
    elif mutation == "approval":
        request["approval_required"] = True
    elif mutation == "stage":
        request["runtime_stage"] = "operate"
    assert not daily_desktop_requests_can_complete_without_model([request])


@pytest.mark.parametrize(
    "goal",
    [
        "打开 Chrome 搜索 yachiyo 然后打开第一个结果，再截图",
        "打开 Chrome 搜索 yachiyo 然后打开第一个结果，再删除文件",
        "请说“打开 Chrome 搜索 yachiyo 然后打开第一个结果”",
        "如果有必要，打开 Chrome 搜索 yachiyo 然后打开第一个结果",
    ],
)
def test_additional_or_nonexecuting_browser_actions_do_not_gain_fast_path(goal):
    from apps.shell.agent.runtime.model_intent_planning import (
        planner_selection_needs_model_assistance,
    )
    from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )

    selection = planner_first_direct_tool_selection(goal, list(TOOL_DISPATCH_REGISTRY))
    assert not selection.requests or planner_selection_needs_model_assistance(selection, goal)


def test_duplicate_native_source_cannot_prove_navigation(navigation_case):
    source, request, result = navigation_case
    receipt = _trusted_postcondition_observation_receipt_for_verifier(
        request,
        result,
        [source, deepcopy(source)],
        tool_timeline_start=0,
        run_id="run",
    )
    assert receipt == {}


def test_local_broker_browser_isolation_is_not_an_execution_adapter(navigation_case):
    from apps.shell.agent.runtime.tool_execution import _trusted_runtime_execution_provider_identity

    source, request, result = navigation_case
    action_provider = _trusted_runtime_execution_provider_identity(source, source["result"])
    assert action_provider == ("local_desktop", "local-native-desktop")
    request["desktop_execution_route"] = {
        "selected_provider_kind": "none",
        "selected_provider_id": "",
    }
    assert _trusted_runtime_execution_provider_identity(request, result) == action_provider
    assert _receipt((source, request, result))["verified_observed_state"] == "open"
