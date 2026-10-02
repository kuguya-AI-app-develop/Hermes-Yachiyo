from __future__ import annotations

import json

from scripts import smoke_desktop_planner_discovery as smoke


def test_desktop_planner_discovery_smoke_covers_discover_operate_verify():
    evidence = smoke.run_smoke()

    assert evidence["ok"] is True
    cases = {case["id"]: case for case in evidence["cases"]}
    assert set(cases) == {
        "generic_app_open",
        "generic_app_read_buttons",
        "generic_unknown_app_sequenced_read_buttons",
        "capability_app_discovery_open",
        "app_scoped_click",
        "app_scoped_type",
        "app_scoped_hotkey",
        "app_scoped_safe_shortcut",
        "app_scoped_safe_shortcut_with_inspect",
        "app_window_focus",
    }
    assert [request["tool"] for request in cases["generic_app_open"]["requests"]] == [
        "desktop.list_apps",
        "app.open",
        "desktop.verify",
    ]
    assert [request["tool"] for request in cases["generic_app_read_buttons"]["requests"]] == [
        "desktop.inspect_app",
    ]
    assert [
        request["tool"]
        for request in cases["generic_unknown_app_sequenced_read_buttons"]["requests"]
    ] == [
        "desktop.list_apps",
        "app.open",
        "desktop.ui_elements",
    ]
    assert cases["generic_unknown_app_sequenced_read_buttons"]["intent_inputs"][
        "app_name_hint"
    ] == "PixelForge"
    assert [request["tool"] for request in cases["capability_app_discovery_open"]["requests"]] == [
        "desktop.list_apps",
        "app.open",
        "desktop.verify",
    ]
    assert not cases["capability_app_discovery_open"]["requests"][0].get(
        "continue_to_model", False
    )
    assert cases["capability_app_discovery_open"]["requests"][1]["input"] == {
        "app_name": "<selected app from desktop.list_apps>",
        "selection_source": "desktop.list_apps",
        "query": "pdf",
    }
    assert cases["capability_app_discovery_open"]["requests"][2]["input"] == {
        "app_name": "<selected app from desktop.list_apps>",
        "selection_source": "desktop.list_apps",
        "query": "pdf",
        "verification_goal": "app_running",
    }
    assert [step["id"] for step in cases["capability_app_discovery_open"]["steps"]] == [
        "discover_apps-desktop-state",
        "open-selected-discovered-app",
        "verify-desktop-result",
    ]
    assert cases["capability_app_discovery_open"]["steps"][1]["input"] == {
        "app_name": "<selected app from desktop.list_apps>",
        "selection_source": "desktop.list_apps",
        "query": "pdf",
    }
    assert cases["app_scoped_click"]["requests"][0]["tool"] == "desktop.inspect_app"
    assert cases["app_scoped_click"]["requests"][1]["tool"] == (
        "app.focus_and_click_ui_element"
    )
    assert cases["app_scoped_type"]["requests"][0]["tool"] == "desktop.inspect_app"
    assert cases["app_scoped_type"]["requests"][1]["tool"] == (
        "app.focus_and_type_into_ui_element"
    )
    assert [request["tool"] for request in cases["app_scoped_hotkey"]["requests"]] == [
        "desktop.list_apps",
        "app.focus",
        "desktop.hotkey",
    ]
    assert [request["tool"] for request in cases["app_scoped_safe_shortcut"]["requests"]] == [
        "desktop.list_apps",
        "app.focus_and_safe_shortcut",
        "desktop.ui_elements",
    ]
    assert cases["app_scoped_safe_shortcut"]["requests"][1]["input"] == {
        "app_name": "Safari",
        "action": "new_tab",
    }
    assert [
        request["tool"]
        for request in cases["app_scoped_safe_shortcut_with_inspect"]["requests"]
    ] == [
        "desktop.list_apps",
        "app.focus_and_safe_shortcut",
        "desktop.ui_elements",
    ]
    assert cases["app_scoped_safe_shortcut_with_inspect"]["requests"][0]["input"] == {
        "query": "PixelForge",
        "limit": 20,
    }
    assert cases["app_scoped_safe_shortcut_with_inspect"]["requests"][1]["input"] == {
        "app_name": "PixelForge",
        "action": "new_tab",
    }
    assert [request["tool"] for request in cases["app_window_focus"]["requests"]] == [
        "desktop.list_apps",
        "desktop.windows",
        "app.focus_window",
        "desktop.verify",
    ]
    assert all(case["checks"]["uses_no_browser_tool"] for case in cases.values())


def test_desktop_planner_discovery_smoke_cli_outputs_json(capsys):
    assert smoke.main([]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["mode"] == "desktop_planner_discovery_smoke"
    assert output["case_count"] == len(smoke.DESKTOP_PLANNER_CASES)


def test_desktop_planner_discovery_smoke_cli_writes_report_json(tmp_path, capsys):
    report_path = tmp_path / "desktop-planner-discovery.json"

    assert smoke.main(["--report-json", str(report_path)]) == 0

    captured = capsys.readouterr()
    output = json.loads(captured.out)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert output == report
    assert report["ok"] is True
    assert report["mode"] == "desktop_planner_discovery_smoke"
    assert "desktop planner discovery smoke report:" in captured.err
    assert str(report_path) in captured.err
