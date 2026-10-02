#!/usr/bin/env python3
"""Smoke-test Runtime Planner tool choices against executable runtime tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from apps.shell.agent.tools.policy import (
    KNOWN_AGENT_TOOLS,
    TOOL_NAME_ALIASES,
    RuntimePolicyCompiler,
    ToolDescriptorRegistry,
)
from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY
from apps.shell.yachiyo_agent import RuntimePlanner
from apps.shell.yachiyo_agent.planner_execution import planner_tool_requests
from apps.shell.yachiyo_agent.runtime_execution import (
    runtime_execution_envelope_payload,
    runtime_execution_requests_from_envelope_payload,
)

PLANNER_TOOL_PARITY_CASES: tuple[dict[str, Any], ...] = (
    {
        "id": "generic_app_open",
        "category": "orchestrator",
        "prompt": "打开 PixelForge",
        "expected_intent": "desktop_operation",
        "expected_plan_tools": ["desktop.list_apps", "app.open", "desktop.verify"],
        "expected_request_tools": ["desktop.list_apps", "app.open", "desktop.verify"],
        "approval_required": [],
    },
    {
        "id": "app_scoped_ui_click",
        "category": "orchestrator",
        "prompt": "在 Notion 点击 New Page",
        "expected_intent": "desktop_operation",
        "expected_plan_tools": [
            "desktop.inspect_app",
            "app.focus_and_click_ui_element",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.inspect_app",
            "app.focus_and_click_ui_element",
            "desktop.ui_elements",
        ],
        "approval_required": ["app.focus_and_click_ui_element"],
    },
    {
        "id": "app_issue_create",
        "category": "orchestrator",
        "prompt": "打开 Linear，把这个 bug 记录成 issue",
        "expected_intent": "desktop_operation",
        "expected_plan_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "capability_project_task_create",
        "category": "orchestrator",
        "prompt": "打开任意项目管理工具，新建任务：整理发布清单",
        "expected_intent": "desktop_operation",
        "expected_plan_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "app.focus_and_safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "app.focus_and_safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "capability_diagram_app_creation",
        "category": "orchestrator",
        "prompt": "找一个能画流程图的软件，画一个登录流程图",
        "expected_intent": "desktop_operation",
        "expected_plan_tools": [
            "desktop.list_apps",
            "app.open",
            "desktop.ui_elements",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.list_apps",
            "app.open",
            "desktop.ui_elements",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "builtin_data_analysis",
        "category": "orchestrator",
        "prompt": "请分析 data/sales.csv 并输出报告",
        "expected_intent": "data_analysis",
        "expected_plan_tools": ["workspace.read", "data.analyze"],
        "expected_request_tools": ["data.analyze"],
        "approval_required": [],
    },
    {
        "id": "visible_table_analysis",
        "category": "orchestrator",
        "prompt": "分析桌面上这个表格并输出报告",
        "expected_intent": "data_analysis",
        "expected_plan_tools": ["desktop.ui_elements", "data.analyze"],
        "expected_request_tools": ["desktop.ui_elements"],
        "expected_deferred_plan_tools": ["data.analyze"],
        "approval_required": [],
    },
    {
        "id": "visible_table_analysis_to_document_app",
        "category": "orchestrator",
        "prompt": "分析当前窗口里的表格并把报告写进任意文档应用",
        "expected_intent": "data_analysis",
        "expected_plan_tools": [
            "desktop.ui_elements",
            "data.analyze",
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": ["desktop.ui_elements"],
        "expected_deferred_plan_tools": [
            "data.analyze",
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "visible_data_analysis_to_wechat_draft",
        "category": "orchestrator",
        "prompt": "分析当前窗口的数据并生成一份带图表的报告再发到微信",
        "expected_intent": "data_analysis",
        "expected_plan_tools": [
            "desktop.ui_elements",
            "data.analyze",
            "app.focus",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": ["desktop.ui_elements"],
        "expected_deferred_plan_tools": [
            "data.analyze",
            "app.focus",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "current_page_report",
        "category": "orchestrator",
        "prompt": "把当前网页总结成一份报告",
        "expected_intent": "web_research",
        "expected_plan_tools": ["browser.extract_text", "artifact.write"],
        "expected_request_tools": ["browser.extract_text"],
        "expected_deferred_plan_tools": ["artifact.write"],
        "approval_required": [],
    },
    {
        "id": "current_page_summary_to_app",
        "category": "orchestrator",
        "prompt": "把当前网页总结到 Notion 新页面",
        "expected_intent": "report_generation",
        "expected_plan_tools": [
            "browser.extract_text",
            "artifact.write",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": ["browser.extract_text"],
        "expected_deferred_plan_tools": [
            "artifact.write",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "current_page_summary_to_document_app",
        "category": "orchestrator",
        "prompt": "把当前网页总结到任意文档应用",
        "expected_intent": "report_generation",
        "expected_plan_tools": [
            "browser.extract_text",
            "artifact.write",
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": ["browser.extract_text"],
        "expected_deferred_plan_tools": [
            "artifact.write",
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "selected_text_transform_back_to_current_input",
        "category": "orchestrator",
        "prompt": "把当前选择的文字翻译成英文并粘贴回去",
        "expected_intent": "report_generation",
        "expected_plan_tools": [
            "desktop.safe_shortcut",
            "clipboard.read",
            "artifact.write",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "expected_request_tools": ["desktop.safe_shortcut", "clipboard.read"],
        "expected_deferred_plan_tools": [
            "artifact.write",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "current_window_markdown_artifact",
        "category": "orchestrator",
        "prompt": "把当前窗口里的内容复制并保存成 markdown",
        "expected_intent": "report_generation",
        "expected_plan_tools": ["desktop.ui_elements", "artifact.write"],
        "expected_request_tools": ["desktop.ui_elements"],
        "expected_deferred_plan_tools": ["artifact.write"],
        "approval_required": [],
    },
    {
        "id": "current_window_release_notes_artifact",
        "category": "orchestrator",
        "prompt": "把当前窗口里的内容整理成发布说明并保存到 Downloads",
        "expected_intent": "report_generation",
        "expected_plan_tools": ["desktop.ui_elements", "artifact.write"],
        "expected_request_tools": ["desktop.ui_elements"],
        "expected_deferred_plan_tools": ["artifact.write"],
        "approval_required": [],
    },
    {
        "id": "named_media_app_playback",
        "category": "orchestrator",
        "prompt": "open VLC play test",
        "expected_intent": "media_playback",
        "expected_plan_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "media.music_app_open_and_play",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "media.music_app_open_and_play",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "capability_media_app_playback",
        "category": "orchestrator",
        "prompt": "打开任意能播放音乐的应用播放 lo-fi",
        "expected_intent": "media_playback",
        "expected_plan_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "media.music_app_open_and_play",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "desktop.list_apps",
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "media.music_app_open_and_play",
            "desktop.ui_elements",
        ],
        "approval_required": [],
    },
    {
        "id": "clipboard_send_to_slack",
        "category": "orchestrator",
        "prompt": "读取剪贴板内容并发给 Slack 的 yachiyo",
        "expected_intent": "communication",
        "native_full_plan": True,
        "expected_plan_tools": [
            "app.focus",
            "desktop.safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "clipboard.read",
            "desktop.ui_elements",
            "desktop.safe_shortcut",
            "desktop.ui_elements",
            "desktop.submit_foreground",
            "desktop.ui_elements",
        ],
        "expected_request_tools": [
            "app.focus",
            "desktop.safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "clipboard.read",
            "desktop.ui_elements",
            "desktop.safe_shortcut",
            "desktop.ui_elements",
            "desktop.submit_foreground",
            "desktop.ui_elements",
        ],
        "expected_legacy_request_tools": [
            "app.focus",
            "desktop.safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
            "desktop.safe_shortcut",
            "desktop.submit_foreground",
        ],
        "expected_deferred_plan_tools": [],
        "expected_native_bindings": {
            "open-or-focus-app": {
                "input": {"app_name": "Slack"}, "depends_on": [],
            },
            "focus-communication-recipient-search": {
                "input": {"action": "find"}, "depends_on": ["open-or-focus-app"],
            },
            "type-communication-recipient": {
                "input": {"text": "yachiyo"},
                "depends_on": ["focus-communication-recipient-search"],
            },
            "submit-communication-recipient-search": {
                "input": {}, "depends_on": ["type-communication-recipient"],
            },
            "read-clipboard-before-paste-communication-message": {
                "input": {"max_chars": 12000},
                "depends_on": ["submit-communication-recipient-search"],
            },
            "inspect-clipboard-paste-target-paste-communication-message": {
                "input": {"role_filter": "", "limit": 80, "app_name": "Slack"},
                "depends_on": ["read-clipboard-before-paste-communication-message"],
            },
            "paste-communication-message": {
                "input": {"action": "paste"},
                "depends_on": [
                    "read-clipboard-before-paste-communication-message",
                    "inspect-clipboard-paste-target-paste-communication-message",
                ],
            },
            "verify-clipboard-paste-paste-communication-message": {
                "input": {"app_name": "Slack", "role_filter": "", "limit": 80},
                "depends_on": ["paste-communication-message"],
            },
            "send-communication-message": {
                "input": {"action": "send"},
                "depends_on": [
                    "paste-communication-message",
                    "verify-clipboard-paste-paste-communication-message",
                ],
            },
            "verify-communication-message": {
                "input": {"app_name": "Slack", "role_filter": "text", "limit": 80},
                "depends_on": ["send-communication-message"],
            },
        },
        "approval_required": ["desktop.submit_foreground"],
    },
    {
        "id": "system_settings_bluetooth",
        "category": "orchestrator",
        "prompt": "打开系统设置里的蓝牙",
        "expected_intent": "system_control",
        "expected_plan_tools": ["system.settings_open"],
        "expected_request_tools": ["system.settings_open"],
        "approval_required": [],
    },
    {
        "id": "generic_calendar_event",
        "category": "orchestrator",
        "prompt": "打开任意日历应用，安排明天下午三点开会",
        "expected_intent": "schedule",
        "expected_plan_tools": ["calendar.create_event"],
        "expected_request_tools": ["calendar.create_event"],
        "approval_required": [],
    },
    {
        "id": "file_organize_invoices",
        "category": "orchestrator",
        "prompt": "把下载里的发票整理到一个文件夹",
        "expected_intent": "file_organization",
        "expected_plan_tools": [
            "workspace.list",
            "artifact.write",
            "file.organize",
        ],
        "expected_request_tools": ["workspace.list"],
        "expected_deferred_plan_tools": ["artifact.write", "file.organize"],
        "approval_required": ["file.organize"],
    },
    {
        "id": "explicit_terminal_command",
        "category": "coding",
        "prompt": "run ls -la in terminal",
        "expected_intent": "code_task",
        "expected_plan_tools": ["terminal.run"],
        "expected_request_tools": ["terminal.run"],
        "approval_required": ["terminal.run"],
    },
    {
        "id": "code_diagnostic_with_workspace_context",
        "category": "coding",
        "prompt": "修复这个仓库里的 failing tests",
        "expected_intent": "code_task",
        "expected_plan_tools": [
            "workspace.list",
            "terminal.run",
            "workspace.write_patch",
            "terminal.run",
        ],
        "expected_request_tools": ["workspace.list", "terminal.run"],
        "expected_deferred_plan_tools": ["workspace.write_patch", "terminal.run"],
        "approval_required": ["terminal.run"],
    },
    {
        "id": "reminder_creation",
        "category": "orchestrator",
        "prompt": "提醒我明天九点开会",
        "expected_intent": "schedule",
        "expected_plan_tools": ["reminders.create"],
        "expected_request_tools": ["reminders.create"],
        "approval_required": [],
    },
)


def _compiled_policy(category: str) -> dict[str, Any]:
    return RuntimePolicyCompiler().compile_tool_policy(
        category,
        RuntimePolicyCompiler.default_tool_policy(category),
    )


def _descriptor_tools(tools: list[str]) -> list[str]:
    schemas = ToolDescriptorRegistry.model_tool_schemas(tools)
    return [
        TOOL_NAME_ALIASES.get(
            str(schema.get("function", {}).get("name") or "").strip(),
            str(schema.get("function", {}).get("name") or "").strip(),
        )
        for schema in schemas
        if isinstance(schema, dict)
    ]


def _deferred_plan_tools(
    plan_tools: list[str],
    request_tools: list[str],
) -> list[str]:
    if not request_tools:
        return []
    if plan_tools[: len(request_tools)] != request_tools:
        return []
    return plan_tools[len(request_tools) :]


_DEFERRED_VERIFICATION_TOOLS = {
    "desktop.active_window",
    "desktop.ui_elements",
    "desktop.windows",
    "screen.capture",
}


def _is_deferred_verification_only(tools: list[str]) -> bool:
    return bool(tools) and set(tools).issubset(_DEFERRED_VERIFICATION_TOOLS)


def _case_evidence(case: dict[str, Any]) -> dict[str, Any]:
    category = str(case["category"])
    policy = _compiled_policy(category)
    allowed_tools = [str(tool) for tool in policy.get("allowed_tools") or []]
    prompt = str(case["prompt"])
    decision = RuntimePlanner().decision(prompt, allowed_tools=allowed_tools)
    legacy_requests = planner_tool_requests(prompt, allowed_tools)
    requests = legacy_requests
    if case.get("native_full_plan") is True:
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                decision, allowed_tools=allowed_tools, full_plan=True,
            ),
            allowed_tools=allowed_tools,
        )
    plan_tools = [
        str(getattr(step, "tool_name", "") or "").strip()
        for step in decision.plan.tool_plan.steps
        if str(getattr(step, "tool_name", "") or "").strip()
    ]
    request_tools = [
        str(request.get("tool") or "").strip()
        for request in requests
        if str(request.get("tool") or "").strip()
    ]
    expected_plan_tools = [str(tool) for tool in case["expected_plan_tools"]]
    expected_request_tools = [str(tool) for tool in case["expected_request_tools"]]
    expected_deferred_plan_tools = [
        str(tool) for tool in case.get("expected_deferred_plan_tools", [])
    ]
    expected_approval_tools = [str(tool) for tool in case["approval_required"]]
    descriptor_tools = _descriptor_tools(sorted(set(plan_tools + request_tools)))
    deferred_plan_tools = _deferred_plan_tools(plan_tools, request_tools)
    request_continue_to_model = [
        bool(request.get("continue_to_model"))
        for request in requests
        if isinstance(request, dict)
    ]
    approval_required = policy.get("approval_required")
    if not isinstance(approval_required, dict):
        approval_required = {}
    checks = {
        "intent_matches": decision.selected_intent.kind == str(case["expected_intent"]),
        "plan_tools_match": plan_tools == expected_plan_tools,
        "request_tools_match": request_tools == expected_request_tools,
        "deferred_plan_tools_match": deferred_plan_tools == expected_deferred_plan_tools,
        "deferred_followup_boundary_present": (
            not deferred_plan_tools
            or bool(request_continue_to_model and request_continue_to_model[-1])
            or _is_deferred_verification_only(deferred_plan_tools)
        ),
        "plan_tools_registered": all(tool in KNOWN_AGENT_TOOLS for tool in plan_tools),
        "request_tools_dispatched": all(tool in TOOL_DISPATCH_REGISTRY for tool in request_tools),
        "tools_have_model_descriptors": set(plan_tools + request_tools).issubset(
            set(descriptor_tools)
        ),
        "request_tools_allowed_by_policy": all(tool in allowed_tools for tool in request_tools),
        "approval_required_matches": all(
            bool(approval_required.get(tool)) for tool in expected_approval_tools
        )
        and all(
            not bool(approval_required.get(tool))
            for tool in request_tools
            if tool not in expected_approval_tools
        ),
    }
    native_bindings = []
    if case.get("native_full_plan") is True:
        native_bindings = [
            {
                "step_id": request.get("step_id"),
                "input": request.get("input"),
                "depends_on": request.get("depends_on") or [],
            }
            for request in requests
        ]
        checks["native_declared_chain_matches"] = native_bindings == [
            {"step_id": step, **binding}
            for step, binding in case["expected_native_bindings"].items()
        ]
        checks["legacy_projection_matches"] = [
            request.get("tool") for request in legacy_requests
        ] == case["expected_legacy_request_tools"]
    return {
        "id": str(case["id"]),
        "ok": all(checks.values()),
        "category": category,
        "prompt": prompt,
        "intent_kind": decision.selected_intent.kind,
        "plan_tools": plan_tools,
        "request_tools": request_tools,
        "request_mode": (
            "native_full_plan" if case.get("native_full_plan") is True else "legacy_projection"
        ),
        "native_request_bindings": native_bindings,
        "legacy_request_tools": [request.get("tool") for request in legacy_requests],
        "deferred_plan_tools": deferred_plan_tools,
        "request_continue_to_model": request_continue_to_model,
        "descriptor_tools": descriptor_tools,
        "approval_required_tools": [
            tool for tool in request_tools if bool(approval_required.get(tool))
        ],
        "checks": checks,
    }


def run_smoke() -> dict[str, Any]:
    cases = [_case_evidence(case) for case in PLANNER_TOOL_PARITY_CASES]
    return {
        "ok": all(case["ok"] for case in cases),
        "mode": "planner_runtime_tool_parity_smoke",
        "case_count": len(cases),
        "cases": cases,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-json", type=Path, help="Optional JSON evidence report path.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    evidence = run_smoke()
    if args.report_json is not None:
        _write_report(args.report_json, evidence)
        print(f"planner runtime tool parity smoke report: {args.report_json}", file=sys.stderr)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
