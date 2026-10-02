"""Browser/CDP structured tool tests."""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
from pathlib import Path
from urllib.error import HTTPError

import pytest

from apps.shell.agent.tools import browser as browser_mod
from apps.shell.agent.tools import desktop as desktop_mod
from apps.shell.agent.tools.broker import ToolBroker
from apps.shell.agent.tools.foreground_lock import ForegroundActionLock


def _broker(tmp_path):
    return ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": ["."]},
        tmp_path / "artifacts",
    )


def test_local_browser_cdp_config_requires_agent_owned_process_attestation(
    tmp_path,
    monkeypatch,
) -> None:
    from apps.shell import config as shell_config

    monkeypatch.delenv("YACHIYO_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(shell_config, "_CONFIG_DIR", tmp_path)
    (tmp_path / "native_tool_config.json").write_text(
        json.dumps({"config": {"browser.cdp_url": "http://127.0.0.1:9222"}}),
        encoding="utf-8",
    )

    assert browser_mod._configured_browser_cdp_url() == ""


def test_local_browser_cdp_config_accepts_only_matching_oha_process(
    tmp_path,
    monkeypatch,
) -> None:
    from apps.shell import config as shell_config

    monkeypatch.delenv("YACHIYO_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("BROWSER_CDP_URL", raising=False)
    monkeypatch.setattr(shell_config, "_CONFIG_DIR", tmp_path)
    profile_dir = Path.home() / ".oha-yachiyo" / "chrome-debug"
    (tmp_path / "native_tool_config.json").write_text(
        json.dumps(
            {
                "config": {
                    "browser.cdp_url": "http://127.0.0.1:43123",
                    "browser.cdp_owner": "oha-yachiyo",
                    "browser.cdp_pid": 43210,
                    "browser.cdp_profile_dir": str(profile_dir),
                }
            }
        ),
        encoding="utf-8",
    )
    checks = []
    monkeypatch.setattr(
        browser_mod,
        "_browser_cdp_process_matches",
        lambda url, **kwargs: checks.append((url, kwargs)) or True,
    )

    assert browser_mod._configured_browser_cdp_url() == "http://127.0.0.1:43123"
    assert checks == [
        (
            "http://127.0.0.1:43123",
            {"pid": 43210, "profile_dir": str(profile_dir)},
        )
    ]


def test_browser_cdp_process_attestation_binds_chrome_pid_to_loopback_listener(
    tmp_path,
) -> None:
    home_dir = tmp_path / "home"
    profile_dir = home_dir / ".oha-yachiyo" / "chrome-debug"
    executable = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("test chrome", encoding="utf-8")
    pid = 43210
    port = 43123
    process_command = shlex.join(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
        ]
    )

    def run(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, stdout=process_command, stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"p{pid}\nn127.0.0.1:{port}\n",
            stderr="",
        )

    assert browser_mod._browser_cdp_process_matches(
        f"http://127.0.0.1:{port}",
        pid=pid,
        profile_dir=str(profile_dir),
        run=run,
        which=lambda name: "/usr/sbin/lsof" if name == "lsof" else None,
        home_dir=home_dir,
    )


def test_browser_cdp_process_attestation_rejects_listener_owned_by_other_pid(
    tmp_path,
) -> None:
    home_dir = tmp_path / "home"
    profile_dir = home_dir / ".oha-yachiyo" / "chrome-debug"
    executable = (
        tmp_path
        / "Applications"
        / "Google Chrome.app"
        / "Contents"
        / "MacOS"
        / "Google Chrome"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("test chrome", encoding="utf-8")
    pid = 43210
    port = 43123
    process_command = shlex.join(
        [
            str(executable),
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
        ]
    )

    def run(command, **_kwargs):
        if command[0] == "ps":
            return subprocess.CompletedProcess(command, 0, stdout=process_command, stderr="")
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=f"p99999\nn127.0.0.1:{port}\n",
            stderr="",
        )

    assert not browser_mod._browser_cdp_process_matches(
        f"http://127.0.0.1:{port}",
        pid=pid,
        profile_dir=str(profile_dir),
        run=run,
        which=lambda name: "/usr/sbin/lsof" if name == "lsof" else None,
        home_dir=home_dir,
    )


def test_browser_open_url_uses_configured_cdp(monkeypatch) -> None:
    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "http://127.0.0.1:9222")
    monkeypatch.setattr(
        browser_mod,
        "_cdp_new_page",
        lambda cdp_url, url: {
            "id": "target-1",
            "type": "page",
            "title": "Example",
            "url": url,
            "cdp_url": cdp_url,
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/target-1",
        },
    )

    result = browser_mod.open_url("https://example.com/demo")

    assert result["ok"] is True
    assert result["fallback_used"] is False
    assert result["data"]["target_id"] == "target-1"
    assert result["data"]["target_websocket_available"] is True
    assert result["data"]["url"] == "https://example.com/demo"


def test_browser_open_url_can_use_explicit_system_browser_fallback(monkeypatch) -> None:
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "")
    monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
    monkeypatch.setattr(browser_mod.subprocess, "run", fake_run)

    result = browser_mod.open_url(
        "https://example.com/fallback",
        allow_system_browser_fallback=True,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "system_browser"
    assert calls[0][0] == ["open", "https://example.com/fallback"]


def test_browser_open_url_never_uses_system_browser_implicitly(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "")
    monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        browser_mod.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = browser_mod.open_url("https://example.com/no-takeover")

    assert result["ok"] is False
    assert result["error"] == "chrome_cdp_unavailable"
    assert result["fallback_used"] is False
    assert calls == []


def test_browser_close_target_revalidates_exact_target_before_closing(monkeypatch) -> None:
    pages = [
        {
            "id": "user-target",
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/user",
        },
        {
            "id": "run-target",
            "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/run",
        },
    ]
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        browser_mod,
        "_configured_browser_cdp_url",
        lambda: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(browser_mod, "_cdp_list_pages", lambda _url: pages)
    monkeypatch.setattr(
        browser_mod,
        "_cdp_close_page",
        lambda cdp_url, target_id: closed.append((cdp_url, target_id)),
    )

    result = browser_mod.close_target("run-target")

    assert result["ok"] is True
    assert result["data"]["target_id"] == "run-target"
    assert closed == [("http://127.0.0.1:9222", "run-target")]


def test_browser_close_target_never_closes_a_different_page(monkeypatch) -> None:
    closed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        browser_mod,
        "_configured_browser_cdp_url",
        lambda: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        browser_mod,
        "_cdp_list_pages",
        lambda _url: [
            {
                "id": "user-target",
                "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/page/user",
            }
        ],
    )
    monkeypatch.setattr(
        browser_mod,
        "_cdp_close_page",
        lambda cdp_url, target_id: closed.append((cdp_url, target_id)),
    )

    result = browser_mod.close_target("missing-run-target")

    assert result["ok"] is True
    assert result["data"]["already_closed"] is True
    assert closed == []


def test_cdp_new_page_does_not_retry_after_timeout(monkeypatch) -> None:
    calls: list[str] = []

    def fake_http_json(_url: str, *, method: str = "GET") -> object:
        calls.append(method)
        raise TimeoutError("response timed out after target creation")

    monkeypatch.setattr(browser_mod, "_http_json", fake_http_json)

    with pytest.raises(TimeoutError):
        browser_mod._cdp_new_page("http://127.0.0.1:9222", "https://example.com")

    assert calls == ["PUT"]


def test_cdp_new_page_uses_get_only_when_put_is_explicitly_unsupported(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_http_json(url: str, *, method: str = "GET") -> object:
        calls.append(method)
        if method == "PUT":
            raise HTTPError(url, 405, "method not allowed", hdrs=None, fp=None)
        return {"id": "target-legacy"}

    monkeypatch.setattr(browser_mod, "_http_json", fake_http_json)

    result = browser_mod._cdp_new_page(
        "http://127.0.0.1:9222",
        "https://example.com",
    )

    assert result == {"id": "target-legacy"}
    assert calls == ["PUT", "GET"]


@pytest.mark.parametrize(
    ("tool_name", "payload"),
    [
        ("browser.open_url", {"url": "https://example.com"}),
        ("browser.open", {"url": "https://example.com"}),
        ("browser.search", {"query": "yachiyo"}),
        (
            "browser.open_url_and_extract_text",
            {"url": "https://example.com", "selector": "main"},
        ),
        (
            "browser.open_url_and_screenshot",
            {"url": "https://example.com", "reason": "test"},
        ),
    ],
)
def test_browser_registry_open_tools_never_activate_system_browser_implicitly(
    tmp_path,
    monkeypatch,
    tool_name: str,
    payload: dict[str, str],
) -> None:
    broker = _broker(tmp_path)
    calls = []

    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "")
    monkeypatch.setattr(browser_mod.sys, "platform", "darwin")
    monkeypatch.setattr(
        browser_mod.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = broker.call(tool_name, payload)

    assert result["ok"] is False
    assert calls == []


def test_browser_cdp_unavailable_returns_recovery_target(monkeypatch) -> None:
    monkeypatch.setattr(browser_mod, "_configured_browser_cdp_url", lambda: "")
    monkeypatch.setattr(browser_mod.sys, "platform", "linux")

    result = browser_mod.open_url("https://example.com/no-cdp")

    assert result["ok"] is False
    assert result["permission_error"] is True
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert "Chrome DevTools/CDP" in result["recovery_hints"][0]


def test_browser_broker_rejects_open_result_without_owned_target(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        browser_mod,
        "open_url",
        lambda url: {
            "ok": True,
            "action": "browser.open_url",
            "data": {"url": url},
            "fallback_used": False,
        },
    )

    result = broker.call(
        "browser.open_url",
        {"url": "https://example.com/unowned"},
    )

    assert result["ok"] is False
    assert result["error"] == "browser_owned_target_unverified"
    assert result["user_handoff_required"] is True
    assert result["replan_allowed"] is False
    assert broker._owned_browser_target_id == ""


@pytest.mark.parametrize("target_id", ["", "target with spaces", "../target", "x" * 129])
def test_browser_broker_rejects_invalid_owned_target_id(
    tmp_path,
    monkeypatch,
    target_id: str,
) -> None:
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        browser_mod,
        "open_url",
        lambda url: {
            "ok": True,
            "action": "browser.open_url",
            "data": {
                "url": url,
                "target_id": target_id,
                "target_websocket_available": True,
            },
            "fallback_used": False,
        },
    )

    result = broker.call("browser.open_url", {"url": "https://example.com"})

    assert result["ok"] is False
    assert result["error"] == "browser_owned_target_unverified"
    assert broker._owned_browser_target_id == ""


def test_browser_broker_requires_authoritative_websocket_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    monkeypatch.setattr(
        browser_mod,
        "open_url",
        lambda url: {
            "ok": True,
            "action": "browser.open_url",
            "data": {"url": url, "target_id": "target-without-websocket"},
            "fallback_used": False,
        },
    )

    result = broker.call("browser.open_url", {"url": "https://example.com"})

    assert result["ok"] is False
    assert result["error"] == "browser_owned_target_unverified"
    assert broker._owned_browser_target_id == ""


def test_browser_brokers_bind_followups_to_their_own_cdp_targets(
    tmp_path,
    monkeypatch,
) -> None:
    first = _broker(tmp_path / "first")
    second = _broker(tmp_path / "second")
    observed: list[tuple[str, str]] = []

    def fake_open(url: str) -> dict[str, object]:
        target_id = "target-first" if "first" in url else "target-second"
        return {
            "ok": True,
            "action": "browser.open_url",
            "data": {
                "url": url,
                "target_id": target_id,
                "target_websocket_available": True,
            },
            "fallback_used": False,
        }

    def fake_click(selector: str, **_kwargs) -> dict[str, object]:
        observed.append((selector, browser_mod._OWNED_BROWSER_TARGET_ID.get()))
        return {
            "ok": True,
            "action": "browser.click",
            "data": {"selector": selector},
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "open_url", fake_open)
    monkeypatch.setattr(browser_mod, "click", fake_click)

    assert first.call(
        "browser.open_url",
        {"url": "https://example.com/first"},
    )["ok"] is True
    assert second.call(
        "browser.open_url",
        {"url": "https://example.com/second"},
    )["ok"] is True
    assert first.call("browser.click", {"selector": "#first"})["ok"] is True
    assert second.call("browser.click", {"selector": "#second"})["ok"] is True

    assert observed == [
        ("#first", "target-first"),
        ("#second", "target-second"),
    ]


def test_browser_broker_closes_only_its_previous_target_before_replacement(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    opened: list[str] = []
    closed: list[str] = []

    def fake_open(url: str) -> dict[str, object]:
        opened.append(url)
        target_id = f"target-{len(opened)}"
        return {
            "ok": True,
            "action": "browser.open_url",
            "data": {
                "url": url,
                "target_id": target_id,
                "target_websocket_available": True,
            },
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "open_url", fake_open)
    monkeypatch.setattr(
        browser_mod,
        "close_target",
        lambda target_id: closed.append(target_id)
        or {"ok": True, "action": "browser.close_target"},
    )

    first = broker.browser_open_url("https://example.com/first")
    assert first["ok"] is True
    assert first["postcondition_verified"] is True
    assert first["data"]["postcondition_verified"] is True
    assert first["data"]["browser_profile_isolated"] is True
    assert first["data"]["browser_profile_isolated_from_user"] is True
    second = broker.browser_open_url("https://example.com/second")

    assert second["ok"] is True
    assert broker._owned_browser_target_id == "target-2"
    assert closed == ["target-1"]


def test_browser_current_page_selects_bound_target_not_first_page(monkeypatch) -> None:
    monkeypatch.setattr(
        browser_mod,
        "_configured_browser_cdp_url",
        lambda: "http://127.0.0.1:9222",
    )
    monkeypatch.setattr(
        browser_mod,
        "_cdp_list_pages",
        lambda _url: [
            {"id": "user-tab", "type": "page", "url": "https://private.example"},
            {"id": "agent-tab", "type": "page", "url": "https://agent.example"},
        ],
    )

    with browser_mod.owned_browser_target("agent-tab"):
        page = browser_mod._current_page()

    assert page["id"] == "agent-tab"


def test_browser_screenshot_never_captures_host_screen_implicitly(
    tmp_path,
    monkeypatch,
) -> None:
    capture_calls = []
    monkeypatch.setattr(
        browser_mod,
        "_page_websocket_url",
        lambda: (_ for _ in ()).throw(RuntimeError("no cdp")),
    )
    monkeypatch.setattr(
        browser_mod,
        "_capture_screen_fallback",
        lambda path: capture_calls.append(path),
    )

    result = browser_mod.screenshot(tmp_path / "page.png")

    assert result["ok"] is False
    assert result["error"] == "chrome_cdp_unavailable"
    assert capture_calls == []


def test_browser_extract_text_uses_current_page_evaluation(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_evaluate(expression: str) -> dict[str, object]:
        expressions.append(expression)
        return {
            "ok": True,
            "text": "Hello from browser",
            "page_url": "https://www.google.com/search?q=yachiyo",
            "link_contexts": [
                {
                    "href": "https://zh.wikipedia.org/wiki/Yachiyo",
                    "text": "Yachiyo · 英文名称: Yachiyo",
                }
            ],
        }

    monkeypatch.setattr(
        browser_mod,
        "_evaluate_current_page",
        fake_evaluate,
    )

    result = browser_mod.extract_text("#main")

    assert result["ok"] is True
    assert result["data"] == {
        "selector": "#main",
        "text": "Hello from browser",
        "truncated": False,
        "page_url": "https://www.google.com/search?q=yachiyo",
        "page_url_truncated": False,
        "link_contexts": [
            {
                "href": "https://zh.wikipedia.org/wiki/Yachiyo",
                "text": "Yachiyo · 英文名称: Yachiyo",
            }
        ],
    }
    assert "anchors.push(...root.querySelectorAll" not in expressions[0]
    assert "anchors.length >= 40" in expressions[0]
    assert "slice(0, 20001)" in expressions[0]
    assert "slice(0, 2048)" in expressions[0]


def test_browser_extract_text_rejects_overlong_structured_evidence_fields(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        browser_mod,
        "_evaluate_current_page",
        lambda _expression: {
            "ok": True,
            "text": "page",
            "page_url": "https://www.google.com/search?q=" + "x" * 3000,
            "link_contexts": [
                {
                    "href": "https://wikipedia.org/" + "x" * 3000,
                    "text": "bounded",
                },
                {
                    "href": "https://en.wikipedia.org/wiki/Example",
                    "text": "x" * 801,
                },
                {
                    "href": "https://en.wikipedia.org/wiki/Valid",
                    "text": "valid context",
                },
            ],
        },
    )

    result = browser_mod.extract_text()

    assert result["ok"] is True
    assert result["data"]["page_url"] == ""
    assert result["data"]["page_url_truncated"] is True
    assert result["data"]["link_contexts"] == [
        {
            "href": "https://en.wikipedia.org/wiki/Valid",
            "text": "valid context",
        }
    ]


def test_browser_click_falls_back_to_foreground_coordinates(monkeypatch) -> None:
    calls = []

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    def fake_foreground_click(x, y, click_count) -> dict[str, object]:
        calls.append((x, y, click_count))
        return {
            "ok": True,
            "action": "desktop.click",
            "summary": "Clicked foreground desktop",
            "data": {"x": x, "y": y, "click_count": click_count},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    result = browser_mod.click(
        "#submit",
        fallback_x=12,
        fallback_y=34,
        click_count=2,
        foreground_fallback=fake_foreground_click,
    )

    assert result["ok"] is True
    assert result["action"] == "browser.click"
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.click"
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert result["data"] == {"x": 12, "y": 34, "click_count": 2, "selector": "#submit"}
    assert calls == [(12, 34, 2)]


def test_browser_click_accepts_text_selector(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_evaluate(expression: str) -> dict[str, object]:
        expressions.append(expression)
        return {"ok": True, "selector": "text=登录", "tag": "BUTTON", "label": "登录"}

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", fake_evaluate)

    result = browser_mod.click("text=登录")

    assert result["ok"] is True
    assert result["action"] == "browser.click"
    assert result["data"] == {"ok": True, "selector": "text=登录", "tag": "BUTTON", "label": "登录"}
    assert "textSelectorPrefix" in expressions[0]
    assert "findByText" in expressions[0]


def test_browser_click_accepts_search_result_selector(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_evaluate(expression: str) -> dict[str, object]:
        expressions.append(expression)
        return {
            "ok": True,
            "selector": "search-result=1",
            "tag": "A",
            "label": "Yachiyo result",
            "click_count": 1,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", fake_evaluate)

    result = browser_mod.click("search-result=1")

    assert result["ok"] is True
    assert result["action"] == "browser.click"
    assert result["data"] == {
        "ok": True,
        "selector": "search-result=1",
        "tag": "A",
        "label": "Yachiyo result",
        "click_count": 1,
    }
    assert "searchResultSelectorPrefix" in expressions[0]
    assert "findSearchResult" in expressions[0]


def test_browser_click_accepts_point_selector(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_evaluate(expression: str) -> dict[str, object]:
        expressions.append(expression)
        return {
            "ok": True,
            "selector": "point=120,240",
            "tag": "BUTTON",
            "label": "继续",
            "x": 120,
            "y": 240,
            "click_count": 1,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", fake_evaluate)

    result = browser_mod.click("point=120,240", fallback_x=120, fallback_y=240)

    assert result["ok"] is True
    assert result["action"] == "browser.click"
    assert result["data"] == {
        "ok": True,
        "selector": "point=120,240",
        "tag": "BUTTON",
        "label": "继续",
        "x": 120,
        "y": 240,
        "click_count": 1,
    }
    assert "pointSelectorPrefix" in expressions[0]
    assert "document.elementFromPoint" in expressions[0]


def test_browser_click_without_explicit_fallback_stays_cdp_only(monkeypatch) -> None:
    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("No debuggable browser page found")

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)

    result = browser_mod.click("#submit")

    assert result["ok"] is False
    assert result["error"] == "chrome_cdp_unavailable"
    assert result["permission_error"] is True
    assert result["fallback_used"] is False
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]


def test_browser_click_reports_foreground_permission_when_fallback_denied(
    monkeypatch,
) -> None:
    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("No debuggable browser page found")

    def deny_foreground_click(_x, _y, _click_count) -> dict[str, object]:
        return {
            "ok": False,
            "action": "desktop.click",
            "summary": "desktop.click failed",
            "error": "not allowed assistive access",
            "permission_error": True,
            "missing_permissions": ["accessibility"],
            "permission_targets": ["accessibility"],
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    result = browser_mod.click(
        "#submit",
        fallback_x=12,
        fallback_y=34,
        foreground_fallback=deny_foreground_click,
    )

    assert result["ok"] is False
    assert result["error"] == "browser_foreground_click_fallback_unavailable"
    assert result["permission_error"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.click"
    assert result["missing_permissions"] == ["chrome_cdp", "accessibility"]
    assert result["permission_targets"] == ["chrome_cdp", "accessibility"]


def test_browser_click_broker_fallback_uses_foreground_lock(tmp_path, monkeypatch) -> None:
    foreground_lock = ForegroundActionLock()
    broker = ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": []},
        tmp_path / "artifacts",
        foreground_lock=foreground_lock,
        foreground_lock_owner="group-run-1:run-1",
    )

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    monkeypatch.setattr(
        desktop_mod,
        "desktop_click",
        lambda x, y, *, click_count=1: {
            "ok": True,
            "data": {"x": x, "y": y, "click_count": click_count},
        },
    )

    result = broker.browser_click(
        "#submit",
        fallback_x=12,
        fallback_y=34,
        click_count=2,
        allow_foreground_fallback=True,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.click"
    assert result["foreground_lock"] == {
        "holder": "group-run-1:run-1",
        "tool": "browser.click",
    }


def test_browser_click_registry_never_uses_foreground_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    desktop_calls = []

    monkeypatch.setattr(
        browser_mod,
        "_evaluate_current_page",
        lambda _expression: (_ for _ in ()).throw(RuntimeError("no cdp")),
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_click",
        lambda *args, **kwargs: desktop_calls.append((args, kwargs)),
    )

    result = broker.call(
        "browser.click",
        {"selector": "#submit", "fallback_x": 12, "fallback_y": 34},
    )

    assert result["ok"] is False
    assert result["error"] == "browser_owned_target_required"
    assert result["user_handoff_required"] is True
    assert result["replan_allowed"] is False
    assert desktop_calls == []


def test_browser_broker_checks_owned_target_before_requesting_approval(tmp_path) -> None:
    broker = _broker(tmp_path)
    broker.approvals["browser.click"] = True

    blocked = broker.call("browser.click", {"selector": "#submit"})

    assert blocked["ok"] is False
    assert blocked["error"] == "browser_owned_target_required"
    assert blocked.get("approval_required") is not True

    broker.restore_owned_browser_target("agent-owned-tab")
    waiting = broker.call("browser.click", {"selector": "#submit"})

    assert waiting["approval_required"] is True
    assert waiting["tool"] == "browser.click"


def test_browser_type_text_falls_back_to_foreground_input(monkeypatch) -> None:
    calls = []

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    def fake_foreground_type(text: str) -> dict[str, object]:
        calls.append(text)
        return {
            "ok": True,
            "action": "desktop.type_text",
            "summary": "Typed text into the foreground app",
            "data": {"character_count": len(text)},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    result = browser_mod.type_text(
        "#search",
        "kaguya",
        foreground_fallback=fake_foreground_type,
    )

    assert result["ok"] is True
    assert result["action"] == "browser.type_text"
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.type_text"
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert result["data"] == {"character_count": 6, "selector": "#search"}
    assert calls == ["kaguya"]


def test_browser_type_text_accepts_point_selector(monkeypatch) -> None:
    expressions: list[str] = []

    def fake_evaluate(expression: str) -> dict[str, object]:
        expressions.append(expression)
        return {
            "ok": True,
            "selector": "point=120,240",
            "tag": "INPUT",
            "length": 5,
            "content_verified": True,
            "x": 120,
            "y": 240,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", fake_evaluate)

    result = browser_mod.type_text("point=120,240", "hello", fallback_x=120, fallback_y=240)

    assert result["ok"] is True
    assert result["action"] == "browser.type_text"
    assert result["data"] == {
        "ok": True,
        "selector": "point=120,240",
        "tag": "INPUT",
        "length": 5,
        "content_verified": True,
        "x": 120,
        "y": 240,
    }
    assert "content_verified" in expressions[0]
    assert "pointSelectorPrefix" in expressions[0]
    assert "document.elementFromPoint" in expressions[0]


def test_browser_type_text_point_falls_back_to_click_then_type(monkeypatch) -> None:
    calls = []

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    def fake_foreground_type(*args) -> dict[str, object]:
        calls.append(args)
        return {
            "ok": True,
            "action": "desktop.click+desktop.type_text",
            "summary": "Clicked and typed",
            "data": {"x": args[0], "y": args[1], "character_count": len(args[2])},
            "permission_error": False,
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)

    result = browser_mod.type_text(
        "point=120,240",
        "hello",
        fallback_x=120,
        fallback_y=240,
        foreground_fallback=fake_foreground_type,
    )

    assert result["ok"] is True
    assert result["action"] == "browser.type_text"
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.click+desktop.type_text"
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert result["data"] == {
        "x": 120,
        "y": 240,
        "character_count": 5,
        "selector": "point=120,240",
    }
    assert calls == [(120, 240, "hello")]


def test_browser_type_text_reports_foreground_permission_when_fallback_denied(
    monkeypatch,
) -> None:
    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("No debuggable browser page found")

    def deny_foreground_type(_text: str) -> dict[str, object]:
        return {
            "ok": False,
            "action": "desktop.type_text",
            "summary": "desktop.type_text failed",
            "error": "not authorized for accessibility",
            "permission_error": True,
            "missing_permissions": ["accessibility"],
            "permission_targets": ["accessibility"],
            "fallback_used": False,
        }

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    result = browser_mod.type_text(
        "#search",
        "kaguya",
        foreground_fallback=deny_foreground_type,
    )

    assert result["ok"] is False
    assert result["error"] == "browser_foreground_fallback_unavailable"
    assert result["permission_error"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.type_text"
    assert result["missing_permissions"] == ["chrome_cdp", "accessibility"]
    assert result["permission_targets"] == ["chrome_cdp", "accessibility"]


def test_browser_type_text_broker_fallback_uses_foreground_lock(tmp_path, monkeypatch) -> None:
    foreground_lock = ForegroundActionLock()
    broker = ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": []},
        tmp_path / "artifacts",
        foreground_lock=foreground_lock,
        foreground_lock_owner="group-run-1:run-1",
    )

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    monkeypatch.setattr(
        desktop_mod,
        "desktop_type_text",
        lambda text: {"ok": True, "data": {"character_count": len(text)}},
    )

    result = broker.browser_type_text(
        "#search",
        "kaguya",
        allow_foreground_fallback=True,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.type_text"
    assert result["foreground_lock"] == {
        "holder": "group-run-1:run-1",
        "tool": "browser.type_text",
    }


def test_browser_type_text_broker_point_fallback_clicks_then_types_with_lock(
    tmp_path,
    monkeypatch,
) -> None:
    foreground_lock = ForegroundActionLock()
    broker = ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": []},
        tmp_path / "artifacts",
        foreground_lock=foreground_lock,
        foreground_lock_owner="group-run-1:run-1",
    )
    calls = []

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        raise RuntimeError("browser.cdp_url is not configured")

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    monkeypatch.setattr(
        desktop_mod,
        "desktop_click",
        lambda x, y: calls.append(("click", x, y))
        or {"ok": True, "data": {"x": x, "y": y}},
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_type_text",
        lambda text: calls.append(("type", text))
        or {"ok": True, "data": {"character_count": len(text)}},
    )

    result = broker.browser_type_text(
        "point=12,34",
        "kaguya",
        fallback_x=12,
        fallback_y=34,
        allow_foreground_fallback=True,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "desktop.click+desktop.type_text"
    assert result["foreground_lock"] == {
        "holder": "group-run-1:run-1",
        "tool": "browser.type_text",
    }
    assert result["data"] == {
        "character_count": 6,
        "x": 12,
        "y": 34,
        "selector": "point=12,34",
    }
    assert calls == [("click", 12, 34), ("type", "kaguya")]


def test_browser_type_text_broker_fallback_preserves_foreground_lock_busy(
    tmp_path,
    monkeypatch,
) -> None:
    foreground_lock = ForegroundActionLock()
    broker = ToolBroker(
        {"default_workdir": str(tmp_path), "readable_scopes": ["."], "writable_scopes": []},
        tmp_path / "artifacts",
        foreground_lock=foreground_lock,
        foreground_lock_owner="group-run-1:run-2",
    )
    broker.restore_owned_browser_target("owned-original")
    lease = foreground_lock.acquire(holder="group-run-1:run-1", tool_name="browser.type_text")
    calls = []

    def raise_no_cdp(_expression: str) -> dict[str, object]:
        calls.append("cdp")
        raise RuntimeError("browser.cdp_url is not configured")

    monkeypatch.setattr(browser_mod, "_evaluate_current_page", raise_no_cdp)
    monkeypatch.setattr(
        browser_mod,
        "_type_text_foreground_fallback",
        lambda *_args: calls.append("native") or {"ok": True},
    )

    try:
        result = broker.browser_type_text(
            "#search",
            "kaguya",
            allow_foreground_fallback=True,
        )
    finally:
        lease.release()

    assert result["ok"] is False
    assert result["action"] == "foreground_lock"
    assert result["foreground_lock_busy"] is True
    assert result["locked_by"] == "group-run-1:run-1"
    assert calls == []
    assert broker._owned_browser_target_id == "owned-original"


def test_browser_type_text_registry_never_uses_foreground_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    desktop_calls = []

    monkeypatch.setattr(
        browser_mod,
        "_evaluate_current_page",
        lambda _expression: (_ for _ in ()).throw(RuntimeError("no cdp")),
    )
    monkeypatch.setattr(
        desktop_mod,
        "desktop_type_text",
        lambda *args, **kwargs: desktop_calls.append((args, kwargs)),
    )

    result = broker.call(
        "browser.type_text",
        {"selector": "#search", "text": "kaguya"},
    )

    assert result["ok"] is False
    assert result["error"] == "browser_owned_target_required"
    assert desktop_calls == []


def test_browser_screenshot_tool_writes_artifact_metadata(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    broker._owned_browser_target_id = "target-1"
    png_bytes = b"browser-png"

    monkeypatch.setattr(
        browser_mod,
        "_page_websocket_url",
        lambda: "ws://127.0.0.1/devtools/page/1",
    )
    monkeypatch.setattr(
        browser_mod,
        "_cdp_command",
        lambda _url, method, params: (
            {"data": base64.b64encode(png_bytes).decode("ascii")}
            if method == "Page.captureScreenshot" and params["format"] == "png"
            else {}
        ),
    )

    result = broker.call("browser.screenshot", {"reason": "capture page"})

    assert result["ok"] is True
    assert result["reason"] == "capture page"
    assert result["artifact"] == {
        "path": "browser/current-page.png",
        "kind": "image",
        "mime_type": "image/png",
        "size_bytes": len(png_bytes),
    }
    assert (tmp_path / "artifacts" / "browser" / "current-page.png").read_bytes() == png_bytes


def test_browser_open_url_and_extract_text_runs_open_then_extract(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    calls: list[tuple[str, str]] = []

    def fake_open_url(url: str) -> dict[str, object]:
        calls.append(("open", url))
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-extract",
                "target_websocket_available": True,
            },
        }

    def fake_extract_text(selector: str = "") -> dict[str, object]:
        calls.append(("extract", selector))
        return {
            "ok": True,
            "action": "browser.extract_text",
            "summary": "Extracted page text",
            "data": {"selector": selector, "text": "GitHub page text", "truncated": False},
        }

    monkeypatch.setattr(browser_mod, "open_url", fake_open_url)
    monkeypatch.setattr(browser_mod, "extract_text", fake_extract_text)

    result = broker.call(
        "browser.open_url_and_extract_text",
        {"url": "https://github.com", "selector": "main"},
    )

    assert calls == [("open", "https://github.com"), ("extract", "main")]
    assert result["ok"] is True
    assert result["action"] == "browser.open_url_and_extract_text"
    assert result["data"]["url"] == "https://github.com"
    assert result["data"]["selector"] == "main"
    assert result["data"]["text"] == "GitHub page text"


def test_browser_open_url_and_screenshot_preserves_artifact(tmp_path, monkeypatch) -> None:
    broker = _broker(tmp_path)
    png_bytes = b"open-browser-png"
    calls: list[tuple[str, str]] = []

    def fake_open_url(url: str) -> dict[str, object]:
        calls.append(("open", url))
        return {
            "ok": True,
            "action": "browser.open_url",
            "summary": f"Opened {url}",
            "data": {
                "url": url,
                "target_id": "target-screenshot",
                "target_websocket_available": True,
            },
        }

    def fake_screenshot(target_path: Path) -> dict[str, object]:
        calls.append(("screenshot", str(target_path)))
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(png_bytes)
        return {
            "ok": True,
            "action": "browser.screenshot",
            "summary": "Captured current browser page",
            "data": {
                "path": str(target_path),
                "mime_type": "image/png",
                "format": "png",
                "size": len(png_bytes),
            },
        }

    monkeypatch.setattr(browser_mod, "open_url", fake_open_url)
    monkeypatch.setattr(browser_mod, "screenshot", fake_screenshot)

    result = broker.call(
        "browser.open_url_and_screenshot",
        {"url": "https://github.com", "reason": "capture GitHub"},
    )

    assert calls[0] == ("open", "https://github.com")
    assert calls[1][0] == "screenshot"
    assert result["ok"] is True
    assert result["action"] == "browser.open_url_and_screenshot"
    assert result["reason"] == "capture GitHub"
    assert result["artifact"]["path"] == "browser/current-page.png"
    assert result["data"]["url"] == "https://github.com"
    assert result["data"]["path"] == "browser/current-page.png"
    assert (tmp_path / "artifacts" / "browser" / "current-page.png").read_bytes() == png_bytes


def test_browser_screenshot_can_use_explicit_screen_capture_fallback(
    tmp_path,
    monkeypatch,
) -> None:
    broker = _broker(tmp_path)
    fallback_bytes = b"fallback-browser-png"

    def raise_no_cdp() -> str:
        raise RuntimeError("browser.cdp_url is not configured")

    def fake_capture_screen(target_path) -> dict[str, object]:
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fallback_bytes)
        return {
            "path": str(target),
            "mime_type": "image/png",
            "format": "png",
            "width": 32,
            "height": 24,
            "size": len(fallback_bytes),
        }

    monkeypatch.setattr(browser_mod, "_page_websocket_url", raise_no_cdp)
    monkeypatch.setattr(browser_mod, "_capture_screen_fallback", fake_capture_screen)

    result = broker.browser_screenshot(
        reason="capture fallback page",
        allow_screen_fallback=True,
    )

    assert result["ok"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "screen.capture"
    assert result["missing_permissions"] == ["chrome_cdp"]
    assert result["permission_targets"] == ["chrome_cdp"]
    assert result["reason"] == "capture fallback page"
    assert result["artifact"] == {
        "path": "browser/current-page.png",
        "kind": "image",
        "mime_type": "image/png",
        "size_bytes": len(fallback_bytes),
    }
    assert result["data"]["path"] == "browser/current-page.png"
    assert result["data"]["width"] == 32
    assert (
        tmp_path / "artifacts" / "browser" / "current-page.png"
    ).read_bytes() == fallback_bytes


def test_browser_screenshot_reports_screen_capture_permission_when_fallback_denied(
    tmp_path,
    monkeypatch,
) -> None:
    def raise_no_cdp() -> str:
        raise RuntimeError("No debuggable browser page found")

    def raise_screen_permission(_target_path) -> dict[str, object]:
        raise RuntimeError("screen recording not authorized")

    monkeypatch.setattr(browser_mod, "_page_websocket_url", raise_no_cdp)
    monkeypatch.setattr(browser_mod, "_capture_screen_fallback", raise_screen_permission)

    result = browser_mod.screenshot(
        tmp_path / "page.png",
        allow_screen_fallback=True,
    )

    assert result["ok"] is False
    assert result["error"] == "browser_screenshot_unavailable"
    assert result["permission_error"] is True
    assert result["fallback_used"] is True
    assert result["fallback"] == "screen.capture"
    assert result["missing_permissions"] == ["chrome_cdp", "screen_recording"]
    assert result["permission_targets"] == ["chrome_cdp", "screen_recording"]
