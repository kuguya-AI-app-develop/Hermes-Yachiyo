"""Native copy observations retain text and reject fabricated identity/revisions."""

import json
import subprocess

import pytest

from apps.shell.agent.tools import desktop


def _output(raw_value, **changes):
    focused = {
        "app_name": "TextEdit", "pid": 123, "window_id": 456,
        "role": "AXTextArea", "focused": True, "value": raw_value, **changes,
    }
    return "META\tTextEdit\t123\tDocument\t456\nFOCUSED\t" + json.dumps(focused)


@pytest.mark.parametrize("value", ["", " 前导\t汉字🤖\n第二行\r\n尾巴 ", '引号"\\\u0000\u001f'])
def test_ui_elements_preserves_the_original_focused_text_inside_json(monkeypatch, value):
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    scripts = []
    def observe(script, args):
        scripts.append(script)
        return {"ok": True, "stdout": _output(value)}
    monkeypatch.setattr(desktop, "_run_osascript", observe)
    result = desktop.ui_elements(limit=1)
    assert result["ok"] is True
    data = result["data"]
    assert data["pid"] == 123 and data["window_id"] == 456
    assert data["focused_element"] == {
        "role": "AXTextArea", "value": value, "focused": True, "editable": True,
    }
    assert "NSJSONSerialization" in scripts[0]
    assert 'attribute "AXFocusedUIElement"' in scripts[0]
    assert 'attribute "AXWindow"' in scripts[0]
    assert "postcondition_verified" not in str(result)


@pytest.mark.parametrize("changes", [
    {"app_name": "Another App"}, {"pid": 999}, {"window_id": 998},
    {"pid": True}, {"window_id": False}, {"pid": "123"}, {"window_id": "456"},
    {"role": "AXButton"}, {"role": "AXSecureTextField"}, {"focused": False},
    {"focused": "true"}, {"value": None}, {"value": {"text": "secret"}},
])
def test_focused_element_requires_exact_observed_target_identity_and_text(changes):
    result = desktop._parse_ui_elements_output(_output("raw", **changes))
    assert "focused_element" not in result


@pytest.mark.parametrize("suffix", [
    "\nFOCUSED\t{}", "\nFOCUSED\t{\"value\":\"extra\"}",
    "\nFOCUSED\tmalformed", "\nFOCUSED\t[]",
    "\nFOCUSED\t" + json.dumps({
        "app_name": "TextEdit", "pid": 123, "window_id": 456,
        "role": "AXTextArea", "focused": True, "value": "other",
    }),
])
def test_duplicate_focused_records_are_not_context_authority(suffix):
    assert "focused_element" not in desktop._parse_ui_elements_output(_output("raw") + suffix)


@pytest.mark.parametrize("header", ["META\tTextEdit\t0\tDoc\t456", "META\tTextEdit\t123\tDoc\t0"])
def test_focused_element_is_omitted_without_positive_header_identity(header):
    raw = _output("raw")
    data = desktop._parse_ui_elements_output(header + "\n" + raw.split("\n")[1])
    assert "focused_element" not in data


@pytest.mark.parametrize("revision", [0, 12, 2147483647])
def test_mac_clipboard_records_only_a_stable_real_change_count(monkeypatch, revision):
    value = "  第一行\t汉字🚀\r\n尾巴  "
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    scripts = []
    def native(script, args=None):
        scripts.append(script)
        return {"ok": True, "stdout": json.dumps({
            "text": value, "revision_before": revision, "revision_after": revision,
        })}
    monkeypatch.setattr(desktop, "_run_jxa", native)
    monkeypatch.setattr(desktop.subprocess, "run", lambda *_a, **_kw: pytest.fail("No fallback"))
    result = desktop.clipboard_read(max_chars=12000)
    assert result["data"] == {
        "text": value, "text_length": len(value), "truncated": False,
        "max_chars": 12000, "platform": "macos",
        "pasteboard_revision": revision, "pasteboard_revision_stable": True,
    }
    assert scripts[0].count("pasteboard.changeCount") == 2
    assert "postcondition_verified" not in result and "postcondition_verified" not in result["data"]


@pytest.mark.parametrize("before,after", [(10, 11), (11, 10), (0, 1)])
def test_clipboard_change_during_read_is_not_a_stable_receipt(monkeypatch, before, after):
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_run_jxa", lambda *_a, **_kw: {"ok": True, "stdout": json.dumps({
        "text": "observed", "revision_before": before, "revision_after": after,
    })})
    result = desktop.clipboard_read()
    assert result["data"]["text"] == "observed"
    assert result["data"]["pasteboard_revision_stable"] is False
    assert "pasteboard_revision" not in result["data"]


@pytest.mark.parametrize("revision", [True, False, -1, "12", None, 1.5])
def test_bad_or_missing_native_revision_is_not_trusted(monkeypatch, revision):
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_run_jxa", lambda *_a, **_kw: {"ok": True, "stdout": json.dumps({
        "text": "observed", "revision_before": revision, "revision_after": revision,
    })})
    data = desktop.clipboard_read()["data"]
    assert data["text"] == "observed"
    assert "pasteboard_revision" not in data and "pasteboard_revision_stable" not in data


@pytest.mark.parametrize("native", [
    {"ok": False, "error": "Unavailable"}, {"ok": True, "stdout": "malformed"},
    {"ok": True, "stdout": '{"text":null}'}, {"ok": True, "stdout": "[]"},
])
def test_native_clipboard_unavailable_uses_existing_fallback_without_revision(monkeypatch, native):
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "macos")
    monkeypatch.setattr(desktop, "_run_jxa", lambda *_a, **_kw: native)
    monkeypatch.setattr(desktop.subprocess, "run", lambda command, **_kw:
        subprocess.CompletedProcess(command, 0, " fallback\n", ""))
    result = desktop.clipboard_read(max_chars=5)
    assert result["data"]["text"] == " fall"
    assert result["data"]["text_length"] == 10
    assert result["data"]["truncated"] is True
    assert "pasteboard_revision" not in result["data"]


def test_non_mac_clipboard_has_no_mac_revision_or_native_probe(monkeypatch):
    monkeypatch.setattr(desktop, "_desktop_platform", lambda: "linux")
    monkeypatch.setattr(desktop, "_clipboard_read_command", lambda: ["wl-paste", "--no-newline"])
    monkeypatch.setattr(desktop, "_run_jxa", lambda *_a, **_kw: pytest.fail("mac probe on Linux"))
    monkeypatch.setattr(desktop.subprocess, "run", lambda command, **_kw:
        subprocess.CompletedProcess(command, 0, "raw\n", ""))
    data = desktop.clipboard_read()["data"]
    assert data["text"] == "raw\n"
    assert "pasteboard_revision" not in data


def test_focused_receipt_ambiguity_is_checked_after_the_display_limit():
    raw = _output("raw") + "\n0\tAXButton\t\tConfirm\t\t\ttrue\t0\t0\t10\t10"
    raw += "\nFOCUSED\tmalformed"
    data = desktop._parse_ui_elements_output(raw, limit=1)
    assert data["count"] == 1
    assert "focused_element" not in data


@pytest.mark.parametrize("identity_key", ["identifier", "name", "description"])
def test_focused_identity_is_only_an_actual_optional_string(identity_key):
    observed = "Message 输入框\t真实名称"
    data = desktop._parse_ui_elements_output(_output("raw", **{identity_key: observed}))
    assert data["focused_element"][identity_key] == observed
    for invalid in (None, "", True, 123, {"name": "Message"}):
        data = desktop._parse_ui_elements_output(_output("raw", **{identity_key: invalid}))
        assert identity_key not in data["focused_element"]
