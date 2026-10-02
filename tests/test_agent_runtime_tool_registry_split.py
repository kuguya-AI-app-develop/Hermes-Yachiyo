"""Tests for the split ToolBroker dispatch registry."""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import zipfile

import pytest

from apps.shell.agent.tools import browser as browser_mod
from apps.shell.agent.tools import desktop as desktop_mod
from apps.shell.agent.runtime.errors import AgentRuntimeError
from apps.shell.agent.runtime.events import (
    RUNTIME_EXECUTION_PROVENANCE_KEY,
    RUNTIME_EXECUTION_PROVENANCE_VERSION,
    RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
)
from apps.shell.agent.runtime.outcome_evaluator import evaluate_main_chat_outcome
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.tools.policy import (
    DAILY_DESKTOP_TOOL_NAMES,
    HIGH_RISK_AGENT_TOOLS,
    KNOWN_AGENT_TOOLS,
    RuntimePolicyCompiler,
    ToolDescriptorRegistry,
)
from apps.shell.agent.tools.plugins import (
    RestrictedPluginTool,
    RestrictedToolPlugin,
    RestrictedToolPluginManager,
    clear_restricted_tool_plugins,
    list_restricted_plugin_tools,
    register_restricted_tool_plugin,
    restricted_plugin_tool_risk,
    unregister_restricted_tool_plugin,
)
from apps.shell.agent.tools.registry import TOOL_DISPATCH_REGISTRY, dispatch_tool_call


@pytest.fixture(autouse=True)
def _clear_plugin_tools(monkeypatch):
    if hasattr(desktop_mod, "_desktop_runtime_blocking_conditions"):
        monkeypatch.setattr(desktop_mod, "_desktop_runtime_blocking_conditions", lambda **_kwargs: {})
    if hasattr(desktop_mod, "_desktop_session_locked_by_runtime_probe"):
        monkeypatch.setattr(desktop_mod, "_desktop_session_locked_by_runtime_probe", lambda: False)
    clear_restricted_tool_plugins()
    yield
    clear_restricted_tool_plugins()


def _broker(tmp_path):
    return ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": ["."]},
        tmp_path / "artifacts",
    )


def _stub_active_window(monkeypatch, app_name: str) -> None:
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": app_name, "title": ""},
        },
    )


def _write_app_bundle(app_dir, name: str, info: dict) -> None:
    contents = app_dir / f"{name}.app" / "Contents"
    contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


def test_tool_dispatch_registry_covers_known_agent_tools() -> None:
    assert set(TOOL_DISPATCH_REGISTRY) == KNOWN_AGENT_TOOLS


def test_local_app_open_fails_closed_for_background_delivery_hint() -> None:
    class NoLocalOpenBroker:
        @staticmethod
        def app_open(_app_name: str) -> dict:
            raise AssertionError("background-only app.open must not use the local broker")

    result = dispatch_tool_call(
        NoLocalOpenBroker(),
        "app.open",
        {"app_name": "TextEdit", "bring_to_front": False},
    )

    assert result["ok"] is False
    assert result["status"] == "provider_required"
    assert result["blocked_by_desktop_execution_policy"] is True
    assert result["blocking_conditions"] == ["sandbox_desktop_provider_required"]


def test_tool_broker_call_uses_split_registry_for_workspace_read(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    broker = _broker(tmp_path)

    assert broker.call("workspace.read", {"path": "note.txt"}) == {
        "ok": True,
        "path": "note.txt",
        "content": "hello",
        "truncated": False,
        "size_bytes": 5,
        "content_bytes": 5,
        "decoding_lossy": False,
    }


def test_workspace_read_reports_authoritative_truncation_metadata(tmp_path) -> None:
    (tmp_path / "large.txt").write_bytes(b"a" * 200_001)
    broker = _broker(tmp_path)

    result = broker.call("workspace.read", {"path": "large.txt"})

    assert result == {
        "ok": True,
        "path": "large.txt",
        "content": "a" * 200_000,
        "truncated": True,
        "size_bytes": 200_001,
        "content_bytes": 200_000,
        "decoding_lossy": False,
    }


def test_workspace_read_reports_lossy_utf8_without_breaking_preview(tmp_path) -> None:
    (tmp_path / "invalid.txt").write_bytes(b"ok\xff")
    broker = _broker(tmp_path)

    result = broker.call("workspace.read", {"path": "invalid.txt"})

    assert result == {
        "ok": True,
        "path": "invalid.txt",
        "content": "ok\ufffd",
        "truncated": False,
        "size_bytes": 3,
        "content_bytes": 3,
        "decoding_lossy": True,
    }


def test_desktop_verify_app_running_uses_status_without_foreground_inspection() -> None:
    calls: list[tuple[str, str]] = []

    class StatusOnlyBroker:
        def app_status(self, app_name: str) -> dict:
            calls.append(("app_status", app_name))
            return {
                "ok": True,
                "action": "app.status",
                "summary": f"{app_name} is running",
                "data": {
                    "app_name": app_name,
                    "running": True,
                    "status": "running",
                },
                "permission_error": False,
            }

        def desktop_inspect_app(self, *_args, **_kwargs):
            raise AssertionError("app_running verification must not inspect or focus UI")

        def desktop_active_window(self):
            raise AssertionError("app_running verification must not require foreground state")

    result = dispatch_tool_call(
        StatusOnlyBroker(),
        "desktop.verify",
        {
            "app_name": "WPS Office",
            "verification_goal": "app_running",
        },
    )

    assert calls == [("app_status", "WPS Office")]
    assert result["ok"] is True
    assert result["action"] == "desktop.verify"
    assert result["running"] is True
    assert result["launch_verified"] is True
    assert result["data"]["running"] is True
    assert result["data"]["launch_verified"] is True


@pytest.mark.parametrize(
    ("status_result", "expected_running", "expected_reason"),
    [
        (
            {
                "ok": True,
                "action": "app.status",
                "summary": "WPS Office is not running",
                "data": {
                    "app_name": "WPS Office",
                    "running": False,
                    "status": "not_running",
                },
                "permission_error": False,
            },
            False,
            "desktop_verification_failed",
        ),
        (
            {
                "ok": False,
                "action": "app.status",
                "summary": "app.status failed",
                "error": "status query failed",
                "data": {"app_name": "WPS Office"},
                "permission_error": False,
            },
            None,
            "desktop_tool_failed",
        ),
        (
            {
                "ok": True,
                "action": "app.status",
                "summary": "status response was incomplete",
                "data": {"app_name": "WPS Office"},
                "permission_error": False,
            },
            None,
            "desktop_verification_failed",
        ),
    ],
)
def test_desktop_verify_app_running_fails_closed(
    status_result: dict,
    expected_running: bool | None,
    expected_reason: str,
) -> None:
    class StatusBroker:
        def app_status(self, _app_name: str) -> dict:
            return status_result

    result = dispatch_tool_call(
        StatusBroker(),
        "desktop.verify",
        {
            "app_name": "WPS Office",
            "verification_goal": "app_running",
        },
    )
    outcome = evaluate_main_chat_outcome(
        {},
        [
            {
                "event_type": "agent.tool.call",
                "payload": {"tool": "desktop.verify", "result": result},
            }
        ],
    )

    assert result["running"] is expected_running
    assert result["launch_verified"] is expected_running
    assert result["data"]["launch_verified"] is expected_running
    assert outcome.allows_completion is False
    assert outcome.reason == expected_reason


def test_desktop_verify_without_app_running_goal_keeps_ui_inspection_semantics() -> None:
    calls: list[tuple] = []

    class InspectingBroker:
        def app_status(self, _app_name: str):
            raise AssertionError("default desktop verification must not switch to app.status")

        def desktop_inspect_app(self, app_name: str, **kwargs):
            calls.append(("desktop_inspect_app", app_name, kwargs))
            return {
                "ok": True,
                "action": "desktop.inspect_app",
                "summary": "Visible controls inspected",
                "data": {"app_name": app_name, "elements": []},
            }

    result = dispatch_tool_call(
        InspectingBroker(),
        "desktop.verify",
        {"app_name": "WPS Office", "role_filter": "button", "limit": 40},
    )

    assert calls == [
        (
            "desktop_inspect_app",
            "WPS Office",
            {
                "open_if_needed": False,
                "focus": False,
                "role_filter": "button",
                "limit": 40,
            },
        )
    ]
    assert result["ok"] is True
    assert result["action"] == "desktop.verify"
    assert result["summary"] == "Visible controls inspected"


def test_tool_broker_call_uses_fs_read_file_alias(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    broker = _broker(tmp_path)

    assert broker.call("fs.read_file", {"path": "note.txt"}) == {
        "ok": True,
        "path": "note.txt",
        "content": "hello",
        "truncated": False,
        "size_bytes": 5,
        "content_bytes": 5,
        "decoding_lossy": False,
    }


def test_tool_broker_call_passes_workspace_list_filters(tmp_path) -> None:
    (tmp_path / "Screen Shot 1.png").write_text("png", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    broker = _broker(tmp_path)

    result = broker.call(
        "workspace.list",
        {
            "path": ".",
            "pattern": "*.{png,jpg,jpeg}",
            "file_type": "screenshot",
        },
    )

    assert result["ok"] is True
    assert result["entries"] == [{"name": "Screen Shot 1.png", "type": "file"}]
    assert result["filter"] == {
        "pattern": "*.{png,jpg,jpeg}",
        "file_type": "screenshot",
        "expanded_patterns": ["*.png", "*.jpg", "*.jpeg"],
    }


def test_tool_broker_call_passes_fs_find_files_filters(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text("csv", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("text", encoding="utf-8")
    broker = _broker(tmp_path)

    result = broker.call(
        "fs.find_files",
        {
            "path": ".",
            "pattern": "*.csv",
            "file_type": "csv",
        },
    )

    assert result["ok"] is True
    assert result["entries"] == [{"name": "sales.csv", "type": "file"}]
    assert result["filter"] == {
        "pattern": "*.csv",
        "file_type": "csv",
        "expanded_patterns": ["*.csv"],
    }


def test_tool_dispatch_registry_routes_python_run_through_terminal(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    calls = []

    monkeypatch.setattr(
        broker,
        "terminal_run",
        lambda command, *, approved=False, timeout_seconds=30, shell=False: calls.append(
            (command, approved, timeout_seconds, shell)
        )
        or {"ok": bool(approved), "approval_required": not approved},
    )

    approval = dispatch_tool_call(
        broker,
        "python.run",
        {"code": "print('ok')", "timeout_seconds": 45},
    )
    result = dispatch_tool_call(
        broker,
        "python.run",
        {"code": "print('ok')", "timeout_seconds": 45},
        approved=True,
    )

    assert approval == {
        "ok": False,
        "approval_required": True,
        "tool": "python.run",
        "alias_for": "terminal.run",
    }
    assert result == {
        "ok": True,
        "approval_required": False,
        "tool": "python.run",
        "alias_for": "terminal.run",
    }
    assert calls == [
        (
            "python - <<'__YACHIYO_PYTHON_RUN__'\nprint('ok')\n__YACHIYO_PYTHON_RUN__",
            False,
            45,
            True,
        ),
        (
            "python - <<'__YACHIYO_PYTHON_RUN__'\nprint('ok')\n__YACHIYO_PYTHON_RUN__",
            True,
            45,
            True,
        ),
    ]


def test_tool_dispatch_registry_routes_fs_move_file_through_file_organize(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls = []

    monkeypatch.setattr(
        broker,
        "file_organize",
        lambda path,
        *,
        operation="organize",
        file_type="",
        pattern="",
        destination="",
        conflict_strategy="keep_both",
        limit=200,
        approved=False: calls.append(
            (path, operation, file_type, pattern, destination, conflict_strategy, limit, approved)
        )
        or {"ok": bool(approved), "approval_required": not approved},
    )

    approval = dispatch_tool_call(
        broker,
        "fs.move_file",
        {
            "path": "Downloads",
            "operation": "organize",
            "file_type": "pdf",
            "pattern": "*.pdf",
            "destination": "Documents",
        },
    )
    result = dispatch_tool_call(
        broker,
        "fs.move_file",
        {
            "path": "Downloads",
            "operation": "organize",
            "file_type": "pdf",
            "pattern": "*.pdf",
            "destination": "Documents",
        },
        approved=True,
    )

    assert approval == {
        "ok": False,
        "approval_required": True,
        "tool": "fs.move_file",
        "alias_for": "file.organize",
    }
    assert result == {
        "ok": True,
        "approval_required": False,
        "tool": "fs.move_file",
        "alias_for": "file.organize",
    }
    assert calls == [
        ("Downloads", "organize", "pdf", "*.pdf", "Documents", "keep_both", 200, False),
        ("Downloads", "organize", "pdf", "*.pdf", "Documents", "keep_both", 200, True),
    ]


def test_tool_broker_call_workspace_list_metadata_is_opt_in(tmp_path) -> None:
    (tmp_path / "recent.pdf").write_text("pdf", encoding="utf-8")
    broker = _broker(tmp_path)

    result = broker.call(
        "workspace.list",
        {
            "path": ".",
            "pattern": "*.pdf",
            "file_type": "pdf",
            "include_metadata": True,
        },
    )

    assert result["ok"] is True
    assert result["entries"][0]["name"] == "recent.pdf"
    assert result["entries"][0]["type"] == "file"
    assert result["entries"][0]["mtime"] > 0
    assert result["entries"][0]["mtime_ns"] > 0
    assert result["entries"][0]["size"] == 3


def test_tool_broker_call_dispatches_file_organize(tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "receipt.pdf").write_text("receipt", encoding="utf-8")
    broker = _broker(tmp_path)

    approval = dispatch_tool_call(
        broker,
        "file.organize",
        {
            "path": "Downloads",
            "operation": "organize",
            "file_type": "invoice",
            "destination": "Invoices",
        },
    )
    result = dispatch_tool_call(
        broker,
        "file.organize",
        {
            "path": "Downloads",
            "operation": "organize",
            "file_type": "invoice",
            "destination": "Invoices",
        },
        approved=True,
    )

    assert approval["approval_required"] is True
    assert result["ok"] is True
    assert result["moved_count"] == 1
    assert (downloads / "Invoices" / "receipt.pdf").exists()


def test_tool_broker_call_analyzes_data_file_and_writes_artifact(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text(
        "region,revenue\nEast,10\nWest,20\nEast,30\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.csv", "artifact_path": "reports/sales.md"},
    )

    assert result["ok"] is True
    assert result["path"] == "sales.csv"
    assert result["source_kind"] == "csv"
    assert result["rows"] == 3
    assert result["columns"] == ["region", "revenue"]
    assert result["artifact"]["path"] == "reports/sales.md"
    assert result["artifact"]["kind"] == "markdown"
    assert result["artifact"]["mime_type"] == "text/markdown"
    assert result["artifact"]["size_bytes"] > 0
    artifact = tmp_path / "artifacts" / "reports" / "sales.md"
    assert artifact.exists()
    content = artifact.read_text(encoding="utf-8")
    assert "# Data Analysis Report" in content
    assert "mean=20.0" in content
    assert "| East | 10 |" in content


def test_tool_broker_call_analyzes_multiple_data_files_and_writes_artifact(tmp_path) -> None:
    (tmp_path / "east.csv").write_text("region,revenue\nEast,10\nEast,30\n", encoding="utf-8")
    (tmp_path / "west.csv").write_text("region,revenue\nWest,20\n", encoding="utf-8")
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {
            "paths": ["east.csv", "west.csv"],
            "source_kind": "csv",
            "artifact_path": "reports/combined.md",
        },
    )

    assert result["ok"] is True
    assert result["paths"] == ["east.csv", "west.csv"]
    assert result["source_file_count"] == 2
    assert result["source_kind"] == "csv"
    assert result["rows"] == 3
    assert result["columns"] == ["source_file", "region", "revenue"]
    assert result["artifact"]["path"] == "reports/combined.md"
    content = (tmp_path / "artifacts" / "reports" / "combined.md").read_text(
        encoding="utf-8"
    )
    assert "# Multi-file Data Analysis" in content
    assert "`east.csv`" in content
    assert "`west.csv`" in content
    assert "mean=20.0" in content
    assert "| east.csv | East | 10 |" in content


def test_tool_broker_call_analyzes_gb18030_csv_file(tmp_path) -> None:
    (tmp_path / "sales-cn.csv").write_text(
        "地区,收入\n华东,10\n华西,20\n",
        encoding="gb18030",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales-cn.csv", "artifact_path": "reports/sales-cn.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "csv"
    assert result["rows"] == 2
    assert result["columns"] == ["地区", "收入"]
    content = (tmp_path / "artifacts" / "reports" / "sales-cn.md").read_text(
        encoding="utf-8"
    )
    assert "| 华东 | 10 |" in content
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_data_file_and_writes_requested_artifacts(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text(
        "region,revenue\nEast,10\nWest,20\nEast,30\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {
            "path": "sales.csv",
            "artifact_path": "reports/sales.md",
            "artifact_paths": [
                "reports/sales.md",
                "reports/sales-summary.csv",
                "reports/sales.html",
                "reports/sales-chart.png",
            ],
            "requested_outputs": ["report", "table", "chart"],
            "artifact_manifest": [
                {"path": "reports/sales.md", "kind": "markdown"},
                {"path": "reports/sales-summary.csv", "kind": "csv"},
                {"path": "reports/sales.html", "kind": "html"},
                {"path": "reports/sales-chart.png", "kind": "chart"},
            ],
        },
    )

    assert result["ok"] is True
    assert result["artifact"]["path"] == "reports/sales.md"
    assert result["artifact"]["planned_kind"] == "markdown"
    assert result["artifact"]["source_kind"] == "csv"
    assert result["artifact"]["requested_outputs"] == ["report", "table", "chart"]
    assert result["artifact"]["manifest_index"] == 0
    assert result["artifact_paths"] == [
        "reports/sales.md",
        "reports/sales-summary.csv",
        "reports/sales.html",
        "reports/sales-chart.png",
    ]
    assert [artifact["path"] for artifact in result["artifacts"]] == result["artifact_paths"]
    assert result["artifacts"][1]["mime_type"] == "text/csv"
    assert result["artifacts"][2]["mime_type"] == "text/html"
    assert result["artifacts"][3]["mime_type"] == "image/png"
    assert result["artifacts"][3]["kind"] == "image"
    assert result["artifacts"][3]["planned_kind"] == "chart"
    assert result["artifacts"][3]["manifest_index"] == 3
    assert result["artifact_manifest"] == [
        {"path": "reports/sales.md", "kind": "markdown"},
        {"path": "reports/sales-summary.csv", "kind": "csv"},
        {"path": "reports/sales.html", "kind": "html"},
        {"path": "reports/sales-chart.png", "kind": "chart", "actual_kind": "image"},
    ]
    assert (tmp_path / "artifacts" / "reports" / "sales-summary.csv").read_text(
        encoding="utf-8"
    ).startswith("column,type,count")
    assert "<!doctype html>" in (tmp_path / "artifacts" / "reports" / "sales.html").read_text(
        encoding="utf-8"
    )
    assert (
        tmp_path / "artifacts" / "reports" / "sales-chart.png"
    ).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_tool_broker_call_analyzes_markdown_table_file(tmp_path) -> None:
    (tmp_path / "sales.md").write_text(
        "| region | revenue |\n"
        "| --- | ---: |\n"
        "| East | 10 |\n"
        "| West | 20 |\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.md", "artifact_path": "reports/sales-table.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "text_table"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "revenue"]
    content = (tmp_path / "artifacts" / "reports" / "sales-table.md").read_text(
        encoding="utf-8"
    )
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_captured_table_content(tmp_path) -> None:
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {
            "content": (
                "| region | revenue |\n"
                "| --- | ---: |\n"
                "| East | 10 |\n"
                "| West | 20 |\n"
            ),
            "display_path": "captured:visible_text",
            "artifact_path": "reports/captured-table.md",
            "source_kind": "text_table",
        },
    )

    assert result["ok"] is True
    assert result["path"] == "captured:visible_text"
    assert result["source_kind"] == "text_table"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "revenue"]
    assert result["artifact"]["path"] == "reports/captured-table.md"
    content = (tmp_path / "artifacts" / "reports" / "captured-table.md").read_text(
        encoding="utf-8"
    )
    assert "captured:visible_text" in content
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_json_file(tmp_path) -> None:
    (tmp_path / "sales.json").write_text(
        (
            "["
            '{"region":"East","metrics":{"revenue":10}},'
            '{"region":"West","metrics":{"revenue":20}}'
            "]"
        ),
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.json", "artifact_path": "reports/sales-json.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "json"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "metrics.revenue"]
    content = (tmp_path / "artifacts" / "reports" / "sales-json.md").read_text(
        encoding="utf-8"
    )
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_jsonl_file(tmp_path) -> None:
    (tmp_path / "events.jsonl").write_text(
        '{"region":"East","revenue":10}\n{"region":"West","revenue":20}\n',
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "events.jsonl", "artifact_path": "reports/events.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "jsonl"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "revenue"]
    content = (tmp_path / "artifacts" / "reports" / "events.md").read_text(
        encoding="utf-8"
    )
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_tsv_file(tmp_path) -> None:
    (tmp_path / "sales.tsv").write_text(
        "region\trevenue\nEast\t10\nWest\t20\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.tsv", "artifact_path": "reports/sales-tsv.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "tsv"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "revenue"]
    content = (tmp_path / "artifacts" / "reports" / "sales-tsv.md").read_text(
        encoding="utf-8"
    )
    assert "mean=15.0" in content


def test_tool_broker_call_analyzes_plain_text_file(tmp_path) -> None:
    (tmp_path / "notes.txt").write_text(
        "first line\nsecond line with words\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "notes.txt", "artifact_path": "reports/notes.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "text"
    assert result["rows"] == 2
    assert result["columns"] == []
    content = (tmp_path / "artifacts" / "reports" / "notes.md").read_text(
        encoding="utf-8"
    )
    assert "- Lines: 2" in content
    assert "- Words: 6" in content


def test_tool_broker_call_analyzes_xlsx_file(tmp_path) -> None:
    workbook = tmp_path / "sales.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            (
                '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<si><t>region</t></si><si><t>revenue</t></si>"
                "<si><t>East</t></si><si><t>West</t></si>"
                "</sst>"
            ),
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<sheetData>"
                '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>'
                '<row r="2"><c r="A2" t="s"><v>2</v></c><c r="B2"><v>10</v></c></row>'
                '<row r="3"><c r="A3" t="s"><v>3</v></c><c r="B3"><v>20</v></c></row>'
                "</sheetData>"
                "</worksheet>"
            ),
        )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.xlsx", "artifact_path": "reports/sales-xlsx.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "xlsx"
    assert result["rows"] == 2
    assert result["columns"] == ["region", "revenue"]
    content = (tmp_path / "artifacts" / "reports" / "sales-xlsx.md").read_text(
        encoding="utf-8"
    )
    assert "mean=15.0" in content


def test_tool_broker_call_handles_empty_xlsx_without_text_fallback(tmp_path) -> None:
    workbook = tmp_path / "empty.xlsx"
    with zipfile.ZipFile(workbook, "w") as archive:
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            (
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                "<sheetData />"
                "</worksheet>"
            ),
        )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "empty.xlsx", "artifact_path": "reports/empty.md"},
    )

    assert result["ok"] is True
    assert result["source_kind"] == "xlsx"
    assert result["rows"] == 0
    assert result["columns"] == []
    content = (tmp_path / "artifacts" / "reports" / "empty.md").read_text(
        encoding="utf-8"
    )
    assert "- Source kind: `xlsx`" in content
    assert "_No rows to preview._" in content


def test_tool_broker_call_returns_data_analysis_parse_error(tmp_path) -> None:
    (tmp_path / "broken.json").write_text('{"region": "East"', encoding="utf-8")
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "broken.json", "artifact_path": "reports/broken.md"},
    )

    assert result["ok"] is False
    assert result["source_kind"] == "json"
    assert result["error"] == "数据文件解析失败"
    assert result["suggested_tool"] == "terminal.run"
    assert "workspace.read + terminal.run" in result["hint"]
    assert not (tmp_path / "artifacts" / "reports" / "broken.md").exists()


def test_tool_broker_data_analysis_verifies_written_artifact_postcondition(tmp_path) -> None:
    (tmp_path / "sales.csv").write_text(
        "region,revenue\nEast,10\nWest,20\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)

    result = broker.call(
        "data.analyze",
        {"path": "sales.csv", "artifact_path": "reports/sales.md"},
    )

    assert result["ok"] is True
    assert result["postcondition_verified"] is True
    assert (tmp_path / "artifacts" / "reports" / "sales.md").is_file()


def test_tool_broker_data_analysis_fails_closed_for_unverified_artifact_target(
    tmp_path,
    monkeypatch,
) -> None:
    (tmp_path / "sales.csv").write_text(
        "region,revenue\nEast,10\n",
        encoding="utf-8",
    )
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        broker,
        "artifact_write",
        lambda _path, _content: {
            "ok": True,
            "path": "reports/wrong-target.md",
            "bytes": 7,
        },
    )

    result = broker.data_analyze(
        "sales.csv",
        artifact_path="reports/sales.md",
    )

    assert result["ok"] is False
    assert result["postcondition_verified"] is False
    assert result["verification_failed"] is True
    assert result["error"] == "data_analysis_artifact_unverified"


def test_data_analyze_schema_rejects_invalid_max_rows() -> None:
    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 max_rows"):
        ToolDescriptorRegistry.validate_payload(
            "data.analyze",
            {"path": "sales.csv", "max_rows": "many"},
        )


def test_data_analyze_schema_rejects_invalid_artifact_paths() -> None:
    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 artifact_paths"):
        ToolDescriptorRegistry.validate_payload(
            "data.analyze",
            {"path": "sales.csv", "artifact_paths": ["reports/out.md", 42]},
        )


def test_data_analyze_schema_accepts_path_or_content() -> None:
    ToolDescriptorRegistry.validate_payload("data.analyze", {"path": "sales.csv"})
    ToolDescriptorRegistry.validate_payload(
        "data.analyze",
        {"paths": ["east.csv", "west.csv"]},
    )
    ToolDescriptorRegistry.validate_payload(
        "data.analyze",
        {
            "content": "region,revenue\nEast,10\n",
            "display_path": "captured:visible_text",
        },
    )

    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 path、paths 或 content"):
        ToolDescriptorRegistry.validate_payload("data.analyze", {"artifact_path": "out.md"})
    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 paths"):
        ToolDescriptorRegistry.validate_payload(
            "data.analyze",
            {"paths": ["east.csv", ""]},
        )
    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 content"):
        ToolDescriptorRegistry.validate_payload("data.analyze", {"content": 42})
    with pytest.raises(AgentRuntimeError, match="data.analyze 参数 display_path"):
        ToolDescriptorRegistry.validate_payload(
            "data.analyze",
            {"content": "a,b\n1,2\n", "display_path": 42},
        )


def test_tool_dispatch_registry_keeps_terminal_approval_gate(tmp_path) -> None:
    broker = _broker(tmp_path)

    result = broker.call("terminal.run", {"command": "printf blocked", "approved": True})

    assert result["ok"] is False
    assert result["approval_required"] is True
    assert result["tool"] == "terminal.run"
    assert result["input_preview"] == {"command": "printf blocked", "shell": False}


def test_tool_dispatch_registry_rejects_unknown_tool(tmp_path) -> None:
    with pytest.raises(AgentRuntimeError, match="未知工具"):
        dispatch_tool_call(_broker(tmp_path), "unknown.tool", {})


def test_restricted_plugin_tool_registers_schema_policy_and_dispatch(tmp_path) -> None:
    def echo_tool(payload, context):
        return {
            "ok": True,
            "echo": payload["text"],
            "tool": "plugin.spoof.echo",
            "plugin_id": "spoof",
            "risk_level": "high",
            "context": {
                "tool_name": context.tool_name,
                "plugin_id": context.plugin_id,
                "risk_level": context.risk_level,
                "workdir": str(context.workdir),
            },
        }

    registered = register_restricted_tool_plugin(
        RestrictedToolPlugin(
            plugin_id="notes",
            tools=(
                RestrictedPluginTool(
                    tool_id="echo",
                    description="Echo text through a restricted test plugin.",
                    properties={"text": {"type": "string"}},
                    required=("text",),
                    risk_level="low",
                    execute=echo_tool,
                ),
            ),
            skill_docs="Use echo for tests only.",
        )
    )
    tool_name = "plugin.notes.echo"

    assert registered[0].name == tool_name
    assert registered[0].function_name == "plugin_notes_echo"
    assert registered[0].skill_docs == "Use echo for tests only."
    assert list_restricted_plugin_tools()[0].name == tool_name
    assert restricted_plugin_tool_risk(tool_name) == "low"
    assert tool_name in KNOWN_AGENT_TOOLS
    assert tool_name in TOOL_DISPATCH_REGISTRY

    schemas = ToolDescriptorRegistry.model_tool_schemas([tool_name])
    assert schemas[0]["function"]["name"] == "plugin_notes_echo"
    assert schemas[0]["function"]["parameters"]["additionalProperties"] is False
    ToolDescriptorRegistry.validate_payload(tool_name, {"text": "hello"})
    with pytest.raises(AgentRuntimeError, match="未声明"):
        ToolDescriptorRegistry.validate_payload(tool_name, {"text": "hello", "extra": True})

    policy = RuntimePolicyCompiler().compile_tool_policy(
        "custom",
        {"allowed_tools": [tool_name, "unknown.tool"]},
    )
    assert policy == {"allowed_tools": [tool_name], "approval_required": {}}
    result = dispatch_tool_call(_broker(tmp_path), tool_name, {"text": "hello"})
    assert result["ok"] is True
    assert result["echo"] == "hello"
    assert result["tool"] == tool_name
    assert result["plugin_id"] == "notes"
    assert result["risk_level"] == "low"
    assert result["context"]["tool_name"] == tool_name

    unregister_restricted_tool_plugin("notes")

    assert tool_name not in KNOWN_AGENT_TOOLS
    assert tool_name not in TOOL_DISPATCH_REGISTRY
    assert restricted_plugin_tool_risk(tool_name) is None
    with pytest.raises(AgentRuntimeError, match="未知工具"):
        dispatch_tool_call(_broker(tmp_path), tool_name, {"text": "hello"})


def test_restricted_plugin_manager_installs_disables_enables_and_uninstalls(
    tmp_path,
) -> None:
    def echo_tool(payload, context):
        return {"ok": True, "text": payload["text"], "plugin_id": context.plugin_id}

    tool_name = "plugin.notes.echo"
    manager = RestrictedToolPluginManager()
    plugin = RestrictedToolPlugin(
        plugin_id="notes",
        tools=(
            RestrictedPluginTool(
                tool_id="echo",
                description="Echo text through a managed test plugin.",
                properties={"text": {"type": "string"}},
                required=("text",),
                risk_level="medium",
                execute=echo_tool,
            ),
        ),
        skill_docs="Use echo for managed plugin tests.",
    )

    installed = manager.install(plugin, enabled=False)
    assert installed.plugin_id == "notes"
    assert installed.enabled is False
    assert installed.tool_names == (tool_name,)
    assert installed.skill_docs == "Use echo for managed plugin tests."
    assert list_restricted_plugin_tools() == []
    assert tool_name not in TOOL_DISPATCH_REGISTRY

    enabled = manager.enable("notes")
    assert enabled.enabled is True
    assert list_restricted_plugin_tools()[0].name == tool_name
    assert restricted_plugin_tool_risk(tool_name) == "medium"
    assert dispatch_tool_call(_broker(tmp_path), tool_name, {"text": "hello"}) == {
        "ok": True,
        "text": "hello",
        "plugin_id": "notes",
        "tool": tool_name,
        "risk_level": "medium",
    }

    disabled = manager.disable("notes")
    assert disabled.enabled is False
    assert manager.list_installed()[0].enabled is False
    assert list_restricted_plugin_tools() == []
    assert tool_name not in TOOL_DISPATCH_REGISTRY
    with pytest.raises(AgentRuntimeError, match="未知工具"):
        dispatch_tool_call(_broker(tmp_path), tool_name, {"text": "hello"})

    assert manager.enable("notes").enabled is True
    uninstalled = manager.uninstall("notes")
    assert uninstalled.enabled is False
    assert manager.list_installed() == []
    assert tool_name not in TOOL_DISPATCH_REGISTRY
    with pytest.raises(AgentRuntimeError, match="插件未安装"):
        manager.enable("notes")


def test_high_risk_restricted_plugin_tool_uses_existing_approval_gate(tmp_path) -> None:
    def delete_like_tool(payload, context):
        return {
            "ok": True,
            "approved": context.approved,
            "target": payload["target"],
        }

    register_restricted_tool_plugin(
        RestrictedToolPlugin(
            plugin_id="ops",
            tools=(
                RestrictedPluginTool(
                    tool_id="delete_file",
                    description="High-risk mock plugin tool.",
                    properties={"target": {"type": "string"}},
                    required=("target",),
                    risk_level="high",
                    execute=delete_like_tool,
                ),
            ),
        )
    )
    tool_name = "plugin.ops.delete_file"

    policy = RuntimePolicyCompiler().compile_tool_policy(
        "custom",
        {"allowed_tools": [tool_name]},
    )
    assert policy == {"allowed_tools": [tool_name], "approval_required": {tool_name: True}}
    assert tool_name in HIGH_RISK_AGENT_TOOLS

    approval = dispatch_tool_call(_broker(tmp_path), tool_name, {"target": "notes.md"})
    assert approval == {
        "ok": False,
        "approval_required": True,
        "tool": tool_name,
        "risk_level": "high",
        "plugin_id": "ops",
    }

    result = dispatch_tool_call(
        _broker(tmp_path),
        tool_name,
        {"target": "notes.md"},
        approved=True,
    )
    assert result["ok"] is True
    assert result["approved"] is True
    assert result["plugin_id"] == "ops"
    assert result["risk_level"] == "high"


def test_desktop_tools_have_schemas_and_do_not_relax_terminal_approval() -> None:
    schemas = {
        schema["function"]["name"]: schema
        for schema in ToolDescriptorRegistry.model_tool_schemas(list(DAILY_DESKTOP_TOOL_NAMES))
    }

    assert {
        "screen_capture",
        "desktop_permissions",
        "desktop_active_window",
        "desktop_running_apps",
        "desktop_list_apps",
        "desktop_windows",
        "app_status",
        "app_open",
        "app_focus",
        "app_focus_window",
        "app_open_and_safe_type_text",
        "app_focus_and_safe_type_text",
        "app_open_and_safe_shortcut",
        "app_focus_and_safe_shortcut",
        "app_open_and_safe_key",
        "app_focus_and_safe_key",
        "app_open_and_hotkey",
        "app_focus_and_hotkey",
        "app_open_and_safe_scroll",
        "app_focus_and_safe_scroll",
        "app_open_and_safe_click",
        "app_focus_and_safe_click",
        "app_open_and_click_ui_element",
        "app_focus_and_click_ui_element",
        "app_open_and_type_into_ui_element",
        "app_focus_and_type_into_ui_element",
        "app_show",
        "app_hide",
        "app_minimize",
        "app_quit",
        "desktop_reveal_path",
        "desktop_open_path",
        "desktop_open_path_with_app",
        "app_open_path_with_app",
        "media_apple_music_play",
        "media_apple_music_open_and_play",
        "media_apple_music_control",
        "media_music_app_open_and_play",
        "system_settings_open",
        "system_volume",
        "system_brightness",
        "system_display_sleep",
        "system_screen_saver_start",
        "clipboard_write",
        "clipboard_read",
        "notes_create",
        "reminders_create",
        "calendar_create_event",
        "desktop_safe_shortcut",
        "desktop_safe_key",
        "desktop_safe_type_text",
        "desktop_safe_click",
        "desktop_safe_scroll",
        "desktop_click_ui_element",
        "desktop_type_into_ui_element",
        "desktop_hide_app",
        "desktop_minimize_window",
        "desktop_close_window",
        "desktop_quit_app",
        "desktop_hotkey",
        "desktop_submit_foreground",
        "desktop_type_text",
        "desktop_click",
        "browser_search",
        "browser_open",
        "browser_open_url",
        "browser_current_page",
        "browser_click",
        "browser_type_text",
        "browser_extract",
        "browser_extract_text",
        "browser_screenshot",
    }.issubset(schemas)
    assert "terminal.run" in HIGH_RISK_AGENT_TOOLS
    assert "workspace.write_patch" in HIGH_RISK_AGENT_TOOLS


def test_desktop_running_apps_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.running_apps", {})


def test_desktop_list_apps_schema_accepts_optional_query_and_limit() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.list_apps", {})
    ToolDescriptorRegistry.validate_payload("desktop.list_apps", {"query": "Word", "limit": 20})

    with pytest.raises(AgentRuntimeError, match="desktop.list_apps 参数 query 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.list_apps", {"query": 123})
    with pytest.raises(AgentRuntimeError, match="desktop.list_apps 参数 limit 必须是 1-500 的整数"):
        ToolDescriptorRegistry.validate_payload("desktop.list_apps", {"limit": 0})


def test_desktop_app_open_focus_schemas_accept_discovery_metadata() -> None:
    payload = {
        "app_name": "PixelForge",
        "selection_source": "desktop.list_apps",
        "query": "image editor",
        "app_resolution_reason": "best installed app match",
    }
    for tool_name in ("app.open", "app.focus", "desktop.open_app", "desktop.focus_app"):
        ToolDescriptorRegistry.validate_payload(tool_name, payload)

    with pytest.raises(AgentRuntimeError, match="desktop.open_app 参数 selection_source 必须是字符串"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.open_app",
            {"app_name": "PixelForge", "selection_source": {"tool": "desktop.list_apps"}},
        )


def test_browser_portable_alias_schemas_validate_payloads() -> None:
    ToolDescriptorRegistry.validate_payload("browser.search", {"query": "open hanako"})
    ToolDescriptorRegistry.validate_payload("browser.open", {"url": "https://example.com"})
    ToolDescriptorRegistry.validate_payload("browser.extract", {})
    ToolDescriptorRegistry.validate_payload("browser.extract", {"selector": "main"})

    with pytest.raises(AgentRuntimeError, match="browser.search 参数 query 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("browser.search", {})
    with pytest.raises(AgentRuntimeError, match="browser.open 参数 url 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("browser.open", {})


def test_fs_portable_alias_schemas_validate_payloads() -> None:
    ToolDescriptorRegistry.validate_payload("fs.find_files", {})
    ToolDescriptorRegistry.validate_payload(
        "fs.find_files",
        {"path": "Downloads", "pattern": "*.csv", "file_type": "csv"},
    )
    ToolDescriptorRegistry.validate_payload("fs.read_file", {"path": "sales.csv"})

    with pytest.raises(AgentRuntimeError, match="fs.read_file 参数 path 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("fs.read_file", {})


def test_python_run_schema_requires_command_or_code() -> None:
    ToolDescriptorRegistry.validate_payload("python.run", {"code": "print('ok')"})
    ToolDescriptorRegistry.validate_payload(
        "python.run",
        {"command": "python scripts/analyze.py", "timeout_seconds": 60},
    )

    with pytest.raises(AgentRuntimeError, match="python.run 参数 command 或 code 必须提供一个"):
        ToolDescriptorRegistry.validate_payload("python.run", {})
    with pytest.raises(AgentRuntimeError, match="python.run 参数 code 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("python.run", {"code": 123})
    with pytest.raises(AgentRuntimeError, match="python.run 参数 timeout_seconds 必须是 1-120 的整数"):
        ToolDescriptorRegistry.validate_payload("python.run", {"code": "print('ok')", "timeout_seconds": 0})


def test_fs_move_file_schema_matches_file_organize_alias() -> None:
    ToolDescriptorRegistry.validate_payload(
        "fs.move_file",
        {
            "path": "Downloads",
            "operation": "organize",
            "file_type": "pdf",
            "pattern": "*.pdf",
            "destination": "Documents",
        },
    )

    with pytest.raises(AgentRuntimeError, match="fs.move_file 参数 operation 必须是 organize、archive 或 move"):
        ToolDescriptorRegistry.validate_payload(
            "fs.move_file",
            {"path": "Downloads", "operation": "delete"},
        )
    with pytest.raises(AgentRuntimeError, match="fs.move_file 参数 limit 必须是 1-500 的整数"):
        ToolDescriptorRegistry.validate_payload(
            "fs.move_file",
            {"path": "Downloads", "operation": "move", "limit": 0},
        )


def test_desktop_permissions_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.permissions", {})


def test_desktop_windows_schema_accepts_optional_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.windows", {})
    ToolDescriptorRegistry.validate_payload("desktop.windows", {"app_name": "Google Chrome"})

    with pytest.raises(AgentRuntimeError, match="desktop.windows 参数 app_name 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.windows", {"app_name": 123})


def test_desktop_ui_elements_schema_accepts_optional_filter_and_limit() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.ui_elements", {})
    ToolDescriptorRegistry.validate_payload("desktop.ui_elements", {"role_filter": "button", "limit": 20})
    ToolDescriptorRegistry.validate_payload(
        "desktop.ui_elements",
        {"app_name": "Google Chrome", "role_filter": "button", "limit": 20},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.ui_elements 参数 app_name 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.ui_elements", {"app_name": 123})
    with pytest.raises(AgentRuntimeError, match="desktop.ui_elements 参数 role_filter 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.ui_elements", {"role_filter": 123})
    with pytest.raises(AgentRuntimeError, match="desktop.ui_elements 参数 limit 必须是 1-200 的整数"):
        ToolDescriptorRegistry.validate_payload("desktop.ui_elements", {"limit": 0})


def test_desktop_inspect_app_schema_requires_app_name_and_valid_options() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.inspect_app", {"app_name": "Linear"})
    ToolDescriptorRegistry.validate_payload(
        "desktop.inspect_app",
        {
            "app_name": "Linear",
            "open_if_needed": False,
            "focus": True,
            "role_filter": "button",
            "limit": 20,
        },
    )

    with pytest.raises(AgentRuntimeError, match="desktop.inspect_app 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.inspect_app", {})
    with pytest.raises(AgentRuntimeError, match="desktop.inspect_app 参数 open_if_needed 必须是布尔值"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.inspect_app",
            {"app_name": "Linear", "open_if_needed": "false"},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.inspect_app 参数 focus 必须是布尔值"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.inspect_app",
            {"app_name": "Linear", "focus": "true"},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.inspect_app 参数 limit 必须是 1-200 的整数"):
        ToolDescriptorRegistry.validate_payload("desktop.inspect_app", {"app_name": "Linear", "limit": 0})


def test_desktop_click_ui_element_schema_requires_target_and_valid_options() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.click_ui_element",
        {"target": "Send", "role_filter": "button", "limit": 20, "click_count": 2},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_click_ui_element",
        {
            "app_name": "Google Chrome",
            "target": "Send",
            "role_filter": "button",
            "limit": 20,
            "click_count": 2,
        },
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_click_ui_element",
        {"app_name": "Slack", "target": "Send"},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.click_ui_element 参数 target 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.click_ui_element", {"target": ""})
    with pytest.raises(AgentRuntimeError, match="app.open_and_click_ui_element 参数 app_name 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_click_ui_element",
            {"app_name": "", "target": "Send"},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_click_ui_element 参数 target 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_click_ui_element",
            {"app_name": "Slack", "target": ""},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.click_ui_element 参数 role_filter 必须是字符串"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.click_ui_element",
            {"target": "Send", "role_filter": 123},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_click_ui_element 参数 role_filter 必须是字符串"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_click_ui_element",
            {"app_name": "Google Chrome", "target": "Send", "role_filter": 123},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.click_ui_element 参数 limit 必须是 1-200"):
        ToolDescriptorRegistry.validate_payload("desktop.click_ui_element", {"target": "Send", "limit": 0})
    with pytest.raises(AgentRuntimeError, match="app.focus_and_click_ui_element 参数 limit 必须是 1-200"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_click_ui_element",
            {"app_name": "Slack", "target": "Send", "limit": 0},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.click_ui_element 参数 click_count 必须是 1-3"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.click_ui_element",
            {"target": "Send", "click_count": 4},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_click_ui_element 参数 click_count 必须是 1-3"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_click_ui_element",
            {"app_name": "Google Chrome", "target": "Send", "click_count": 4},
        )


def test_desktop_type_into_ui_element_schema_requires_target_text_and_valid_options() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.type_into_ui_element",
        {"target": "Search", "text": "hello", "role_filter": "text", "limit": 20},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_type_into_ui_element",
        {
            "app_name": "Google Chrome",
            "target": "Search",
            "text": "hello",
            "role_filter": "text",
            "limit": 20,
        },
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_type_into_ui_element",
        {"app_name": "Slack", "target": "Message", "text": "hello"},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.type_into_ui_element 参数 target 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.type_into_ui_element", {"target": "", "text": "hello"})
    with pytest.raises(AgentRuntimeError, match="desktop.type_into_ui_element 参数 text 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.type_into_ui_element", {"target": "Search", "text": ""})
    with pytest.raises(AgentRuntimeError, match="app.open_and_type_into_ui_element 参数 app_name 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_type_into_ui_element",
            {"app_name": "", "target": "Search", "text": "hello"},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_type_into_ui_element 参数 text 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_type_into_ui_element",
            {"app_name": "Slack", "target": "Message", "text": ""},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.type_into_ui_element 参数 role_filter 必须是字符串"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.type_into_ui_element",
            {"target": "Search", "text": "hello", "role_filter": 123},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_type_into_ui_element 参数 role_filter 必须是字符串"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_type_into_ui_element",
            {"app_name": "Google Chrome", "target": "Search", "text": "hello", "role_filter": 123},
        )
    with pytest.raises(AgentRuntimeError, match="desktop.type_into_ui_element 参数 limit 必须是 1-200"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.type_into_ui_element",
            {"target": "Search", "text": "hello", "limit": 0},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_type_into_ui_element 参数 limit 必须是 1-200"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_type_into_ui_element",
            {"app_name": "Slack", "target": "Message", "text": "hello", "limit": 0},
        )


def test_app_status_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("app.status", {"app_name": "Google Chrome"})

    with pytest.raises(AgentRuntimeError, match="app.status 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("app.status", {"app_name": ""})


def test_desktop_verify_schema_accepts_only_app_running_verification_goal() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.verify", {})
    ToolDescriptorRegistry.validate_payload(
        "desktop.verify",
        {"app_name": "WPS Office"},
    )
    ToolDescriptorRegistry.validate_payload(
        "desktop.verify",
        {
            "app_name": "WPS Office",
            "verification_goal": "app_running",
        },
    )

    with pytest.raises(
        AgentRuntimeError,
        match="desktop.verify 参数 verification_goal 必须是以下值之一：app_running",
    ):
        ToolDescriptorRegistry.validate_payload(
            "desktop.verify",
            {
                "app_name": "WPS Office",
                "verification_goal": "foreground",
            },
        )

    with pytest.raises(
        AgentRuntimeError,
        match=(
            "desktop.verify 参数 app_name 在 verification_goal=app_running 时"
            "必须是非空字符串"
        ),
    ):
        ToolDescriptorRegistry.validate_payload(
            "desktop.verify",
            {"verification_goal": "app_running"},
        )


def test_app_quit_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("app.quit", {"app_name": "Slack"})

    with pytest.raises(AgentRuntimeError, match="app.quit 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("app.quit", {"app_name": ""})


def test_app_show_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("app.show", {"app_name": "Slack"})

    with pytest.raises(AgentRuntimeError, match="app.show 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("app.show", {"app_name": ""})


def test_app_focus_window_schema_requires_app_name_and_title() -> None:
    ToolDescriptorRegistry.validate_payload(
        "app.focus_window",
        {"app_name": "Slack", "title_contains": "general"},
    )

    with pytest.raises(AgentRuntimeError, match="app.focus_window 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_window",
            {"app_name": "", "title_contains": "general"},
        )
    with pytest.raises(
        AgentRuntimeError,
        match="app.focus_window 参数 title_contains 必须是非空字符串",
    ):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_window",
            {"app_name": "Slack", "title_contains": ""},
        )


def test_app_foreground_action_schemas_require_app_and_explicit_action() -> None:
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_type_text",
        {"app_name": "Notes", "text": "hello"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_type_text",
        {"app_name": "Notes", "text": "hello"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_shortcut",
        {"app_name": "Google Chrome", "action": "new_tab"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_shortcut",
        {"app_name": "Google Chrome", "action": "close_tab"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Slack", "action": "paste"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Slack", "action": "new_message"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "finder_quick_look"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "finder_get_info"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "new_folder"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "rename_selected"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "parent_folder"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "finder_airdrop"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "finder_network"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_shortcut",
        {"app_name": "Finder", "action": "finder_recents"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_key",
        {"app_name": "Google Chrome", "action": "tab"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_key",
        {"app_name": "Slack", "action": "arrow_down", "repeat_count": 3},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_hotkey",
        {"app_name": "Google Chrome", "key": "l", "modifiers": ["command"]},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_hotkey",
        {"app_name": "Slack", "key": "k", "modifiers": ["command"]},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_scroll",
        {"app_name": "Google Chrome", "direction": "down"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_scroll",
        {"app_name": "Slack", "direction": "up", "pages": 3},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_and_safe_click",
        {"app_name": "Google Chrome", "x": 120, "y": 240},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.focus_and_safe_click",
        {"app_name": "Slack", "x": "120", "y": "240"},
    )

    with pytest.raises(AgentRuntimeError, match="app.open_and_safe_type_text 参数 text 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_safe_type_text",
            {"app_name": "Notes", "text": ""},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "delete_tab"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "finder_quick_look"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "finder_get_info"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "finder_airdrop"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "finder_network"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "finder_recents"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "new_folder"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "rename_selected"},
        )
    with pytest.raises(AgentRuntimeError, match="仅支持 app_name=Finder"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_shortcut",
            {"app_name": "Slack", "action": "parent_folder"},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_safe_key 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_safe_key",
            {"app_name": "Slack", "action": "return"},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_safe_key 参数 repeat_count 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_key",
            {"app_name": "Slack", "action": "tab", "repeat_count": 0},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_hotkey 参数 modifiers 只能包含"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_hotkey",
            {"app_name": "Slack", "key": "l", "modifiers": ["meta"]},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_safe_scroll 参数 direction 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_safe_scroll",
            {"app_name": "Slack", "direction": "left"},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_safe_scroll 参数 pages 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_scroll",
            {"app_name": "Slack", "direction": "down", "pages": 0},
        )
    with pytest.raises(AgentRuntimeError, match="app.open_and_safe_click 参数 x 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.open_and_safe_click",
            {"app_name": "Slack", "x": -1, "y": 240},
        )
    with pytest.raises(AgentRuntimeError, match="app.focus_and_safe_click 参数 y 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "app.focus_and_safe_click",
            {"app_name": "Slack", "x": 120, "y": True},
        )


def test_app_hide_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("app.hide", {"app_name": "Slack"})

    with pytest.raises(AgentRuntimeError, match="app.hide 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("app.hide", {"app_name": ""})


def test_desktop_safe_shortcut_schema_accepts_only_whitelisted_actions() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "copy"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_tab"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "close_tab"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "next_tab"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "previous_tab"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "next_window"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "previous_window"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "mission_control"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "application_windows"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "spotlight_search"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "emoji_picker"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "screenshot_selection"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "screenshot_toolbar"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "lock_screen"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "force_quit_dialog"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_window"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_document"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_note"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_reminder"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_event"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "focus_address_bar"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_private_window"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "bookmark_page"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "show_history"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "open_devtools"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "zoom_in"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "zoom_out"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "reset_zoom"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "browser_back"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "browser_forward"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "reopen_closed_tab"})

    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "finder_quick_look"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "finder_get_info"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "finder_airdrop"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "finder_network"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "finder_recents"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_message"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "new_folder"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "rename_selected"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "parent_folder"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_shortcut 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_shortcut", {"action": "delete_tab"})


def test_desktop_safe_key_schema_accepts_only_whitelisted_navigation_keys() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.safe_key", {"action": "tab"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_key", {"action": "shift_tab"})
    ToolDescriptorRegistry.validate_payload(
        "desktop.safe_key",
        {"action": "arrow_down", "repeat_count": 3},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.safe_key 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_key", {"action": "return"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_key 参数 repeat_count 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_key", {"action": "tab", "repeat_count": 0})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_key 参数 repeat_count 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_key", {"action": "tab", "repeat_count": True})


def test_desktop_safe_type_text_schema_requires_user_text() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.safe_type_text", {"text": "hello"})

    with pytest.raises(AgentRuntimeError, match="desktop.safe_type_text 参数 text 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_type_text", {"text": ""})


def test_desktop_safe_click_schema_accepts_only_coordinates() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.safe_click", {"x": 12, "y": 34.5})

    with pytest.raises(AgentRuntimeError, match="desktop.safe_click 参数 x 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_click", {"x": -1, "y": 34})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_click 参数 y 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_click", {"x": 12, "y": True})


def test_desktop_safe_scroll_schema_accepts_direction_and_pages() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.safe_scroll", {"direction": "down"})
    ToolDescriptorRegistry.validate_payload("desktop.safe_scroll", {"direction": "up", "pages": 3})

    with pytest.raises(AgentRuntimeError, match="desktop.safe_scroll 参数 direction 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_scroll", {"direction": "left"})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_scroll 参数 pages 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_scroll", {"direction": "down", "pages": 0})
    with pytest.raises(AgentRuntimeError, match="desktop.safe_scroll 参数 pages 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.safe_scroll", {"direction": "down", "pages": True})


def test_desktop_submit_foreground_schema_requires_known_action() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.submit_foreground", {"action": "send"})
    ToolDescriptorRegistry.validate_payload("desktop.submit_foreground", {"action": "submit"})
    ToolDescriptorRegistry.validate_payload("desktop.submit_foreground", {"action": "confirm"})

    with pytest.raises(AgentRuntimeError, match="desktop.submit_foreground 参数 action 必须是"):
        ToolDescriptorRegistry.validate_payload("desktop.submit_foreground", {"action": "return"})


def test_app_minimize_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload("app.minimize", {"app_name": "Slack"})

    with pytest.raises(AgentRuntimeError, match="app.minimize 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("app.minimize", {"app_name": ""})


def test_desktop_reveal_path_schema_accepts_local_path() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.reveal_path",
        {"path": "~/Downloads/report.pdf"},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.reveal_path 参数 path 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.reveal_path", {"path": ""})


def test_desktop_open_path_schema_accepts_local_path() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.open_path",
        {"path": "~/Downloads/report.pdf"},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.open_path 参数 path 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("desktop.open_path", {"path": ""})


def test_desktop_open_path_with_app_schema_accepts_local_path_and_app() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.open_path_with_app",
        {"app_name": "Preview", "path": "~/Downloads/report.pdf"},
    )
    ToolDescriptorRegistry.validate_payload(
        "app.open_path_with_app",
        {"app_name": "Preview", "path": "~/Downloads/report.pdf"},
    )

    with pytest.raises(
        AgentRuntimeError,
        match="desktop.open_path_with_app 参数 path 必须是非空字符串",
    ):
        ToolDescriptorRegistry.validate_payload(
            "desktop.open_path_with_app",
            {"app_name": "Preview", "path": ""},
        )
    with pytest.raises(
        AgentRuntimeError,
        match="desktop.open_path_with_app 参数 app_name 必须是非空字符串",
    ):
        ToolDescriptorRegistry.validate_payload(
            "desktop.open_path_with_app",
            {"app_name": "", "path": "~/Downloads/report.pdf"},
        )
    with pytest.raises(
        AgentRuntimeError,
        match="app.open_path_with_app 参数 app_name 必须是非空字符串",
    ):
        ToolDescriptorRegistry.validate_payload(
            "app.open_path_with_app",
            {"app_name": "", "path": "~/Downloads/report.pdf"},
        )


def test_system_settings_open_schema_requires_target() -> None:
    ToolDescriptorRegistry.validate_payload(
        "system.settings_open",
        {"target": "辅助功能权限"},
    )

    with pytest.raises(AgentRuntimeError, match="system.settings_open 参数 target 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("system.settings_open", {})
    with pytest.raises(AgentRuntimeError, match="system.settings_open 参数 target 必须是字符串"):
        ToolDescriptorRegistry.validate_payload("system.settings_open", {"target": 123})


def test_system_volume_schema_accepts_safe_volume_actions() -> None:
    ToolDescriptorRegistry.validate_payload("system.volume", {"action": "status"})
    ToolDescriptorRegistry.validate_payload("system.volume", {"action": "set", "level": 35})
    ToolDescriptorRegistry.validate_payload("system.volume", {"action": "up", "step": 5})
    ToolDescriptorRegistry.validate_payload("system.volume", {"action": "mute"})

    with pytest.raises(AgentRuntimeError, match="system.volume 参数 action"):
        ToolDescriptorRegistry.validate_payload("system.volume", {"action": "shutdown"})
    with pytest.raises(AgentRuntimeError, match="system.volume 参数 level"):
        ToolDescriptorRegistry.validate_payload("system.volume", {"action": "set"})
    with pytest.raises(AgentRuntimeError, match="system.volume 参数 level"):
        ToolDescriptorRegistry.validate_payload("system.volume", {"action": "set", "level": 150})


def test_system_brightness_schema_accepts_relative_brightness_actions() -> None:
    ToolDescriptorRegistry.validate_payload("system.brightness", {"action": "up"})
    ToolDescriptorRegistry.validate_payload("system.brightness", {"action": "down", "step": 3})

    with pytest.raises(AgentRuntimeError, match="system.brightness 参数 action"):
        ToolDescriptorRegistry.validate_payload("system.brightness", {"action": "set"})
    with pytest.raises(AgentRuntimeError, match="system.brightness 参数 step"):
        ToolDescriptorRegistry.validate_payload("system.brightness", {"action": "up", "step": 0})
    with pytest.raises(AgentRuntimeError, match="system.brightness 参数 step"):
        ToolDescriptorRegistry.validate_payload("system.brightness", {"action": "down", "step": True})


def test_system_display_sleep_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("system.display_sleep", {})


def test_system_screen_saver_start_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("system.screen_saver_start", {})


def test_clipboard_write_schema_requires_text() -> None:
    ToolDescriptorRegistry.validate_payload("clipboard.write", {"text": "hello"})

    with pytest.raises(AgentRuntimeError, match="clipboard.write 参数 text 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("clipboard.write", {"text": ""})


def test_clipboard_read_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("clipboard.read", {})
    ToolDescriptorRegistry.validate_payload("clipboard.read", {"max_chars": 120})


def test_notes_create_schema_requires_body() -> None:
    ToolDescriptorRegistry.validate_payload("notes.create", {"body": "hello"})
    ToolDescriptorRegistry.validate_payload(
        "notes.create",
        {"body": "hello", "title": "Greeting", "folder_name": "Notes"},
    )

    with pytest.raises(AgentRuntimeError, match="notes.create 参数 body 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("notes.create", {"body": ""})


def test_native_schedule_creation_schemas_validate_titles_and_times() -> None:
    ToolDescriptorRegistry.validate_payload("reminders.create", {"title": "开会"})
    ToolDescriptorRegistry.validate_payload(
        "reminders.create",
        {"title": "开会", "due_at": "2026-06-25T15:00"},
    )
    ToolDescriptorRegistry.validate_payload(
        "calendar.create_event",
        {"title": "开会", "start_at": "2026-06-25T15:00"},
    )
    ToolDescriptorRegistry.validate_payload(
        "calendar.create_event",
        {
            "title": "开会",
            "start_at": "2026-06-25T15:00",
            "end_at": "2026-06-25T16:00",
        },
    )

    with pytest.raises(AgentRuntimeError, match="reminders.create 参数 title 必须是"):
        ToolDescriptorRegistry.validate_payload("reminders.create", {"title": ""})
    with pytest.raises(AgentRuntimeError, match="reminders.create 参数 due_at 必须是"):
        ToolDescriptorRegistry.validate_payload("reminders.create", {"title": "开会", "due_at": "tomorrow"})
    with pytest.raises(AgentRuntimeError, match="reminders.create 参数 due_at 必须是"):
        ToolDescriptorRegistry.validate_payload("reminders.create", {"title": "开会", "due_at": "2026-06-25"})
    with pytest.raises(AgentRuntimeError, match="calendar.create_event 参数 start_at 必须是"):
        ToolDescriptorRegistry.validate_payload("calendar.create_event", {"title": "开会"})
    with pytest.raises(AgentRuntimeError, match="calendar.create_event 参数 end_at 必须是"):
        ToolDescriptorRegistry.validate_payload(
            "calendar.create_event",
            {"title": "开会", "start_at": "2026-06-25T15:00", "end_at": "tomorrow"},
        )


def test_desktop_close_window_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.close_window", {})


def test_desktop_quit_app_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.quit_app", {})


def test_desktop_minimize_window_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.minimize_window", {})


def test_desktop_hide_app_schema_accepts_empty_payload() -> None:
    ToolDescriptorRegistry.validate_payload("desktop.hide_app", {})


def test_compile_tool_policy_accepts_desktop_tools_with_foreground_approval() -> None:
    compiler = RuntimePolicyCompiler()

    policy = compiler.compile_tool_policy(
        "custom",
        {
            "allowed_tools": [
                "screen.capture",
                "app.focus_window",
                "app.show",
                "app.hide",
                "app.minimize",
                "desktop.hide_app",
                "desktop.minimize_window",
                "desktop.safe_key",
                "desktop.safe_scroll",
                "app.quit",
                "app.open_and_click_ui_element",
                "app.focus_and_click_ui_element",
                "app.open_and_type_into_ui_element",
                "app.focus_and_type_into_ui_element",
                "app.open_and_hotkey",
                "app.focus_and_hotkey",
                "desktop.close_window",
                "desktop.click",
                "desktop.click_ui_element",
                "desktop.type_into_ui_element",
                "desktop.type_text",
                "terminal.run",
            ]
        },
    )

    assert policy["allowed_tools"] == [
        "screen.capture",
        "app.focus_window",
        "app.show",
        "app.hide",
        "app.minimize",
        "desktop.hide_app",
        "desktop.minimize_window",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "app.quit",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
        "desktop.close_window",
        "desktop.click",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.type_text",
        "terminal.run",
    ]
    assert policy["approval_required"] == {
        "app.quit": True,
        "app.open_and_click_ui_element": True,
        "app.focus_and_click_ui_element": True,
        "app.open_and_type_into_ui_element": True,
        "app.focus_and_type_into_ui_element": True,
        "app.open_and_hotkey": True,
        "app.focus_and_hotkey": True,
        "desktop.close_window": True,
        "desktop.click": True,
        "desktop.click_ui_element": True,
        "desktop.type_into_ui_element": True,
        "desktop.type_text": True,
        "terminal.run": True,
    }


def test_compile_tool_policy_marks_python_run_as_approval_required() -> None:
    policy = RuntimePolicyCompiler().compile_tool_policy(
        "custom",
        {"allowed_tools": ["python.run", "workspace.read"]},
    )

    assert policy == {
        "allowed_tools": ["python.run", "workspace.read"],
        "approval_required": {"python.run": True},
    }
    assert "python.run" in HIGH_RISK_AGENT_TOOLS


def test_compile_tool_policy_marks_fs_move_file_as_approval_required() -> None:
    policy = RuntimePolicyCompiler().compile_tool_policy(
        "custom",
        {"allowed_tools": ["fs.move_file", "workspace.list"]},
    )

    assert policy == {
        "allowed_tools": ["fs.move_file", "workspace.list"],
        "approval_required": {"fs.move_file": True},
    }
    assert "fs.move_file" in HIGH_RISK_AGENT_TOOLS


def test_desktop_click_schema_accepts_coordinates_and_rejects_bad_payload() -> None:
    ToolDescriptorRegistry.validate_payload(
        "desktop.click",
        {"x": 12, "y": 34.5, "click_count": 2},
    )

    with pytest.raises(AgentRuntimeError, match="desktop.click 参数 x 必须是非负坐标数字"):
        ToolDescriptorRegistry.validate_payload("desktop.click", {"x": -1, "y": 34})
    with pytest.raises(AgentRuntimeError, match="desktop.click 参数 click_count 必须是 1-3"):
        ToolDescriptorRegistry.validate_payload(
            "desktop.click",
            {"x": 12, "y": 34, "click_count": 4},
        )


def test_apple_music_control_schema_accepts_safe_playback_actions() -> None:
    ToolDescriptorRegistry.validate_payload(
        "media.apple_music_control",
        {"action": "pause"},
    )
    ToolDescriptorRegistry.validate_payload(
        "media.apple_music_control",
        {"action": "next"},
    )

    with pytest.raises(
        AgentRuntimeError,
        match="media.apple_music_control 参数 action 必须是",
    ):
        ToolDescriptorRegistry.validate_payload(
            "media.apple_music_control",
            {"action": "volume_up"},
        )


def test_music_app_open_and_play_schema_requires_app_name() -> None:
    ToolDescriptorRegistry.validate_payload(
        "media.music_app_open_and_play",
        {"app_name": "Spotify"},
    )

    with pytest.raises(AgentRuntimeError, match="media.music_app_open_and_play 参数 app_name 必须是非空字符串"):
        ToolDescriptorRegistry.validate_payload("media.music_app_open_and_play", {})


def test_browser_click_schema_exposes_only_cdp_page_target_fields() -> None:
    ToolDescriptorRegistry.validate_payload(
        "browser.open_url_and_extract_text",
        {"url": "https://example.com/docs", "selector": "main"},
    )
    ToolDescriptorRegistry.validate_payload(
        "browser.open_url_and_screenshot",
        {"url": "https://example.com/docs", "reason": "capture docs"},
    )
    ToolDescriptorRegistry.validate_payload(
        "browser.click",
        {"selector": "#submit", "click_count": 2},
    )

    with pytest.raises(AgentRuntimeError, match="参数包含未声明字段"):
        ToolDescriptorRegistry.validate_payload(
            "browser.click",
            {"selector": "#submit", "fallback_x": 12, "fallback_y": 34},
        )


def test_browser_type_text_schema_exposes_only_cdp_page_target_fields() -> None:
    ToolDescriptorRegistry.validate_payload(
        "browser.type_text",
        {"selector": "point=12,34", "text": "hello"},
    )

    with pytest.raises(AgentRuntimeError, match="参数包含未声明字段"):
        ToolDescriptorRegistry.validate_payload(
            "browser.type_text",
            {"selector": "point=12,34", "text": "hello", "fallback_y": 34},
        )


def test_compile_tool_policy_accepts_browser_tools_with_interaction_approval() -> None:
    compiler = RuntimePolicyCompiler()

    policy = compiler.compile_tool_policy(
        "custom",
        {"allowed_tools": ["browser.open_url", "browser.click", "workspace.write_patch"]},
    )

    assert policy["allowed_tools"] == ["browser.open_url", "browser.click", "workspace.write_patch"]
    assert policy["approval_required"] == {"browser.click": True, "workspace.write_patch": True}


def test_tool_dispatch_registry_routes_desktop_tools(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    calls = []

    monkeypatch.setattr(
        broker,
        "media_apple_music_play",
        lambda query: calls.append(("music", query)) or {"ok": True, "query": query},
    )
    monkeypatch.setattr(
        broker,
        "media_apple_music_control",
        lambda action: calls.append(("music_control", action)) or {"ok": True, "action": action},
    )
    monkeypatch.setattr(
        broker,
        "media_apple_music_open_and_play",
        lambda: calls.append(("music_open_and_play",)) or {"ok": True, "action": "open_and_play"},
    )
    monkeypatch.setattr(
        broker,
        "notes_create",
        lambda body, *, title="", folder_name="": calls.append(("note", body, title, folder_name))
        or {"ok": True, "body": body},
    )
    monkeypatch.setattr(
        broker,
        "desktop_safe_shortcut",
        lambda action: calls.append(("safe_shortcut", action)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_safe_key",
        lambda action, *, repeat_count=1: calls.append(("safe_key", action, repeat_count))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_safe_type_text",
        lambda text: calls.append(("safe_type_text", text)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_safe_click",
        lambda x, y: calls.append(("safe_click", x, y)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_safe_scroll",
        lambda direction, *, pages=1: calls.append(("safe_scroll", direction, pages))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_click_ui_element",
        lambda target, *, role_filter="", limit=80, click_count=1: calls.append(
            ("click_ui_element", target, role_filter, limit, click_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_type_into_ui_element",
        lambda target, text, *, role_filter="", limit=80: calls.append(
            ("type_into_ui_element", target, text, role_filter, limit)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_hotkey",
        lambda key, *, modifiers=None: calls.append(("hotkey", key, modifiers))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_submit_foreground",
        lambda action="submit": calls.append(("submit_foreground", action))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_click",
        lambda x, y, *, click_count=1: calls.append(("click", x, y, click_count))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_hide_app",
        lambda: calls.append(("hide_app",)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_minimize_window",
        lambda: calls.append(("minimize_window",)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_close_window",
        lambda: calls.append(("close_window",)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_quit_app",
        lambda: calls.append(("quit_app",)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "desktop_reveal_path",
        lambda path: calls.append(("reveal", path)) or {"ok": True, "path": path},
    )
    monkeypatch.setattr(
        broker,
        "desktop_open_path_with_app",
        lambda path, app_name: calls.append(("open_path_with_app", path, app_name))
        or {"ok": True, "path": path, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "system_settings_open",
        lambda target: calls.append(("settings_open", target)) or {"ok": True, "target": target},
    )
    monkeypatch.setattr(
        broker,
        "desktop_permissions",
        lambda: calls.append(("permissions",)) or {"ok": True, "action": "desktop.permissions"},
    )
    monkeypatch.setattr(
        broker,
        "desktop_running_apps",
        lambda: calls.append(("running",)) or {"ok": True, "apps": ["Finder"]},
    )
    monkeypatch.setattr(
        broker,
        "desktop_list_apps",
        lambda query="", limit=200: calls.append(("list_apps", query, limit))
        or {"ok": True, "query": query, "limit": limit},
    )
    monkeypatch.setattr(
        broker,
        "desktop_windows",
        lambda app_name="": calls.append(("windows", app_name)) or {"ok": True, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "desktop_ui_elements",
        lambda role_filter="", limit=80, app_name="": calls.append(
            ("ui_elements", role_filter, limit, app_name)
        )
        or {"ok": True, "role_filter": role_filter, "limit": limit, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "desktop_inspect_app",
        lambda app_name, *, open_if_needed=True, focus=True, role_filter="", limit=80: calls.append(
            ("inspect_app", app_name, open_if_needed, focus, role_filter, limit)
        )
        or {
            "ok": True,
            "app_name": app_name,
            "open_if_needed": open_if_needed,
            "focus": focus,
            "role_filter": role_filter,
            "limit": limit,
        },
    )
    monkeypatch.setattr(
        broker,
        "app_status",
        lambda app_name: calls.append(("status", app_name)) or {"ok": True, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_window",
        lambda app_name, title_contains: calls.append(("focus_window", app_name, title_contains))
        or {"ok": True, "app_name": app_name, "title_contains": title_contains},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_safe_type_text",
        lambda app_name, text: calls.append(("open_type", app_name, text)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_safe_type_text",
        lambda app_name, text: calls.append(("focus_type", app_name, text)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_safe_shortcut",
        lambda app_name, action: calls.append(("open_shortcut", app_name, action)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_safe_shortcut",
        lambda app_name, action: calls.append(("focus_shortcut", app_name, action)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_safe_key",
        lambda app_name, action, *, repeat_count=1: calls.append(
            ("open_key", app_name, action, repeat_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_safe_key",
        lambda app_name, action, *, repeat_count=1: calls.append(
            ("focus_key", app_name, action, repeat_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_hotkey",
        lambda app_name, key, *, modifiers=None: calls.append(
            ("open_hotkey", app_name, key, list(modifiers or []))
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_hotkey",
        lambda app_name, key, *, modifiers=None: calls.append(
            ("focus_hotkey", app_name, key, list(modifiers or []))
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_safe_scroll",
        lambda app_name, direction, *, pages=1: calls.append(
            ("open_scroll", app_name, direction, pages)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_safe_scroll",
        lambda app_name, direction, *, pages=1: calls.append(
            ("focus_scroll", app_name, direction, pages)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_safe_click",
        lambda app_name, x, y: calls.append(("open_click", app_name, x, y))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_safe_click",
        lambda app_name, x, y: calls.append(("focus_click", app_name, x, y))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_click_ui_element",
        lambda app_name, target, *, role_filter="", limit=80, click_count=1: calls.append(
            ("open_click_ui", app_name, target, role_filter, limit, click_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_click_ui_element",
        lambda app_name, target, *, role_filter="", limit=80, click_count=1: calls.append(
            ("focus_click_ui", app_name, target, role_filter, limit, click_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_open_and_type_into_ui_element",
        lambda app_name, target, text, *, role_filter="", limit=80: calls.append(
            ("open_type_into_ui", app_name, target, text, role_filter, limit)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_focus_and_type_into_ui_element",
        lambda app_name, target, text, *, role_filter="", limit=80: calls.append(
            ("focus_type_into_ui", app_name, target, text, role_filter, limit)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "app_show",
        lambda app_name: calls.append(("show_named_app", app_name))
        or {"ok": True, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "app_hide",
        lambda app_name: calls.append(("hide_named_app", app_name))
        or {"ok": True, "app_name": app_name},
    )
    monkeypatch.setattr(
        broker,
        "app_minimize",
        lambda app_name: calls.append(("minimize_named_app", app_name))
        or {"ok": True, "app_name": app_name},
    )

    assert dispatch_tool_call(
        broker,
        "media.apple_music_play",
        {"query": "超时空辉夜姬"},
    ) == {"ok": True, "query": "超时空辉夜姬"}
    assert dispatch_tool_call(
        broker,
        "media.apple_music_control",
        {"action": "pause"},
    ) == {"ok": True, "action": "pause"}
    assert dispatch_tool_call(
        broker,
        "media.apple_music_open_and_play",
        {},
    ) == {"ok": True, "action": "open_and_play"}
    assert dispatch_tool_call(
        broker,
        "notes.create",
        {"body": "hello", "title": "Greeting", "folder_name": "Notes"},
    ) == {"ok": True, "body": "hello"}
    assert dispatch_tool_call(
        broker,
        "desktop.safe_shortcut",
        {"action": "copy"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.safe_key",
        {"action": "arrow_down", "repeat_count": 3},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.safe_type_text",
        {"text": "hello"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.safe_click",
        {"x": 12, "y": 34},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.safe_scroll",
        {"direction": "down", "pages": 2},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.click_ui_element",
        {"target": "Send", "role_filter": "button", "limit": 20, "click_count": 2},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.type_into_ui_element",
        {"target": "Search", "text": "hello", "role_filter": "text", "limit": 20},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.hotkey",
        {"key": "l", "modifiers": ["command"]},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.submit_foreground",
        {"action": "send"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.click",
        {"x": 12, "y": 34, "click_count": 2},
    ) == {"ok": True}
    assert dispatch_tool_call(broker, "desktop.hide_app", {}) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_window",
        {"app_name": "Slack", "title_contains": "general"},
    ) == {
        "ok": True,
        "app_name": "Slack",
        "title_contains": "general",
    }
    assert dispatch_tool_call(
        broker,
        "app.open_and_safe_type_text",
        {"app_name": "Notes", "text": "hello"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_safe_type_text",
        {"app_name": "Notes", "text": "hello"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_safe_shortcut",
        {"app_name": "Google Chrome", "action": "new_tab"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_safe_shortcut",
        {"app_name": "Slack", "action": "paste"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_safe_key",
        {"app_name": "Google Chrome", "action": "tab"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_safe_key",
        {"app_name": "Slack", "action": "arrow_down", "repeat_count": 3},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_hotkey",
        {"app_name": "Google Chrome", "key": "l", "modifiers": ["command"]},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_hotkey",
        {"app_name": "Slack", "key": "k", "modifiers": ["command"]},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_safe_scroll",
        {"app_name": "Google Chrome", "direction": "down"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_safe_scroll",
        {"app_name": "Slack", "direction": "up", "pages": 3},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_safe_click",
        {"app_name": "Google Chrome", "x": 120, "y": 240},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_safe_click",
        {"app_name": "Slack", "x": 320, "y": 180},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_click_ui_element",
        {
            "app_name": "Google Chrome",
            "target": "Sign in",
            "role_filter": "button",
            "limit": 20,
            "click_count": 2,
        },
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_click_ui_element",
        {"app_name": "Slack", "target": "Send"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.open_and_type_into_ui_element",
        {
            "app_name": "Google Chrome",
            "target": "Address",
            "text": "github.com",
            "role_filter": "text",
            "limit": 20,
        },
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "app.focus_and_type_into_ui_element",
        {"app_name": "Slack", "target": "Message", "text": "hello"},
    ) == {"ok": True}
    assert dispatch_tool_call(broker, "app.show", {"app_name": "Slack"}) == {
        "ok": True,
        "app_name": "Slack",
    }
    assert dispatch_tool_call(broker, "app.hide", {"app_name": "Slack"}) == {
        "ok": True,
        "app_name": "Slack",
    }
    assert dispatch_tool_call(broker, "app.minimize", {"app_name": "Slack"}) == {
        "ok": True,
        "app_name": "Slack",
    }
    assert dispatch_tool_call(broker, "desktop.minimize_window", {}) == {"ok": True}
    assert dispatch_tool_call(broker, "desktop.close_window", {}) == {"ok": True}
    assert dispatch_tool_call(broker, "desktop.quit_app", {}) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "desktop.reveal_path",
        {"path": "~/Downloads/report.pdf"},
    ) == {"ok": True, "path": "~/Downloads/report.pdf"}
    assert dispatch_tool_call(
        broker,
        "app.open_path_with_app",
        {"path": "~/Downloads/report.pdf", "app_name": "Preview"},
    ) == {"ok": True, "path": "~/Downloads/report.pdf", "app_name": "Preview"}
    assert dispatch_tool_call(
        broker,
        "system.settings_open",
        {"target": "辅助功能权限"},
    ) == {"ok": True, "target": "辅助功能权限"}
    assert dispatch_tool_call(broker, "desktop.permissions", {}) == {
        "ok": True,
        "action": "desktop.permissions",
    }
    assert dispatch_tool_call(broker, "desktop.running_apps", {}) == {
        "ok": True,
        "apps": ["Finder"],
    }
    assert dispatch_tool_call(broker, "desktop.list_apps", {"query": "Word", "limit": 10}) == {
        "ok": True,
        "query": "Word",
        "limit": 10,
    }
    assert dispatch_tool_call(broker, "desktop.windows", {"app_name": "Google Chrome"}) == {
        "ok": True,
        "app_name": "Google Chrome",
    }
    assert dispatch_tool_call(
        broker,
        "desktop.ui_elements",
        {"app_name": "Google Chrome", "role_filter": "button", "limit": 20},
    ) == {"ok": True, "role_filter": "button", "limit": 20, "app_name": "Google Chrome"}
    assert dispatch_tool_call(
        broker,
        "desktop.inspect_app",
        {
            "app_name": "Linear",
            "open_if_needed": False,
            "focus": True,
            "role_filter": "button",
            "limit": 30,
        },
    ) == {
        "ok": True,
        "app_name": "Linear",
        "open_if_needed": False,
        "focus": True,
        "role_filter": "button",
        "limit": 30,
    }
    assert dispatch_tool_call(
        broker,
        "app.status",
        {"app_name": "Google Chrome"},
    ) == {"ok": True, "app_name": "Google Chrome"}
    assert calls == [
        ("music", "超时空辉夜姬"),
        ("music_control", "pause"),
        ("music_open_and_play",),
        ("note", "hello", "Greeting", "Notes"),
        ("safe_shortcut", "copy"),
        ("safe_key", "arrow_down", 3),
        ("safe_type_text", "hello"),
        ("safe_click", 12, 34),
        ("safe_scroll", "down", 2),
        ("click_ui_element", "Send", "button", 20, 2),
        ("type_into_ui_element", "Search", "hello", "text", 20),
        ("hotkey", "l", ["command"]),
        ("submit_foreground", "send"),
        ("click", 12, 34, 2),
        ("hide_app",),
        ("focus_window", "Slack", "general"),
        ("open_type", "Notes", "hello"),
        ("focus_type", "Notes", "hello"),
        ("open_shortcut", "Google Chrome", "new_tab"),
        ("focus_shortcut", "Slack", "paste"),
        ("open_key", "Google Chrome", "tab", 1),
        ("focus_key", "Slack", "arrow_down", 3),
        ("open_hotkey", "Google Chrome", "l", ["command"]),
        ("focus_hotkey", "Slack", "k", ["command"]),
        ("open_scroll", "Google Chrome", "down", 1),
        ("focus_scroll", "Slack", "up", 3),
        ("open_click", "Google Chrome", 120, 240),
        ("focus_click", "Slack", 320, 180),
        ("open_click_ui", "Google Chrome", "Sign in", "button", 20, 2),
        ("focus_click_ui", "Slack", "Send", "", 80, 1),
        ("open_type_into_ui", "Google Chrome", "Address", "github.com", "text", 20),
        ("focus_type_into_ui", "Slack", "Message", "hello", "", 80),
        ("show_named_app", "Slack"),
        ("hide_named_app", "Slack"),
        ("minimize_named_app", "Slack"),
        ("minimize_window",),
        ("close_window",),
        ("quit_app",),
        ("reveal", "~/Downloads/report.pdf"),
        ("open_path_with_app", "~/Downloads/report.pdf", "Preview"),
        ("settings_open", "辅助功能权限"),
        ("permissions",),
        ("running",),
        ("list_apps", "Word", 10),
        ("windows", "Google Chrome"),
        ("ui_elements", "button", 20, "Google Chrome"),
        ("inspect_app", "Linear", False, True, "button", 30),
        ("status", "Google Chrome"),
    ]


def test_tool_broker_app_open_and_safe_type_text_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Notes")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_type_text",
        lambda text: calls.append(("type", text))
        or {
            "ok": True,
            "action": "desktop.safe_type_text",
            "data": {"character_count": len(text), "explicit_user_text": True},
        },
    )

    result = broker.app_open_and_safe_type_text("Notes", "hello")

    assert calls == [("open", "Notes"), ("focus", "Notes"), ("type", "hello")]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_safe_type_text"
    assert result["data"] == {
        "app_name": "Notes",
        "foreground_action": "safe_type_text",
        "character_count": 5,
        "explicit_user_text": True,
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "safe_type_text",
    ]


def test_tool_broker_app_open_and_safe_key_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_key",
        lambda action, *, repeat_count=1: calls.append(("key", action))
        or {
            "ok": True,
            "action": "desktop.safe_key",
            "data": {
                "key_action": action,
                "key_label": "Tab",
                "key_code": 48,
                "repeat_count": repeat_count,
                "explicit_user_key": True,
            },
        },
    )

    result = broker.app_open_and_safe_key("Google Chrome", "tab", repeat_count=2)

    assert calls == [("open", "Google Chrome"), ("focus", "Google Chrome"), ("key", "tab")]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_safe_key"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "safe_key",
        "key_action": "tab",
        "key_label": "Tab",
        "key_code": 48,
        "repeat_count": 2,
        "explicit_user_key": True,
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "safe_key",
    ]


def test_tool_broker_app_open_and_hotkey_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_hotkey",
        lambda key, *, modifiers=None: calls.append(("hotkey", key))
        or {
            "ok": True,
            "action": "desktop.hotkey",
            "data": {"key": key, "modifiers": list(modifiers or [])},
        },
    )

    result = broker.app_open_and_hotkey("Google Chrome", "l", modifiers=["command"])

    assert calls == [("open", "Google Chrome"), ("focus", "Google Chrome"), ("hotkey", "l")]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_hotkey"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "hotkey",
        "key": "l",
        "modifiers": ["command"],
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "hotkey",
    ]


def test_tool_broker_app_open_and_safe_scroll_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_scroll",
        lambda direction, *, pages=1: calls.append(("scroll", direction))
        or {
            "ok": True,
            "action": "desktop.safe_scroll",
            "data": {
                "direction": direction,
                "pages": pages,
                "explicit_user_scroll": True,
            },
        },
    )

    result = broker.app_open_and_safe_scroll("Google Chrome", "down", pages=2)

    assert calls == [("open", "Google Chrome"), ("focus", "Google Chrome"), ("scroll", "down")]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_safe_scroll"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "safe_scroll",
        "direction": "down",
        "pages": 2,
        "explicit_user_scroll": True,
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "safe_scroll",
    ]


def test_tool_broker_app_open_and_safe_click_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, Any]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_click",
        lambda x, y: calls.append(("click", x, y))
        or {
            "ok": True,
            "action": "desktop.safe_click",
            "data": {
                "x": int(x),
                "y": int(y),
                "click_count": 1,
                "explicit_user_coordinates": True,
            },
        },
    )

    result = broker.app_open_and_safe_click("Google Chrome", 120, 240)

    assert calls == [("open", "Google Chrome"), ("focus", "Google Chrome"), ("click", 120, 240)]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_safe_click"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "safe_click",
        "x": 120,
        "y": 240,
        "click_count": 1,
        "explicit_user_coordinates": True,
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "safe_click",
    ]


def test_tool_broker_app_open_and_click_ui_element_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, Any]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    def fake_click_ui_element(
        target,
        *,
        role_filter="",
        limit=80,
        click_count=1,
        expected_app_name="",
    ):
        calls.append(("click_ui", target, role_filter, limit, click_count, expected_app_name))
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "data": {
                "target": target,
                "matched_label": "Sign in",
                "x": 120,
                "y": 240,
                "click_count": click_count,
                "role_filter": role_filter,
            },
        }

    monkeypatch.setattr(desktop_mod, "click_ui_element", fake_click_ui_element)

    result = broker.app_open_and_click_ui_element(
        "Google Chrome",
        "Sign in",
        role_filter="button",
        limit=20,
        click_count=2,
    )

    assert calls == [
        ("open", "Google Chrome"),
        ("focus", "Google Chrome"),
        ("click_ui", "Sign in", "button", 20, 2, "Google Chrome"),
    ]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_click_ui_element"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "click_ui_element",
        "target": "Sign in",
        "matched_label": "Sign in",
        "x": 120,
        "y": 240,
        "click_count": 2,
        "role_filter": "button",
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "click_ui_element",
    ]


def test_tool_broker_legacy_ui_callback_runs_only_after_exact_foreground_observation(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    click_calls: list[str] = []
    _stub_active_window(monkeypatch, "Google Chrome")
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": app_name, "focus_verified": True},
        },
    )

    def legacy_click(target: str, *, role_filter: str = "", limit: int = 80, click_count: int = 1):
        click_calls.append(target)
        return {
            "ok": True,
            "action": "desktop.click_ui_element",
            "data": {"target": target, "role_filter": role_filter},
        }

    monkeypatch.setattr(desktop_mod, "click_ui_element", legacy_click)

    result = broker.app_focus_and_click_ui_element(
        "Google Chrome",
        "Sign in",
        role_filter="button",
    )

    assert result["ok"] is True
    assert click_calls == ["Sign in"]
    assert result["fallback_result"]["active_window"]["data"]["app_name"] == (
        "Google Chrome"
    )


def test_tool_broker_legacy_ui_callback_fails_closed_for_wrong_foreground_app(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    click_calls: list[str] = []
    _stub_active_window(monkeypatch, "Safari")
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": app_name, "focus_verified": True},
        },
    )

    def legacy_click(target: str, *, role_filter: str = "", limit: int = 80, click_count: int = 1):
        click_calls.append(target)
        return {"ok": True, "action": "desktop.click_ui_element"}

    monkeypatch.setattr(desktop_mod, "click_ui_element", legacy_click)

    result = broker.app_focus_and_click_ui_element("Google Chrome", "Sign in")

    assert result["ok"] is False
    assert result["error"] == "foreground_app_mismatch"
    assert result["data"]["expected_app_name"] == "Google Chrome"
    assert result["data"]["active_app_name"] == "Safari"
    assert click_calls == []


def test_tool_broker_app_open_and_type_into_ui_element_sequences_foreground_action(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, Any]] = []
    _stub_active_window(monkeypatch, "Google Chrome")

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: calls.append(("open", app_name))
        or {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    def fake_type_into_ui_element(
        target,
        text,
        *,
        role_filter="",
        limit=80,
        expected_app_name="",
    ):
        calls.append(("type_into_ui", target, text, role_filter, limit, expected_app_name))
        return {
            "ok": True,
            "action": "desktop.type_into_ui_element",
            "data": {
                "target": target,
                "matched_label": "Address",
                "character_count": len(text),
                "role_filter": role_filter,
            },
        }

    monkeypatch.setattr(desktop_mod, "type_into_ui_element", fake_type_into_ui_element)

    result = broker.app_open_and_type_into_ui_element(
        "Google Chrome",
        "Address",
        "github.com",
        role_filter="text",
        limit=20,
    )

    assert calls == [
        ("open", "Google Chrome"),
        ("focus", "Google Chrome"),
        ("type_into_ui", "Address", "github.com", "text", 20, "Google Chrome"),
    ]
    assert result["ok"] is True
    assert result["action"] == "app.open_and_type_into_ui_element"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "foreground_action": "type_into_ui_element",
        "target": "Address",
        "matched_label": "Address",
        "character_count": 10,
        "role_filter": "text",
    }
    assert list(result["fallback_result"]) == [
        "open",
        "focus",
        "active_window",
        "type_into_ui_element",
    ]


def test_tool_broker_app_focus_and_safe_shortcut_reports_action_failure(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Slack")

    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_shortcut",
        lambda action: calls.append(("shortcut", action))
        or {
            "ok": False,
            "action": "desktop.safe_shortcut",
            "permission_error": True,
            "permission_targets": ["accessibility"],
            "data": {"shortcut_action": action},
        },
    )

    result = broker.app_focus_and_safe_shortcut("Slack", "paste")

    assert calls == [("focus", "Slack"), ("shortcut", "paste")]
    assert result["ok"] is False
    assert result["action"] == "app.focus_and_safe_shortcut"
    assert result["permission_targets"] == ["accessibility"]
    assert result["data"] == {
        "app_name": "Slack",
        "foreground_action": "safe_shortcut",
        "shortcut_action": "paste",
    }
    assert list(result["fallback_result"]) == [
        "focus",
        "active_window",
        "safe_shortcut",
    ]


def test_tool_broker_safe_shortcut_verifies_active_window_after_focus(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []
    _stub_active_window(monkeypatch, "Slack")

    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {"ok": True, "action": "app.focus", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_shortcut",
        lambda action: calls.append(("shortcut", action))
        or {
            "ok": True,
            "action": "desktop.safe_shortcut",
            "data": {"shortcut_action": action},
        },
    )

    result = broker.app_focus_and_safe_shortcut("Slack", "paste")

    assert calls == [("focus", "Slack"), ("shortcut", "paste")]
    assert result["ok"] is True
    assert result["action"] == "app.focus_and_safe_shortcut"
    assert result["summary"] == "Focused app and completed foreground action"
    assert result["data"] == {
        "app_name": "Slack",
        "foreground_action": "safe_shortcut",
        "shortcut_action": "paste",
    }
    assert list(result["fallback_result"]) == ["focus", "active_window", "safe_shortcut"]


def test_tool_broker_app_focus_and_safe_shortcut_stops_when_focus_unverified(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {
            "ok": True,
            "action": "app.focus",
            "summary": f"Could not verify {app_name} is foreground",
            "data": {
                "app_name": app_name,
                "focus_verified": False,
                "focus_status": "not_frontmost",
                "frontmost_app": "Codex",
            },
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_safe_shortcut",
        lambda action: calls.append(("shortcut", action)) or {"ok": True},
    )

    result = broker.app_focus_and_safe_shortcut("Slack", "paste")

    assert calls == [("focus", "Slack")]
    assert result["ok"] is False
    assert result["action"] == "app.focus_and_safe_shortcut"
    assert result["error"] == "app_focus_not_verified"
    assert result["summary"] == "Could not verify app focus before foreground action"
    assert result["blocking_condition"] == "foreground_focus_unavailable"
    assert result["blocking_conditions"] == ["foreground_focus_unavailable"]
    assert result["retryable"] is True
    assert result["data"] == {
        "app_name": "Slack",
        "focus_verified": False,
        "focus_status": "not_frontmost",
        "frontmost_app": "Codex",
        "blocking_condition": "foreground_focus_unavailable",
        "retryable": True,
    }
    assert result["recovery_actions"][0]["tool"] == "app.open"
    assert result["recovery_actions"][1]["tool"] == "desktop.active_window"
    assert result["recovery_actions"][2]["tool"] == "screen.capture"
    assert result["recovery_actions"][3] == {
        "label": "打开自动化权限",
        "tool": "system.settings_open",
        "input": {"target": "自动化权限"},
        "permission_target": "automation",
        "risk_level": "low",
    }
    assert result["recovery_actions"][4] == {
        "label": "打开辅助功能权限",
        "tool": "system.settings_open",
        "input": {"target": "辅助功能权限"},
        "permission_target": "accessibility",
        "risk_level": "low",
    }
    assert list(result["fallback_result"]) == ["focus"]


def test_tool_dispatch_registry_routes_browser_tools(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    calls = []
    search_url = "https://www.google.com/search?q=open+hanako"
    search_evidence = {
        "ok": True,
        "action": "browser.open_url_and_extract_text",
        "summary": "Opened browser page and extracted text",
        "data": {
            "selector": "",
            "text": "OpenHanako result evidence",
            "truncated": False,
            "page_url": search_url,
            "page_url_truncated": False,
            "link_contexts": [
                {"href": "https://example.com/openhanako", "text": "OpenHanako"}
            ],
        },
        "permission_error": False,
        "fallback_used": False,
    }

    def open_and_extract(url: str, *, selector: str = "") -> dict:
        calls.append(("open_extract", url, selector))
        return search_evidence if url == search_url else {"ok": True}

    monkeypatch.setattr(
        broker,
        "browser_open_url",
        lambda url: calls.append(("open", url)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "browser_open_url_and_extract_text",
        open_and_extract,
    )
    monkeypatch.setattr(
        broker,
        "browser_open_url_and_screenshot",
        lambda url, *, reason="": calls.append(("open_screenshot", url, reason)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "browser_click",
        lambda selector, *, fallback_x=None, fallback_y=None, click_count=1: calls.append(
            ("click", selector, fallback_x, fallback_y, click_count)
        )
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "browser_type_text",
        lambda selector, text, **kwargs: calls.append(("type", selector, text, kwargs))
        or {"ok": True},
    )
    monkeypatch.setattr(
        broker,
        "browser_extract_text",
        lambda selector="": calls.append(("extract", selector)) or {"ok": True},
    )

    search_result = dispatch_tool_call(
        broker,
        "browser.search",
        {"query": "open hanako"},
    )
    assert search_result["action"] == "browser.search"
    assert search_result["data"] == {
        **search_evidence["data"],
        "query": "open hanako",
        "search_url": search_url,
    }
    assert search_result["summary"] == search_evidence["summary"]
    assert search_result["permission_error"] is False
    assert dispatch_tool_call(
        broker,
        "browser.open",
        {"url": "https://example.com/alias"},
    ) == {"ok": True}

    assert dispatch_tool_call(
        broker,
        "browser.open_url",
        {"url": "https://example.com"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "browser.open_url_and_extract_text",
        {"url": "https://example.com/docs", "selector": "main"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "browser.open_url_and_screenshot",
        {"url": "https://example.com/docs", "reason": "capture docs"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "browser.click",
        {"selector": "#go", "click_count": 2},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "browser.type_text",
        {"selector": "point=12,34", "text": "八千代"},
    ) == {"ok": True}
    assert dispatch_tool_call(
        broker,
        "browser.extract",
        {"selector": "main"},
    ) == {"ok": True}
    assert calls == [
        ("open_extract", search_url, ""),
        ("open", "https://example.com/alias"),
        ("open", "https://example.com"),
        ("open_extract", "https://example.com/docs", "main"),
        ("open_screenshot", "https://example.com/docs", "capture docs"),
        ("click", "#go", None, None, 2),
        ("type", "point=12,34", "八千代", {}),
        ("extract", "main"),
    ]


def test_browser_current_page_cdp_failure_returns_recovery_action(monkeypatch) -> None:
    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "")

    result = browser_mod.current_page()

    assert result["ok"] is False
    assert result["action"] == "browser.current_page"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert result["recovery_actions"] == [
        {
            "label": "打开 Google Chrome",
            "tool": "app.open",
            "input": {"app_name": "Google Chrome"},
            "permission_target": "chrome_cdp",
            "risk_level": "low",
        }
    ]


def test_screen_capture_tool_writes_artifact_metadata(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)

    def fake_capture(target):
        target.write_bytes(b"png")
        return {
            "path": str(target),
            "mime_type": "image/png",
            "format": "png",
            "width": 320,
            "height": 200,
            "size": 3,
        }

    monkeypatch.setattr("apps.shell.agent.tools.desktop._desktop_platform", lambda: "macos")
    monkeypatch.setattr("apps.locald.screenshot.capture_screenshot_to_file", fake_capture)

    result = broker.call("screen.capture", {"reason": "check desktop"})

    assert result["ok"] is True
    assert result["reason"] == "check desktop"
    assert result["artifact"] == {
        "path": "screenshots/current-screen.png",
        "kind": "image",
        "mime_type": "image/png",
        "size_bytes": 3,
        "width": 320,
        "height": 200,
    }
    assert (tmp_path / "artifacts" / "screenshots" / "current-screen.png").read_bytes() == b"png"


def test_screen_capture_permission_failure_returns_recovery_targets(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)

    class ScreenCapturePermissionError(RuntimeError):
        pass

    def fake_capture(_target):
        raise ScreenCapturePermissionError("screen recording permission denied")

    monkeypatch.setattr("apps.shell.agent.tools.desktop._desktop_platform", lambda: "macos")
    monkeypatch.setattr("apps.locald.screenshot.capture_screenshot_to_file", fake_capture)

    result = broker.call("screen.capture", {"reason": "check desktop"})

    assert result["ok"] is False
    assert result["action"] == "screen.capture"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["screen_recording"]
    assert result["permission_targets"] == ["screen_recording"]
    assert result["recovery_hints"] == [
        (
            "Grant Screen Recording permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Screen Recording."
        )
    ]
    assert result["recovery_actions"] == [
        {
            "label": "打开屏幕录制权限",
            "tool": "system.settings_open",
            "input": {"target": "屏幕录制权限"},
            "permission_target": "screen_recording",
            "risk_level": "low",
        }
    ]


def test_app_open_failure_returns_unified_desktop_result(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Application not found.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [])

    result = desktop_mod.app_open("Missing App")

    assert result["ok"] is False
    assert result["action"] == "app.open"
    assert result["summary"] == "app.open failed"
    assert result["data"] == {"app_name": "Missing App"}
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["error_code"] == "app_not_found"
    assert "确认应用已安装" in result["recovery_hints"][0]
    assert result["recovery_actions"] == [
        {
            "label": "打开应用程序文件夹",
            "tool": "desktop.open_path",
            "input": {"path": "/Applications"},
            "permission_target": "app_not_found",
            "risk_level": "low",
        },
        {
            "label": "打开 App Store",
            "tool": "app.open",
            "input": {"app_name": "App Store"},
            "permission_target": "app_not_found",
            "risk_level": "low",
        },
    ]


def test_app_open_resolves_installed_bundle_after_open_failure(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Microsoft Word.app").mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0 if command == ["open", "-a", "Microsoft Word"] else 1,
            stdout="",
            stderr="" if command == ["open", "-a", "Microsoft Word"] else "Application not found.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {"ok": True, "stdout": "running", "stderr": ""},
    )

    result = desktop_mod.app_open("Word")

    assert result["ok"] is True
    assert result["action"] == "app.open"
    assert result["summary"] == "Opened Microsoft Word"
    assert result["permission_error"] is False
    assert result["fallback_used"] is True
    assert result["data"] == {
        "app_name": "Microsoft Word",
        "launch_verified": True,
        "launch_status": "running",
        "requested_app_name": "Word",
        "resolved_app_name": "Microsoft Word",
        "app_resolution": "installed_app_bundle",
        "app_resolution_source": "desktop.list_apps",
        "app_resolution_score": 100,
        "app_resolution_confidence": "high",
        "app_resolution_reason": "exact_name",
        "app_resolution_matched_name": "Microsoft Word",
        "app_resolution_matched_name_source": "bundle_name",
        "resolved_app_path": str(app_dir / "Microsoft Word.app"),
    }
    assert [call[0] for call in calls] == [
        ["open", "-a", "Word"],
        ["open", "-a", "Microsoft Word"],
    ]


def test_app_open_resolves_shorter_bundle_from_qualified_request(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Music.app").mkdir(parents=True)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            0 if command == ["open", "-a", "Music"] else 1,
            stdout="",
            stderr="" if command == ["open", "-a", "Music"] else "Application not found.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {"ok": True, "stdout": "running", "stderr": ""},
    )

    result = desktop_mod.app_open("Apple Music")

    assert result["ok"] is True
    assert result["summary"] == "Opened Music"
    assert result["fallback_used"] is True
    assert result["data"]["app_name"] == "Music"
    assert result["data"]["requested_app_name"] == "Apple Music"
    assert result["data"]["resolved_app_name"] == "Music"
    assert result["data"]["app_resolution_source"] == "desktop.list_apps"
    assert result["data"]["app_resolution_score"] == 100
    assert result["data"]["app_resolution_confidence"] == "high"
    assert result["data"]["app_resolution_reason"] == "exact_name"
    assert result["data"]["app_resolution_matched_name"] == "Music"
    assert result["data"]["app_resolution_matched_name_source"] == "bundle_name"
    assert result["data"]["resolved_app_path"] == str(app_dir / "Music.app")
    assert [call[0] for call in calls] == [
        ["open", "-a", "Apple Music"],
        ["open", "-a", "Music"],
    ]


def test_app_open_success_records_launch_verification(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {"ok": True, "stdout": "running", "stderr": ""},
    )

    result = desktop_mod.app_open("Google Chrome")

    assert result["ok"] is True
    assert result["action"] == "app.open"
    assert result["data"] == {
        "app_name": "Google Chrome",
        "launch_verified": True,
        "launch_status": "running",
    }
    assert calls[0][0] == ["open", "-a", "Google Chrome"]


def test_tool_broker_projects_existing_native_app_verification_receipts(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda _app_name: {
            "ok": True,
            "action": "app.open",
            "data": {"app_name": "Notes", "launch_verified": True},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda _app_name: {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": "Notes", "focus_verified": True},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus_window",
        lambda _app_name, _title: {
            "ok": True,
            "action": "app.focus_window",
            "data": {
                "app_name": "Slack",
                "focus_status": "focused",
                "window_title": "general - Slack",
            },
        },
    )

    opened = broker.app_open("Notes")
    focused = broker.app_focus("Notes")
    window = broker.app_focus_window("Slack", "general")

    for result in (opened, focused, window):
        assert result["postcondition_verified"] is True
        assert result["data"]["postcondition_verified"] is True


def test_tool_broker_does_not_promote_unverified_app_open(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda _app_name: {
            "ok": True,
            "action": "app.open",
            "data": {"app_name": "Notes", "launch_verified": False},
        },
    )

    result = broker.app_open("Notes")

    assert "postcondition_verified" not in result
    assert "postcondition_verified" not in result["data"]


def test_app_open_handles_common_finder_folder_aliases(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.app_open("下载文件夹")

    assert result["ok"] is True
    assert result["action"] == "app.open"
    assert result["summary"] == "Opened Downloads"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "app_name": "下载文件夹",
        "path": str(desktop_mod.Path.home() / "Downloads"),
        "open_target": "folder",
    }
    assert calls[0][0] == ["open", str(desktop_mod.Path.home() / "Downloads")]

    utilities = desktop_mod.app_open("Utilities folder")
    library = desktop_mod.app_open("Library folder")

    assert utilities["ok"] is True
    assert utilities["summary"] == "Opened Utilities"
    assert utilities["data"] == {
        "app_name": "Utilities folder",
        "path": "/Applications/Utilities",
        "open_target": "folder",
    }
    assert calls[1][0] == ["open", "/Applications/Utilities"]
    assert library["ok"] is True
    assert library["summary"] == "Opened Library"
    assert library["data"] == {
        "app_name": "Library folder",
        "path": str(desktop_mod.Path.home() / "Library"),
        "open_target": "folder",
    }
    assert calls[2][0] == ["open", str(desktop_mod.Path.home() / "Library")]


def test_app_open_handles_system_settings_permission_aliases(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.app_open("屏幕录制权限")

    assert result["ok"] is True
    assert result["action"] == "app.open"
    assert result["summary"] == "Opened System Settings: Screen Recording Permission"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "app_name": "屏幕录制权限",
        "open_target": "system_settings",
        "settings_label": "Screen Recording Permission",
        "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
        "fallback_used": False,
    }
    assert calls[0][0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture",
    ]

    result = desktop_mod.app_open("桌面权限")

    assert result["ok"] is True
    assert result["summary"] == "Opened System Settings: Privacy & Security"
    assert result["data"]["settings_label"] == "Privacy & Security"
    assert calls[1][0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy",
    ]

    result = desktop_mod.app_open("蓝牙设置")

    assert result["ok"] is True
    assert result["summary"] == "Opened System Settings: Bluetooth"
    assert result["data"]["settings_label"] == "Bluetooth"
    assert calls[2][0] == [
        "open",
        "x-apple.systempreferences:com.apple.BluetoothSettings",
    ]

    result = desktop_mod.app_open("Wi-Fi 设置")

    assert result["ok"] is True
    assert result["summary"] == "Opened System Settings: Wi-Fi"
    assert result["data"]["settings_label"] == "Wi-Fi"
    assert calls[3][0] == [
        "open",
        "x-apple.systempreferences:com.apple.wifi-settings-extension",
    ]

    result = desktop_mod.app_open("系统设置里的辅助功能")

    assert result["ok"] is True
    assert result["summary"] == "Opened System Settings: Accessibility Permission"
    assert result["data"]["settings_label"] == "Accessibility Permission"
    assert calls[4][0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ]

    result = desktop_mod.system_settings_open("辅助功能权限")

    assert result["ok"] is True
    assert result["action"] == "system.settings_open"
    assert result["summary"] == "Opened System Settings: Accessibility Permission"
    assert result["data"] == {
        "target": "辅助功能权限",
        "open_target": "system_settings",
        "settings_label": "Accessibility Permission",
        "settings_url": "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
        "fallback_used": False,
    }
    assert calls[5][0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility",
    ]

    result = desktop_mod.system_settings_open("系统设置")

    assert result["ok"] is True
    assert result["action"] == "system.settings_open"
    assert result["summary"] == "Opened System Settings"
    assert result["data"] == {
        "target": "系统设置",
        "open_target": "system_settings",
        "settings_label": "System Settings",
    }
    assert calls[6][0] == ["open", "-a", "System Settings"]


def test_system_settings_open_handles_common_pane_aliases(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    sound = desktop_mod.system_settings_open("声音")
    keyboard = desktop_mod.system_settings_open("键盘")
    notifications = desktop_mod.system_settings_open("通知")

    assert sound["ok"] is True
    assert sound["summary"] == "Opened System Settings: Sound"
    assert sound["data"]["settings_label"] == "Sound"
    assert calls[0][0] == [
        "open",
        "x-apple.systempreferences:com.apple.Sound-Settings.extension",
    ]
    assert keyboard["ok"] is True
    assert keyboard["summary"] == "Opened System Settings: Keyboard"
    assert keyboard["data"]["settings_label"] == "Keyboard"
    assert calls[1][0] == [
        "open",
        "x-apple.systempreferences:com.apple.Keyboard-Settings.extension",
    ]
    assert notifications["ok"] is True
    assert notifications["summary"] == "Opened System Settings: Notifications"
    assert notifications["data"]["settings_label"] == "Notifications"
    assert calls[2][0] == [
        "open",
        "x-apple.systempreferences:com.apple.Notifications-Settings.extension",
    ]

    extra_cases = (
        ("电池", "Battery", "x-apple.systempreferences:com.apple.Battery-Settings.extension"),
        ("鼠标", "Mouse", "x-apple.systempreferences:com.apple.Mouse-Settings.extension"),
        ("触控板", "Trackpad", "x-apple.systempreferences:com.apple.Trackpad-Settings.extension"),
        (
            "打印机与扫描仪",
            "Printers & Scanners",
            "x-apple.systempreferences:com.apple.Print-Scan-Settings.extension",
        ),
        ("专注模式", "Focus", "x-apple.systempreferences:com.apple.Focus-Settings.extension"),
        ("墙纸", "Wallpaper", "x-apple.systempreferences:com.apple.Wallpaper-Settings.extension"),
        (
            "桌面与程序坞",
            "Desktop & Dock",
            "x-apple.systempreferences:com.apple.Desktop-Settings.extension",
        ),
        (
            "屏幕保护程序",
            "Screen Saver",
            "x-apple.systempreferences:com.apple.ScreenSaver-Settings.extension",
        ),
        ("Siri", "Siri", "x-apple.systempreferences:com.apple.Siri-Settings.extension"),
        (
            "语言与地区",
            "Language & Region",
            "x-apple.systempreferences:com.apple.Localization-Settings.extension",
        ),
        (
            "日期与时间",
            "Date & Time",
            "x-apple.systempreferences:com.apple.Date-Time-Settings.extension",
        ),
        (
            "软件更新",
            "Software Update",
            "x-apple.systempreferences:com.apple.Software-Update-Settings.extension",
        ),
        ("储存空间", "Storage", "x-apple.systempreferences:com.apple.Storage-Settings.extension"),
        ("登录项", "Login Items", "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"),
        (
            "用户与群组",
            "Users & Groups",
            "x-apple.systempreferences:com.apple.Users-Groups-Settings.extension",
        ),
    )
    for index, (target, label, url) in enumerate(extra_cases, start=3):
        result = desktop_mod.system_settings_open(target)

        assert result["ok"] is True
        assert result["summary"] == f"Opened System Settings: {label}"
        assert result["data"]["settings_label"] == label
        assert calls[index][0] == ["open", url]


def test_app_open_system_settings_tries_fallback_url(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            1 if len(calls) == 1 else 0,
            stdout="",
            stderr="unsupported URL" if len(calls) == 1 else "",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.app_open("设置的隐私与安全性")

    assert result["ok"] is True
    assert result["action"] == "app.open"
    assert result["summary"] == "Opened System Settings: Privacy & Security"
    assert result["fallback_used"] is True
    assert result["data"] == {
        "app_name": "设置的隐私与安全性",
        "open_target": "system_settings",
        "settings_label": "Privacy & Security",
        "settings_url": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
        "fallback_used": True,
    }
    assert calls[0][0] == [
        "open",
        "x-apple.systempreferences:com.apple.preference.security?Privacy",
    ]
    assert calls[1][0] == [
        "open",
        "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension",
    ]


def test_desktop_reveal_path_reveals_existing_path(monkeypatch, tmp_path) -> None:
    target = tmp_path / "report.md"
    target.write_text("hello", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.reveal_path(str(target))
    expanded = str(target.resolve(strict=False))

    assert result["ok"] is True
    assert result["action"] == "desktop.reveal_path"
    assert result["summary"] == "Revealed report.md in Finder"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "path": str(target),
        "expanded_path": expanded,
        "open_target": "finder_reveal",
        "exists": True,
        "is_dir": False,
    }
    assert calls[0][0] == ["open", "-R", expanded]


def test_desktop_reveal_path_reports_missing_path(monkeypatch, tmp_path) -> None:
    missing = tmp_path / "missing.md"
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")

    result = desktop_mod.reveal_path(str(missing))
    expanded = str(missing.resolve(strict=False))

    assert result["ok"] is False
    assert result["action"] == "desktop.reveal_path"
    assert result["summary"] == "desktop.reveal_path failed"
    assert result["error_code"] == "path_not_found"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "path": str(missing),
        "expanded_path": expanded,
        "open_target": "finder_reveal",
        "exists": False,
    }


def test_desktop_open_path_opens_safe_existing_file(monkeypatch, tmp_path) -> None:
    target = tmp_path / "report.pdf"
    target.write_text("pdf", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path(str(target))
    expanded = str(target.resolve(strict=False))

    assert result["ok"] is True
    assert result["action"] == "desktop.open_path"
    assert result["summary"] == "Opened report.pdf"
    assert result["data"] == {
        "path": str(target),
        "expanded_path": expanded,
        "open_target": "system_open",
        "exists": True,
        "is_dir": False,
        "suffix": ".pdf",
    }
    assert calls[0][0] == ["open", expanded]


def test_desktop_open_path_with_app_opens_safe_existing_file(monkeypatch, tmp_path) -> None:
    target = tmp_path / "report.pdf"
    target.write_text("pdf", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path_with_app(str(target), "Preview")
    expanded = str(target.resolve(strict=False))

    assert result["ok"] is True
    assert result["action"] == "desktop.open_path_with_app"
    assert result["summary"] == "Opened report.pdf with Preview"
    assert result["postcondition_verified"] is True
    assert result["data"] == {
        "path": str(target),
        "expanded_path": expanded,
        "app_name": "Preview",
        "open_target": "app_open",
        "exists": True,
        "postcondition_verified": True,
        "is_dir": False,
        "suffix": ".pdf",
    }
    assert calls[0][0] == ["open", "-a", "Preview", expanded]


def test_desktop_open_path_resolves_latest_download_alias(monkeypatch, tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    older = downloads / "older.pdf"
    newer = downloads / "newer.pdf"
    partial = downloads / "still-downloading.crdownload"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    partial.write_text("partial", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    os.utime(partial, (300, 300))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path("latest_download")

    assert result["ok"] is True
    assert result["action"] == "desktop.open_path"
    assert result["summary"] == "Opened newer.pdf"
    assert result["data"] == {
        "path": "latest_download",
        "open_target": "system_open",
        "desktop_object": "latest_download",
        "source_folder": str(downloads),
        "expanded_path": str(newer),
        "resolved_path": str(newer),
        "display_path": str(newer),
        "source_exists": True,
        "exists": True,
        "is_dir": False,
        "suffix": ".pdf",
    }
    assert calls[0][0] == ["open", str(newer)]


def test_desktop_reveal_path_resolves_latest_download_alias(monkeypatch, tmp_path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    target = downloads / "latest.txt"
    target.write_text("latest", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.reveal_path("latest_download")

    assert result["ok"] is True
    assert result["action"] == "desktop.reveal_path"
    assert result["summary"] == "Revealed latest.txt in Finder"
    assert result["data"]["desktop_object"] == "latest_download"
    assert result["data"]["resolved_path"] == str(target)
    assert result["data"]["is_dir"] is False
    assert calls[0][0] == ["open", "-R", str(target)]


def test_desktop_open_path_resolves_finder_selection_alias(monkeypatch, tmp_path) -> None:
    target = tmp_path / "selected.pdf"
    target.write_text("pdf", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "osascript":
            return subprocess.CompletedProcess(command, 0, stdout=f"{target}\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path("finder_selection")

    assert result["ok"] is True
    assert result["action"] == "desktop.open_path"
    assert result["summary"] == "Opened selected.pdf"
    assert result["data"] == {
        "path": "finder_selection",
        "open_target": "system_open",
        "desktop_object": "finder_selection",
        "source_app": "Finder",
        "expanded_path": str(target),
        "resolved_path": str(target),
        "display_path": str(target),
        "source_exists": True,
        "exists": True,
        "is_dir": False,
        "suffix": ".pdf",
    }
    assert calls[0][0][0] == "osascript"
    assert calls[1][0] == ["open", str(target)]


def test_desktop_reveal_path_resolves_finder_selection_alias(monkeypatch, tmp_path) -> None:
    target = tmp_path / "selected.txt"
    target.write_text("selected", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if command[0] == "osascript":
            return subprocess.CompletedProcess(command, 0, stdout=f"{target}\n", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.reveal_path("finder_selection")

    assert result["ok"] is True
    assert result["action"] == "desktop.reveal_path"
    assert result["summary"] == "Revealed selected.txt in Finder"
    assert result["data"]["desktop_object"] == "finder_selection"
    assert result["data"]["resolved_path"] == str(target)
    assert result["data"]["source_app"] == "Finder"
    assert calls[0][0][0] == "osascript"
    assert calls[1][0] == ["open", "-R", str(target)]


def test_desktop_open_path_reports_empty_finder_selection(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path("finder_selection")

    assert result["ok"] is False
    assert result["action"] == "desktop.open_path"
    assert result["error_code"] == "finder_selection_not_found"
    assert result["data"]["desktop_object"] == "finder_selection"
    assert calls[0][0][0] == "osascript"
    assert len(calls) == 1


def test_desktop_open_path_resolves_latest_screenshot_alias(monkeypatch, tmp_path) -> None:
    desktop = tmp_path / "Desktop"
    downloads = tmp_path / "Downloads"
    pictures = tmp_path / "Pictures"
    desktop.mkdir()
    downloads.mkdir()
    pictures.mkdir()
    older = desktop / "Screenshot 2026-06-01 at 10.00.00.png"
    newer = downloads / "Screen Shot 2026-06-01 at 11.00.00.png"
    not_screenshot = pictures / "vacation.png"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    not_screenshot.write_text("image", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    os.utime(not_screenshot, (300, 300))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path("latest_screenshot")

    assert result["ok"] is True
    assert result["action"] == "desktop.open_path"
    assert result["summary"] == "Opened Screen Shot 2026-06-01 at 11.00.00.png"
    assert result["data"] == {
        "path": "latest_screenshot",
        "open_target": "system_open",
        "desktop_object": "latest_screenshot",
        "source_folders": [str(desktop), str(downloads), str(pictures)],
        "source_folder": str(downloads),
        "expanded_path": str(newer),
        "resolved_path": str(newer),
        "display_path": str(newer),
        "source_exists": True,
        "exists": True,
        "is_dir": False,
        "suffix": ".png",
    }
    assert calls[0][0] == ["open", str(newer)]


def test_desktop_reveal_path_resolves_latest_desktop_item_alias(monkeypatch, tmp_path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    older = desktop / "older.txt"
    newer = desktop / "newer.txt"
    partial = desktop / "still-downloading.part"
    older.write_text("old", encoding="utf-8")
    newer.write_text("new", encoding="utf-8")
    partial.write_text("partial", encoding="utf-8")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    os.utime(partial, (300, 300))
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.reveal_path("latest_desktop_item")

    assert result["ok"] is True
    assert result["action"] == "desktop.reveal_path"
    assert result["summary"] == "Revealed newer.txt in Finder"
    assert result["data"]["desktop_object"] == "latest_desktop_item"
    assert result["data"]["source_folder"] == str(desktop)
    assert result["data"]["resolved_path"] == str(newer)
    assert calls[0][0] == ["open", "-R", str(newer)]


def test_desktop_open_path_keeps_safety_for_latest_desktop_item_alias(monkeypatch, tmp_path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    target = desktop / "run.sh"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.open_path("latest_desktop_item")

    assert result["ok"] is False
    assert result["action"] == "desktop.open_path"
    assert result["error_code"] == "unsafe_path_type"
    assert result["data"]["desktop_object"] == "latest_desktop_item"
    assert result["data"]["resolved_path"] == str(target)
    assert result["data"]["suffix"] == ".sh"
    assert calls == []


def test_desktop_open_path_blocks_unsafe_file_types(monkeypatch, tmp_path) -> None:
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )

    result = desktop_mod.open_path(str(target))

    assert result["ok"] is False
    assert result["action"] == "desktop.open_path"
    assert result["summary"] == "desktop.open_path blocked"
    assert result["error_code"] == "unsafe_path_type"
    assert ".sh" in result["error"]
    assert result["data"]["suffix"] == ".sh"
    assert calls == []


def test_desktop_open_path_with_app_blocks_unsafe_file_types(monkeypatch, tmp_path) -> None:
    target = tmp_path / "run.sh"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda *_args, **_kwargs: calls.append((_args, _kwargs)),
    )

    result = desktop_mod.open_path_with_app(str(target), "Terminal")

    assert result["ok"] is False
    assert result["action"] == "desktop.open_path_with_app"
    assert result["summary"] == "desktop.open_path_with_app blocked"
    assert result["error_code"] == "unsafe_path_type"
    assert result["data"]["app_name"] == "Terminal"
    assert result["data"]["suffix"] == ".sh"
    assert calls == []


def test_parse_app_focus_output_requires_observed_frontmost_app_match() -> None:
    mismatched = desktop_mod._parse_app_focus_output(
        "focused|Calculator|true|ChatGPT",
        "Calculator",
    )
    matched = desktop_mod._parse_app_focus_output(
        "focused|Calculator|false|Calculator",
        "Calculator",
    )

    assert mismatched["focus_verified"] is False
    assert mismatched["focus_status"] == "not_frontmost"
    assert mismatched["frontmost_app"] == "ChatGPT"
    assert mismatched["system_events_reported_frontmost"] is True
    assert matched["focus_verified"] is True
    assert matched["system_events_reported_frontmost"] is False


def test_parse_appkit_focus_output_requires_observed_frontmost_app_match() -> None:
    mismatched = desktop_mod._parse_appkit_focus_output(
        "appkit|Calculator|true|true|ChatGPT",
        "Calculator",
    )
    matched = desktop_mod._parse_appkit_focus_output(
        "appkit|Calculator|true|false|Calculator",
        "Calculator",
    )

    assert mismatched["focus_verified"] is False
    assert mismatched["focus_status"] == "not_frontmost"
    assert mismatched["frontmost_app"] == "ChatGPT"
    assert mismatched["appkit_reported_active"] is True
    assert matched["focus_verified"] is True
    assert matched["appkit_reported_active"] is False


def test_parse_dock_focus_output_requires_observed_frontmost_app_match() -> None:
    mismatched = desktop_mod._parse_dock_focus_output(
        "dock|Calculator|clicked|true|ChatGPT|true|1|计算器",
        "Calculator",
    )
    matched = desktop_mod._parse_dock_focus_output(
        "dock|Calculator|clicked|false|Calculator|true|1|计算器",
        "Calculator",
    )

    assert mismatched["focus_verified"] is False
    assert mismatched["focus_status"] == "not_frontmost"
    assert mismatched["frontmost_app"] == "ChatGPT"
    assert mismatched["dock_reported_frontmost"] is True
    assert matched["focus_verified"] is True
    assert matched["dock_reported_frontmost"] is False


def test_app_focus_falls_back_to_open_when_automation_is_blocked(monkeypatch) -> None:
    open_calls = []

    def fake_run(command, **kwargs):
        open_calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    osascript_calls = []

    def fake_osascript(script, args=None):
        osascript_calls.append(args)
        if len(osascript_calls) == 1:
            return {
                "ok": False,
                "action": "osascript",
                "summary": "osascript failed",
                "error": "Not authorized to send Apple events to Slack.",
                "permission_error": True,
                "fallback_used": False,
            }
        return {"ok": True, "stdout": "running", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.example.Slack")
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Slack|true|false|Codex",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Slack")

    assert result["ok"] is False
    assert result["action"] == "app.focus"
    assert result["error"] == "app_focus_not_verified"
    assert result["permission_error"] is False
    assert result["fallback_used"] is True
    assert result["fallback_result"]["action"] == "app.open"
    assert result["data"]["app_name"] == "Slack"
    assert result["data"]["launch_verified"] is True
    assert result["data"]["focus_fallback"] == "app.open"
    assert result["data"]["focus_verified"] is False
    assert result["data"]["focus_status"] == "not_frontmost"
    assert result["data"]["frontmost_app"] == "Codex"
    assert result["blocking_condition"] == "foreground_focus_unavailable"
    assert result["retryable"] is True
    assert result["data"]["blocking_condition"] == "foreground_focus_unavailable"
    assert result["data"]["retryable"] is True
    assert result["data"]["focus_attempts"] == [
        {
            "strategy": "applescript_system_events",
            "ok": False,
            "error": "Not authorized to send Apple events to Slack.",
        },
        {
            "strategy": "appkit_nsrunningapplication",
            "ok": True,
            "focus_verified": False,
            "focus_status": "not_frontmost",
            "frontmost_app": "Codex",
            "appkit_activate_result": "true",
        },
    ]
    assert result["permission_targets"] == ["foreground_focus"]
    assert open_calls[0][0] == ["open", "-a", "Slack"]
    assert osascript_calls == [["Slack"], ["Slack"]]


def test_app_focus_uses_appkit_fallback_when_applescript_does_not_verify(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.example.Slack")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Slack|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Slack|true|true|Slack",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Slack")

    assert result["ok"] is True
    assert result["action"] == "app.focus"
    assert result["summary"] == "Focused Slack via AppKit"
    assert result["fallback_used"] is True
    assert result["data"]["focus_verified"] is True
    assert result["data"]["focus_status"] == "frontmost"
    assert result["data"]["frontmost_app"] == "Slack"
    assert result["data"]["appkit_activate_result"] == "true"
    assert result["data"]["focus_attempts"][-1]["strategy"] == "appkit_nsrunningapplication"


def test_app_focus_verifies_frontmost_process(monkeypatch) -> None:
    osascript_calls = []

    def fake_osascript(script, args=None):
        osascript_calls.append(args)
        return {"ok": True, "stdout": "focused|Slack|true|Slack", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_focus("Slack")

    assert result["ok"] is True
    assert result["action"] == "app.focus"
    assert result["summary"] == "Focused Slack"
    assert result["data"] == {
        "app_name": "Slack",
        "focus_verified": True,
        "focus_status": "frontmost",
        "frontmost_app": "Slack",
        "system_events_reported_frontmost": True,
    }
    assert osascript_calls == [["Slack"]]


def test_app_focus_reports_process_visibility_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Slack|true|Slack|true|2",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Slack")

    assert result["ok"] is True
    assert result["data"] == {
        "app_name": "Slack",
        "focus_verified": True,
        "focus_status": "frontmost",
        "frontmost_app": "Slack",
        "system_events_reported_frontmost": True,
        "process_visible": True,
        "window_count": 2,
    }


def test_app_focus_uses_dock_fallback_when_process_has_no_visible_surface(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(
        desktop_mod,
        "_system_events_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"focused|{app_name}|false|Codex|false|0",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_activate_app",
        lambda app_name, *, bundle_id="": {
            "ok": True,
            "stdout": f"appkit|{app_name}|true|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_launchservices_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"focused|{app_name}|false|Codex|true|0",
            "stderr": "",
            "launchservices_returncode": 0,
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_dock_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"dock|{app_name}|clicked|true|{app_name}|true|1|计算器",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is True
    assert result["summary"] == "Focused Calculator via Dock"
    assert result["fallback_used"] is True
    assert result["data"]["focus_verified"] is True
    assert result["data"]["focus_fallback"] == "dock"
    assert result["data"]["frontmost_app"] == "Calculator"
    assert result["data"]["process_visible"] is True
    assert result["data"]["window_count"] == 1
    assert result["data"]["dock_status"] == "clicked"
    assert result["data"]["dock_item_name"] == "计算器"
    assert [attempt["strategy"] for attempt in result["data"]["focus_attempts"]] == [
        "applescript_system_events",
        "appkit_nsrunningapplication",
        "launchservices_open_a",
        "dock",
    ]


def test_app_focus_uses_electron_native_bridge_after_local_focus_fails(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(
        desktop_mod,
        "_system_events_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"focused|{app_name}|false|Codex|true|0",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_activate_app",
        lambda app_name, *, bundle_id="": {
            "ok": True,
            "stdout": f"appkit|{app_name}|true|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_launchservices_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"focused|{app_name}|false|Codex|true|0",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_dock_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"dock|{app_name}|clicked|false|Codex|true|0|计算器",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_electron_native_bridge_config",
        lambda: ("http://127.0.0.1:50123", "token"),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_electron_native_focus_app",
        lambda app_name: {
            "ok": True,
            "action": "electron.native.desktop.focus",
            "summary": f"Focused {app_name} via Electron native bridge",
            "data": {
                "app_name": app_name,
                "focus_verified": True,
                "focus_status": "frontmost",
                "frontmost_app": app_name,
                "process_visible": True,
                "window_count": 1,
                "native_bridge": "electron_main",
                "native_bridge_available": True,
            },
            "permission_error": False,
            "fallback_used": False,
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is True
    assert result["summary"] == "Focused Calculator via Electron native bridge"
    assert result["data"]["focus_fallback"] == "electron_native_bridge"
    assert result["data"]["native_bridge"] == "electron_main"
    assert result["data"]["frontmost_app"] == "Calculator"
    assert [attempt["strategy"] for attempt in result["data"]["focus_attempts"]] == [
        "applescript_system_events",
        "appkit_nsrunningapplication",
        "launchservices_open_a",
        "dock",
        "electron_native_bridge",
    ]


def test_electron_native_focus_app_parses_http_error_payload(monkeypatch) -> None:
    monkeypatch.setenv(desktop_mod._ELECTRON_NATIVE_URL_ENV, "http://127.0.0.1:50123")
    monkeypatch.setenv(desktop_mod._ELECTRON_NATIVE_TOKEN_ENV, "token")
    body = json.dumps(
        {
            "ok": False,
            "action": "electron.native.desktop.focus",
            "summary": "Could not verify Calculator is foreground via Electron native bridge",
            "error": "app_focus_not_verified",
            "data": {
                "app_name": "Calculator",
                "focus_verified": False,
                "focus_status": "not_frontmost",
                "frontmost_app": "Codex",
                "native_bridge": "electron_main",
            },
            "permission_error": False,
            "fallback_used": False,
        }
    ).encode("utf-8")

    def fake_urlopen(request, timeout):
        assert request.full_url == "http://127.0.0.1:50123/native/desktop/focus"
        assert request.headers["X-oha-yachiyo-bridge-token"] == "token"
        assert timeout == 6
        raise desktop_mod.HTTPError(
            request.full_url,
            409,
            "Conflict",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(desktop_mod, "urlopen", fake_urlopen)

    result = desktop_mod._electron_native_focus_app("Calculator", timeout_seconds=6)

    assert result["ok"] is False
    assert result["http_status"] == 409
    assert result["native_bridge_available"] is True
    assert result["data"]["native_bridge_available"] is True
    assert result["data"]["frontmost_app"] == "Codex"


def test_app_focus_reports_unverified_foreground(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.example.Slack")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Slack|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Slack|true|false|Codex",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Slack")

    assert result["ok"] is False
    assert result["action"] == "app.focus"
    assert result["error"] == "app_focus_not_verified"
    assert result["data"]["app_name"] == "Slack"
    assert result["data"]["focus_verified"] is False
    assert result["data"]["focus_status"] == "not_frontmost"
    assert result["data"]["frontmost_app"] == "Codex"
    assert result["data"]["focus_attempts"] == [
        {
            "strategy": "applescript_system_events",
            "ok": True,
            "focus_verified": False,
            "focus_status": "not_frontmost",
            "frontmost_app": "Codex",
        },
        {
            "strategy": "appkit_nsrunningapplication",
            "ok": True,
            "focus_verified": False,
            "focus_status": "not_frontmost",
            "frontmost_app": "Codex",
            "appkit_activate_result": "true",
        },
    ]
    assert result["data"]["recommended_tools"] == [
        "desktop.running_apps",
        "desktop.active_window",
        "screen.capture",
    ]
    assert result["recommended_tools"] == ["app.open", "desktop.active_window", "screen.capture"]
    assert result["blocking_condition"] == "foreground_focus_unavailable"
    assert result["retryable"] is True
    assert result["data"]["blocking_condition"] == "foreground_focus_unavailable"
    assert result["data"]["retryable"] is True
    assert result["missing_permissions"] == ["foreground_focus"]
    assert result["permission_targets"] == ["foreground_focus"]
    assert result["recovery_hints"] == [
        (
            "Allow the current Oha-Yachiyo runtime to bring target apps to the foreground. "
            "Check Automation and Accessibility permissions in macOS System Settings > Privacy & Security."
        )
    ]
    assert result["recovery_actions"] == [
        {
            "label": "重新打开Slack",
            "tool": "app.open",
            "input": {"app_name": "Slack"},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
        {
            "label": "查看前台窗口",
            "tool": "desktop.active_window",
            "input": {},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
        {
            "label": "截图确认前台",
            "tool": "screen.capture",
            "input": {"reason": "verify foreground app after focus failure"},
            "permission_target": "foreground_focus",
            "risk_level": "low",
        },
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        },
    ]


def test_app_focus_reports_locked_desktop_session_without_permission_error(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Calculator|false|",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Calculator|true|false|loginwindow",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is False
    assert result["error"] == "desktop_session_locked"
    assert result["blocking_condition"] == "desktop_session_locked"
    assert result["retryable"] is True
    assert result["permission_error"] is False
    assert "missing_permissions" not in result
    assert "permission_targets" not in result
    assert result["data"]["frontmost_app"] == "loginwindow"
    assert result["data"]["desktop_session_locked"] is True
    assert result["data"]["retryable"] is True
    assert result["recovery_actions"] == [
        {
            "label": "解锁后重试Calculator",
            "tool": "app.focus",
            "input": {"app_name": "Calculator"},
            "permission_target": "desktop_session_unlocked",
            "risk_level": "low",
        }
    ]


def test_app_focus_ignores_locked_runtime_probe_when_focus_observes_unlocked_frontmost(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(desktop_mod, "_desktop_session_locked_by_runtime_probe", lambda: True)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Calculator|false|Codex|true|0",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Calculator|true|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_launchservices_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"focused|{app_name}|false|Codex|true|0",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_dock_focus_app",
        lambda app_name: {
            "ok": True,
            "stdout": f"dock|{app_name}|clicked|false|Codex|true|0|计算器",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is False
    assert result["error"] == "app_focus_not_verified"
    assert result["blocking_condition"] == "foreground_focus_unavailable"
    assert result["permission_error"] is False
    assert result["data"]["blocking_condition"] == "foreground_focus_unavailable"
    assert result["data"]["desktop_session_locked_runtime_probe_ignored"] is True
    assert "desktop_session_locked" not in result["data"]
    assert "desktop_session_locked_by_runtime_probe" not in result["data"]
    assert result["data"]["frontmost_app"] == "Codex"


def test_app_focus_reports_locked_desktop_session_from_runtime_probe_without_unlocked_observation(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(desktop_mod, "_desktop_session_locked_by_runtime_probe", lambda: True)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Calculator|false|",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Calculator|true|false|loginwindow",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is False
    assert result["error"] == "desktop_session_locked"
    assert result["blocking_condition"] == "desktop_session_locked"
    assert result["permission_error"] is False
    assert result["data"]["desktop_session_locked"] is True
    assert result["data"]["desktop_session_locked_by_runtime_probe"] is True
    assert result["data"]["frontmost_app"] == "loginwindow"


def test_app_focus_does_not_report_locked_when_system_events_saw_unlocked_frontmost(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda app_name: app_name)
    monkeypatch.setattr(desktop_mod, "_app_bundle_id", lambda app_name: "com.apple.calculator")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, args=None: {
            "ok": True,
            "stdout": "focused|Calculator|false|Codex",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda script, args=None: {
            "ok": True,
            "stdout": "appkit|Calculator|true|false|loginwindow",
            "stderr": "",
        },
    )

    result = desktop_mod.app_focus("Calculator")

    assert result["ok"] is False
    assert result["error"] == "app_focus_not_verified"
    assert result["data"]["frontmost_app"] == "loginwindow"
    assert result["data"]["focus_attempts"][0]["frontmost_app"] == "Codex"
    assert result["data"]["focus_attempts"][1]["frontmost_app"] == "loginwindow"
    assert result["blocking_condition"] == "foreground_focus_unavailable"
    assert result["retryable"] is True
    assert result["data"]["blocking_condition"] == "foreground_focus_unavailable"
    assert result["data"]["retryable"] is True
    assert result["missing_permissions"] == ["foreground_focus"]
    assert result["permission_targets"] == ["foreground_focus"]


def test_app_quit_uses_osascript_and_verifies_running_state(monkeypatch) -> None:
    osascript_calls = []

    def fake_osascript(script, args=None):
        osascript_calls.append(args)
        if len(osascript_calls) == 1:
            return {"ok": True, "stdout": "quit|Slack", "stderr": ""}
        return {"ok": True, "stdout": "not_running", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_quit("Slack")

    assert result["ok"] is True
    assert result["action"] == "app.quit"
    assert result["summary"] == "Quit Slack"
    assert result["data"] == {
        "app_name": "Slack",
        "quit_status": "quit",
        "quit_verified": True,
        "running": False,
        "launch_verified": False,
        "launch_status": "not_running",
    }
    assert result["fallback_used"] is False
    assert osascript_calls == [["Slack"], ["Slack"]]


def test_app_focus_window_raises_matching_window(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "focused|Slack|2|general", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_focus_window("Slack", "general")

    assert result == {
        "ok": True,
        "action": "app.focus_window",
        "summary": "Focused Slack window: general",
        "data": {
            "app_name": "Slack",
            "title_contains": "general",
            "focus_status": "focused",
            "window_index": 2,
            "window_title": "general",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert 'perform action "AXRaise"' in calls[0][0]
    assert 'attribute "AXMinimized"' in calls[0][0]
    assert calls[0][1] == ["Slack", "general"]


def test_app_focus_window_resolves_installed_bundle_name(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Microsoft Word.app").mkdir(parents=True)
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "focused|Microsoft Word|1|Report", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_focus_window("Word", "Report")

    assert result["ok"] is True
    assert result["summary"] == "Focused Microsoft Word window: Report"
    assert result["fallback_used"] is True
    assert result["data"] == {
        "app_name": "Microsoft Word",
        "title_contains": "Report",
        "focus_status": "focused",
        "window_index": 1,
        "window_title": "Report",
        "requested_app_name": "Word",
        "resolved_app_name": "Microsoft Word",
        "app_resolution": "installed_app_bundle",
    }
    assert calls[0][1] == ["Microsoft Word", "Report"]


def test_app_focus_window_reports_window_not_found(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, args=None: {"ok": True, "stdout": "not_found|Slack|general", "stderr": ""},
    )

    result = desktop_mod.app_focus_window("Slack", "general")

    assert result["ok"] is False
    assert result["action"] == "app.focus_window"
    assert result["summary"] == "No Slack window matched general"
    assert result["error_code"] == "window_not_found"
    assert result["data"] == {
        "app_name": "Slack",
        "title_contains": "general",
        "focus_status": "not_found",
    }


def test_app_show_unhides_restores_and_activates_app(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "shown|Slack|2", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_show("Slack")

    assert result == {
        "ok": True,
        "action": "app.show",
        "summary": "Showed Slack",
        "data": {
            "app_name": "Slack",
            "show_status": "shown",
            "restored_window_count": 2,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert "set visible of application process appName to true" in calls[0][0]
    assert 'attribute "AXMinimized"' in calls[0][0]
    assert "tell application appName to activate" in calls[0][0]
    assert calls[0][1] == ["Slack"]


def test_app_show_reports_launch_when_app_was_not_running(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, args=None: {"ok": True, "stdout": "launched|Slack|0", "stderr": ""},
    )

    result = desktop_mod.app_show("Slack")

    assert result["ok"] is True
    assert result["action"] == "app.show"
    assert result["summary"] == "Launched and showed Slack"
    assert result["data"] == {
        "app_name": "Slack",
        "show_status": "launched",
        "restored_window_count": 0,
    }


def test_app_hide_uses_system_events_process_visibility(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "hidden|Slack", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_hide("Slack")

    assert result == {
        "ok": True,
        "action": "app.hide",
        "summary": "Hid Slack",
        "data": {"app_name": "Slack", "hide_status": "hidden"},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "set visible of application process appName to false" in calls[0][0]
    assert calls[0][1] == ["Slack"]


def test_app_hide_reports_not_running(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, args=None: {"ok": True, "stdout": "not_running|Slack", "stderr": ""},
    )

    result = desktop_mod.app_hide("Slack")

    assert result["ok"] is False
    assert result["action"] == "app.hide"
    assert result["summary"] == "Slack is not running"
    assert result["error_code"] == "app_not_running"
    assert result["data"] == {"app_name": "Slack", "hide_status": "not_running"}


def test_app_minimize_uses_system_events_window_minimize(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "minimized|Slack|2", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.app_minimize("Slack")

    assert result == {
        "ok": True,
        "action": "app.minimize",
        "summary": "Minimized Slack",
        "data": {
            "app_name": "Slack",
            "minimize_status": "minimized",
            "window_count": 2,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert 'attribute "AXMinimized"' in calls[0][0]
    assert calls[0][1] == ["Slack"]


def test_app_minimize_reports_no_windows(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, args=None: {"ok": True, "stdout": "no_windows|Slack|0", "stderr": ""},
    )

    result = desktop_mod.app_minimize("Slack")

    assert result["ok"] is False
    assert result["action"] == "app.minimize"
    assert result["summary"] == "Slack has no windows to minimize"
    assert result["error_code"] == "app_no_windows"
    assert result["data"] == {
        "app_name": "Slack",
        "minimize_status": "no_windows",
        "window_count": 0,
    }


def test_desktop_active_window_permission_failure_returns_recovery_targets(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Not authorized to send Apple events to System Events.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.active_window()

    assert result["ok"] is False
    assert result["action"] == "desktop.active_window"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]
    assert result["recovery_hints"] == [
        (
            "Grant Automation permission so Oha-Yachiyo can control System Events "
            "or the target app in macOS System Settings > Privacy & Security > Automation."
        ),
        (
            "Grant Accessibility permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Accessibility."
        ),
    ]


def test_desktop_active_window_reports_locked_desktop_session(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": False,
            "error": "no frontmost application process",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": "loginwindow",
            "stderr": "",
        },
    )

    result = desktop_mod.active_window()

    assert result["ok"] is False
    assert result["error"] == "desktop_session_locked"
    assert result["blocking_condition"] == "desktop_session_locked"
    assert result["retryable"] is True
    assert result["permission_error"] is False
    assert "missing_permissions" not in result
    assert result["data"] == {
        "frontmost_app": "loginwindow",
        "desktop_session_locked": True,
        "blocking_condition": "desktop_session_locked",
        "retryable": True,
    }
    assert result["recovery_actions"][0]["tool"] == "desktop.active_window"


def test_desktop_permissions_reports_ready_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {
            "desktop_execution": [],
            "screen_capture": [],
            "active_window": [],
        },
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["ok"] is True
    assert result["action"] == "desktop.permissions"
    assert result["summary"] == "Desktop execution permissions are ready."
    assert result["permission_error"] is False
    assert result["permission_targets"] == []
    assert result["affected_tools"] == []
    assert result["data"]["diagnostic_route"] == "/yachiyo/readiness"


def test_desktop_permissions_reports_missing_targets_and_affected_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {
            "screen_capture": ["screen_recording"],
            "active_window": ["automation_or_accessibility"],
            "media_control": ["music_app", "automation"],
        },
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["ok"] is True
    assert result["action"] == "desktop.permissions"
    assert result["permission_error"] is True
    assert result["permission_targets"] == [
        "screen_recording",
        "automation_or_accessibility",
        "music_app",
        "automation",
    ]
    assert result["affected_tools"] == [
        "screen.capture",
        "desktop.active_window",
        "desktop.running_apps",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.inspect_app",
        "desktop.verify",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "media.system_control",
        "media.apple_music_play",
        "media.apple_music_status",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
        "media.music_app_control",
    ]
    assert result["data"]["missing_permissions"] == {
        "screen_capture": ["screen_recording"],
        "active_window": ["automation_or_accessibility"],
        "media_control": ["music_app", "automation"],
    }
    assert result["recovery_actions"] == [
        {
            "label": "打开屏幕录制权限",
            "tool": "system.settings_open",
            "input": {"target": "屏幕录制权限"},
            "permission_target": "screen_recording",
            "risk_level": "low",
        },
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        },
        {
            "label": "打开 Apple Music",
            "tool": "app.open",
            "input": {"app_name": "Music"},
            "permission_target": "music_app",
            "risk_level": "low",
        },
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]
    assert any("Screen Recording permission" in hint for hint in result["recovery_hints"])


def test_desktop_permissions_reports_generic_desktop_aliases_as_affected_tools(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {
            "app_control": ["automation"],
            "foreground_input": ["accessibility"],
        },
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["ok"] is True
    assert result["permission_error"] is True
    for tool in (
        "desktop.open_app",
        "desktop.focus_app",
        "desktop.shortcut",
        "desktop.type",
    ):
        assert tool in result["affected_tools"]
        assert tool in result["data"]["affected_tools"]


def test_desktop_permissions_reports_runtime_blockers(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )
    monkeypatch.setattr(
        desktop_mod,
        "_desktop_runtime_blocking_conditions",
        lambda **_kwargs: {"foreground_activation": ["desktop_session_locked"]},
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["ok"] is True
    assert result["permission_error"] is False
    assert result["runtime_blocked"] is True
    assert result["data"]["ready"] is False
    assert result["blocking_conditions"] == ["desktop_session_locked"]
    assert result["runtime_blocking_conditions"] == {
        "foreground_activation": ["desktop_session_locked"]
    }
    assert result["affected_tools"][:2] == ["app.focus", "desktop.inspect_app"]
    assert "desktop.focus_app" in result["affected_tools"]
    assert result["summary"].startswith("Desktop runtime blockers: desktop_session_locked")
    assert result["recovery_hints"] == [
        (
            "Unlock the active macOS user session, then approve desktop.permissions.verify "
            "or retry the foreground desktop action."
        )
    ]
    assert result["recovery_actions"] == [
        {
            "label": "解锁后重新检查桌面权限",
            "tool": "desktop.permissions.verify",
            "input": {},
            "permission_target": "desktop_session_unlocked",
            "risk_level": "medium",
        }
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]


def test_desktop_permissions_reports_blank_screen_runtime_blocker(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {},
    )
    monkeypatch.setattr(
        desktop_mod,
        "_desktop_runtime_blocking_conditions",
        lambda **_kwargs: {"screen_capture": ["screen_capture_blank"]},
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["ok"] is True
    assert result["runtime_blocked"] is True
    assert result["blocking_conditions"] == ["screen_capture_blank"]
    assert result["runtime_blocking_conditions"] == {
        "screen_capture": ["screen_capture_blank"]
    }
    assert any("blank/black" in hint for hint in result["recovery_hints"])
    assert result["recovery_actions"] == [
        {
            "label": "重新截图确认桌面画面",
            "tool": "screen.capture",
            "input": {"reason": "verify desktop is visible after blank screenshot"},
            "permission_target": "desktop_screen_visible",
            "risk_level": "low",
        },
        {
            "label": "查看当前前台窗口",
            "tool": "desktop.active_window",
            "input": {},
            "permission_target": "desktop_screen_visible",
            "risk_level": "low",
        },
    ]


def test_desktop_permissions_reports_extended_privacy_recovery_actions(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_missing_by_capability",
        lambda use_cache=True: {
            "desktop_execution": ["input_monitoring", "full_disk_access", "files_and_folders"],
            "media_control": ["microphone", "camera"],
        },
    )

    result = desktop_mod.permissions(active_verification=True)

    assert result["permission_error"] is True
    assert result["permission_targets"] == [
        "input_monitoring",
        "full_disk_access",
        "files_and_folders",
        "microphone",
        "camera",
    ]
    assert result["recovery_actions"] == [
        {
            "label": "打开输入监控权限",
            "tool": "system.settings_open",
            "input": {"target": "输入监控"},
            "permission_target": "input_monitoring",
            "risk_level": "low",
        },
        {
            "label": "打开完全磁盘访问权限",
            "tool": "system.settings_open",
            "input": {"target": "完全磁盘访问"},
            "permission_target": "full_disk_access",
            "risk_level": "low",
        },
        {
            "label": "打开文件和文件夹权限",
            "tool": "system.settings_open",
            "input": {"target": "文件和文件夹"},
            "permission_target": "files_and_folders",
            "risk_level": "low",
        },
        {
            "label": "打开麦克风权限",
            "tool": "system.settings_open",
            "input": {"target": "麦克风"},
            "permission_target": "microphone",
            "risk_level": "low",
        },
        {
            "label": "打开摄像头权限",
            "tool": "system.settings_open",
            "input": {"target": "摄像头"},
            "permission_target": "camera",
            "risk_level": "low",
        },
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]
    assert any("Input Monitoring permission" in hint for hint in result["recovery_hints"])
    assert any("Full Disk Access" in hint for hint in result["recovery_hints"])
    assert any("Microphone permission" in hint for hint in result["recovery_hints"])


def test_desktop_permission_preflight_reports_cached_missing_targets(monkeypatch) -> None:
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.desktop_permission_probe_cache_status",
        lambda: {
            "checked": True,
            "permission_checked": True,
            "runtime_blockers_checked": True,
            "status": "cached",
        },
    )
    monkeypatch.setattr(
        "apps.shell.yachiyo_agent.desktop_permissions.cached_desktop_permission_missing_by_capability",
        lambda: {
            "foreground_input": ["accessibility"],
        },
    )

    result = desktop_mod.permission_preflight()

    assert result["ok"] is True
    assert result["action"] == "desktop.permission_preflight"
    assert result["permission_error"] is True
    assert result["permission_targets"] == ["accessibility"]
    assert "desktop.safe_type_text" in result["affected_tools"]
    assert result["recovery_actions"] == [
        {
            "label": "打开辅助功能权限",
            "tool": "system.settings_open",
            "input": {"target": "辅助功能权限"},
            "permission_target": "accessibility",
            "risk_level": "low",
        }
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]


def test_desktop_list_apps_returns_installed_app_bundles(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Google Chrome.app").mkdir(parents=True)
    (app_dir / "Microsoft Word.app").mkdir()
    (app_dir / "Utilities").mkdir()
    (app_dir / "Utilities" / "Terminal.app").mkdir()

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])

    result = desktop_mod.list_apps(limit=2)

    assert result["ok"] is True
    assert result["action"] == "desktop.list_apps"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"]["count"] == 2
    assert result["data"]["total_count"] == 3
    assert result["data"]["truncated"] is True
    assert [app["name"] for app in result["data"]["apps"]] == [
        "Google Chrome",
        "Microsoft Word",
    ]


def test_desktop_list_apps_filters_by_query(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Music.app").mkdir(parents=True)
    (app_dir / "Microsoft Word.app").mkdir()

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])

    result = desktop_mod.list_apps(query="Apple Music")

    assert result["ok"] is True
    assert result["summary"] == "Installed apps matching Apple Music: Music"
    assert result["data"]["query"] == "Apple Music"
    assert result["data"]["normalized_query"] == "applemusic"
    assert result["data"]["count"] == 1
    assert result["data"]["apps"][0]["name"] == "Music"
    assert result["data"]["apps"][0]["match_score"] == 100
    assert result["data"]["apps"][0]["match_confidence"] == "high"
    assert result["data"]["apps"][0]["match_reason"] == "exact_name"
    assert result["data"]["apps"][0]["matched_query"] == "Music"
    assert result["data"]["apps"][0]["matched_query_source"] == "app_alias"
    assert result["data"]["best_match"]["name"] == "Music"
    assert result["data"]["resolution"]["requested_app_name"] == "Apple Music"
    assert result["data"]["resolution"]["resolved_app_name"] == "Music"
    assert result["data"]["resolution"]["app_resolution_source"] == "desktop.list_apps"


def test_desktop_list_apps_does_not_match_ascii_query_inside_token(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Passwords.app").mkdir(parents=True)
    (app_dir / "Microsoft Word.app").mkdir()

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])

    result = desktop_mod.list_apps(query="Word")

    assert result["ok"] is True
    assert [app["name"] for app in result["data"]["apps"]] == ["Microsoft Word"]


def test_desktop_list_apps_matches_browser_capability_from_bundle_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    app_dir = tmp_path / "Applications"
    _write_app_bundle(
        app_dir,
        "Safari",
        {
            "CFBundleName": "Safari",
            "CFBundleIdentifier": "com.apple.Safari",
            "CFBundleURLTypes": [
                {"CFBundleURLSchemes": ["http", "https", "file"]},
            ],
            "CFBundleDocumentTypes": [
                {"LSItemContentTypes": ["public.html", "public.xhtml"]},
            ],
        },
    )
    _write_app_bundle(
        app_dir,
        "Calculator",
        {
            "CFBundleName": "Calculator",
            "CFBundleIdentifier": "com.apple.calculator",
        },
    )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])

    result = desktop_mod.list_apps(query="browser")

    assert result["ok"] is True
    assert [app["name"] for app in result["data"]["apps"]] == ["Safari"]
    assert result["data"]["apps"][0]["matched_capability"] == "web_browser"
    assert result["data"]["apps"][0]["match_reason"] == "capability_web_browser"
    assert result["data"]["resolution"]["app_resolution_reason"] == "capability_web_browser"


def test_desktop_list_apps_matches_file_manager_capability_from_finder_metadata(
    monkeypatch,
    tmp_path,
) -> None:
    core_services = tmp_path / "CoreServices"
    _write_app_bundle(
        core_services,
        "Finder",
        {
            "CFBundleName": "Finder",
            "CFBundleIdentifier": "com.apple.finder",
            "CFBundleURLTypes": [
                {"CFBundleURLSchemes": ["file", "smb", "afp"]},
            ],
            "CFBundleDocumentTypes": [
                {"LSItemContentTypes": ["public.folder"], "CFBundleTypeName": "Folder"},
            ],
        },
    )
    _write_app_bundle(
        core_services,
        "Preview",
        {
            "CFBundleName": "Preview",
            "CFBundleIdentifier": "com.apple.Preview",
            "CFBundleDocumentTypes": [
                {"LSItemContentTypes": ["public.folder"], "CFBundleTypeName": "Folder"},
            ],
        },
    )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [core_services])

    result = desktop_mod.list_apps(query="file manager")

    assert result["ok"] is True
    assert [app["name"] for app in result["data"]["apps"]] == ["Finder"]
    assert result["data"]["apps"][0]["matched_capability"] == "file_manager"
    assert result["data"]["apps"][0]["match_reason"] == "capability_file_manager"


def test_desktop_running_apps_returns_foreground_app_list(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": "Finder|101|false\nGoogle Chrome|202|true\nMusic|303|false",
            "stderr": "",
        },
    )

    result = desktop_mod.running_apps()

    assert result["ok"] is True
    assert result["action"] == "desktop.running_apps"
    assert result["summary"] == "Running apps: Finder, Google Chrome, Music"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "apps": [
            {"name": "Finder", "pid": 101, "frontmost": False},
            {"name": "Google Chrome", "pid": 202, "frontmost": True},
            {"name": "Music", "pid": 303, "frontmost": False},
        ],
        "count": 3,
        "frontmost": "Google Chrome",
    }


def test_desktop_running_apps_permission_failure_returns_recovery_targets(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Not authorized to send Apple events to System Events.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.running_apps()

    assert result["ok"] is False
    assert result["action"] == "desktop.running_apps"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]


def test_desktop_windows_returns_window_titles(monkeypatch) -> None:
    osascript_args = []

    def fake_osascript(_script, args=None):
        osascript_args.append(args)
        return {
            "ok": True,
            "stdout": "Finder\t101\t1\tfalse\tDownloads\nGoogle Chrome\t202\t1\ttrue\tChatGPT",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.windows()

    assert result["ok"] is True
    assert result["action"] == "desktop.windows"
    assert result["summary"] == "Open windows: Finder: Downloads, Google Chrome: ChatGPT"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "app_name": "",
        "windows": [
            {
                "app_name": "Finder",
                "pid": 101,
                "index": 1,
                "frontmost": False,
                "title": "Downloads",
            },
            {
                "app_name": "Google Chrome",
                "pid": 202,
                "index": 1,
                "frontmost": True,
                "title": "ChatGPT",
            },
        ],
        "count": 2,
    }
    assert osascript_args == [[""]]


def test_desktop_windows_resolves_installed_bundle_filter(monkeypatch, tmp_path) -> None:
    app_dir = tmp_path / "Applications"
    (app_dir / "Microsoft Word.app").mkdir(parents=True)
    osascript_args = []

    def fake_osascript(_script, args=None):
        osascript_args.append(args)
        return {
            "ok": True,
            "stdout": "Microsoft Word\t303\t1\ttrue\tReport",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_application_search_dirs", lambda: [app_dir])
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.windows("Word")

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["data"] == {
        "app_name": "Microsoft Word",
        "windows": [
            {
                "app_name": "Microsoft Word",
                "pid": 303,
                "index": 1,
                "frontmost": True,
                "title": "Report",
            }
        ],
        "count": 1,
        "requested_app_name": "Word",
        "resolved_app_name": "Microsoft Word",
        "app_resolution": "installed_app_bundle",
    }
    assert osascript_args == [["Microsoft Word"]]


def test_desktop_windows_diagnoses_running_app_without_visible_windows(monkeypatch) -> None:
    osascript_args = []

    def fake_osascript(script, args=None):
        osascript_args.append(args)
        if "if application appName is running" in script:
            return {"ok": True, "stdout": "running", "stderr": ""}
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app_name", lambda _app_name: "")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.windows("Notes")

    assert result["ok"] is True
    assert result["summary"] == "No windows found for Notes"
    assert result["data"]["app_name"] == "Notes"
    assert result["data"]["windows"] == []
    assert result["data"]["count"] == 0
    assert result["data"]["visibility_limited"] is True
    assert result["data"]["window_visibility_status"] == "running_without_visible_windows"
    assert result["data"]["launch_verified"] is True
    assert result["data"]["launch_status"] == "running"
    assert osascript_args == [["Notes"], ["Notes"]]


def test_desktop_windows_permission_failure_returns_recovery_targets(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Not authorized to send Apple events to System Events.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.windows()

    assert result["ok"] is False
    assert result["action"] == "desktop.windows"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]


def test_desktop_ui_elements_returns_foreground_accessibility_controls(monkeypatch) -> None:
    osascript_args = []
    osascript_scripts = []

    def fake_osascript(script, args=None):
        osascript_scripts.append(script)
        osascript_args.append(args)
        return {
            "ok": True,
            "stdout": (
                "META\tGoogle Chrome\t202\tChatGPT\n"
                "0\tAXButton\t\tSend\tSend message\t\ttrue\t100\t220\t40\t40\n"
                "1\tAXTextField\t\t\tMessage\tDraft\ttrue\t20\t200\t300\t40"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.ui_elements(role_filter="button", limit=20)

    assert result["ok"] is True
    assert result["action"] == "desktop.ui_elements"
    assert result["summary"] == "Google Chrome UI elements: AXButton: Send"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "app_name": "Google Chrome",
        "pid": 202,
        "title": "ChatGPT",
        "elements": [
            {
                "depth": 0,
                "role": "AXButton",
                "subrole": "",
                "name": "Send",
                "description": "Send message",
                "value": "",
                "enabled": True,
                "frame": {"x": 100, "y": 220, "width": 40, "height": 40},
                "center": {"x": 120, "y": 240},
            },
        ],
        "count": 1,
        "truncated": False,
        "role_filter": "button",
        "limit": 20,
        "role_counts": {"AXButton": 1},
        "unclassified_count": 0,
        "menu_level_count": 0,
        "control_like_count": 1,
        "inspection_level": "control",
        "menu_level_only": False,
        "window_title_missing": False,
        "visibility_limited": False,
        "visibility_status": "control_accessible",
    }
    assert osascript_args == [["20", "6", ""]]
    assert "set elementProperties to properties of targetElement" in osascript_scripts[0]
    assert 'if valueToClean is missing value then return ""' in osascript_scripts[0]
    assert "help of elementProperties" in osascript_scripts[0]


def test_desktop_ui_elements_diagnoses_menu_level_only_visibility(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": (
                "META\tOha-Yachiyo\t404\t\n"
                "0\tAXMenuBar\t\t\t\t\ttrue\t0\t0\t1200\t24\n"
                "1\tAXMenuBarItem\t\tFile\t\t\ttrue\t0\t0\t60\t24"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.ui_elements(app_name="Oha-Yachiyo", limit=20)

    assert result["ok"] is True
    assert result["data"]["role_counts"] == {"AXMenuBar": 1, "AXMenuBarItem": 1}
    assert result["data"]["unclassified_count"] == 0
    assert result["data"]["menu_level_count"] == 2
    assert result["data"]["control_like_count"] == 0
    assert result["data"]["inspection_level"] == "menu"
    assert result["data"]["menu_level_only"] is True
    assert result["data"]["window_title_missing"] is True
    assert result["data"]["visibility_limited"] is True
    assert result["data"]["visibility_status"] == "menu_level_only"


def test_desktop_ui_elements_does_not_count_menu_items_as_body_controls(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": (
                "META\tCalculator\t505\t\n"
                "0\tAXMenuBar\t\t\t\t\ttrue\t0\t0\t1200\t24\n"
                "1\tAXMenu\t\tApple\t\t\ttrue\t0\t0\t0\t0\n"
                "2\tAXMenuItem\t\tAbout This Mac\t\t\ttrue\t0\t0\t0\t0\n"
                "2\tAXMenuItem\t\tSystem Settings\t\t\ttrue\t0\t0\t0\t0"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.ui_elements(app_name="Calculator", limit=20)

    assert result["ok"] is True
    assert result["data"]["role_counts"] == {
        "AXMenuBar": 1,
        "AXMenu": 1,
        "AXMenuItem": 2,
    }
    assert result["data"]["menu_level_count"] == 4
    assert result["data"]["control_like_count"] == 0
    assert result["data"]["inspection_level"] == "menu"
    assert result["data"]["menu_level_only"] is True
    assert result["data"]["visibility_status"] == "menu_level_only"


def test_desktop_ui_elements_classifies_selectable_file_cells_as_controls(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": (
                "META\tTextEdit\t505\tOpen\n"
                "4\tAXCell\t\tDownloads\tCell\t\ttrue\t20\t120\t100\t32"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.ui_elements(app_name="TextEdit", limit=20)

    assert result["ok"] is True
    assert result["data"]["role_counts"] == {"AXCell": 1}
    assert result["data"]["control_like_count"] == 1
    assert result["data"]["inspection_level"] == "control"
    assert result["data"]["visibility_status"] == "control_accessible"


def test_desktop_ui_elements_can_read_named_running_app(monkeypatch) -> None:
    osascript_args = []

    def fake_osascript(_script, args=None):
        osascript_args.append(args)
        return {
            "ok": True,
            "stdout": (
                "META\tGoogle Chrome\t202\tChatGPT\n"
                "0\tAXButton\t\tSend\tSend message\t\ttrue\t100\t220\t40\t40"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_resolve_installed_app_name",
        lambda app_name: "Google Chrome",
    )
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.ui_elements(
        app_name="Chrome",
        role_filter="button",
        limit=20,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["data"]["app_name"] == "Google Chrome"
    assert result["data"]["requested_app_name"] == "Chrome"
    assert result["data"]["resolved_app_name"] == "Google Chrome"
    assert result["data"]["app_resolution"] == "installed_app_bundle"
    assert result["data"]["elements"][0]["name"] == "Send"
    assert osascript_args == [["20", "6", "Google Chrome"]]


def test_desktop_inspect_app_returns_ready_snapshot_for_accessible_controls(monkeypatch) -> None:
    calls: list[tuple] = []

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "list_apps",
        lambda query="", limit=200: calls.append(("list_apps", query, limit))
        or {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {"apps": [{"name": "Linear"}]},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_status",
        lambda app_name: calls.append(("status", app_name))
        or {
            "ok": True,
            "action": "app.status",
            "data": {"app_name": app_name, "running": True},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: calls.append(("focus", app_name))
        or {
            "ok": True,
            "action": "app.focus",
            "data": {"app_name": app_name, "focus_verified": True},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: calls.append(("active_window",))
        or {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Linear"},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "windows",
        lambda app_name="": calls.append(("windows", app_name))
        or {
            "ok": True,
            "action": "desktop.windows",
            "data": {"app_name": app_name, "count": 1, "windows": [{"title": "Inbox"}]},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "ui_elements",
        lambda app_name="", role_filter="", limit=80: calls.append(
            ("ui_elements", app_name, role_filter, limit)
        )
        or {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": app_name,
                "count": 2,
                "inspection_level": "control",
                "control_like_count": 2,
                "visibility_limited": False,
                "visibility_status": "control_accessible",
            },
        },
    )

    result = desktop_mod.inspect_app(
        "Linear",
        focus=True,
        role_filter="button",
        limit=20,
    )

    assert result["ok"] is True
    assert result["action"] == "desktop.inspect_app"
    assert result["summary"] == "Inspected Linear: focused with accessible controls"
    assert result["data"]["ready_for_foreground_action"] is True
    assert result["data"]["focus_verified"] is True
    assert result["data"]["window_count"] == 1
    assert result["data"]["ui_element_count"] == 2
    assert result["data"]["recommended_tools"] == [
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
    ]
    assert calls == [
        ("list_apps", "Linear", 10),
        ("status", "Linear"),
        ("status", "Linear"),
        ("focus", "Linear"),
        ("active_window",),
        ("windows", "Linear"),
        ("ui_elements", "Linear", "button", 20),
    ]


def test_desktop_inspect_app_reports_limited_menu_visibility(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "list_apps",
        lambda query="", limit=200: {
            "ok": True,
            "action": "desktop.list_apps",
            "data": {"apps": [{"name": "Calculator"}]},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_status",
        lambda app_name: {
            "ok": True,
            "action": "app.status",
            "data": {"app_name": app_name, "running": True},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_focus",
        lambda app_name: {
            "ok": False,
            "action": "app.focus",
            "error": "app_focus_not_verified",
            "data": {"app_name": app_name, "focus_verified": False},
            "recovery_actions": [
                {"label": "查看前台窗口", "tool": "desktop.active_window", "input": {}}
            ],
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {"ok": True, "action": "desktop.active_window", "data": {"app_name": "Codex"}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "windows",
        lambda app_name="": {
            "ok": True,
            "action": "desktop.windows",
            "data": {
                "app_name": app_name,
                "count": 0,
                "visibility_limited": True,
                "window_visibility_status": "running_without_visible_windows",
            },
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "ui_elements",
        lambda app_name="", role_filter="", limit=80: {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {
                "app_name": app_name,
                "count": 4,
                "inspection_level": "menu",
                "control_like_count": 0,
                "visibility_limited": True,
                "visibility_status": "menu_level_only",
            },
        },
    )

    result = desktop_mod.inspect_app("Calculator", focus=True)

    assert result["ok"] is True
    assert result["summary"] == (
        "Inspected Calculator: limited to menu-level UI; foreground action is not ready"
    )
    assert result["data"]["ready_for_foreground_action"] is False
    assert result["data"]["focus_verified"] is False
    assert result["data"]["visibility_limited"] is True
    assert result["data"]["visibility_status"] == "menu_level_only"
    assert result["data"]["recommended_tools"] == [
        "app.focus",
        "desktop.permissions",
        "app.show",
        "screen.capture",
        "desktop.windows",
        "desktop.ui_elements",
    ]
    assert result["data"]["recovery_actions"][0]["tool"] == "desktop.active_window"
    assert any(action["tool"] == "screen.capture" for action in result["data"]["recovery_actions"])


def test_desktop_inspect_app_reports_app_not_found(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "list_apps",
        lambda query="", limit=200: {"ok": True, "action": "desktop.list_apps", "data": {"apps": []}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_status",
        lambda app_name: {
            "ok": True,
            "action": "app.status",
            "data": {"app_name": app_name, "running": False},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: {
            "ok": False,
            "action": "app.open",
            "data": {"app_name": app_name},
        },
    )
    monkeypatch.setattr(desktop_mod, "active_window", lambda: {"ok": True, "data": {}})
    monkeypatch.setattr(
        desktop_mod,
        "windows",
        lambda app_name="": {"ok": True, "action": "desktop.windows", "data": {"count": 0}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "ui_elements",
        lambda app_name="", role_filter="", limit=80: {
            "ok": True,
            "action": "desktop.ui_elements",
            "data": {"app_name": app_name, "count": 0},
        },
    )

    result = desktop_mod.inspect_app("Definitely Missing App")

    assert result["ok"] is False
    assert result["action"] == "desktop.inspect_app"
    assert result["error"] == "app_not_found"
    assert result["data"]["app_found"] is False
    assert result["recommended_tools"] == ["desktop.list_apps", "app.open", "screen.capture", "desktop.permissions"]


def test_desktop_ui_elements_permission_failure_returns_recovery_targets(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Not authorized to send Apple events to System Events.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.ui_elements()

    assert result["ok"] is False
    assert result["action"] == "desktop.ui_elements"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]


def test_desktop_click_ui_element_matches_foreground_control_and_clicks_center(
    monkeypatch,
) -> None:
    clicks: list[tuple[str, int, int, int]] = []
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "Google Chrome UI elements: AXButton: Send",
        "data": {
            "app_name": "Google Chrome",
            "title": "ChatGPT",
            "elements": [
                {
                    "depth": 0,
                    "role": "AXTextField",
                    "name": "",
                    "description": "Message",
                    "value": "",
                    "enabled": True,
                    "center": {"x": 80, "y": 240},
                },
                {
                    "depth": 0,
                    "role": "AXButton",
                    "name": "Send",
                    "description": "Send message",
                    "value": "",
                    "enabled": True,
                    "center": {"x": 120, "y": 240},
                },
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "ui_elements",
        lambda role_filter="", limit=80: observed,
    )

    def fake_click(action_name, x, y, *, click_count=1):
        clicks.append((action_name, x, y, click_count))
        return {
            "ok": True,
            "action": action_name,
            "summary": f"Clicked foreground desktop at ({x}, {y})",
            "data": {"x": x, "y": y, "click_count": click_count},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(desktop_mod, "_send_desktop_click", fake_click)

    result = desktop_mod.click_ui_element(
        "Send",
        role_filter="button",
        limit=20,
        click_count=2,
    )

    assert clicks == [("desktop.click_ui_element", 120, 240, 2)]
    assert result["ok"] is True
    assert result["action"] == "desktop.click_ui_element"
    assert result["summary"] == "Clicked foreground UI element: Send"
    assert result["data"]["target"] == "Send"
    assert result["data"]["matched_label"] == "Send"
    assert result["data"]["role_filter"] == "button"
    assert result["data"]["match_count"] == 1
    assert result["data"]["element"]["role"] == "AXButton"
    assert result["fallback_result"] == {"observe": observed}


def test_desktop_click_ui_element_returns_candidates_without_blind_click(
    monkeypatch,
) -> None:
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "Notes UI elements",
        "data": {
            "app_name": "Notes",
            "title": "Draft",
            "inspection_level": "control",
            "visibility_status": "control_accessible",
            "visibility_limited": False,
            "elements": [
                {
                    "depth": 0,
                    "role": "AXButton",
                    "name": "Cancel",
                    "enabled": True,
                    "center": {"x": 44, "y": 55},
                }
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_click",
        lambda *args, **kwargs: pytest.fail("should not click without a UI element match"),
    )

    result = desktop_mod.click_ui_element("Send", role_filter="button")

    assert result["ok"] is False
    assert result["action"] == "desktop.click_ui_element"
    assert result["error"] == "ui_element_not_found"
    assert result["fallback"] == "screen.capture"
    assert result["data"]["target"] == "Send"
    assert result["data"]["inspection_level"] == "control"
    assert result["data"]["visibility_status"] == "control_accessible"
    assert result["data"]["visibility_limited"] is False
    assert result["data"]["recommended_tools"] == ["screen.capture", "desktop.click"]
    assert result["data"]["candidates"] == [
        {
            "role": "AXButton",
            "label": "Cancel",
            "enabled": True,
            "center": {"x": 44, "y": 55},
        }
    ]
    assert result["recovery_actions"] == [
        {
            "label": "截取屏幕重新定位控件",
            "tool": "screen.capture",
            "input": {
                "reason": (
                    "desktop.click_ui_element could not find Send; "
                    "capture screen before coordinate click"
                )
            },
            "permission_target": "screen_observation",
            "risk_level": "low",
            "retry_tool": "desktop.click",
            "recovery_retry_tool": "desktop.click",
            "retry_input": {"click_count": 1},
            "recovery_retry_input": {"click_count": 1},
            "retry_input_schema": {
                "type": "object",
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0},
                    "click_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "default": 1,
                    },
                },
            },
            "recovery_retry_input_schema": {
                "type": "object",
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0},
                    "click_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "default": 1,
                    },
                },
            },
            "retry_input_source": "screen_capture_artifact",
            "recovery_retry_input_source": "screen_capture_artifact",
            "retry_artifact_tool": "screen.capture",
            "recovery_retry_artifact_tool": "screen.capture",
            "retry_artifact_kind": "image",
            "recovery_retry_artifact_kind": "image",
            "required_retry_fields": ["x", "y"],
            "recommended_tools": ["screen.capture", "desktop.click"],
            "retry_prompt": "根据截图定位「Send」并用 desktop.click 点击坐标",
            "recovery_retry_prompt": "根据截图定位「Send」并用 desktop.click 点击坐标",
            "target": "Send",
            "role_filter": "button",
            "click_count": 1,
        }
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]


def test_desktop_click_ui_element_stops_when_expected_app_mismatches(
    monkeypatch,
) -> None:
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "QQ UI elements",
        "data": {
            "app_name": "QQ",
            "title": "QQ",
            "elements": [
                {
                    "depth": 0,
                    "role": "AXButton",
                    "name": "关闭按钮",
                    "enabled": True,
                    "center": {"x": 44, "y": 55},
                }
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_click",
        lambda *args, **kwargs: pytest.fail("should not click a mismatched foreground app"),
    )

    result = desktop_mod.click_ui_element(
        "关闭按钮",
        role_filter="button",
        expected_app_name="TextEdit",
    )

    assert result["ok"] is False
    assert result["action"] == "desktop.click_ui_element"
    assert result["error"] == "foreground_app_mismatch"
    assert result["summary"] == "Foreground app changed before clicking UI element"
    assert result["data"]["expected_app_name"] == "TextEdit"
    assert result["data"]["observed_app_name"] == "QQ"
    assert result["data"]["recommended_tools"] == [
        "app.focus",
        "desktop.active_window",
        "screen.capture",
    ]
    assert result["fallback_result"] == {"observe": observed}


def test_desktop_click_ui_element_permission_failure_returns_recovery_targets(
    monkeypatch,
) -> None:
    observed = {
        "ok": False,
        "action": "desktop.ui_elements",
        "summary": "desktop.ui_elements failed",
        "error": "Not authorized to send Apple events to System Events.",
        "data": {},
        "permission_error": True,
        "fallback_used": False,
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)

    result = desktop_mod.click_ui_element("Send")

    assert result["ok"] is False
    assert result["action"] == "desktop.click_ui_element"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]
    assert result["data"]["target"] == "Send"
    assert result["fallback_result"] == {"observe": observed}


def test_desktop_type_into_ui_element_matches_input_focuses_and_types(
    monkeypatch,
) -> None:
    clicks: list[tuple[str, int, int, int]] = []
    typed: list[tuple[str, str, str]] = []
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "Google Chrome UI elements: AXTextField: Search",
        "data": {
            "app_name": "Google Chrome",
            "title": "Search",
            "elements": [
                {
                    "depth": 0,
                    "role": "AXTextField",
                    "name": "Search",
                    "description": "Search field",
                    "value": "",
                    "enabled": True,
                    "center": {"x": 120, "y": 240},
                }
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)

    def fake_click(action_name, x, y, *, click_count=1):
        clicks.append((action_name, x, y, click_count))
        return {
            "ok": True,
            "action": action_name,
            "summary": f"Clicked foreground desktop at ({x}, {y})",
            "data": {"x": x, "y": y, "click_count": click_count},
            "permission_error": False,
            "fallback_used": False,
        }

    def fake_type(action_name, text, *, summary):
        typed.append((action_name, text, summary))
        return {
            "ok": True,
            "action": action_name,
            "summary": summary,
            "data": {"character_count": len(text)},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(desktop_mod, "_send_desktop_click", fake_click)
    monkeypatch.setattr(desktop_mod, "_send_desktop_text", fake_type)

    result = desktop_mod.type_into_ui_element("Search", "yachiyo", role_filter="text", limit=20)

    assert clicks == [("desktop.type_into_ui_element", 120, 240, 1)]
    assert typed == [
        (
            "desktop.type_into_ui_element",
            "yachiyo",
            "Typed into foreground UI element: Search",
        )
    ]
    assert result["ok"] is True
    assert result["action"] == "desktop.type_into_ui_element"
    assert result["summary"] == "Typed into foreground UI element: Search"
    assert result["data"]["target"] == "Search"
    assert result["data"]["matched_label"] == "Search"
    assert result["data"]["role_filter"] == "text"
    assert result["data"]["character_count"] == 7
    assert result["data"]["match_count"] == 1
    assert result["data"]["element"]["role"] == "AXTextField"
    assert "text" not in result["data"]
    assert list(result["fallback_result"]) == ["observe", "focus", "type_text"]


def test_desktop_type_into_ui_element_returns_candidates_without_blind_typing(
    monkeypatch,
) -> None:
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "Notes UI elements",
        "data": {
            "app_name": "Notes",
            "title": "Draft",
            "inspection_level": "control",
            "visibility_status": "control_accessible",
            "visibility_limited": False,
            "elements": [
                {
                    "depth": 0,
                    "role": "AXTextField",
                    "name": "Title",
                    "enabled": True,
                    "center": {"x": 44, "y": 55},
                }
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_click",
        lambda *args, **kwargs: pytest.fail("should not focus without a UI element match"),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_text",
        lambda *args, **kwargs: pytest.fail("should not type without a UI element match"),
    )

    result = desktop_mod.type_into_ui_element("Search", "hello")

    assert result["ok"] is False
    assert result["action"] == "desktop.type_into_ui_element"
    assert result["error"] == "ui_element_not_found"
    assert result["fallback"] == "screen.capture"
    assert result["data"]["target"] == "Search"
    assert result["data"]["character_count"] == 5
    assert result["data"]["inspection_level"] == "control"
    assert result["data"]["visibility_status"] == "control_accessible"
    assert result["data"]["visibility_limited"] is False
    assert result["data"]["recommended_tools"] == [
        "screen.capture",
        "desktop.click",
        "desktop.type_text",
    ]
    assert result["data"]["candidates"] == [
        {
            "role": "AXTextField",
            "label": "Title",
            "enabled": True,
            "center": {"x": 44, "y": 55},
        }
    ]
    assert result["recovery_actions"] == [
        {
            "label": "截取屏幕重新定位输入框",
            "tool": "screen.capture",
            "input": {
                "reason": (
                    "desktop.type_into_ui_element could not find Search; "
                    "capture screen before coordinate focus and text entry"
                )
            },
            "permission_target": "screen_observation",
            "risk_level": "low",
            "retry_tool": "desktop.click",
            "recovery_retry_tool": "desktop.click",
            "retry_input": {"click_count": 1},
            "recovery_retry_input": {"click_count": 1},
            "retry_input_schema": {
                "type": "object",
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0},
                    "click_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "default": 1,
                    },
                },
            },
            "recovery_retry_input_schema": {
                "type": "object",
                "required": ["x", "y"],
                "properties": {
                    "x": {"type": "number", "minimum": 0},
                    "y": {"type": "number", "minimum": 0},
                    "click_count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "default": 1,
                    },
                },
            },
            "retry_input_source": "screen_capture_artifact",
            "recovery_retry_input_source": "screen_capture_artifact",
            "retry_artifact_tool": "screen.capture",
            "recovery_retry_artifact_tool": "screen.capture",
            "retry_artifact_kind": "image",
            "recovery_retry_artifact_kind": "image",
            "required_retry_fields": ["x", "y"],
            "followup_tool": "desktop.type_text",
            "recovery_followup_tool": "desktop.type_text",
            "followup_input": {
                "text_source": "original_request",
                "character_count": 5,
            },
            "recovery_followup_input": {
                "text_source": "original_request",
                "character_count": 5,
            },
            "recommended_tools": ["screen.capture", "desktop.click", "desktop.type_text"],
            "retry_prompt": (
                "根据截图定位「Search」输入框，先用 desktop.click 点击坐标，"
                "再用 desktop.type_text 输入原请求文本"
            ),
            "recovery_retry_prompt": (
                "根据截图定位「Search」输入框，先用 desktop.click 点击坐标，"
                "再用 desktop.type_text 输入原请求文本"
            ),
            "target": "Search",
            "role_filter": "text",
            "character_count": 5,
        }
    ]
    assert result["data"]["recovery_actions"] == result["recovery_actions"]
    assert "hello" not in str(result["recovery_actions"])


def test_desktop_type_into_ui_element_stops_when_expected_app_mismatches(
    monkeypatch,
) -> None:
    observed = {
        "ok": True,
        "action": "desktop.ui_elements",
        "summary": "QQ UI elements",
        "data": {
            "app_name": "QQ",
            "title": "QQ",
            "elements": [
                {
                    "depth": 0,
                    "role": "AXTextField",
                    "name": "搜索",
                    "enabled": True,
                    "center": {"x": 44, "y": 55},
                }
            ],
        },
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_click",
        lambda *args, **kwargs: pytest.fail("should not focus a mismatched foreground app"),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_send_desktop_text",
        lambda *args, **kwargs: pytest.fail("should not type into a mismatched foreground app"),
    )

    result = desktop_mod.type_into_ui_element(
        "搜索",
        "hello",
        expected_app_name="TextEdit",
    )

    assert result["ok"] is False
    assert result["action"] == "desktop.type_into_ui_element"
    assert result["error"] == "foreground_app_mismatch"
    assert result["summary"] == "Foreground app changed before typing into UI element"
    assert result["data"]["expected_app_name"] == "TextEdit"
    assert result["data"]["observed_app_name"] == "QQ"
    assert result["data"]["character_count"] == 5
    assert result["fallback_result"] == {"observe": observed}


def test_desktop_type_into_ui_element_permission_failure_returns_recovery_targets(
    monkeypatch,
) -> None:
    observed = {
        "ok": False,
        "action": "desktop.ui_elements",
        "summary": "desktop.ui_elements failed",
        "error": "Not authorized to send Apple events to System Events.",
        "data": {},
        "permission_error": True,
        "fallback_used": False,
    }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "ui_elements", lambda role_filter="", limit=80: observed)

    result = desktop_mod.type_into_ui_element("Search", "hello")

    assert result["ok"] is False
    assert result["action"] == "desktop.type_into_ui_element"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["automation_or_accessibility"]
    assert result["permission_targets"] == ["automation", "accessibility"]
    assert result["data"]["target"] == "Search"
    assert result["data"]["character_count"] == 5
    assert result["fallback_result"] == {"observe": observed}


def test_app_status_resolves_bundle_id_alias_without_target_apple_event(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def forbidden_osascript(*_args, **_kwargs):
        raise AssertionError("app.status must not send Apple Events to the target app")

    def fake_jxa(script: str, args=None) -> dict:
        captured["script"] = script
        captured["args"] = args
        return {
            "ok": True,
            "stdout": json.dumps({"running": True}),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_resolve_installed_app",
        lambda _app_name: {
            "name": "wpsoffice",
            "matched_name": "WPS Office",
            "metadata": {"bundle_id": "com.kingsoft.wpsoffice.mac"},
        },
    )
    monkeypatch.setattr(desktop_mod, "_run_osascript", forbidden_osascript)
    monkeypatch.setattr(desktop_mod, "_run_jxa", fake_jxa)

    result = desktop_mod.app_status("WPS")

    assert result["ok"] is True
    assert result["action"] == "app.status"
    assert result["summary"] == "WPS is running"
    assert result["permission_error"] is False
    assert result["fallback_used"] is False
    assert result["data"] == {
        "app_name": "WPS",
        "running": True,
        "status": "running",
    }
    criteria = json.loads(captured["args"][0])
    assert criteria == {
        "bundle_id": "com.kingsoft.wpsoffice.mac",
        "names": ["WPS Office", "wpsoffice", "WPS"],
    }
    assert "NSWorkspace" in captured["script"]
    assert "runningApplications" in captured["script"]
    assert "activate" not in captured["script"]
    assert "frontmostApplication" not in captured["script"]


def test_app_status_reports_not_running_state(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app", lambda _app_name: {})
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app.status must not use target-application AppleScript")
        ),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": json.dumps({"running": False}),
            "stderr": "",
        },
    )

    result = desktop_mod.app_status("Slack")

    assert result["ok"] is True
    assert result["action"] == "app.status"
    assert result["summary"] == "Slack is not running"
    assert result["data"] == {
        "app_name": "Slack",
        "running": False,
        "status": "not_running",
    }


def test_app_status_jxa_failure_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_resolve_installed_app", lambda _app_name: {})
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("app.status must not fall back to target-application AppleScript")
        ),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_run_jxa",
        lambda _script, _args=None: {
            "ok": False,
            "error": "jxa query timed out",
            "permission_error": False,
        },
    )

    result = desktop_mod.app_status("WPS Office")

    assert result["ok"] is False
    assert result["action"] == "app.status"
    assert result["summary"] == "app.status failed"
    assert result["error"] == "jxa query timed out"
    assert result["permission_error"] is False
    assert result["data"] == {"app_name": "WPS Office"}


def test_desktop_close_window_uses_standard_foreground_shortcut(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "closed_window", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.desktop_close_window()

    assert result == {
        "ok": True,
        "action": "desktop.close_window",
        "summary": "Closed the foreground window",
        "data": {"key": "w", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "keystroke \"w\" using {command down}" in calls[0][0]
    assert calls[0][1] is None


def test_desktop_quit_app_uses_standard_foreground_shortcut(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "quit_foreground_app", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.desktop_quit_app()

    assert result == {
        "ok": True,
        "action": "desktop.quit_app",
        "summary": "Sent quit request to the foreground app",
        "data": {"key": "q", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "keystroke \"q\" using {command down}" in calls[0][0]
    assert calls[0][1] is None


def test_desktop_minimize_window_uses_standard_foreground_shortcut(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "minimized_window", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.desktop_minimize_window()

    assert result == {
        "ok": True,
        "action": "desktop.minimize_window",
        "summary": "Minimized the foreground window",
        "data": {"key": "m", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "keystroke \"m\" using {command down}" in calls[0][0]
    assert calls[0][1] is None


def test_desktop_hide_app_uses_standard_foreground_shortcut(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "hidden_app", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.desktop_hide_app()

    assert result == {
        "ok": True,
        "action": "desktop.hide_app",
        "summary": "Hid the foreground app",
        "data": {"key": "h", "modifiers": ["command"]},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "keystroke \"h\" using {command down}" in calls[0][0]
    assert calls[0][1] is None


def test_desktop_type_text_permission_failure_returns_accessibility_target(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="System Events got an error: osascript is not allowed assistive access.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_type_text("hello")

    assert result["ok"] is False
    assert result["action"] == "desktop.type_text"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["accessibility"]
    assert result["permission_targets"] == ["accessibility"]
    assert result["recovery_hints"] == [
        (
            "Grant Accessibility permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Accessibility."
        )
    ]


def test_desktop_safe_type_text_uses_system_events_with_explicit_text(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="typed\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_type_text("hello")

    assert result == {
        "ok": True,
        "action": "desktop.safe_type_text",
        "summary": "Typed user-provided text into the foreground app",
        "data": {"character_count": 5, "explicit_user_text": True},
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert calls[0][0][-1] == "hello"
    assert "keystroke textToType" in calls[0][0][2]


def test_desktop_click_uses_system_events_with_coordinates(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="clicked\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_click(12.2, "34.6", click_count=2)

    assert result == {
        "ok": True,
        "action": "desktop.click",
        "summary": "Clicked foreground desktop at (12, 35)",
        "data": {"x": 12, "y": 35, "click_count": 2},
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert calls[0][0][-3:] == ["12", "35", "2"]


def test_desktop_safe_click_uses_single_system_events_click(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="clicked\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_click(12.2, "34.6")

    assert result == {
        "ok": True,
        "action": "desktop.safe_click",
        "summary": "Clicked explicit foreground coordinate at (12, 35)",
        "data": {
            "x": 12,
            "y": 35,
            "click_count": 1,
            "explicit_user_coordinates": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert calls[0][0][-3:] == ["12", "35", "1"]


def test_desktop_safe_scroll_uses_system_events_page_keys(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="scrolled\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    down = desktop_mod.desktop_safe_scroll("down", pages=2)
    up = desktop_mod.desktop_safe_scroll("up")

    assert down == {
        "ok": True,
        "action": "desktop.safe_scroll",
        "summary": "Scrolled foreground desktop down 2 pages",
        "data": {
            "direction": "down",
            "pages": 2,
            "key_code": 121,
            "explicit_user_scroll": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert up["ok"] is True
    assert up["data"] == {
        "direction": "up",
        "pages": 1,
        "key_code": 116,
        "explicit_user_scroll": True,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert calls[0][0][-2:] == ["121", "2"]
    assert calls[1][0][-2:] == ["116", "1"]


def test_desktop_safe_shortcut_uses_whitelisted_system_events_keystroke(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("browser_forward")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: browser forward",
        "data": {
            "key": "]",
            "modifiers": ["command"],
            "shortcut_action": "browser_forward",
            "shortcut_label": "browser forward",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert 'keystroke keyName using {command down}' in calls[0][0][2]
    assert calls[0][0][-1] == "]"


def test_desktop_safe_shortcut_reopen_closed_tab_uses_command_shift_t(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("reopen_closed_tab")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: reopen closed tab",
        "data": {
            "key": "t",
            "modifiers": ["command", "shift"],
            "shortcut_action": "reopen_closed_tab",
            "shortcut_label": "reopen closed tab",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert "keystroke keyName using {command down, shift down}" in calls[0][0][2]
    assert calls[0][0][-1] == "t"


def test_desktop_safe_shortcut_next_window_uses_grave_key_code(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("next_window")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: next window",
        "data": {
            "key": "`",
            "modifiers": ["command"],
            "key_code": 50,
            "shortcut_action": "next_window",
            "shortcut_label": "next window",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert "key code keyCodeValue using {command down}" in calls[0][0][2]
    assert calls[0][0][-1] == "50"


def test_desktop_safe_shortcut_mission_control_uses_control_up(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("mission_control")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: mission control",
        "data": {
            "key": "up",
            "modifiers": ["control"],
            "key_code": 126,
            "shortcut_action": "mission_control",
            "shortcut_label": "mission control",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert "key code keyCodeValue using {control down}" in calls[0][0][2]
    assert calls[0][0][-1] == "126"


def test_desktop_safe_shortcut_force_quit_dialog_uses_command_option_escape(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("force_quit_dialog")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: force quit dialog",
        "data": {
            "key": "escape",
            "modifiers": ["command", "option"],
            "key_code": 53,
            "shortcut_action": "force_quit_dialog",
            "shortcut_label": "force quit dialog",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert "key code keyCodeValue using {command down, option down}" in calls[0][0][2]
    assert calls[0][0][-1] == "53"


def test_desktop_safe_shortcut_application_windows_uses_control_down(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("application_windows")

    assert result == {
        "ok": True,
        "action": "desktop.safe_shortcut",
        "summary": "Executed safe shortcut: application windows",
        "data": {
            "key": "down",
            "modifiers": ["control"],
            "key_code": 125,
            "shortcut_action": "application_windows",
            "shortcut_label": "application windows",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert "key code keyCodeValue using {control down}" in calls[0][0][2]
    assert calls[0][0][-1] == "125"


def test_desktop_safe_shortcut_system_overlays_use_whitelisted_shortcuts(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    spotlight = desktop_mod.desktop_safe_shortcut("spotlight_search")
    emoji = desktop_mod.desktop_safe_shortcut("emoji_picker")
    screenshot_selection = desktop_mod.desktop_safe_shortcut("screenshot_selection")
    screenshot_toolbar = desktop_mod.desktop_safe_shortcut("screenshot_toolbar")
    lock_screen = desktop_mod.desktop_safe_shortcut("lock_screen")
    finder_quick_look = desktop_mod.desktop_safe_shortcut("finder_quick_look")
    finder_airdrop = desktop_mod.desktop_safe_shortcut("finder_airdrop")

    assert spotlight["summary"] == "Executed safe shortcut: spotlight search"
    assert spotlight["data"] == {
        "key": "space",
        "modifiers": ["command"],
        "key_code": 49,
        "shortcut_action": "spotlight_search",
        "shortcut_label": "spotlight search",
    }
    assert emoji["summary"] == "Executed safe shortcut: emoji picker"
    assert emoji["data"] == {
        "key": "space",
        "modifiers": ["control", "command"],
        "key_code": 49,
        "shortcut_action": "emoji_picker",
        "shortcut_label": "emoji picker",
    }
    assert screenshot_selection["summary"] == "Executed safe shortcut: screenshot selection"
    assert screenshot_selection["data"] == {
        "key": "4",
        "modifiers": ["command", "shift"],
        "shortcut_action": "screenshot_selection",
        "shortcut_label": "screenshot selection",
    }
    assert screenshot_toolbar["summary"] == "Executed safe shortcut: screenshot toolbar"
    assert screenshot_toolbar["data"] == {
        "key": "5",
        "modifiers": ["command", "shift"],
        "shortcut_action": "screenshot_toolbar",
        "shortcut_label": "screenshot toolbar",
    }
    assert lock_screen["summary"] == "Executed safe shortcut: lock screen"
    assert lock_screen["data"] == {
        "key": "q",
        "modifiers": ["control", "command"],
        "shortcut_action": "lock_screen",
        "shortcut_label": "lock screen",
    }
    assert finder_quick_look["summary"] == "Executed safe shortcut: Finder Quick Look"
    assert finder_quick_look["data"] == {
        "key": "space",
        "modifiers": [],
        "key_code": 49,
        "shortcut_action": "finder_quick_look",
        "shortcut_label": "Finder Quick Look",
    }
    assert finder_airdrop["summary"] == "Executed safe shortcut: Finder AirDrop"
    assert finder_airdrop["data"] == {
        "key": "r",
        "modifiers": ["command", "shift"],
        "shortcut_action": "finder_airdrop",
        "shortcut_label": "Finder AirDrop",
    }
    assert "key code keyCodeValue using {command down}" in calls[0][0][2]
    assert calls[0][0][-1] == "49"
    assert "key code keyCodeValue using {control down, command down}" in calls[1][0][2]
    assert calls[1][0][-1] == "49"
    assert "keystroke keyName using {command down, shift down}" in calls[2][0][2]
    assert calls[2][0][-1] == "4"
    assert "keystroke keyName using {command down, shift down}" in calls[3][0][2]
    assert calls[3][0][-1] == "5"
    assert "keystroke keyName using {control down, command down}" in calls[4][0][2]
    assert calls[4][0][-1] == "q"
    assert "key code keyCodeValue" in calls[5][0][2]
    assert " using " not in calls[5][0][2]
    assert calls[5][0][-1] == "49"


def test_desktop_safe_shortcut_tab_management_uses_whitelisted_shortcuts(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    close_tab = desktop_mod.desktop_safe_shortcut("close_tab")
    next_tab = desktop_mod.desktop_safe_shortcut("next_tab")
    previous_tab = desktop_mod.desktop_safe_shortcut("previous_tab")

    assert close_tab["summary"] == "Executed safe shortcut: close tab"
    assert close_tab["data"]["key"] == "w"
    assert close_tab["data"]["modifiers"] == ["command"]
    assert next_tab["summary"] == "Executed safe shortcut: next tab"
    assert next_tab["data"]["key"] == "]"
    assert next_tab["data"]["modifiers"] == ["command", "shift"]
    assert previous_tab["summary"] == "Executed safe shortcut: previous tab"
    assert previous_tab["data"]["key"] == "["
    assert previous_tab["data"]["modifiers"] == ["command", "shift"]
    assert len(calls) == 3


def test_desktop_safe_shortcut_browser_utilities_use_whitelisted_shortcuts(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    expected = (
        ("focus_address_bar", "l", ["command"], "focus address bar"),
        ("new_private_window", "n", ["command", "shift"], "new private window"),
        ("bookmark_page", "d", ["command"], "bookmark current page"),
        ("show_history", "y", ["command"], "show history"),
        ("open_devtools", "i", ["command", "option"], "open developer tools"),
        ("zoom_in", "+", ["command"], "zoom in"),
        ("zoom_out", "-", ["command"], "zoom out"),
        ("reset_zoom", "0", ["command"], "reset zoom"),
    )

    for action, key, modifiers, label in expected:
        result = desktop_mod.desktop_safe_shortcut(action)
        assert result["summary"] == f"Executed safe shortcut: {label}"
        assert result["data"]["shortcut_action"] == action
        assert result["data"]["key"] == key
        assert result["data"]["modifiers"] == modifiers

    assert len(calls) == len(expected)


def test_desktop_safe_shortcut_new_document_uses_command_n(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="new document\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("new_document")
    message = desktop_mod.desktop_safe_shortcut("new_message")

    assert result["ok"] is True
    assert result["summary"] == "Executed safe shortcut: new document"
    assert result["data"]["key"] == "n"
    assert result["data"]["modifiers"] == ["command"]
    assert result["data"]["shortcut_action"] == "new_document"
    assert result["data"]["shortcut_label"] == "new document"
    assert calls[0][0][-1] == "n"
    assert message["ok"] is True
    assert message["summary"] == "Executed safe shortcut: new message"
    assert message["data"]["key"] == "n"
    assert message["data"]["modifiers"] == ["command"]
    assert message["data"]["shortcut_action"] == "new_message"
    assert message["data"]["shortcut_label"] == "new message"
    assert calls[1][0][-1] == "n"


def test_desktop_safe_shortcut_new_folder_uses_command_shift_n(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="new folder\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("new_folder")

    assert result["ok"] is True
    assert result["summary"] == "Executed safe shortcut: new folder"
    assert result["data"]["key"] == "n"
    assert result["data"]["modifiers"] == ["command", "shift"]
    assert result["data"]["shortcut_action"] == "new_folder"
    assert result["data"]["shortcut_label"] == "new folder"
    assert "keystroke keyName using {command down, shift down}" in calls[0][0][2]
    assert calls[0][0][-1] == "n"


def test_desktop_safe_shortcut_finder_item_actions_use_expected_keys(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="finder action\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    rename = desktop_mod.desktop_safe_shortcut("rename_selected")
    parent = desktop_mod.desktop_safe_shortcut("parent_folder")
    get_info = desktop_mod.desktop_safe_shortcut("finder_get_info")

    assert rename["ok"] is True
    assert rename["summary"] == "Executed safe shortcut: rename selected Finder item"
    assert rename["data"] == {
        "key": "return",
        "modifiers": [],
        "key_code": 36,
        "shortcut_action": "rename_selected",
        "shortcut_label": "rename selected Finder item",
    }
    assert parent["ok"] is True
    assert parent["summary"] == "Executed safe shortcut: open parent folder"
    assert parent["data"] == {
        "key": "up",
        "modifiers": ["command"],
        "key_code": 126,
        "shortcut_action": "parent_folder",
        "shortcut_label": "open parent folder",
    }
    assert get_info["ok"] is True
    assert get_info["summary"] == "Executed safe shortcut: Finder Get Info"
    assert get_info["data"] == {
        "key": "i",
        "modifiers": ["command"],
        "shortcut_action": "finder_get_info",
        "shortcut_label": "Finder Get Info",
    }
    assert "key code keyCodeValue" in calls[0][0][2]
    assert " using " not in calls[0][0][2]
    assert calls[0][0][-1] == "36"
    assert "key code keyCodeValue using {command down}" in calls[1][0][2]
    assert calls[1][0][-1] == "126"
    assert "keystroke keyName using {command down}" in calls[2][0][2]
    assert calls[2][0][-1] == "i"


def test_desktop_safe_shortcut_new_event_uses_command_n(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="new event\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_shortcut("new_event")

    assert result["ok"] is True
    assert result["summary"] == "Executed safe shortcut: new calendar event"
    assert result["data"]["key"] == "n"
    assert result["data"]["modifiers"] == ["command"]
    assert result["data"]["shortcut_action"] == "new_event"
    assert result["data"]["shortcut_label"] == "new calendar event"
    assert calls[0][0][-1] == "n"


def test_desktop_notes_create_uses_macos_notes_automation(monkeypatch) -> None:
    calls = []

    def fake_run_osascript(script, args=None):
        calls.append((script, args or []))
        return {"ok": True, "stdout": "note-id", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_run_osascript)

    result = desktop_mod.notes_create("hello world")

    assert result["ok"] is True
    assert result["action"] == "notes.create"
    assert result["postcondition_verified"] is True
    assert result["data"] == {
        "title": "hello world",
        "body_length": 11,
        "folder_name": "",
        "note_id": "note-id",
        "postcondition_verified": True,
    }
    assert "tell application \"Notes\"" in calls[0][0]
    assert calls[0][1] == ["hello world", "hello world", ""]


def test_desktop_reminders_create_uses_macos_reminders_automation(monkeypatch) -> None:
    calls = []

    def fake_run_osascript(script, args=None):
        calls.append((script, args or []))
        return {"ok": True, "stdout": "reminder-id", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_run_osascript)

    result = desktop_mod.reminders_create("开会", due_at="2026-06-25T15:00")

    assert result["ok"] is True
    assert result["action"] == "reminders.create"
    assert result["postcondition_verified"] is True
    assert result["data"] == {
        "title": "开会",
        "due_at": "2026-06-25T15:00",
        "list_name": "",
        "reminder_id": "reminder-id",
        "postcondition_verified": True,
    }
    assert "tell application \"Reminders\"" in calls[0][0]
    assert calls[0][1] == ["开会", "", "true", "2026", "6", "25", "15", "0"]


def test_desktop_calendar_create_event_defaults_to_one_hour(monkeypatch) -> None:
    calls = []

    def fake_run_osascript(script, args=None):
        calls.append((script, args or []))
        return {"ok": True, "stdout": "event-id", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_run_osascript)

    result = desktop_mod.calendar_create_event("开会", start_at="2026-06-25T15:00")

    assert result["ok"] is True
    assert result["action"] == "calendar.create_event"
    assert result["postcondition_verified"] is True
    assert result["data"] == {
        "title": "开会",
        "start_at": "2026-06-25T15:00",
        "end_at": "2026-06-25T16:00",
        "calendar_name": "",
        "event_id": "event-id",
        "postcondition_verified": True,
    }
    assert "tell application \"Calendar\"" in calls[0][0]
    assert calls[0][1] == [
        "开会",
        "",
        "true",
        "2026",
        "6",
        "25",
        "15",
        "0",
        "true",
        "2026",
        "6",
        "25",
        "16",
        "0",
    ]


def test_native_note_and_schedule_permission_failures_return_automation_targets(monkeypatch) -> None:
    def fake_run_osascript(_script, _args=None):
        return {
            "ok": False,
            "action": "osascript",
            "summary": "osascript failed",
            "permission_error": True,
            "fallback_used": False,
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_run_osascript)

    note = desktop_mod.notes_create("hello")
    reminder = desktop_mod.reminders_create("开会", due_at="2026-06-25T15:00")
    calendar_event = desktop_mod.calendar_create_event("开会", start_at="2026-06-25T15:00")

    assert note["ok"] is False
    assert note["action"] == "notes.create"
    assert note["missing_permissions"] == ["automation"]
    assert note["permission_targets"] == ["automation"]
    assert note["recovery_actions"][0]["permission_target"] == "automation"
    assert reminder["ok"] is False
    assert reminder["action"] == "reminders.create"
    assert reminder["missing_permissions"] == ["automation"]
    assert reminder["permission_targets"] == ["automation"]
    assert reminder["recovery_actions"][0]["permission_target"] == "automation"
    assert calendar_event["ok"] is False
    assert calendar_event["action"] == "calendar.create_event"
    assert calendar_event["missing_permissions"] == ["automation"]
    assert calendar_event["permission_targets"] == ["automation"]
    assert calendar_event["recovery_actions"][0]["permission_target"] == "automation"


def test_desktop_safe_key_uses_whitelisted_system_events_key_code(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="pressed\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_key("arrow_down", repeat_count=3)

    assert result == {
        "ok": True,
        "action": "desktop.safe_key",
        "summary": "Pressed safe foreground key: Down Arrow x3",
        "data": {
            "key_action": "arrow_down",
            "key_label": "Down Arrow",
            "key_code": 125,
            "repeat_count": 3,
            "explicit_user_key": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][0][0:2] == ["osascript", "-e"]
    assert calls[0][0][-2:] == ["125", "3"]


def test_desktop_safe_key_accepts_directional_chinese_aliases(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="pressed\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_key("向下箭头", repeat_count=2)

    assert result["ok"] is True
    assert result["data"]["key_action"] == "arrow_down"
    assert result["data"]["key_label"] == "Down Arrow"
    assert result["data"]["repeat_count"] == 2
    assert calls[0][0][-2:] == ["125", "2"]


def test_desktop_safe_key_uses_shift_modifier_for_shift_tab(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="pressed\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_key("shift_tab")

    assert result["ok"] is True
    assert result["data"]["key_action"] == "shift_tab"
    assert result["data"]["key_label"] == "Shift+Tab"
    assert result["data"]["key_code"] == 48
    assert result["data"]["repeat_count"] == 1
    assert any("using {shift down}" in str(part) for part in calls[0][0])
    assert calls[0][0][-2:] == ["48", "1"]


def test_desktop_hotkey_uses_key_code_for_special_keys(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="hotkey\n", stderr="")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_hotkey("tab", ["command"])

    assert result == {
        "ok": True,
        "action": "desktop.hotkey",
        "summary": "Pressed hotkey command+tab",
        "data": {"key": "tab", "modifiers": ["command"], "key_code": 48},
        "permission_error": False,
        "fallback_used": False,
    }
    assert "key code keyCodeValue using {command down}" in calls[0][0][2]
    assert calls[0][0][-1] == "48"


def test_desktop_click_permission_failure_returns_accessibility_target(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="System Events got an error: not allowed assistive access.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_click(12, 34)

    assert result["ok"] is False
    assert result["action"] == "desktop.click"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["accessibility"]
    assert result["permission_targets"] == ["accessibility"]
    assert result["recovery_hints"] == [
        (
            "Grant Accessibility permission to Oha-Yachiyo or the current terminal "
            "in macOS System Settings > Privacy & Security > Accessibility."
        )
    ]


def test_desktop_safe_scroll_permission_failure_returns_accessibility_target(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="System Events got an error: not allowed assistive access.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_scroll("down")

    assert result["ok"] is False
    assert result["action"] == "desktop.safe_scroll"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["accessibility"]
    assert result["permission_targets"] == ["accessibility"]


def test_desktop_safe_key_permission_failure_returns_accessibility_target(monkeypatch) -> None:
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="System Events got an error: not allowed assistive access.",
        )

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.desktop_safe_key("tab")

    assert result["ok"] is False
    assert result["action"] == "desktop.safe_key"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["accessibility"]
    assert result["permission_targets"] == ["accessibility"]


def test_apple_music_permission_failure_returns_music_and_automation_targets(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": False,
            "action": "osascript",
            "summary": "osascript failed",
            "error": "Not authorized to send Apple events to Music.",
            "permission_error": True,
            "fallback_used": False,
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda _app_name: (_ for _ in ()).throw(
            AssertionError("background-safe playback must not open Music after an error")
        ),
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is False
    assert result["action"] == "media.apple_music_play"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["music_app", "automation"]
    assert result["permission_targets"] == ["music_app", "automation"]
    assert result["recovery_hints"] == [
        (
            "Open Music.app once, confirm the track exists in the local library, "
            "and allow Automation when macOS asks for Music control."
        ),
        (
            "Grant Automation permission so Oha-Yachiyo can control System Events "
            "or the target app in macOS System Settings > Privacy & Security > Automation."
        ),
    ]
    assert result["recovery_actions"] == [
        {
            "label": "打开 Apple Music",
            "tool": "app.open",
            "input": {"app_name": "Music"},
            "permission_target": "music_app",
            "risk_level": "low",
        },
        {
            "label": "打开自动化权限",
            "tool": "system.settings_open",
            "input": {"target": "自动化权限"},
            "permission_target": "automation",
            "risk_level": "low",
        },
    ]
    assert result["fallback_used"] is False


def _verified_music_focus_result() -> dict:
    return {
        "ok": True,
        "action": "app.focus",
        "data": {
            "app_name": "Music",
            "focus_verified": True,
            "focus_strategy": "electron_native_bridge",
            "frontmost_app": "Music",
        },
        "permission_error": False,
        "fallback_used": False,
    }


def _mock_empty_apple_music_catalog(monkeypatch) -> None:
    class EmptyCatalogResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, _limit=-1) -> bytes:
            return b'{"resultCount":0,"results":[]}'

    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda _request, *, timeout=None: EmptyCatalogResponse(),
    )


def test_apple_music_play_uses_background_safe_library_automation(monkeypatch) -> None:
    scripts: list[str] = []

    def fake_osascript(script: str, _args=None) -> dict:
        scripts.append(script)
        return {
            "ok": True,
            "stdout": (
                "played|超时空辉夜姬|花谱|playing|track|"
                "超时空辉夜姬 OST|identity_verified"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("local library hits must remain zero-network")
        ),
        raising=False,
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["track"] == "超时空辉夜姬"
    assert result["data"]["album"] == "超时空辉夜姬 OST"
    assert result["data"]["match_kind"] == "track"
    assert result["data"]["track_identity_verified"] is True
    assert result["data"]["player_state"] == "playing"
    assert result["data"]["playback_started"] is True
    assert result["data"]["background_safe"] is True
    assert result["data"]["foreground_action_taken"] is False
    assert len(scripts) == 1
    assert "activate" not in scripts[0].lower()


def test_apple_music_library_fuzzy_candidates_never_play(monkeypatch) -> None:
    scripts: list[str] = []

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        scripts.append(script)
        return {
            "ok": True,
            "stdout": "no_exact_match|Remember (Piano Cover)||",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    _mock_empty_apple_music_catalog(monkeypatch)

    result = desktop_mod.apple_music_play("Remember")

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["playback_started"] is False
    assert result["data"]["library_match_status"] == "no_exact_match"
    assert len(scripts) == 1
    assert "set trackRef to item 1 of matches" not in scripts[0]
    assert "play trackRef" not in scripts[0]


def test_apple_music_library_ambiguous_exact_track_never_plays(monkeypatch) -> None:
    scripts: list[str] = []

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        scripts.append(script)
        return {
            "ok": True,
            "stdout": "ambiguous_exact_track|Remember|2|",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    _mock_empty_apple_music_catalog(monkeypatch)

    result = desktop_mod.apple_music_play("Remember")

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["playback_started"] is False
    assert result["data"]["library_match_status"] == "ambiguous_exact_track"
    assert "unambiguous exact" in result["summary"]
    assert len(scripts) == 1
    assert "exactTrackCount is 1" in scripts[0]


def test_apple_music_library_unique_exact_track_is_atomic_and_zero_network(
    monkeypatch,
) -> None:
    scripts: list[str] = []

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        scripts.append(script)
        return {
            "ok": True,
            "stdout": (
                "played|Remember|KAF|playing|track|"
                "Cosmic Princess Kaguya!|identity_verified"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exact local track must not use the network")
        ),
    )

    result = desktop_mod.apple_music_play("Remember")

    assert result["ok"] is True
    assert result["data"]["track"] == "Remember"
    assert result["data"]["artist"] == "KAF"
    assert result["data"]["album"] == "Cosmic Princess Kaguya!"
    assert result["data"]["match_kind"] == "track"
    assert result["data"]["track_identity_verified"] is True
    assert result["data"]["player_state"] == "playing"
    assert result["data"]["playback_started"] is True
    assert len(scripts) == 1
    assert scripts[0].index("exactTrackCount is 1") < scripts[0].index(
        "play selectedTrack"
    )


def test_apple_music_library_receipt_cannot_claim_a_nonmatching_track_succeeded(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None, **_kwargs: {
            "ok": True,
            "stdout": (
                "played|Remember (Piano Cover)|Cover Artist|playing|track|"
                "Covers|identity_verified"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.apple_music_play("Remember")

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["playback_state_unverified"] is True
    assert result["data"]["playback_started"] is False


def test_apple_music_library_exact_album_uses_stable_first_track(monkeypatch) -> None:
    scripts: list[str] = []

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        scripts.append(script)
        return {
            "ok": True,
            "stdout": (
                "played|Remember|KAF|playing|album|"
                "Cosmic Princess Kaguya!|identity_verified"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("an exact local album must not use the network")
        ),
    )

    result = desktop_mod.apple_music_play("Cosmic Princess Kaguya!")

    assert result["ok"] is True
    assert result["data"]["match_kind"] == "album"
    assert result["data"]["album"] == "Cosmic Princess Kaguya!"
    assert result["data"]["track"] == "Remember"
    assert result["data"]["playback_started"] is True
    assert len(scripts) == 1
    assert "disc number" in scripts[0]
    assert "track number" in scripts[0]
    assert "database ID" in scripts[0]
    assert scripts[0].index("exactAlbumIdentityCount is not 1") < scripts[0].index(
        "play selectedTrack"
    )


def test_apple_music_library_allows_only_terminal_title_punctuation_variants(
    monkeypatch,
) -> None:
    received_args: list[str] = []

    def fake_osascript(_script: str, args=None, **_kwargs) -> dict:
        received_args.extend(str(value) for value in (args or []))
        if "Cosmic Princess Kaguya!" not in received_args:
            return {"ok": True, "stdout": "not_found|||", "stderr": ""}
        return {
            "ok": True,
            "stdout": (
                "played|Remember|KAF|playing|album|"
                "Cosmic Princess Kaguya!|identity_verified"
            ),
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.apple_music_play("Cosmic Princess Kaguya")

    assert result["ok"] is True
    assert result["data"]["status"] == "played"
    assert result["data"]["match_kind"] == "album"
    assert result["data"]["album"] == "Cosmic Princess Kaguya!"
    assert result["data"]["track_identity_verified"] is True
    assert "Cosmic Princess Kaguya!" in received_args
    assert "Cosmic Princess Kaguya—" not in received_args


def test_apple_music_play_returns_background_partial_when_track_is_not_in_library(
    monkeypatch,
) -> None:
    def unexpected_foreground_helper(*_args, **_kwargs):
        raise AssertionError("background-safe playback must not invoke foreground helpers")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    _mock_empty_apple_music_catalog(monkeypatch)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": "not_found|超时空辉夜姬|",
            "stderr": "",
        },
    )
    monkeypatch.setattr(desktop_mod, "app_open", unexpected_foreground_helper)
    monkeypatch.setattr(desktop_mod, "_open_apple_music_search", unexpected_foreground_helper)
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        unexpected_foreground_helper,
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert result["action"] == "media.apple_music_play"
    assert result["summary"] == (
        "Apple Music local library and official catalog did not contain an exact "
        "match for 超时空辉夜姬; no foreground search was opened."
    )
    assert "error" not in result
    assert result["data"]["query"] == "超时空辉夜姬"
    assert result["data"]["status"] == "not_found"
    assert result["data"]["background_safe"] is True
    assert result["data"]["library_search_completed"] is True
    assert result["data"]["catalog_lookup_completed"] is True
    assert result["data"]["catalog_match_verified"] is False
    assert result["data"]["foreground_action_taken"] is False
    assert result["data"]["target_app"] == "Music"
    assert result["data"]["search_opened"] is False
    assert result["data"]["playback_started"] is False
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["user_action_required"] is False
    assert result["permission_error"] is False
    assert result["fallback_used"] is False


def test_apple_music_play_uses_exact_official_catalog_match_without_taking_foreground(
    monkeypatch,
) -> None:
    scripts: list[str] = []
    snapshot_count = 0
    requested_urls: list[str] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, _limit=-1) -> bytes:
            return json.dumps(
                {
                    "resultCount": 3,
                    "results": [
                        {
                            "kind": "song",
                            "trackId": 101,
                            "trackName": "超时空辉夜姬 (Piano Cover)",
                            "artistName": "Cover Artist",
                            "collectionName": "超时空辉夜姬",
                            "trackNumber": 1,
                            "trackViewUrl": "https://music.apple.com/us/album/cover/1?i=101",
                        },
                        {
                            "kind": "song",
                            "trackId": 102,
                            "trackName": "The Moon",
                            "artistName": "KAF",
                            "collectionName": "超时空辉夜姬",
                            "trackNumber": 2,
                            "trackViewUrl": "https://music.apple.com/us/album/the-moon/2?i=102",
                        },
                        {
                            "kind": "song",
                            "trackId": 103,
                            "trackName": "Ready For The Princess",
                            "artistName": "KAF",
                            "collectionName": "超时空辉夜姬",
                            "trackNumber": 1,
                            "trackViewUrl": "https://music.apple.com/us/album/ready/3?i=103",
                        },
                    ],
                }
            ).encode("utf-8")

    def fake_urlopen(request, *, timeout=None):
        assert timeout is not None and timeout > 0
        requested_urls.append(request.full_url)
        return FakeResponse()

    def fake_osascript(script: str, args=None, **_kwargs) -> dict:
        nonlocal snapshot_count
        scripts.append(script)
        if "search library playlist 1" in script:
            return {"ok": True, "stdout": "not_found|超时空辉夜姬|", "stderr": ""}
        if "open location" in script:
            assert args == ["https://music.apple.com/us/album/ready/3?i=103"]
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "play current track" in script:
            return {"ok": True, "stdout": "played", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            states = {
                1: "status|playing|Old Song|Old Artist",
                2: "status|paused|Ready For The Princess|KAF",
                3: "status|playing|Ready For The Princess|KAF",
            }
            return {"ok": True, "stdout": states[min(snapshot_count, 3)], "stderr": ""}
        raise AssertionError(f"unexpected Music AppleScript: {script}")

    frontmost = iter(["Codex", "Codex"])
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(desktop_mod, "urlopen_with_bundled_ca", fake_urlopen, raising=False)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": next(frontmost, "Codex")},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["catalog_match_verified"] is True
    assert result["data"]["track_identity_verified"] is True
    assert result["data"]["track"] == "Ready For The Princess"
    assert result["data"]["artist"] == "KAF"
    assert result["data"]["player_state"] == "playing"
    assert result["data"]["playback_started"] is True
    assert result["data"]["foreground_action_taken"] is False
    assert result["data"]["frontmost_before"] == "Codex"
    assert result["data"]["frontmost_after"] == "Codex"
    assert len(requested_urls) == 1
    assert "entity=song" in requested_urls[0]
    assert "limit=" in requested_urls[0]
    combined_scripts = "\n".join(scripts).lower()
    assert "activate" not in combined_scripts
    assert "system events" not in combined_scripts


def test_apple_music_catalog_prefers_exact_collection_over_exact_track_name() -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Cosmic Princess Kaguya!",
        [
            {
                "kind": "song",
                "trackId": 201,
                "trackName": "Cosmic Princess Kaguya!",
                "artistName": "Unrelated Artist",
                "collectionName": "Unrelated Album",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/wrong/1?i=201",
            },
            {
                "kind": "song",
                "trackId": 202,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/right/2?i=202",
            },
        ],
    )

    assert match is not None
    assert match["track_id"] == "202"
    assert match["match_kind"] == "collection"


def test_apple_music_catalog_rejects_same_name_from_multiple_collection_ids() -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Cosmic Princess Kaguya!",
        [
            {
                "kind": "song",
                "trackId": 211,
                "collectionId": 9001,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/one/1?i=211",
            },
            {
                "kind": "song",
                "trackId": 212,
                "collectionId": 9002,
                "trackName": "Another Remember",
                "artistName": "Different Artist",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/two/2?i=212",
            },
        ],
    )

    assert match is None


def test_apple_music_catalog_missing_collection_ids_use_conservative_artist_identity() -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Cosmic Princess Kaguya!",
        [
            {
                "kind": "song",
                "trackId": 213,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/one/1?i=213",
            },
            {
                "kind": "song",
                "trackId": 214,
                "trackName": "Another Remember",
                "artistName": "Different Artist",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 2,
                "trackViewUrl": "https://music.apple.com/us/album/two/2?i=214",
            },
        ],
    )

    assert match is None


def test_apple_music_catalog_rejects_ambiguous_exact_track_names() -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Remember",
        [
            {
                "kind": "song",
                "trackId": 301,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/one/1?i=301",
            },
            {
                "kind": "song",
                "trackId": 302,
                "trackName": "Remember",
                "artistName": "Different Artist",
                "collectionName": "Different Album",
                "trackNumber": 4,
                "trackViewUrl": "https://music.apple.com/us/album/two/2?i=302",
            },
        ],
    )

    assert match is None


def test_apple_music_catalog_accepts_a_unique_exact_track_name() -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Remember",
        [
            {
                "kind": "song",
                "trackId": 303,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": "Cosmic Princess Kaguya!",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/one/1?i=303",
            }
        ],
    )

    assert match is not None
    assert match["track_id"] == "303"
    assert match["match_kind"] == "track"


@pytest.mark.parametrize(
    ("query", "catalog_title"),
    [
        ("WALL-E", "WALLE"),
        ("A/B", "AB"),
        ("Foo (Bar)", "FooBar"),
        ("Re:Member", "Remember"),
    ],
)
def test_apple_music_catalog_semantic_punctuation_is_not_discarded(
    query: str,
    catalog_title: str,
) -> None:
    match = desktop_mod._apple_music_catalog_match(
        query,
        [
            {
                "kind": "song",
                "trackId": 304,
                "trackName": catalog_title,
                "artistName": "Example Artist",
                "collectionName": "Example Album",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/example/1?i=304",
            }
        ],
    )

    assert match is None


@pytest.mark.parametrize(
    ("query", "catalog_title"),
    [
        ("What?", "What!"),
        ("Foo!!", "Foo?"),
        ("Foo", "Foo!!"),
    ],
)
def test_apple_music_catalog_terminal_punctuation_tolerance_is_asymmetric(
    query: str,
    catalog_title: str,
) -> None:
    match = desktop_mod._apple_music_catalog_match(
        query,
        [
            {
                "kind": "song",
                "trackId": 306,
                "trackName": catalog_title,
                "artistName": "Example Artist",
                "collectionName": "Example Album",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/example/1?i=306",
            }
        ],
    )

    assert match is None


@pytest.mark.parametrize("terminal_punctuation", ["!", "！", "?", "？"])
def test_apple_music_catalog_tolerates_omitted_terminal_exclamation_or_question_mark(
    terminal_punctuation: str,
) -> None:
    match = desktop_mod._apple_music_catalog_match(
        "Cosmic Princess Kaguya",
        [
            {
                "kind": "song",
                "trackId": 305,
                "trackName": "Remember",
                "artistName": "KAF",
                "collectionName": f"Cosmic Princess Kaguya{terminal_punctuation}",
                "trackNumber": 1,
                "trackViewUrl": "https://music.apple.com/us/album/kaguya/1?i=305",
            }
        ],
    )

    assert match is not None
    assert match["track_id"] == "305"
    assert match["match_kind"] == "collection"


@pytest.mark.parametrize(
    ("query", "reported_track"),
    [
        ("WALL-E", "WALLE"),
        ("A/B", "AB"),
        ("Foo (Bar)", "FooBar"),
        ("Re:Member", "Remember"),
    ],
)
def test_apple_music_library_receipt_preserves_semantic_punctuation(
    monkeypatch,
    query: str,
    reported_track: str,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None, **_kwargs: {
            "ok": True,
            "stdout": (
                f"played|{reported_track}|Example Artist|playing|track|"
                "Example Album|identity_verified"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.apple_music_play(query)

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["playback_started"] is False


@pytest.mark.parametrize(
    ("query", "reported_track"),
    [
        ("What?", "What!"),
        ("Foo!!", "Foo?"),
        ("Foo", "Foo!!"),
    ],
)
def test_apple_music_library_receipt_terminal_punctuation_is_asymmetric(
    monkeypatch,
    query: str,
    reported_track: str,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None, **_kwargs: {
            "ok": True,
            "stdout": (
                f"played|{reported_track}|Example Artist|playing|track|"
                "Example Album|identity_verified"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod.apple_music_play(query)

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["playback_started"] is False


def test_apple_music_local_name_variants_do_not_rewrite_explicit_terminal_punctuation() -> None:
    assert desktop_mod._apple_music_terminal_punctuation_variants("What?") == [
        "What?"
    ]
    assert desktop_mod._apple_music_terminal_punctuation_variants("Foo!!") == [
        "Foo!!"
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://music.apple.com/us/album/x/1?i=103",
        "https://evil.example/us/album/x/1?i=103",
        "https://user@music.apple.com/us/album/x/1?i=103",
        "https://music.apple.com:444/us/album/x/1?i=103",
        "https://music.apple.com/us/album/x/1?i=999",
        "https://music.apple.com/us/album/x/1",
    ],
)
def test_apple_music_catalog_rejects_untrusted_track_urls(url: str) -> None:
    assert desktop_mod._apple_music_catalog_url_is_valid(url, 103) is False


def test_apple_music_catalog_api_failure_stays_an_honest_background_partial(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda script, _args=None, **_kwargs: {
            "ok": True,
            "stdout": "not_found|超时空辉夜姬|",
            "stderr": "",
        }
        if "search library playlist 1" in script
        else (_ for _ in ()).throw(AssertionError("API failure must not drive Music UI")),
    )
    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert "error" not in result
    assert result["data"]["status"] == "not_found"
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["catalog_lookup_completed"] is False
    assert result["data"]["catalog_lookup_status"] == "catalog_lookup_TimeoutError"
    assert result["data"]["foreground_action_taken"] is False
    assert result["data"]["playback_started"] is False
    assert "official catalog lookup did not complete" in result["summary"]
    assert "did not contain an exact match" not in result["summary"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"resultCount": 0, "results": None},
        {"resultCount": 0, "results": {}},
    ],
)
def test_apple_music_catalog_rejects_missing_or_non_list_results(
    monkeypatch,
    payload: dict,
) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, _limit=-1) -> bytes:
            return json.dumps(payload).encode("utf-8")

    monkeypatch.setattr(
        desktop_mod,
        "urlopen_with_bundled_ca",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    match, error = desktop_mod._fetch_apple_music_catalog_match(
        "Cosmic Princess Kaguya!",
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert match is None
    assert error == "catalog_response_invalid"


def test_apple_music_catalog_foreground_switch_never_reports_background_success(
    monkeypatch,
) -> None:
    snapshot_count = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal snapshot_count
        if "open location" in script:
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "play current track" in script:
            return {"ok": True, "stdout": "played", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            states = {
                1: "status|playing|Old Song|Old Artist",
                2: "status|paused|Ready For The Princess|KAF",
                3: "status|playing|Ready For The Princess|KAF",
            }
            return {"ok": True, "stdout": states[min(snapshot_count, 3)], "stderr": ""}
        raise AssertionError("unexpected AppleScript")

    frontmost = iter(["Codex", "Music", "Codex", "Codex", "Codex"])
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": next(frontmost, "Codex")},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["playback_started"] is False
    assert result["data"]["foreground_action_taken"] is True
    assert result["data"]["background_safe"] is False
    assert result["data"]["frontmost_after"] == "Music"
    assert "Music" in result["data"]["frontmost_observations"]
    assert result["data"]["foreground_observation_verified"] is False


def test_apple_music_catalog_missing_initial_foreground_observation_never_dispatches(
    monkeypatch,
) -> None:
    dispatch_calls = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal dispatch_calls
        if "open location" in script:
            dispatch_calls += 1
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "player state" in script:
            return {
                "ok": True,
                "stdout": "status|playing|Ready For The Princess|KAF",
                "stderr": "",
            }
        if "play current track" in script:
            return {"ok": True, "stdout": "played", "stderr": ""}
        raise AssertionError("unexpected AppleScript")

    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": False, "app_name": ""},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["foreground_observation_verified"] is False
    assert result["data"]["catalog_dispatch_verified"] is False
    assert result["data"]["playback_started"] is False
    assert dispatch_calls == 0


def test_apple_music_catalog_switch_to_any_other_app_fails_closed_before_play(
    monkeypatch,
) -> None:
    play_calls = 0
    snapshot_count = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal play_calls, snapshot_count
        if "open location" in script:
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            stdout = (
                "status|playing|Old Song|Old Artist"
                if snapshot_count == 1
                else "status|paused|Ready For The Princess|KAF"
            )
            return {"ok": True, "stdout": stdout, "stderr": ""}
        if "play current track" in script:
            play_calls += 1
            return {"ok": True, "stdout": "played", "stderr": ""}
        raise AssertionError("unexpected AppleScript")

    frontmost = iter(["Codex", "Slack", "Slack", "Slack"])
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": next(frontmost, "Slack")},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["foreground_observation_verified"] is False
    assert result["data"]["foreground_action_taken"] is True
    assert result["data"]["background_safe"] is False
    assert result["data"]["playback_started"] is False
    assert play_calls == 0


def test_apple_music_catalog_identity_match_cannot_bypass_foreground_sampling(
    monkeypatch,
) -> None:
    play_calls = 0
    snapshot_count = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal play_calls, snapshot_count
        if "open location" in script:
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            stdout = (
                "status|playing|Old Song|Old Artist"
                if snapshot_count == 1
                else "status|paused|Ready For The Princess|KAF"
            )
            return {"ok": True, "stdout": stdout, "stderr": ""}
        if "play current track" in script:
            play_calls += 1
            return {"ok": True, "stdout": "played", "stderr": ""}
        raise AssertionError("unexpected AppleScript")

    frontmost = iter(["Codex", "Codex", "Slack"])
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": next(frontmost, "Slack")},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["foreground_observation_verified"] is False
    assert result["data"]["playback_started"] is False
    assert play_calls == 0


@pytest.mark.parametrize(
    ("observed_track", "observed_artist"),
    [
        ("Old Song", "Old Artist"),
        ("Ready For The Princess", "Wrong Artist"),
    ],
)
def test_apple_music_catalog_never_plays_until_track_and_artist_both_match(
    monkeypatch,
    observed_track: str,
    observed_artist: str,
) -> None:
    play_calls = 0
    snapshot_count = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal play_calls, snapshot_count
        if "open location" in script:
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "play current track" in script:
            play_calls += 1
            return {"ok": True, "stdout": "played", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            if snapshot_count == 1:
                stdout = "status|playing|Old Song|Old Artist"
            else:
                stdout = f"status|playing|{observed_track}|{observed_artist}"
            return {"ok": True, "stdout": stdout, "stderr": ""}
        raise AssertionError("unexpected AppleScript")

    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": "Codex"},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["playback_started"] is False
    assert play_calls == 0


def test_apple_music_catalog_rechecks_identity_inside_the_play_script(
    monkeypatch,
) -> None:
    snapshot_count = 0
    guarded_play_scripts: list[str] = []

    def fake_osascript(script: str, args=None, **_kwargs) -> dict:
        nonlocal snapshot_count
        if "open location" in script:
            return {"ok": True, "stdout": "dispatched", "stderr": ""}
        if "player state" in script:
            snapshot_count += 1
            stdout = (
                "status|playing|Old Song|Old Artist"
                if snapshot_count == 1
                else "status|paused|Ready For The Princess|KAF"
            )
            return {"ok": True, "stdout": stdout, "stderr": ""}
        if "play current track" in script:
            guarded_play_scripts.append(script)
            assert args == ["Ready For The Princess", "KAF"]
            assert "expectedTrack" in script
            assert "expectedArtist" in script
            return {
                "ok": True,
                "stdout": "identity_changed|Different Song|Different Artist",
                "stderr": "",
            }
        raise AssertionError("unexpected AppleScript")

    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": "Codex"},
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    result = desktop_mod._apple_music_play_catalog_match(
        "超时空辉夜姬",
        {
            "track_id": "103",
            "track": "Ready For The Princess",
            "artist": "KAF",
            "collection": "超时空辉夜姬",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is True
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["identity_changed_before_play"] is True
    assert result["data"]["observed_track"] == "Different Song"
    assert result["data"]["observed_artist"] == "Different Artist"
    assert result["data"]["playback_started"] is False
    assert len(guarded_play_scripts) == 1


def test_apple_music_catalog_preserves_real_automation_permission_failures(
    monkeypatch,
) -> None:
    dispatch_calls = 0

    def fake_osascript(script: str, _args=None, **_kwargs) -> dict:
        nonlocal dispatch_calls
        if "player state" in script:
            return {
                "ok": False,
                "error": "Not authorized to send Apple events to Music",
                "stderr": "",
            }
        if "open location" in script:
            dispatch_calls += 1
        raise AssertionError("permission failure must stop before catalog dispatch")

    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_appkit_frontmost_app_name",
        lambda: {"ok": True, "app_name": "Codex"},
    )

    result = desktop_mod._apple_music_play_catalog_match(
        "Cho Kaguya Hime",
        {
            "track_id": "103",
            "track": "Cho Kaguya Hime",
            "artist": "KAF",
            "collection": "Cho Kaguya Hime",
            "track_url": "https://music.apple.com/us/album/ready/3?i=103",
        },
        deadline=desktop_mod.time.monotonic() + 30,
    )

    assert result["ok"] is False
    assert result["permission_error"] is True
    assert "automation" in result["missing_permissions"]
    assert dispatch_calls == 0


def test_apple_music_search_open_failure_stays_failed(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: {"ok": False, "data": {"result_marker": False}},
        raising=False,
    )
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "Music could not open URL",
        ),
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["data"]["search_opened"] is False
    assert result["data"]["dispatch_verified"] is False
    assert result["data"]["foreground_verified"] is False


def test_apple_music_search_refocuses_music_after_chatgpt_takes_foreground(
    monkeypatch,
) -> None:
    observed_apps = iter(["ChatGPT", "Music", "Music"])
    focus_calls: list[str] = []
    matching = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": True,
            "search_query_identity_verified": True,
            "fingerprint": "matching-results",
        },
    }

    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": next(observed_apps), "title": ""},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (
            focus_calls.append("Music") or _verified_music_focus_result()
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: matching,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["search_query_verified"] is True
    assert focus_calls == ["Music"]
    assert [
        item["data"]["app_name"]
        for item in result["fallback_result"]["foreground_observations"]
    ] == ["ChatGPT", "Music"]
    assert (
        result["fallback_result"]["final_foreground_observation"]["data"]["app_name"]
        == "Music"
    )


def test_apple_music_search_fails_closed_when_music_refocus_fails(
    monkeypatch,
) -> None:
    observed_apps = iter(["ChatGPT", "Music"])
    evidence_calls: list[str] = []

    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": next(observed_apps), "title": ""},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: {
            "ok": False,
            "action": "app.focus",
            "error": "app_focus_not_verified",
            "data": {
                "app_name": "Music",
                "focus_verified": False,
                "frontmost_app": "ChatGPT",
            },
            "permission_error": False,
        },
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)

    def result_evidence(query: str) -> dict:
        evidence_calls.append(query)
        return {
            "ok": True,
            "data": {
                "result_marker": True,
                "query_match": True,
                "fingerprint": "matching-results",
            },
        }

    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        result_evidence,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["error"] == "app_focus_not_verified"
    assert result["data"]["foreground_verified"] is False
    assert result["data"]["search_query_verified"] is False
    assert evidence_calls == ["超时空辉夜姬"]


def test_apple_music_search_focus_prefers_electron_native_bridge(monkeypatch) -> None:
    native_calls: list[str] = []
    monkeypatch.setattr(
        desktop_mod,
        "_electron_native_bridge_config",
        lambda: ("http://127.0.0.1:50123", "token"),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_activate_apple_music_after_search_dispatch",
        lambda: (_ for _ in ()).throw(
            AssertionError("native bridge must be preferred when available")
        ),
    )

    def native_focus(app_name: str) -> dict:
        native_calls.append(app_name)
        return {
            "ok": True,
            "action": "electron.native.desktop.focus",
            "summary": f"Focused {app_name} via native bridge",
            "data": {
                "app_name": app_name,
                "focus_verified": True,
                "frontmost_app": app_name,
            },
            "permission_error": False,
        }

    monkeypatch.setattr(desktop_mod, "_electron_native_focus_app", native_focus)

    result = desktop_mod._focus_apple_music_after_search_dispatch()

    assert result["ok"] is True
    assert result["data"]["focus_strategy"] == "electron_native_bridge"
    assert native_calls == ["Music"]


def test_packaged_apple_music_search_verifies_focus_through_electron_even_if_already_frontmost(
    monkeypatch,
) -> None:
    focus_calls: list[str] = []
    observed_apps = iter(["Music", "Music"])
    matching = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": True,
            "search_query_identity_verified": True,
            "fingerprint": "matching-results",
        },
    }
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": next(observed_apps), "title": ""},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_electron_native_bridge_config",
        lambda: ("http://127.0.0.1:50123", "token"),
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (
            focus_calls.append("Music")
            or {
                "ok": True,
                "action": "app.focus",
                "data": {
                    "app_name": "Music",
                    "focus_verified": True,
                    "frontmost_app": "Music",
                    "focus_strategy": "electron_native_bridge",
                },
            }
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: matching,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is True
    assert focus_calls == ["Music"]
    assert result["fallback_result"]["focus"]["data"]["focus_strategy"] == (
        "electron_native_bridge"
    )


def test_apple_music_search_fails_closed_when_music_never_reaches_foreground(
    monkeypatch,
) -> None:
    def observed_window() -> dict:
        return {
            "ok": True,
            "action": "desktop.active_window",
            "summary": "Active window: Finder",
            "data": {"app_name": "Finder", "title": ""},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(desktop_mod, "active_window", observed_window)
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: _verified_music_focus_result(),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: {"ok": True, "data": {"result_marker": True, "fingerprint": "old"}},
        raising=False,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["data"]["search_opened"] is False
    assert result["data"]["dispatch_verified"] is True
    assert result["data"]["foreground_verified"] is False
    assert result["data"]["observed_app"] == "Finder"
    assert result["data"]["poll_attempts"] == 8


def test_apple_music_search_fails_when_universal_link_does_not_change_results(
    monkeypatch,
) -> None:
    unchanged = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": False,
            "fingerprint": "same-results",
        },
    }
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "title": "音乐"},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: _verified_music_focus_result(),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: unchanged,
        raising=False,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["data"]["search_opened"] is False
    assert result["data"]["dispatch_verified"] is True
    assert result["data"]["foreground_verified"] is True
    assert result["data"]["search_query_verified"] is False


def test_apple_music_search_rejects_irrelevant_changed_result_fingerprint(
    monkeypatch,
) -> None:
    evidence = iter(
        [
            {
                "ok": True,
                "data": {
                    "result_marker": True,
                    "query_match": False,
                    "fingerprint": "old-irrelevant-results",
                },
            },
            *[
                {
                    "ok": True,
                    "data": {
                        "result_marker": True,
                        "query_match": False,
                        "fingerprint": "new-irrelevant-results",
                    },
                }
                for _ in range(8)
            ],
        ]
    )
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "title": "音乐"},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: _verified_music_focus_result(),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: next(evidence),
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["data"]["search_query_verified"] is False
    assert len(result["fallback_result"]["result_evidence_observations"]) == 8


def test_apple_music_search_accepts_unchanged_matching_result_on_first_poll(
    monkeypatch,
) -> None:
    matching = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": True,
            "search_query_identity_verified": True,
            "fingerprint": "same-matching-results",
        },
    }
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "title": "音乐"},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (_ for _ in ()).throw(
            AssertionError("already-foreground Music must not be refocused")
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: matching,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["search_query_verified"] is True
    assert result["data"]["search_query_identity_verified"] is True
    assert result["data"]["search_result_changed_from_nonmatching_baseline"] is False
    assert result["data"]["result_fingerprint"] == "same-matching-results"
    assert len(result["fallback_result"]["result_evidence_observations"]) == 1


def test_apple_music_search_rejects_changed_results_from_matching_baseline_without_identity(
    monkeypatch,
) -> None:
    evidence = iter(
        [
            {
                "ok": True,
                "data": {
                    "result_marker": True,
                    "query_match": True,
                    "search_query_identity_verified": False,
                    "fingerprint": "matching-baseline",
                },
            },
            *[
                {
                    "ok": True,
                    "data": {
                        "result_marker": True,
                        "query_match": True,
                        "search_query_identity_verified": False,
                        "fingerprint": "changed-but-ambiguous",
                    },
                }
                for _ in range(8)
            ],
        ]
    )
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": "Music", "title": "音乐"},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (_ for _ in ()).throw(
            AssertionError("already-foreground Music must not be refocused")
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: next(evidence),
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["data"]["search_query_verified"] is False
    assert result["data"]["search_query_identity_verified"] is False
    assert result["data"]["search_result_changed_from_nonmatching_baseline"] is False


def test_apple_music_search_yields_when_focus_moves_after_result_evidence(
    monkeypatch,
) -> None:
    observed_apps = iter(["Music", "ChatGPT"])
    matching = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": True,
            "search_query_identity_verified": True,
            "fingerprint": "matching-results",
        },
    }
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(
        desktop_mod,
        "active_window",
        lambda: {
            "ok": True,
            "action": "desktop.active_window",
            "data": {"app_name": next(observed_apps), "title": ""},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (_ for _ in ()).throw(
            AssertionError("user focus changes must not be overridden")
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: matching,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["foreground_verified"] is False
    assert result["data"]["search_query_verified"] is True
    assert result["data"]["focus_changed_after_search"] is True
    assert result["data"]["observed_app"] == "ChatGPT"
    assert "error" not in result
    assert (
        result["fallback_result"]["final_foreground_observation"]["data"]["app_name"]
        == "ChatGPT"
    )


def test_apple_music_search_fails_closed_when_final_foreground_observation_fails(
    monkeypatch,
) -> None:
    observations = iter(
        [
            {
                "ok": True,
                "action": "desktop.active_window",
                "data": {"app_name": "Music", "title": ""},
            },
            {
                "ok": False,
                "error": "accessibility_permission_required",
                "permission_error": True,
                "permission_targets": ["accessibility"],
                "data": {},
            },
        ]
    )
    matching = {
        "ok": True,
        "data": {
            "result_marker": True,
            "query_match": True,
            "search_query_identity_verified": True,
            "fingerprint": "matching-results",
        },
    }
    monkeypatch.setattr(
        desktop_mod.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
    )
    monkeypatch.setattr(desktop_mod, "active_window", lambda: next(observations))
    monkeypatch.setattr(
        desktop_mod,
        "_focus_apple_music_after_search_dispatch",
        lambda: (_ for _ in ()).throw(
            AssertionError("already-foreground Music must not be refocused")
        ),
    )
    monkeypatch.setattr(desktop_mod.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        desktop_mod,
        "_apple_music_search_result_evidence",
        lambda _query: matching,
    )

    result = desktop_mod._open_apple_music_search("超时空辉夜姬")

    assert result["ok"] is False
    assert result["permission_error"] is True
    assert result["data"]["search_opened"] is False
    assert result["data"]["search_query_verified"] is False
    assert result["blocking_condition"] == "foreground_focus_unverified"


def test_apple_music_search_result_evidence_matches_unicode_query(monkeypatch) -> None:
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": (
                "results\n"
                "identity\tsearch_field\t超时空·辉夜姬\n"
                "marker\t最佳结果\n"
                "marker\t超时空辉夜姬\n"
                "marker\t艺人\n"
                "marker\t花谱"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod._apple_music_search_result_evidence("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["result_marker"] is True
    assert result["data"]["query_match"] is True
    assert result["data"]["normalized_query_match"] == "超时空辉夜姬"
    assert result["data"]["search_query_identity_verified"] is True
    assert result["data"]["search_query_identity_source"] == "search_field"
    assert result["data"]["fingerprint"]


def test_apple_music_search_result_evidence_uses_bounded_direct_ax_loops(
    monkeypatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_osascript(script: str, _args=None) -> dict:
        captured["script"] = script
        return {
            "ok": True,
            "stdout": "results\n最佳结果\n超时空辉夜姬",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod._apple_music_search_result_evidence("超时空辉夜姬")

    assert result["ok"] is True
    script = captured["script"]
    assert "every UI element" not in script
    assert script.count("exit repeat") >= 4
    assert "markerCount is greater than or equal to 64" in script
    assert '"AXSearchField"' in script
    assert '"AXTextField"' in script
    assert "toolbarChildIndex is greater than 24" in script
    assert "searchContainerChildIndex is greater than 16" in script


def test_apple_music_search_result_evidence_accepts_exact_ax_text_field_identity(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": (
                "results\n"
                "identity\tsearch_field\t超时空辉夜姬\n"
                "identity_role\tAXTextField\n"
                "identity_description\t搜索\n"
                "marker\t超时空辉夜姬"
            ),
            "stderr": "",
        },
    )

    result = desktop_mod._apple_music_search_result_evidence("超时空·辉夜姬")

    assert result["ok"] is True
    assert result["data"]["search_query_identity_verified"] is True
    assert result["data"]["search_query_identity_role"] == "AXTextField"
    assert result["data"]["search_query_identity_description"] == "搜索"


def test_apple_music_play_does_not_search_after_music_error(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": "error|-1743|Not authorized to send Apple events",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_open_apple_music_search",
        lambda _query: (_ for _ in ()).throw(AssertionError("status=error must not dispatch search")),
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is False
    assert result["permission_error"] is True
    assert "Not authorized" in result["error"]


def test_apple_music_play_not_found_never_dispatches_search_fallback(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    _mock_empty_apple_music_catalog(monkeypatch)
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": True,
            "stdout": "not_found|超时空辉夜姬|",
            "stderr": "",
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "_open_apple_music_search",
        lambda _query: (_ for _ in ()).throw(
            AssertionError("not_found must remain on the background-safe path")
        ),
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert result["data"]["status"] == "not_found"
    assert result["data"]["search_opened"] is False


def test_apple_music_play_deadline_bounds_background_library_search(monkeypatch) -> None:
    now = 0.0
    timed_calls: list[tuple[str, float]] = []

    def monotonic() -> float:
        return now

    def advance(seconds: float) -> None:
        nonlocal now
        now += seconds

    def bounded_osascript(
        _script: str,
        _args=None,
        *,
        timeout_seconds: float = 10,
    ) -> dict:
        timed_calls.append(("library", timeout_seconds))
        advance(timeout_seconds)
        return {
            "ok": True,
            "stdout": "not_found|超时空辉夜姬|",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_APPLE_MUSIC_PLAY_DEADLINE_SECONDS", 8.0, raising=False)
    monkeypatch.setattr(desktop_mod.time, "monotonic", monotonic)
    monkeypatch.setattr(desktop_mod, "_run_osascript", bounded_osascript)
    monkeypatch.setattr(
        desktop_mod,
        "_open_apple_music_search",
        lambda _query: (_ for _ in ()).throw(
            AssertionError("deadline failure must not dispatch foreground fallback")
        ),
    )

    result = desktop_mod.apple_music_play("超时空辉夜姬")

    assert result["ok"] is True
    assert "error" not in result
    assert result["data"]["outcome"] == "partial"
    assert result["data"]["status"] == "playback_unverified"
    assert result["data"]["deadline_exceeded"] is True
    assert result["data"]["search_opened"] is False
    assert result["data"]["playback_state_unverified"] is True
    assert result["data"]["track_identity_verified"] is False
    assert result["data"]["catalog_dispatch_verified"] is False
    assert result["data"]["playback_started"] is False
    trusted_result = {
        **result,
        RUNTIME_EXECUTION_PROVENANCE_KEY: {
            "source": RUNTIME_LOCAL_TOOL_BROKER_PROVENANCE_SOURCE,
            "version": RUNTIME_EXECUTION_PROVENANCE_VERSION,
        },
    }
    outcome = evaluate_main_chat_outcome(
        {},
        [
            {
                "event_type": "agent.tool.call",
                "payload": {
                    "tool": "media.apple_music_play",
                    "result": trusted_result,
                },
            }
        ],
    )
    assert outcome.kind == "completed"
    assert outcome.reason == "partial_background_library_not_found"
    assert now == pytest.approx(8.0)
    assert timed_calls == [("library", 8.0)]


def test_apple_music_control_executes_low_risk_playback_action(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {
            "ok": True,
            "stdout": "controlled|pause|paused|超时空辉夜姬|Yachiyo",
            "stderr": "",
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.apple_music_control("pause")

    assert result == {
        "ok": True,
        "action": "media.apple_music_control",
        "summary": "Apple Music pause executed",
        "data": {
            "control": "pause",
            "player_state": "paused",
            "track": "超时空辉夜姬",
            "artist": "Yachiyo",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][1] == ["pause"]
    assert 'if application "Music" is not running' in calls[0][0]


def test_apple_music_open_and_play_opens_music_then_starts_playback(monkeypatch) -> None:
    calls = []

    def fake_app_open(app_name: str) -> dict[str, Any]:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": "Opened Music",
            "data": {"app_name": app_name},
        }

    def fake_music_control(action: str) -> dict[str, Any]:
        calls.append(("control", action))
        return {
            "ok": True,
            "action": "media.apple_music_control",
            "summary": "Apple Music play executed",
            "data": {
                "control": action,
                "player_state": "playing",
                "track": "超时空辉夜姬",
                "artist": "Yachiyo",
            },
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "app_open", fake_app_open)
    monkeypatch.setattr(desktop_mod, "apple_music_control", fake_music_control)

    result = desktop_mod.apple_music_open_and_play()

    assert result == {
        "ok": True,
        "action": "media.apple_music_open_and_play",
        "summary": "Opened Music and started playback",
        "data": {
            "app_name": "Music",
            "open_ok": True,
            "open_summary": "Opened Music",
            "playback_ok": True,
            "control": "play",
            "player_state": "playing",
            "track": "超时空辉夜姬",
            "artist": "Yachiyo",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls == [("open", "Music"), ("control", "play")]


def test_apple_music_open_and_play_reports_media_key_fallback(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: {
            "ok": True,
            "action": "app.open",
            "summary": "Opened Music",
            "data": {"app_name": app_name},
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "apple_music_control",
        lambda action: {
            "ok": True,
            "action": "media.apple_music_control",
            "summary": "Apple Music play attempted via media key fallback",
            "data": {
                "control": action,
                "player_state": "unknown",
                "track": "",
                "artist": "",
                "fallback": "system_media_key",
                "fallback_control": "toggle",
                "media_key": "Play/Pause",
                "playback_state_unverified": True,
            },
            "permission_error": False,
            "missing_permissions": ["music_app", "automation"],
            "permission_targets": ["music_app", "automation"],
            "recovery_hints": ["Grant Automation permission."],
            "recovery_actions": [
                {
                    "label": "打开自动化权限",
                    "tool": "system.settings_open",
                    "input": {"target": "自动化权限"},
                    "permission_target": "automation",
                    "risk_level": "low",
                }
            ],
            "fallback_used": True,
            "fallback": "system_media_key",
            "fallback_result": {"media_key": {"ok": True}},
        },
    )

    result = desktop_mod.apple_music_open_and_play()

    assert result["ok"] is True
    assert result["summary"] == "Opened Music and attempted playback with media key fallback"
    assert result["data"] == {
        "app_name": "Music",
        "open_ok": True,
        "open_summary": "Opened Music",
        "playback_ok": True,
        "control": "play",
        "player_state": "unknown",
        "track": "",
        "artist": "",
        "fallback": "system_media_key",
        "fallback_control": "toggle",
        "media_key": "Play/Pause",
        "playback_state_unverified": True,
    }
    assert result["permission_error"] is False
    assert result["permission_targets"] == ["music_app", "automation"]
    assert result["recovery_actions"][0]["permission_target"] == "automation"
    assert result["fallback_used"] is True
    assert result["fallback"] == "system_media_key"


def test_music_app_open_and_play_opens_app_then_uses_media_key(monkeypatch) -> None:
    calls = []

    def fake_app_open(app_name: str) -> dict[str, Any]:
        calls.append(("open", app_name))
        return {
            "ok": True,
            "action": "app.open",
            "summary": f"Opened {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_app_focus(app_name: str) -> dict[str, Any]:
        calls.append(("focus", app_name))
        return {
            "ok": True,
            "action": "app.focus",
            "summary": f"Focused {app_name}",
            "data": {"app_name": app_name},
        }

    def fake_osascript(_script, args=None):
        calls.append(("osascript", args))
        return {"ok": True, "stdout": "pressed|toggle", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "app_open", fake_app_open)
    monkeypatch.setattr(desktop_mod, "app_focus", fake_app_focus)
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.music_app_open_and_play("Spotify")

    assert result == {
        "ok": True,
        "action": "media.music_app_open_and_play",
        "summary": "Opened Spotify and attempted playback with media key",
        "data": {
            "app_name": "Spotify",
            "open_ok": True,
            "open_summary": "Opened Spotify",
            "focus_ok": True,
            "focus_summary": "Focused Spotify",
            "playback_ok": True,
            "control": "play",
            "player_state": "unknown",
            "playback_state_unverified": True,
            "media_key": "Play/Pause",
            "fallback_control": "toggle",
        },
        "permission_error": False,
        "fallback_used": True,
        "fallback": "system_media_key",
        "fallback_result": {
            "open": {
                "ok": True,
                "action": "app.open",
                "summary": "Opened Spotify",
                "data": {"app_name": "Spotify"},
            },
            "focus": {
                "ok": True,
                "action": "app.focus",
                "summary": "Focused Spotify",
                "data": {"app_name": "Spotify"},
            },
            "media_key": {
                "ok": True,
                "action": "media.music_app_open_and_play",
                "summary": "Pressed Play/Pause media key",
                "data": {
                    "requested_control": "play",
                    "media_control": "toggle",
                    "media_key": "Play/Pause",
                    "key_code": 100,
                },
                "permission_error": False,
                "fallback_used": False,
            },
        },
    }
    assert calls == [("open", "Spotify"), ("focus", "Spotify"), ("osascript", ["100", "toggle"])]


def test_system_volume_executes_low_risk_volume_action(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        if not args:
            return {"ok": True, "stdout": "40|false", "stderr": ""}
        return {"ok": True, "stdout": "50|false", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.system_volume("up")

    assert result == {
        "ok": True,
        "action": "system.volume",
        "summary": "System volume increased from 40% to 50%",
        "data": {
            "requested_action": "up",
            "old_level": 40,
            "old_muted": False,
            "level": 50,
            "muted": False,
            "changed": True,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][1] is None
    assert calls[1][1] == ["50", "false"]


def test_system_brightness_executes_low_risk_brightness_action(monkeypatch) -> None:
    calls = []

    def fake_osascript(script, args=None):
        calls.append((script, args))
        return {"ok": True, "stdout": "adjusted", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    result = desktop_mod.system_brightness("up", step=3)

    assert result == {
        "ok": True,
        "action": "system.brightness",
        "summary": "Display brightness increased",
        "data": {
            "requested_action": "up",
            "step": 3,
            "key_code": 145,
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls[0][1] == ["145", "3"]


def test_system_display_sleep_executes_low_risk_display_sleep(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.system_display_sleep()

    assert result == {
        "ok": True,
        "action": "system.display_sleep",
        "summary": "Display sleep requested",
        "data": {
            "requested_action": "sleep",
            "command": "pmset displaysleepnow",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls == [
        (
            ["pmset", "displaysleepnow"],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_system_screen_saver_start_executes_low_risk_screen_saver_start(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.system_screen_saver_start()

    target = "/System/Library/CoreServices/ScreenSaverEngine.app"
    assert result == {
        "ok": True,
        "action": "system.screen_saver_start",
        "summary": "Screen saver start requested",
        "data": {
            "requested_action": "start",
            "target": target,
            "command": f"open {target}",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls == [
        (
            ["open", target],
            {
                "capture_output": True,
                "text": True,
                "timeout": 5,
                "check": False,
            },
        )
    ]


def test_clipboard_write_uses_system_clipboard_without_echoing_text(monkeypatch) -> None:
    calls = []

    def fake_run(command, *, input=None, text=None, capture_output=None, timeout=None, check=None):
        calls.append(
            {
                "command": command,
                "input": input,
                "text": text,
                "capture_output": capture_output,
                "timeout": timeout,
                "check": check,
            }
        )
        stdout = "hello world" if command == ["pbpaste"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.clipboard_write("hello world")

    assert result == {
        "ok": True,
        "action": "clipboard.write",
        "summary": "Copied 11 characters to clipboard",
        "data": {
            "text_length": 11,
            "platform": "macos",
            "postcondition_verified": True,
        },
        "postcondition_verified": True,
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls == [
        {
            "command": ["pbcopy"],
            "input": "hello world",
            "text": True,
            "capture_output": True,
            "timeout": 3,
            "check": False,
        },
        {
            "command": ["pbpaste"],
            "input": None,
            "text": True,
            "capture_output": True,
            "timeout": 3,
            "check": False,
        },
    ]
    assert "hello world" not in result["summary"]
    assert "hello world" not in str(result["data"])


def test_clipboard_write_fails_closed_when_exact_readback_does_not_match(
    monkeypatch,
) -> None:
    def fake_run(
        command,
        *,
        input=None,
        text=None,
        capture_output=None,
        timeout=None,
        check=None,
    ):
        stdout = "changed by another process" if command == ["pbpaste"] else ""
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.clipboard_write("hello world")

    assert result["ok"] is False
    assert result["verification_failed"] is True
    assert result["error"] == "clipboard_write_readback_unverified"
    assert result["data"]["postcondition_verified"] is False
    assert result.get("postcondition_verified") is not True
    assert "hello world" not in repr(result)
    assert "changed by another process" not in repr(result)


def test_clipboard_read_uses_system_clipboard_with_bounded_preview(monkeypatch) -> None:
    calls = []

    def fake_run(command, *, capture_output=None, text=None, timeout=None, check=None):
        calls.append(
            {
                "command": command,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "check": check,
            }
        )
        return subprocess.CompletedProcess(command, 0, "hello world", "")

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    # Keep the established pbpaste fallback contract when native observation
    # is unavailable; stable native revision/text are covered separately.
    monkeypatch.setattr(desktop_mod, "_run_jxa", lambda *_a, **_kw: {"ok": False})
    monkeypatch.setattr(desktop_mod.subprocess, "run", fake_run)

    result = desktop_mod.clipboard_read(max_chars=5)

    assert result == {
        "ok": True,
        "action": "clipboard.read",
        "summary": "Read 11 characters from clipboard",
        "data": {
            "text": "hello",
            "text_length": 11,
            "truncated": True,
            "max_chars": 5,
            "platform": "macos",
        },
        "permission_error": False,
        "fallback_used": False,
    }
    assert calls == [
        {
            "command": ["pbpaste"],
            "capture_output": True,
            "text": True,
            "timeout": 5,
            "check": False,
        }
    ]


def test_clipboard_read_rejects_invalid_preview_limit(monkeypatch) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")

    result = desktop_mod.clipboard_read(max_chars=True)

    assert result["ok"] is False
    assert result["action"] == "clipboard.read"
    assert "max_chars must be an integer" in result["error"]


def test_apple_music_control_permission_failure_returns_music_and_automation_targets(
    monkeypatch,
) -> None:
    open_calls: list[str] = []

    def fake_app_open(app_name: str) -> dict[str, Any]:
        open_calls.append(app_name)
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "_run_osascript",
        lambda _script, _args=None: {
            "ok": False,
            "action": "osascript",
            "summary": "osascript failed",
            "error": "Not authorized to send Apple events to Music.",
            "permission_error": True,
            "fallback_used": False,
        },
    )
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        fake_app_open,
    )

    result = desktop_mod.apple_music_control("next")

    assert result["ok"] is False
    assert result["action"] == "media.apple_music_control"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["music_app", "automation"]
    assert result["permission_targets"] == ["music_app", "automation"]
    assert result["fallback_used"] is False
    assert open_calls == []


def test_apple_music_control_never_uses_foreground_fallback_when_automation_fails(
    monkeypatch,
) -> None:
    calls = []
    open_calls: list[str] = []

    def fake_osascript(_script, args=None):
        calls.append(args)
        if len(calls) == 1:
            return {
                "ok": True,
                "stdout": "error|-1743|Not authorized to send Apple events to Music.",
                "stderr": "",
            }
        return {"ok": True, "stdout": "pressed|toggle", "stderr": ""}

    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop_mod, "_run_osascript", fake_osascript)

    def fake_app_open(app_name: str) -> dict[str, Any]:
        open_calls.append(app_name)
        return {"ok": True, "action": "app.open", "data": {"app_name": app_name}}

    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        fake_app_open,
    )

    result = desktop_mod.apple_music_control("play")

    assert result["ok"] is False
    assert result["action"] == "media.apple_music_control"
    assert result["data"] == {"control": "play", "status": "error"}
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["music_app", "automation"]
    assert result["permission_targets"] == ["music_app", "automation"]
    assert result["fallback_used"] is False
    assert open_calls == []
    assert calls == [["play"]]


def test_apple_music_open_and_play_permission_failure_returns_music_and_automation_targets(
    monkeypatch,
) -> None:
    monkeypatch.setattr(desktop_mod, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(
        desktop_mod,
        "app_open",
        lambda app_name: {"ok": True, "action": "app.open", "data": {"app_name": app_name}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "apple_music_control",
        lambda action: {
            "ok": False,
            "action": "media.apple_music_control",
            "summary": "media.apple_music_control failed",
            "error": "Not authorized to send Apple events to Music.",
            "data": {"control": action},
            "permission_error": True,
            "fallback_used": True,
        },
    )

    result = desktop_mod.apple_music_open_and_play()

    assert result["ok"] is False
    assert result["action"] == "media.apple_music_open_and_play"
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["music_app", "automation"]
    assert result["permission_targets"] == ["music_app", "automation"]
    assert result["fallback_used"] is True
