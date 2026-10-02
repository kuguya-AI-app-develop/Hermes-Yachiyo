from __future__ import annotations

import pytest

from apps.shell.agent.runtime.action_targets import bind_planned_action_target
from apps.shell.yachiyo_agent import runtime_execution as runtime_execution_module
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_from_decision,
    runtime_execution_requests_from_envelope_payload,
)
from apps.shell.yachiyo_agent.runtime_planner import RuntimePlanner
from apps.shell.yachiyo_agent.daily_desktop import daily_desktop_allowed_tools
from apps.shell.yachiyo_agent.planner_execution import planner_decision_and_tool_requests
from apps.shell.yachiyo_agent.planner_execution import _desktop_observation_step_is_direct_readback


@pytest.mark.parametrize("prompt", ["打开 ChatGPT 客户端", "打开微信在消息框输入文件传输助手"])
def test_execution_projection_preserves_canonical_app_and_exact_ui_target(prompt: str) -> None:
    allowed_tools = daily_desktop_allowed_tools()
    decision, _requests = planner_decision_and_tool_requests(prompt, allowed_tools)
    envelope = runtime_execution_envelope_from_decision(
        decision, allowed_tools=allowed_tools, full_plan=True,
    )

    assert envelope is not None
    if "消息框" in prompt:
        action = next(request for request in envelope.requests if "type_into_ui_element" in request.tool_name)
        assert action.input["target"] == "消息框"
        assert action.action_target["target"] == "消息框"
    else:
        action = next(request for request in envelope.requests if request.tool_name == "app.open")
        assert action.input["app_name"] == "ChatGPT 客户端"
        assert action.action_target["app_name"] == "ChatGPT 客户端"


@pytest.mark.parametrize("prompt", ["what app am I using?", "what windows are open in Slack"])
def test_identity_readback_does_not_require_model_followup(prompt: str) -> None:
    _decision, requests = planner_decision_and_tool_requests(prompt, daily_desktop_allowed_tools())

    assert requests
    assert not any(request.get("continue_to_model") for request in requests)


def test_identity_readback_does_not_bypass_requested_analysis() -> None:
    assert _desktop_observation_step_is_direct_readback(
        "what app am I using and summarize its content",
        {"operation_hint": "read_active_window"},
    ) is False


def test_dispatch_shortcut_binds_exact_keyboard_copy_identity() -> None:
    target = bind_planned_action_target(
        {
            "action": "dispatch_shortcut",
            "shortcut_action": "copy",
            "app_name": "Slack",
            "target": "Message",
        },
        {
            "action": "keyboard_shortcut",
            "shortcut_action": "copy",
            "key": "c",
            "modifiers": ["cmd"],
            "app_name": "slack",
            "target": "message",
        },
        capability_id="clipboard.read_write",
        source_step_id="copy-selected-text",
        tool_name="desktop.safe_shortcut",
        runtime_stage="operate",
    )

    assert target["action"] == "dispatch_shortcut"
    assert target["shortcut_action"] == "copy"
    assert target["key"] == "c"
    assert target["modifiers"] == ["command"]


@pytest.mark.parametrize(
    "conflict",
    [
        pytest.param({"shortcut_action": "paste"}, id="wrong-action"),
        pytest.param({"modifiers": ["option"]}, id="wrong-modifier"),
        pytest.param({"app_name": "Notes"}, id="wrong-app"),
    ],
)
def test_dispatch_shortcut_rejects_mismatched_keyboard_identity(
    conflict: dict[str, object],
) -> None:
    planned = {
        "action": "dispatch_shortcut",
        "shortcut_action": "copy",
        "app_name": "Slack",
        "target": "Message",
    }
    projected = {
        "action": "keyboard_shortcut",
        "shortcut_action": "copy",
        "key": "c",
        "modifiers": ["command"],
        "app_name": "Slack",
        "target": "Message",
        **conflict,
    }

    with pytest.raises(ValueError, match="runtime_execution_action_target_conflict"):
        bind_planned_action_target(
            planned,
            projected,
            capability_id="clipboard.read_write",
            source_step_id="copy-selected-text",
            tool_name="desktop.safe_shortcut",
            runtime_stage="operate",
        )


def test_discovered_app_scope_does_not_replace_semantic_in_app_query() -> None:
    target = bind_planned_action_target(
        {
            "kind": "desktop_ui",
            "action": "submit_ui",
            "query": "logo 模板",
            "target": "搜索",
            "role_filter": "text",
        },
        {
            "kind": "desktop_app",
            "action": "submit_ui",
            "selection_source": "desktop.list_apps",
            "app_name": "<selected app from desktop.list_apps>",
            "query": "image",
        },
        capability_id="desktop.ui_operation",
        source_step_id="submit-app-search",
        tool_name="desktop.search_submit",
        runtime_stage="operate",
    )

    assert target == {
        "kind": "desktop_app",
        "action": "submit_ui",
        "query": "logo 模板",
        "target": "搜索",
        "role_filter": "text",
        "selection_source": "desktop.list_apps",
        "app_name": "<selected app from desktop.list_apps>",
        "step_id": "submit-app-search",
        "selection_query": "image",
    }


def test_direct_app_scope_cannot_replace_semantic_in_app_query() -> None:
    with pytest.raises(ValueError, match="runtime_execution_action_target_conflict"):
        bind_planned_action_target(
            {
                "kind": "desktop_ui",
                "action": "submit_ui",
                "query": "logo 模板",
                "target": "搜索",
            },
            {
                "kind": "desktop_app",
                "action": "submit_ui",
                "selection_source": "direct_app_name",
                "app_name": "Notes",
                "query": "Notes",
            },
            capability_id="desktop.ui_operation",
            source_step_id="submit-app-search",
            tool_name="desktop.search_submit",
            runtime_stage="operate",
        )


def test_inspect_app_execution_target_uses_goal_contract_semantics() -> None:
    allowed_tools = ["desktop.list_apps", "desktop.inspect_app"]
    decision = RuntimePlanner().decision(
        "打开 PixelForge 并读取界面",
        allowed_tools=allowed_tools,
    )

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    inspect_request = next(
        request
        for request in envelope.requests
        if request.tool_name == "desktop.inspect_app"
    )
    assert inspect_request.step_id == "inspect-app"
    assert inspect_request.action_target["action"] == "inspect_app"
    assert inspect_request.action_target["app_name"] == "PixelForge"
    assert inspect_request.action_target["limit"] == 80


def test_unscoped_screen_capture_uses_desktop_discovery_target() -> None:
    allowed_tools = ["screen.capture"]
    decision = RuntimePlanner().decision(
        "截个图看看",
        allowed_tools=allowed_tools,
    )

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    assert len(envelope.requests) == 1
    capture = envelope.requests[0]
    assert capture.tool_name == "screen.capture"
    assert capture.action_target == {
        "kind": "desktop_discovery",
        "action": "capture_screen",
        "selection_source": "screen.capture",
        "target_scope": "foreground",
        "step_id": "capture-screen",
    }


@pytest.mark.parametrize(
    ("prompt", "tool_name", "expected_action"),
    [
        ("Slack 开着吗", "app.status", "status_app"),
        ("显示 Slack", "app.show", "show_app"),
        ("隐藏 Slack", "app.hide", "hide_app"),
        ("最小化 Slack", "app.minimize", "minimize_app"),
        ("退出 Slack", "app.quit", "quit_app"),
    ],
)
def test_app_management_execution_target_uses_planner_action_ontology(
    prompt: str,
    tool_name: str,
    expected_action: str,
) -> None:
    allowed_tools = ["desktop.list_apps", tool_name, "desktop.running_apps"]
    decision = RuntimePlanner().decision(prompt, allowed_tools=allowed_tools)

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    management = next(
        request for request in envelope.requests if request.step_id == "manage-app"
    )
    assert management.tool_name == tool_name
    assert management.action_target["kind"] == "desktop_app"
    assert management.action_target["action"] == expected_action
    assert management.action_target["app_name"] == "Slack"


@pytest.mark.parametrize(
    ("prompt", "allowed_tools"),
    [
        (
            "打开 PixelForge",
            ["desktop.list_apps", "app.open", "desktop.verify"],
        ),
        (
            "打开 PixelForge 并读取界面",
            ["desktop.list_apps", "desktop.inspect_app"],
        ),
        (
            "找一个能编辑 PDF 的本机应用并打开它",
            ["desktop.list_apps", "app.open", "desktop.verify"],
        ),
        (
            "打开一个能编辑 PDF 的应用并打开 Downloads/report.pdf",
            [
                "desktop.list_apps",
                "desktop.open_path_with_app",
                "desktop.ui_elements",
            ],
        ),
    ],
)
def test_desktop_smoke_source_target_matches_goal_contract(
    prompt: str,
    allowed_tools: list[str],
) -> None:
    decision = RuntimePlanner().decision(prompt, allowed_tools=allowed_tools)
    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    task_core = decision.plan.task_core.model_dump()  # type: ignore[union-attr]
    criteria = task_core["goal_contract"]["criteria"]
    assert len(criteria) == 1
    criterion = criteria[0]
    source_step_id = criterion["source_step_ids"][0]
    expected_target = criterion["expected"]["target"]
    source_request = next(
        request for request in envelope.requests if request.step_id == source_step_id
    )

    for key, value in expected_target.items():
        assert source_request.action_target.get(key) == value


def test_runtime_execution_projection_skips_blocked_desktop_routes() -> None:
    envelope = {
        "envelope_id": "execution-envelope-test",
        "requests": [
            {
                "request_id": "blocked-open",
                "tool_name": "app.open",
                "input": {"app_name": "Music"},
                "status": "planned",
                "desktop_execution_route": {
                    "status": "real_virtual_desktop_provider_required",
                    "can_execute": False,
                    "blocking_conditions": [
                        "loopback_desktop_backend",
                        "real_virtual_desktop_backend_required",
                    ],
                },
            },
            {
                "request_id": "safe-discovery",
                "tool_name": "desktop.list_apps",
                "input": {"query": "Music"},
                "status": "planned",
                "desktop_execution_route": {
                    "status": "sandbox_ready",
                    "can_execute": True,
                    "blocking_conditions": [],
                },
            },
        ],
    }

    projected = runtime_execution_requests_from_envelope_payload(
        envelope,
        allowed_tools=["app.open", "desktop.list_apps"],
    )

    assert [request["tool"] for request in projected] == ["desktop.list_apps"]
    assert projected[0]["request_id"] == "safe-discovery"
    assert projected[0]["desktop_execution_route"]["status"] == "sandbox_ready"


def test_runtime_execution_projection_preserves_blocked_semantic_click_as_followup() -> None:
    envelope = {
        "envelope_id": "execution-envelope-click",
        "requests": [
            {
                "request_id": "discover-chrome",
                "step_id": "discover-app",
                "tool_name": "desktop.list_apps",
                "input": {"query": "Chrome", "limit": 20},
                "status": "planned",
                "desktop_execution_route": {"status": "ready", "can_execute": True},
            },
            {
                "request_id": "inspect-chrome",
                "step_id": "inspect-app",
                "tool_name": "desktop.inspect_app",
                "input": {"app_name": "Chrome", "focus": True},
                "status": "planned",
                "desktop_execution_route": {
                    "status": "provider_required",
                    "can_execute": False,
                },
            },
            {
                "request_id": "click-login",
                "step_id": "operate-foreground-ui",
                "tool_name": "app.focus_and_click_ui_element",
                "input": {
                    "app_name": "Chrome",
                    "target": "登录",
                    "role_filter": "button",
                    "limit": 80,
                    "click_count": 1,
                },
                "approval_required": True,
                "depends_on": ["inspect-app"],
                "status": "planned",
            },
            {
                "request_id": "verify-click",
                "step_id": "verify-desktop-result",
                "tool_name": "desktop.ui_elements",
                "input": {"app_name": "Chrome", "role_filter": "button"},
                "depends_on": ["operate-foreground-ui"],
                "status": "planned",
            },
        ],
    }

    projected = runtime_execution_requests_from_envelope_payload(
        envelope,
        allowed_tools=[
            "desktop.list_apps",
            "desktop.inspect_app",
            "app.focus_and_click_ui_element",
            "desktop.ui_elements",
        ],
    )

    assert [request["tool"] for request in projected] == ["desktop.list_apps"]
    assert projected[0]["continue_to_model"] is True
    assert projected[0]["followup_target"] == {
        "kind": "desktop_discovered_app_action",
        "app_query": "Chrome",
        "app_name_source": "desktop.list_apps",
        "target_action": "click",
        "target": "登录",
        "role_filter": "button",
        "limit": 80,
        "click_count": 1,
    }


def test_full_web_search_envelope_preserves_summary_for_model_synthesis() -> None:
    allowed_tools = ["browser.search"]
    decision = RuntimePlanner().decision(
        "打开任意浏览器搜索 Hermes agent architecture 并总结",
        allowed_tools=allowed_tools,
    )

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    assert len(envelope.requests) == 1
    request = envelope.requests[0]
    assert request.tool_name == "browser.search"
    assert request.input == {"query": "Hermes agent architecture"}
    assert request.presentation == "summary"
    assert request.continue_to_model is True


def test_runtime_execution_projection_keeps_planned_background_provider_until_health_check() -> None:
    envelope = {
        "envelope_id": "execution-envelope-background-provider",
        "requests": [
            {
                "request_id": "verify-pixelforge",
                "step_id": "verify-app",
                "tool_name": "desktop.verify",
                "input": {
                    "app_name": "PixelForge",
                    "verification_goal": "app_running",
                    "limit": 80,
                },
                "status": "planned",
                "desktop_execution_policy": {
                    "mode": "preview",
                    "prefer_background_desktop": True,
                },
                "desktop_execution_route": {
                    "route_id": "desktop-route:desktop.verify",
                    "tool_name": "desktop.verify",
                    "requested_mode": "preview",
                    "selected_provider_kind": "background_desktop",
                    "selected_provider_id": "cua-driver",
                    "status": "provider_required",
                    "can_execute": False,
                    "blocking_conditions": ["sandbox_desktop_provider_required"],
                },
                "sandbox_provider": {
                    "provider_kind": "background_desktop",
                    "provider_id": "cua-driver",
                    "status": "installed_not_checked",
                    "health": {
                        "checked": False,
                        "status": "not_checked",
                    },
                },
            }
        ],
    }

    projected = runtime_execution_requests_from_envelope_payload(
        envelope,
        allowed_tools=["desktop.verify"],
    )

    assert [request["tool"] for request in projected] == ["desktop.verify"]
    assert projected[0]["desktop_execution_route"]["status"] == "provider_required"
    assert projected[0]["desktop_execution_route"]["selected_provider_kind"] == (
        "background_desktop"
    )
    assert projected[0]["desktop_execution_route"]["selected_provider_id"] == (
        "cua-driver"
    )
    assert projected[0]["sandbox_provider"]["status"] == "installed_not_checked"
    assert projected[0]["sandbox_provider"]["health"]["checked"] is False


def test_canonical_plan_step_rejects_request_binding_override() -> None:
    allowed_tools = [
        "browser.search",
        "browser.extract_text",
        "artifact.write",
        "app.open",
        "desktop.safe_type_text",
        "desktop.verify",
    ]
    decision = RuntimePlanner().decision(
        "搜索上海明天天气，并把结果写入备忘录",
        allowed_tools=allowed_tools,
    )
    steps = {
        step.step_id: step
        for step in decision.plan.tool_plan.steps
    }
    sink = steps["insert-research-into-target-app"]

    with pytest.raises(ValueError, match="runtime_execution_input_bindings_conflict"):
        runtime_execution_module._execution_request_snapshot(
            {
                "tool": sink.tool_name,
                "step_id": sink.step_id,
                "input": dict(sink.input_preview),
                "input_bindings": [
                    {
                        "binding_id": "forged-binding",
                        "source_step_id": "model-selected-source",
                        "source_tool_name": "browser.extract_text",
                        "source_result_path": "/data/text",
                        "target_input_path": "/input/text",
                        "value_type": "string",
                        "required": True,
                        "max_bytes": 4096,
                    }
                ],
            },
            index=1,
            decision=decision,
            steps=steps,
        )


@pytest.mark.parametrize(
    "prompt",
    [
        "打开 Notes，然后搜索 hello",
        "打开 Notes 搜索 hello 并确认",
    ],
)
def test_full_plan_direct_app_search_verifier_is_terminal_without_model(
    prompt: str,
) -> None:
    allowed_tools = [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
    ]
    decision = RuntimePlanner().decision(prompt, allowed_tools=allowed_tools)

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == "verify-desktop-result"
    )
    assert verifier.runtime_stage == "verify"
    assert [
        target["step_id"] for target in verifier.task_verification_targets
    ] == ["submit-app-search"]
    assert verifier.continue_to_model is False


def test_full_plan_suffixed_desktop_verifier_is_terminal_without_model() -> None:
    allowed_tools = [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
        "desktop.active_window",
    ]
    decision = RuntimePlanner().decision(
        "打开 Notes，然后搜索 hello",
        allowed_tools=allowed_tools,
    )
    old_step_id = "verify-desktop-result"
    verifier_step_id = "verify-desktop-result-2"
    steps = [
        step.model_copy(
            update={
                "step_id": verifier_step_id,
                "tool_name": "desktop.active_window",
                "input_preview": {},
            }
        )
        if step.step_id == old_step_id
        else step
        for step in decision.plan.tool_plan.steps
    ]
    task_core = decision.plan.task_core
    todos = [
        todo.model_copy(update={"step_id": verifier_step_id})
        if todo.step_id == old_step_id
        else todo
        for todo in task_core.todos
    ]
    workspace = task_core.workspace.model_copy(
        update={
            "items": [
                item.model_copy(update={"source_step_id": verifier_step_id})
                if item.source_step_id == old_step_id
                else item
                for item in task_core.workspace.items
            ]
        }
    )
    goal_contract = task_core.goal_contract
    assert goal_contract is not None
    criteria = [
        criterion.model_copy(
            update={
                "verifier_step_ids": [
                    verifier_step_id if value == old_step_id else value
                    for value in criterion.verifier_step_ids
                ]
            }
        )
        for criterion in goal_contract.criteria
    ]
    task_core = task_core.model_copy(
        update={
            "todos": todos,
            "workspace": workspace,
            "goal_contract": goal_contract.model_copy(update={"criteria": criteria}),
        }
    )
    plan = decision.plan.model_copy(
        update={
            "tool_plan": decision.plan.tool_plan.model_copy(update={"steps": steps}),
            "task_core": task_core,
        }
    )
    decision = decision.model_copy(update={"plan": plan})

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == verifier_step_id
    )
    assert verifier.tool_name == "desktop.active_window"
    assert verifier.runtime_stage == "verify"
    assert verifier.continue_to_model is False


def test_full_plan_clipboard_readback_preserves_terminal_policy() -> None:
    allowed_tools = ["clipboard.write", "clipboard.read"]
    decision = RuntimePlanner().decision(
        "把 hello 写入剪贴板",
        allowed_tools=allowed_tools,
    )

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == "verify-clipboard-write"
    )
    assert verifier.tool_name == "clipboard.read"
    assert verifier.runtime_stage == "verify"
    assert verifier.continue_to_model is False


def test_full_plan_desktop_verifier_without_goal_contract_keeps_model_followup() -> None:
    allowed_tools = [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
    ]
    decision = RuntimePlanner().decision(
        "打开 Notes，然后搜索 hello",
        allowed_tools=allowed_tools,
    )
    task_core = decision.plan.task_core.model_copy(update={"goal_contract": None})
    plan = decision.plan.model_copy(update={"task_core": task_core})
    decision = decision.model_copy(update={"plan": plan})

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == "verify-desktop-result"
    )
    assert verifier.continue_to_model is True


def test_full_plan_desktop_verifier_with_unbound_goal_contract_keeps_model_followup() -> None:
    allowed_tools = [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
    ]
    decision = RuntimePlanner().decision(
        "打开 Notes，然后搜索 hello",
        allowed_tools=allowed_tools,
    )
    task_core = decision.plan.task_core
    goal_contract = task_core.goal_contract
    assert goal_contract is not None
    unrelated_criteria = [
        criterion.model_copy(
            update={"source_step_ids": [], "verifier_step_ids": []}
        )
        for criterion in goal_contract.criteria
    ]
    task_core = task_core.model_copy(
        update={
            "goal_contract": goal_contract.model_copy(
                update={"criteria": unrelated_criteria}
            )
        }
    )
    plan = decision.plan.model_copy(update={"task_core": task_core})
    decision = decision.model_copy(update={"plan": plan})

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == "verify-desktop-result"
    )
    assert verifier.continue_to_model is True


def test_full_plan_desktop_verifier_rejects_foreign_goal_contract_target() -> None:
    allowed_tools = [
        "desktop.list_apps",
        "app.open_and_safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
    ]
    decision = RuntimePlanner().decision(
        "打开 Notes，然后搜索 hello",
        allowed_tools=allowed_tools,
    )
    foreign_decision = RuntimePlanner().decision(
        "打开 Finder，然后搜索 world",
        allowed_tools=allowed_tools,
    )
    task_core = decision.plan.task_core
    foreign_goal_contract = foreign_decision.plan.task_core.goal_contract
    assert foreign_goal_contract is not None
    assert foreign_goal_contract.criteria[0].source_step_ids == ["submit-app-search"]
    assert foreign_goal_contract.criteria[0].verifier_step_ids == [
        "verify-desktop-result"
    ]
    assert foreign_goal_contract.criteria[0].expected["target"] != (
        task_core.goal_contract.criteria[0].expected["target"]
    )
    task_core = task_core.model_copy(
        update={"goal_contract": foreign_goal_contract}
    )
    plan = decision.plan.model_copy(update={"task_core": task_core})
    decision = decision.model_copy(update={"plan": plan})

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    verifier = next(
        request
        for request in envelope.requests
        if request.step_id == "verify-desktop-result"
    )
    assert verifier.continue_to_model is True


@pytest.mark.parametrize(
    ("prompt", "allowed_tools", "step_id", "tool_name"),
    [
        pytest.param(
            "打开 Notes 搜索 hello，然后判断结果里是否有 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "read-desktop-content",
            "desktop.ui_elements",
            id="read-ui-and-judge",
        ),
        pytest.param(
            "打开 Notes 搜索 hello，然后看看结果里有没有 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "read-desktop-content",
            "desktop.ui_elements",
            id="read-ui-and-see-if",
        ),
        pytest.param(
            "打开 Notes 搜索 hello，然后检查结果是否包含 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "verify-desktop-result",
            "desktop.ui_elements",
            id="verify-ui-and-check-result",
        ),
        pytest.param(
            "打开 Notes 搜索 hello，然后确认结果里有没有 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "verify-desktop-result",
            "desktop.ui_elements",
            id="verify-ui-and-confirm-result",
        ),
        pytest.param(
            "打开 Notes 搜索 hello，然后验证结果里有 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "verify-desktop-result",
            "desktop.ui_elements",
            id="verify-ui-and-validate-result",
        ),
        pytest.param(
            "打开 Notes 搜索 hello，然后比较结果和 world",
            [
                "desktop.list_apps",
                "app.open_and_safe_shortcut",
                "desktop.safe_type_text",
                "desktop.search_submit",
                "desktop.ui_elements",
            ],
            "verify-desktop-result",
            "desktop.ui_elements",
            id="verify-ui-and-compare-result",
        ),
        pytest.param(
            "打开 Notes，读取界面并总结",
            ["desktop.list_apps", "app.open", "screen.capture"],
            "capture-screen",
            "screen.capture",
            id="capture-and-summarize",
        ),
    ],
)
def test_full_plan_semantic_desktop_observation_keeps_model_followup(
    prompt: str,
    allowed_tools: list[str],
    step_id: str,
    tool_name: str,
) -> None:
    decision = RuntimePlanner().decision(prompt, allowed_tools=allowed_tools)

    envelope = runtime_execution_envelope_from_decision(
        decision,
        allowed_tools=allowed_tools,
        full_plan=True,
    )

    assert envelope is not None
    observation = next(
        request for request in envelope.requests if request.step_id == step_id
    )
    assert observation.tool_name == tool_name
    assert observation.continue_to_model is True
