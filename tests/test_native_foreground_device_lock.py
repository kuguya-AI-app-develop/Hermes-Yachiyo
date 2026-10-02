"""Actual entrypoint brokers share the controlled Native foreground device."""

from copy import deepcopy
from threading import Event, Thread

import pytest

from apps.shell.agent.runtime.foreground_search_receipts import (
    PRE,
    foreground_search_live_binding,
    foreground_search_live_matches,
)
from apps.shell.agent.tools import broker as broker_module
from apps.shell.agent.tools.foreground_lock import ForegroundActionLock
from apps.shell.agent_runtime import AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore
from tests.test_native_foreground_search_receipts import _case


@pytest.fixture
def services(tmp_path):
    instances = []

    def create(name):
        service = AgentRuntimeService(
            db_path=tmp_path / name / "runtime.db",
            workspace_dir=tmp_path / name / "workspace",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        instances.append(service)
        return service

    yield create
    for service in instances:
        service.close()


@pytest.mark.parametrize(
    "context", ["main_chat", "ordinary", "different_groups", "separate_factories", "explicit"]
)
@pytest.mark.parametrize("other_action", ["focus", "shortcut"])
@pytest.mark.parametrize("source_tool", ["desktop.search_submit", "desktop.safe_type_text"])
def test_actual_factory_brokers_cannot_change_target_between_native_check_and_input(
    services, monkeypatch, context, other_action, source_tool
):
    first_service = services("first")
    second_service = services("second") if context == "separate_factories" else first_service
    if context == "different_groups":
        first = first_service.tool_brokers.for_run(
            run_id="first", workspace_policy={}, foreground_lock_key="group-a"
        )
        second = second_service.tool_brokers.for_run(
            run_id="second", workspace_policy={}, foreground_lock_key="group-b"
        )
        assert first.foreground_lock is not second.foreground_lock
    else:
        first = first_service.tool_brokers.for_main_chat(run_id="first", workspace_policy={})
        if context in {"ordinary", "explicit"}:
            kwargs = {"foreground_lock": ForegroundActionLock()} if context == "explicit" else {}
            second = second_service.tool_brokers.for_run(
                run_id="second", workspace_policy={}, **kwargs
            )
        else:
            second = second_service.tool_brokers.for_main_chat(run_id="second", workspace_policy={})
    assert first.foreground_device_lock is second.foreground_device_lock
    assert first.foreground_device_lock is not None

    goal = (
        "提交当前搜索"
        if source_tool == "desktop.search_submit"
        else "点搜索框输入 yachiyo 然后搜索"
    )
    _, _, _, _, timeline = _case(goal)
    request = next(e for e in timeline if e.get("tool") == source_tool)
    prior = timeline[: timeline.index(request)]
    expected = foreground_search_live_binding(request, prior, run_id="foreground-run")
    assert expected
    snapshot = deepcopy(next(e for e in prior if e.get("step_id") == PRE)["result"])
    live = {"target": "Google Chrome/Search"}
    calls = []
    validated, continue_input = Event(), Event()

    def validate_pre(observation):
        valid = foreground_search_live_matches(observation, expected)
        validated.set()
        assert continue_input.wait(3)
        return valid

    def disturb(*args, **kwargs):
        live["target"] = "Slack/Composer"
        calls.append("disturb")
        return {"ok": True, "action": "app.focus", "data": {}}

    def dispatch(*args):
        calls.append((source_tool, live["target"]))
        return {"ok": True, "action": source_tool}

    monkeypatch.setattr(broker_module.desktop, "ui_elements", lambda **kwargs: deepcopy(snapshot))
    monkeypatch.setattr(broker_module.desktop, "app_focus", disturb)
    monkeypatch.setattr(broker_module.desktop, "desktop_safe_shortcut", disturb)
    monkeypatch.setattr(broker_module.desktop, "desktop_search_submit", dispatch)
    monkeypatch.setattr(broker_module.desktop, "desktop_safe_type_text", dispatch)
    results, errors = [], []

    def run():
        try:
            results.append(
                first.runtime_exact_search_input(source_tool, "yachiyo", validate_pre=validate_pre)
            )
        except BaseException as exc:
            errors.append(exc)

    worker = Thread(target=run)
    worker.start()
    try:
        assert validated.wait(3)
        concurrent = (
            second.app_focus("Slack")
            if other_action == "focus"
            else second.desktop_safe_shortcut("find")
        )
    finally:
        continue_input.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert concurrent["ok"] is False and concurrent["foreground_lock_busy"] is True
    assert concurrent["locked_by"] == (first.foreground_lock_owner or str(first.artifact_root))
    assert results[0]["ok"] is True
    assert calls == [(source_tool, "Google Chrome/Search")]
    assert first.foreground_device_lock.owner == ""


MUTATIONS = [
    ("app_open", "app_open", ("Slack",), {}),
    ("app_focus", "app_focus", ("Slack",), {}),
    ("app_focus_window", "app_focus_window", ("Slack", "Chat"), {}),
    ("app_show", "app_show", ("Slack",), {}),
    ("app_hide", "app_hide", ("Slack",), {}),
    ("app_minimize", "app_minimize", ("Slack",), {}),
    ("app_quit", "app_quit", ("Slack",), {}),
    ("desktop_reveal_path", "reveal_path", ("file.txt",), {}),
    ("desktop_open_path", "open_path", ("file.txt",), {}),
    ("desktop_open_path_with_app", "open_path_with_app", ("file.txt", "Slack"), {}),
    ("media_apple_music_play", "apple_music_play", ("song",), {}),
    ("media_apple_music_open_and_play", "apple_music_open_and_play", (), {}),
    ("media_apple_music_control", "apple_music_control", ("pause",), {}),
    ("media_music_app_open_and_play", "music_app_open_and_play", ("Spotify",), {}),
    ("media_music_app_control", "music_app_control", ("Spotify", "pause"), {}),
    ("media_system_control", "system_media_control", ("pause",), {}),
    ("system_settings_open", "system_settings_open", ("sound",), {}),
    ("system_volume", "system_volume", ("set",), {"level": 20}),
    ("system_brightness", "system_brightness", ("up",), {}),
    ("system_display_sleep", "system_display_sleep", (), {}),
    ("system_screen_saver_start", "system_screen_saver_start", (), {}),
    ("clipboard_write", "clipboard_write", ("text",), {}),
    ("notes_create", "notes_create", ("body",), {}),
    ("reminders_create", "reminders_create", ("title",), {}),
    (
        "calendar_create_event",
        "calendar_create_event",
        ("title",),
        {"start_at": "2026-10-03T10:00"},
    ),
    ("desktop_inspect_app", "inspect_app", ("Slack",), {"focus": True}),
    ("desktop_inspect_app", "inspect_app", ("Slack",), {"open_if_needed": "yes"}),
]


@pytest.mark.parametrize("method,native,args,kwargs", MUTATIONS)
def test_native_mutations_respect_device_lease_before_any_adapter_call(
    services, monkeypatch, method, native, args, kwargs
):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    calls = []
    monkeypatch.setattr(
        broker_module.desktop, native, lambda *a, **k: calls.append((a, k)) or {"ok": True}
    )
    lease = broker.foreground_device_lock.acquire(
        holder="other-run", tool_name="desktop.search_submit"
    )
    try:
        result = getattr(broker, method)(*args, **kwargs)
    finally:
        lease.release()
    assert result["foreground_lock_busy"] is True and result["locked_by"] == "other-run"
    assert not calls


@pytest.mark.parametrize(
    "method,native,args,kwargs",
    [
        ("desktop_active_window", "active_window", (), {}),
        ("desktop_ui_elements", "ui_elements", (), {}),
        ("desktop_running_apps", "running_apps", (), {}),
        ("app_status", "app_status", ("Slack",), {}),
        ("clipboard_read", "clipboard_read", (), {}),
        ("media_apple_music_status", "apple_music_status", (), {}),
        (
            "desktop_inspect_app",
            "inspect_app",
            ("Slack",),
            {"open_if_needed": "false", "focus": "false"},
        ),
    ],
)
def test_passive_native_observations_do_not_take_mutation_lease(
    services, monkeypatch, method, native, args, kwargs
):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    monkeypatch.setattr(
        broker_module.desktop, native, lambda *a, **k: {"ok": True, "observed": True}
    )
    lease = broker.foreground_device_lock.acquire(
        holder="other-run", tool_name="desktop.search_submit"
    )
    try:
        assert getattr(broker, method)(*args, **kwargs) == {"ok": True, "observed": True}
    finally:
        lease.release()


def test_external_scoped_lease_still_blocks_and_releases_device(services, monkeypatch):
    external = ForegroundActionLock()
    broker = services("service").tool_brokers.for_run(
        run_id="main",
        workspace_policy={},
        foreground_lock=external,
        foreground_lock_owner="group:main",
    )
    assert broker.foreground_lock is external
    calls = []
    monkeypatch.setattr(
        broker_module.desktop,
        "app_focus",
        lambda name: calls.append(name) or {"ok": True, "data": {}},
    )
    lease = external.acquire(holder="external-owner", tool_name="app.focus")
    try:
        denied = broker.app_focus("Slack")
        assert denied["locked_by"] == "external-owner" and not calls
        assert broker.foreground_device_lock.owner == ""
    finally:
        lease.release()
    assert broker.app_focus("Slack")["foreground_lock"] == {
        "holder": "group:main",
        "tool": "app.focus",
    }
    assert calls == ["Slack"]


def test_same_device_and_explicit_lock_is_only_acquired_once(services, monkeypatch):
    factory = services("service").tool_brokers
    first = factory.for_main_chat(run_id="first", workspace_policy={})
    second = factory.for_run(
        run_id="second", workspace_policy={}, foreground_lock=first.foreground_device_lock
    )
    monkeypatch.setattr(broker_module.desktop, "app_focus", lambda name: {"ok": True, "data": {}})
    assert second.app_focus("Slack")["ok"] is True
    assert first.foreground_device_lock.owner == ""


@pytest.mark.parametrize("failure", ["action", "scoped_acquire"])
def test_device_lease_is_released_when_native_action_or_scoped_acquisition_raises(
    services, monkeypatch, failure
):
    class RaisingLock:
        def acquire(self, **kwargs):
            raise RuntimeError("scoped acquisition failed")

    factory = services("service").tool_brokers
    broker = factory.for_run(
        run_id="main",
        workspace_policy={},
        foreground_lock=RaisingLock() if failure == "scoped_acquire" else ForegroundActionLock(),
    )

    def raise_action(name):
        raise RuntimeError("native action failed")

    monkeypatch.setattr(broker_module.desktop, "app_focus", raise_action)
    with pytest.raises(RuntimeError, match="failed"):
        broker.app_focus("Slack")
    assert broker.foreground_device_lock.owner == ""
    next_lease = broker.foreground_device_lock.acquire(holder="next-run", tool_name="app.focus")
    try:
        assert next_lease.acquired
    finally:
        next_lease.release()


def test_device_and_scoped_leases_release_in_reverse_order(services, monkeypatch):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    events = []

    class RecordingLock(ForegroundActionLock):
        def __init__(self, name):
            super().__init__()
            self.name = name

        def acquire(self, **kwargs):
            events.append("acquire-" + self.name)
            return super().acquire(**kwargs)

        def _release(self, holder):
            events.append("release-" + self.name)
            super()._release(holder)

    broker.foreground_device_lock = RecordingLock("device")
    broker.foreground_lock = RecordingLock("scope")
    monkeypatch.setattr(
        broker_module.desktop,
        "app_focus",
        lambda name: events.append("action") or {"ok": True, "data": {}},
    )
    assert broker.app_focus("Slack")["ok"] is True
    assert events == [
        "acquire-device",
        "acquire-scope",
        "action",
        "release-scope",
        "release-device",
    ]


def test_scoped_release_exception_still_releases_shared_device(services, monkeypatch):
    class FailingReleaseLock(ForegroundActionLock):
        def _release(self, holder):
            super()._release(holder)
            raise RuntimeError("scoped release failed")

    factory = services("service").tool_brokers
    scoped = FailingReleaseLock()
    broker = factory.for_run(run_id="main", workspace_policy={}, foreground_lock=scoped)
    monkeypatch.setattr(broker_module.desktop, "app_focus", lambda name: {"ok": True, "data": {}})
    with pytest.raises(RuntimeError, match="scoped release failed"):
        broker.app_focus("Slack")
    assert scoped.owner == "" and broker.foreground_device_lock.owner == ""
    next_lease = broker.foreground_device_lock.acquire(holder="next-run", tool_name="app.focus")
    try:
        assert next_lease.acquired
    finally:
        next_lease.release()


@pytest.mark.parametrize("fallback", [True, False])
def test_browser_new_target_and_cleanup_need_device_lease(services, monkeypatch, fallback):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    broker.restore_owned_browser_target("owned-prior")
    calls = []
    monkeypatch.setattr(
        broker_module.browser,
        "close_target",
        lambda target: calls.append(("close", target)) or {"ok": True},
    )
    monkeypatch.setattr(
        broker_module.browser,
        "open_url",
        lambda url, **kwargs: (
            calls.append(("open", url, kwargs))
            or {"ok": True, "data": {"target_id": "owned-new", "target_websocket_available": True}}
        ),
    )
    lease = broker.foreground_device_lock.acquire(
        holder="other-run", tool_name="desktop.search_submit"
    )
    try:
        result = broker.browser_open_url(
            "https://example.com", allow_system_browser_fallback=fallback
        )
    finally:
        lease.release()
    assert result["foreground_lock_busy"] is True and result["locked_by"] == "other-run"
    assert not calls and broker._owned_browser_target_id == "owned-prior"
    assert (
        broker.browser_open_url("https://example.com", allow_system_browser_fallback=fallback)["ok"]
        is True
    )
    assert broker._owned_browser_target_id == "owned-new"
    assert calls == [
        ("close", "owned-prior"),
        (
            "open",
            "https://example.com",
            {"allow_system_browser_fallback": True} if fallback else {},
        ),
    ]


@pytest.mark.parametrize(
    "method,native,args",
    [
        ("browser_click", "click", ("#search",)),
        ("browser_type_text", "type_text", ("#search", "query")),
        ("close_owned_browser_target", "close_target", ()),
    ],
)
def test_owned_cdp_input_and_close_do_not_bypass_device_lease(
    services, monkeypatch, method, native, args
):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    broker.restore_owned_browser_target("owned-target")
    calls = []
    monkeypatch.setattr(
        broker_module.browser, native, lambda *a, **k: calls.append((a, k)) or {"ok": True}
    )
    lease = broker.foreground_device_lock.acquire(
        holder="other-run", tool_name="desktop.search_submit"
    )
    try:
        result = getattr(broker, method)(*args)
    finally:
        lease.release()
    assert result["foreground_lock_busy"] is True and not calls
    assert broker._owned_browser_target_id == "owned-target"


@pytest.mark.parametrize("method", ["browser_click", "browser_type_text"])
def test_explicit_native_browser_fallback_runs_inside_one_outer_device_lease(
    services, monkeypatch, method
):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    calls = []

    def native(*args, **kwargs):
        calls.append((args, kwargs))
        assert broker.foreground_device_lock.owner == str(broker.artifact_root)
        return {"ok": True, "fallback_used": True}

    monkeypatch.setattr(broker_module.desktop, "desktop_click", native)
    monkeypatch.setattr(broker_module.browser, "_type_text_foreground_fallback", native)
    monkeypatch.setattr(
        broker_module.browser, "click", lambda *a, **k: k["foreground_fallback"](1, 2, 1)
    )
    monkeypatch.setattr(
        broker_module.browser, "type_text", lambda *a, **k: k["foreground_fallback"](1, 2, "query")
    )
    args = ("#search",) if method == "browser_click" else ("#search", "query")
    assert getattr(broker, method)(*args, allow_foreground_fallback=True)["ok"] is True
    assert len(calls) == 1 and broker.foreground_device_lock.owner == ""


@pytest.mark.parametrize("action", ["status", "read", "get"])
def test_volume_status_aliases_remain_passive_while_device_is_busy(services, monkeypatch, action):
    broker = services("service").tool_brokers.for_main_chat(run_id="main", workspace_policy={})
    calls = []
    monkeypatch.setattr(
        broker_module.desktop,
        "system_volume",
        lambda *a, **k: calls.append((a, k)) or {"ok": True, "data": {"changed": False}},
    )
    lease = broker.foreground_device_lock.acquire(
        holder="other-run", tool_name="desktop.search_submit"
    )
    try:
        assert broker.system_volume(action) == {"ok": True, "data": {"changed": False}}
    finally:
        lease.release()
    assert calls == [((action,), {"level": None, "step": None})]
