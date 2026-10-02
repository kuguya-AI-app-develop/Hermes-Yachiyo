"""Current search binding preserves the root goal and actual native window."""

from copy import deepcopy

import pytest

from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.foreground_search_receipts import (
    POST,
    PRE,
    READY,
    foreground_search_dispatch_ready,
    trusted_foreground_search_receipt,
)
from apps.shell.agent.runtime.goal_runtime import runtime_goal_assessment, runtime_goal_contract
from apps.shell.agent.runtime.tool_execution import _tool_result_with_trusted_observation_receipt
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner


def _case(goal="点搜索框输入 yachiyo 然后搜索"):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    payload = runtime_execution_envelope_payload(
        decision, allowed_tools=DAILY_DESKTOP_TOOL_NAMES, full_plan=True
    )
    specs = {
        r["step_id"]: r
        for r in runtime_execution_requests_from_envelope_payload(
            payload, allowed_tools=DAILY_DESKTOP_TOOL_NAMES
        )
    }
    contract = runtime_goal_contract(
        run_id="foreground-run",
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

    def observation(query, results=False):
        field = {
            "role": "AXTextField",
            "name": "Search",
            "identifier": "field",
            "value": query,
            "focused": True,
            "editable": True,
            "depth": 1,
        }
        elements = [deepcopy(field)]
        if results:
            elements += [
                {"role": "AXTable", "name": "Search Results", "depth": 1},
                {"role": "AXRow", "name": query, "depth": 2},
            ]
        return {
            "ok": True,
            "action": "desktop.ui_elements",
            **deepcopy(provenance),
            "data": {
                "app_name": "Google Chrome",
                "pid": 100,
                "window_id": 200,
                "focused_element": field,
                "elements": elements,
            },
        }

    timeline = [
        {
            "event": "agent.goal.contract",
            "run_id": "foreground-run",
            "goal_contract": contract.to_payload(),
        }
    ]
    source = None
    for step, spec in specs.items():
        if step == POST:
            continue
        if spec["tool"] == "desktop.ui_elements":
            result = observation("" if step == PRE and READY in specs else "yachiyo")
        else:
            result = {
                "ok": True,
                "action": spec["tool"],
                **deepcopy(provenance),
                "data": {"key": "return", "modifiers": []}
                if spec["tool"] == "desktop.search_submit"
                else {},
            }
        event = {
            **spec,
            "run_id": "foreground-run",
            "actor": "native_runtime",
            "execution_authority": "runtime_tool_executor",
            "event": "agent.tool.call",
            "detail": spec["tool"],
            "input_preview": deepcopy(spec["input"]),
            "tool_call_id": step + "-call",
            "result": result,
        }
        timeline.append(event)
        if spec["tool"] == "desktop.search_submit":
            source = event
    verifier = {
        **specs[POST],
        "run_id": "foreground-run",
        "tool_call_id": "post-call",
        "actor": "native_runtime",
        "execution_authority": "runtime_tool_executor",
        "source_tool_call_id": source["tool_call_id"],
        "source_step_id": source["step_id"],
        "source_request_id": source["request_id"],
    }
    return contract, source, verifier, observation("yachiyo", True), timeline


def _receipt(case):
    _, s, v, o, t = case
    return trusted_foreground_search_receipt(
        "desktop.search_submit", s, v, o, t, run_id="foreground-run"
    )


@pytest.mark.parametrize(
    "goal", ["点搜索框输入 yachiyo 然后搜索", "提交当前搜索", "press enter to search"]
)
def test_exact_before_query_and_after_results_fulfill_current_search(goal):
    case = _case(goal)
    c, s, v, o, t = case
    receipt = _receipt(case)
    assert receipt["verification_predicate_kind"] == "exact_app_search_result_present"
    assert c.original_goal == goal
    assert not runtime_goal_assessment(c, t).completed
    event = {
        **v,
        "event": "agent.tool.call",
        "detail": "desktop.ui_elements",
        "source": "runtime_native_postcondition_receipt",
        "input_preview": v["input"],
        "result": _tool_result_with_trusted_observation_receipt(
            o,
            {
                **receipt,
                "source_tool": "desktop.search_submit",
                "source_step_id": s["step_id"],
                "source_tool_call_id": s["tool_call_id"],
                "source_request_id": s["request_id"],
                "run_id": "foreground-run",
                "plan_id": s["plan_id"],
                "provider_kind": "local_desktop",
                "provider_id": "local-native-desktop",
            },
        ),
    }
    assert runtime_goal_assessment(c, [*t, event]).completed


@pytest.mark.parametrize(
    "goal",
    [
        "如果有必要，点搜索框输入 yachiyo 然后搜索",
        "请说点搜索框输入 yachiyo 然后搜索",
        "不要点搜索框输入 yachiyo 然后搜索",
        "解释“点搜索框输入 yachiyo 然后搜索”",
        "点搜索框输入 yachiyo，如果有必要然后搜索",
    ],
)
def test_unexecuting_search_speech_does_not_dispatch(goal):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert not any(
        s.tool_name in {"desktop.safe_shortcut", "desktop.safe_type_text", "desktop.search_submit"}
        for s in decision.plan.tool_plan.steps
    )


@pytest.mark.parametrize(
    "change",
    [
        "missing_pre",
        "pre_wrong_window",
        "post_wrong_window",
        "post_wrong_pid",
        "post_wrong_app",
        "post_wrong_query",
        "post_no_results",
        "post_wrong_results",
        "unrelated_results",
        "duplicate_search",
        "duplicate_pre",
        "foreign_run",
        "foreign_decision",
        "foreign_tool_plan",
        "foreign_request",
        "extra_input",
        "wrong_provider",
        "fallback",
        "truncated",
        "stale_results",
        "unfocused_pre",
        "ready_wrong_query",
        "interleaved_action",
        "false_flags",
        "missing_find",
        "duplicate_find",
        "changed_find",
        "malformed_data",
        "malformed_input",
    ],
)
def test_foreign_missing_or_self_claimed_search_evidence_is_rejected(change):
    case = _case()
    _, s, v, o, t = case
    pre = next(e for e in t if e.get("step_id") == PRE)
    ready = next(e for e in t if e.get("step_id") == READY)
    if change == "missing_pre":
        t.remove(pre)
    elif change == "pre_wrong_window":
        pre["result"]["data"]["window_id"] = 201
    elif change == "post_wrong_window":
        o["data"]["window_id"] = 201
    elif change == "post_wrong_pid":
        o["data"]["pid"] = 101
    elif change == "post_wrong_app":
        o["data"]["app_name"] = "Slack"
    elif change == "post_wrong_query":
        o["data"]["elements"][0]["value"] = "other"
    elif change == "post_no_results":
        o["data"]["elements"] = o["data"]["elements"][:1]
    elif change == "post_wrong_results":
        o["data"]["elements"][2]["name"] = "other"
    elif change == "unrelated_results":
        o["data"]["elements"][2]["depth"] = 1
    elif change == "duplicate_search":
        o["data"]["elements"].append(deepcopy(o["data"]["elements"][0]))
    elif change == "duplicate_pre":
        t.insert(2, deepcopy(pre))
    elif change == "foreign_run":
        pre["run_id"] = "other-run"
    elif change == "foreign_decision":
        pre["decision_id"] = "other-decision"
    elif change == "foreign_tool_plan":
        pre["tool_plan_id"] = "other-plan"
    elif change == "foreign_request":
        v["request_id"] = "other-request"
    elif change == "extra_input":
        v["input"]["app_name"] = "Slack"
    elif change == "wrong_provider":
        pre["result"]["local_desktop_provider"]["provider_id"] = "other-provider"
    elif change == "fallback":
        pre["result"]["fallback_used"] = True
    elif change == "truncated":
        o["truncated"] = True
    elif change == "stale_results":
        pre["result"]["data"]["elements"] += deepcopy(o["data"]["elements"][1:])
    elif change == "unfocused_pre":
        pre["result"]["data"]["focused_element"]["focused"] = False
    elif change == "ready_wrong_query":
        ready["result"]["data"]["focused_element"]["value"] = "other"
    elif change == "interleaved_action":
        t.insert(-1, {**deepcopy(s), "step_id": "foreign-effect", "tool_call_id": "foreign-call"})
    elif change == "false_flags":
        o["data"]["elements"] = []
        o.update(
            postcondition_verified=True, verification_passed=True, verified_observed_state="sent"
        )
    elif change == "missing_find":
        t.remove(next(e for e in t if e.get("step_id") == "prepare-foreground-search-field"))
    elif change == "duplicate_find":
        t.insert(
            2, deepcopy(next(e for e in t if e.get("step_id") == "prepare-foreground-search-field"))
        )
    elif change == "changed_find":
        next(e for e in t if e.get("step_id") == "prepare-foreground-search-field")[
            "input_preview"
        ] = {"action": "paste"}
    elif change == "malformed_data":
        pre["result"]["data"] = "invalid"
    elif change == "malformed_input":
        v["input"] = "invalid"
    assert _receipt(case) == {}
    assert not runtime_goal_assessment(case[0], t).completed


@pytest.mark.parametrize("tool", ["desktop.safe_type_text", "desktop.search_submit"])
@pytest.mark.parametrize(
    "drift", ["none", "app", "pid", "window", "field", "query", "unfocused", "unknown", "fallback"]
)
def test_live_search_binding_is_revalidated_inside_foreground_lock(
    tmp_path, monkeypatch, tool, drift
):
    from apps.shell.agent.runtime.foreground_search_receipts import (
        foreground_search_live_binding,
        foreground_search_live_matches,
    )
    from apps.shell.agent.tools import desktop
    from apps.shell.agent.tools.broker import ToolBroker
    from apps.shell.agent.tools.foreground_lock import ForegroundActionLock

    _, source, _, _, timeline = _case()
    event = (
        source
        if tool == "desktop.search_submit"
        else next(e for e in timeline if e.get("step_id") == "prepare-foreground-search-query")
    )
    prefix = timeline[: timeline.index(event)]
    expected = foreground_search_live_binding(event, prefix, run_id="foreground-run")
    assert expected
    pre = next(
        e for e in prefix if e.get("step_id") == (READY if tool == "desktop.search_submit" else PRE)
    )
    raw = deepcopy(pre["result"])
    if drift == "app":
        raw["data"]["app_name"] = "WeChat"
    elif drift == "pid":
        raw["data"]["pid"] = 101
    elif drift == "window":
        raw["data"]["window_id"] = 201
    elif drift == "field":
        raw["data"]["focused_element"]["name"] = "Message"
    elif drift == "query":
        raw["data"]["focused_element"]["value"] = "other"
    elif drift == "unfocused":
        raw["data"]["focused_element"]["focused"] = False
    elif drift == "unknown":
        raw["data"].pop("focused_element")
    elif drift == "fallback":
        raw["fallback_used"] = True
    calls = []
    lock = ForegroundActionLock()
    broker = ToolBroker(
        workspace_policy={},
        artifact_root=tmp_path,
        foreground_lock=lock,
        foreground_lock_owner="test",
    )
    monkeypatch.setattr(desktop, "ui_elements", lambda **kw: raw)
    monkeypatch.setattr(
        desktop,
        "desktop_search_submit",
        lambda: calls.append("Return") or {"ok": True, "action": tool},
    )
    monkeypatch.setattr(
        desktop,
        "desktop_safe_type_text",
        lambda text: calls.append(text) or {"ok": True, "action": tool},
    )
    result = broker.runtime_exact_search_input(
        tool, "yachiyo", validate_pre=lambda snap: foreground_search_live_matches(snap, expected)
    )
    assert (result["ok"] is True) == (drift == "none")
    assert calls == (
        ["yachiyo" if tool == "desktop.safe_type_text" else "Return"] if drift == "none" else []
    )


@pytest.mark.parametrize(
    "change",
    [
        "removed_dependencies",
        "wrong_step",
        "wrong_input",
        "wrong_decision",
        "wrong_run",
        "missing_find",
        "wrong_find",
        "interleaved",
    ],
)
def test_canonical_search_dispatch_cannot_drop_its_field_binding(change):
    _, source, _, _, timeline = _case()
    prefix = timeline[: timeline.index(source)]
    request = deepcopy(source)
    if change == "removed_dependencies":
        request["depends_on"] = []
    elif change == "wrong_step":
        request["step_id"] = "other-step"
    elif change == "wrong_input":
        request["input"] = {"app_name": "WeChat"}
    elif change == "wrong_decision":
        request["decision_id"] = "other-decision"
    elif change == "wrong_run":
        request["run_id"] = "other-run"
    elif change == "missing_find":
        prefix.remove(
            next(e for e in prefix if e.get("step_id") == "prepare-foreground-search-field")
        )
    elif change == "wrong_find":
        next(e for e in prefix if e.get("step_id") == "prepare-foreground-search-field")[
            "input_preview"
        ] = {"action": "paste"}
    elif change == "interleaved":
        prefix.append({**deepcopy(source), "step_id": "foreign-action"})
    assert not foreground_search_dispatch_ready(request, prefix, run_id="foreground-run")


@pytest.mark.parametrize(
    "goal", ['点搜索框输入 "search" 然后搜索', '点搜索框输入 "if search" 然后搜索']
)
def test_quoted_query_is_data_and_does_not_supply_search_authority(goal):
    decision = RuntimePlanner().decision(goal, allowed_tools=DAILY_DESKTOP_TOOL_NAMES)
    assert any(s.step_id == PRE for s in decision.plan.tool_plan.steps)
    assert any(s.step_id == POST for s in decision.plan.tool_plan.steps)


@pytest.mark.parametrize("provider", ["sandbox_desktop", "background_desktop", "wrong_local_id"])
def test_current_search_never_falls_back_to_unbound_routed_effect(tmp_path, monkeypatch, provider):
    import time

    from apps.shell.agent.runtime.budget import RunBudget, RunBudgetLimits
    from apps.shell.agent.runtime.tool_execution import RuntimeToolCallExecutor
    from apps.shell.agent.tools import desktop
    from apps.shell.agent.tools.broker import ToolBroker

    _, source, _, _, timeline = _case("提交当前搜索")
    prefix = timeline[: timeline.index(source)]
    calls = []

    class Callbacks:
        def __getattr__(self, name):
            return lambda *args, **kwargs: None

    class Registry:
        def bind_tool_request_to_owned_provider(self, tool, request):
            return {
                **request,
                "desktop_execution_route": {
                    "selected_provider_kind": "local_desktop"
                    if provider == "wrong_local_id"
                    else provider,
                    "selected_provider_id": "another-native"
                    if provider == "wrong_local_id"
                    else "remote-provider",
                    "can_execute": True,
                    "status": "provider_ready",
                    "provider_execution_required": True,
                    "foreground_takeover_allowed": True,
                },
            }

        def execute_if_routed(self, *args, **kwargs):
            calls.append("routed-effect")
            return {"ok": True, "action": "desktop.search_submit"}

    budget = RunBudget(RunBudgetLimits(), time.time())
    executor = RuntimeToolCallExecutor(
        normalize_tool_name=str,
        input_preview=lambda v: v,
        run_budget=lambda *_: budget,
        validate_tool_payload=lambda *_: None,
        limit_tool_result=lambda v: v,
        timeline_factory=lambda event, detail, **kw: {"event": event, "detail": detail, **kw},
        tool_call_events=Callbacks(),
        trace_events=Callbacks(),
        append_run_event=lambda *_: None,
        desktop_provider_registry=Registry(),
    )
    broker = ToolBroker(workspace_policy={}, artifact_root=tmp_path)
    monkeypatch.setattr(
        desktop, "desktop_search_submit", lambda: calls.append("Return") or {"ok": True}
    )
    monkeypatch.setattr(
        desktop, "desktop_safe_type_text", lambda text: calls.append("type") or {"ok": True}
    )
    result = executor.execute(
        deepcopy(source), DAILY_DESKTOP_TOOL_NAMES, broker, prefix, run_id="foreground-run"
    )
    assert result["ok"] is False
    assert result.get("reason") == "foreground_search_atomic_binding_unavailable"
    assert calls == []


@pytest.mark.parametrize("change", ["actor", "executor", "empty_call", "source_call", "pre_call"])
def test_search_verifier_has_independent_runtime_execution_identity(change):
    case = _case()
    _, source, verifier, _, timeline = case
    if change == "actor":
        verifier["actor"] = "model"
    elif change == "executor":
        verifier["execution_authority"] = "provider"
    elif change == "empty_call":
        verifier["tool_call_id"] = ""
    elif change == "source_call":
        verifier["tool_call_id"] = source["tool_call_id"]
    elif change == "pre_call":
        verifier["tool_call_id"] = next(
            e["tool_call_id"] for e in timeline if e.get("step_id") == PRE
        )
    assert _receipt(case) == {}


def test_raw_post_event_uses_the_same_independent_verifier_call():
    case = _case()
    _, _, verifier, result, timeline = case
    timeline.append(
        {
            **verifier,
            "event": "agent.tool.call",
            "detail": "desktop.ui_elements",
            "input_preview": verifier["input"],
            "result": result,
        }
    )
    assert _receipt(case)["verification_predicate_kind"] == "exact_app_search_result_present"
