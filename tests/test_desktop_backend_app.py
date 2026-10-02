import os
from types import SimpleNamespace

from apps.bridge.server import bridge_request_violation
from apps.desktop_backend import app as desktop_backend_app
from apps.desktop_backend.app import _bridge_endpoint_from_env, _ensure_bridge_session_token


def test_bridge_endpoint_prefers_electron_env_url(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:49321")

    endpoint = _bridge_endpoint_from_env(
        SimpleNamespace(bridge_host="127.0.0.1", bridge_port=8420)
    )

    assert endpoint == ("127.0.0.1", 49321)


def test_bridge_endpoint_rejects_packaged_default_in_source_backend(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:18420")
    monkeypatch.setattr("apps.desktop_backend.app._running_from_packaged_backend", lambda: False)

    endpoint = _bridge_endpoint_from_env(
        SimpleNamespace(bridge_host="127.0.0.1", bridge_port=8420)
    )

    assert endpoint == ("127.0.0.1", 8420)


def test_bridge_endpoint_allows_packaged_default_in_frozen_backend(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:18420")
    monkeypatch.setattr("apps.desktop_backend.app._running_from_packaged_backend", lambda: True)

    endpoint = _bridge_endpoint_from_env(
        SimpleNamespace(bridge_host="127.0.0.1", bridge_port=8420)
    )

    assert endpoint == ("127.0.0.1", 18420)


def test_bridge_endpoint_falls_back_to_config_for_invalid_env_url(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:not-a-port")

    endpoint = _bridge_endpoint_from_env(
        SimpleNamespace(bridge_host="127.0.0.1", bridge_port=8420)
    )

    assert endpoint == ("127.0.0.1", 8420)


def test_desktop_backend_generates_bridge_session_token_when_missing(monkeypatch):
    # Track an initially absent key so the token minted by production is undone.
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "")

    generated = _ensure_bridge_session_token()

    assert generated is True
    token = os.getenv("OHA_YACHIYO_BRIDGE_TOKEN") or ""
    assert len(token) >= 32
    assert bridge_request_violation("POST", {"host": "127.0.0.1:8420"}) == "invalid_bridge_token"
    assert bridge_request_violation(
        "POST",
        {"host": "127.0.0.1:8420", "x-oha-yachiyo-bridge-token": token},
    ) == ""


def test_desktop_backend_preserves_injected_bridge_session_token(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "injected-token")

    generated = _ensure_bridge_session_token()

    assert generated is False
    assert os.getenv("OHA_YACHIYO_BRIDGE_TOKEN") == "injected-token"


def test_desktop_backend_main_starts_native_app_runtime_without_hermes(monkeypatch, tmp_path):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("HERMES_YACHIYO_HOME", raising=False)
    monkeypatch.setattr(desktop_backend_app, "_setup_logging", lambda: None)

    calls: list[tuple[str, object]] = []

    class FakeConfig:
        bridge_host = "127.0.0.1"
        bridge_port = 8420

    class FakeRuntime:
        def __init__(self, config):
            self.config = config
            self.started = False
            self.stopped = False
            calls.append(("runtime_config", (config.bridge_host, config.bridge_port)))

        def start(self):
            self.started = True
            calls.append(("runtime_start", self.started))

        def stop(self):
            self.stopped = True
            calls.append(("runtime_stop", self.stopped))

    def fake_start_bridge(*, host, port):
        calls.append(("start_bridge", (host, port)))

    def fake_set_runtime(runtime):
        calls.append(("set_runtime", runtime.started))

    import apps.bridge.deps as bridge_deps
    import apps.bridge.server as bridge_server
    import apps.core.activity_store as activity_store_mod
    import apps.core.chat_store as chat_store_mod
    import apps.core.runtime as runtime_mod
    import apps.core.tls as tls_mod
    import apps.shell.agent_runtime as agent_runtime_mod
    import apps.shell.config as config_mod
    import apps.shell.model_profiles as model_profiles_mod

    monkeypatch.setattr(tls_mod, "install_bundled_ca_env", lambda: calls.append(("tls", True)))
    monkeypatch.setattr(config_mod, "load_config", lambda: FakeConfig())
    monkeypatch.setattr(runtime_mod, "AppRuntime", FakeRuntime)
    monkeypatch.setattr(bridge_deps, "set_runtime", fake_set_runtime, raising=False)
    monkeypatch.setattr(bridge_server, "start_bridge", fake_start_bridge)
    monkeypatch.setattr(bridge_server, "stop_bridge", lambda: calls.append(("stop_bridge", True)))
    monkeypatch.setattr(agent_runtime_mod, "close_agent_runtime_service", lambda: calls.append(("close_runtime", True)))
    monkeypatch.setattr(model_profiles_mod, "close_model_profile_service", lambda: calls.append(("close_profiles", True)))
    monkeypatch.setattr(chat_store_mod, "close_chat_store", lambda: calls.append(("close_chat_store", True)))
    monkeypatch.setattr(activity_store_mod, "close_activity_store", lambda: calls.append(("close_activity", True)))
    monkeypatch.setattr(desktop_backend_app.signal, "signal", lambda *_args, **_kwargs: None)

    desktop_backend_app.main()

    token = os.getenv("OHA_YACHIYO_BRIDGE_TOKEN") or ""
    assert len(token) >= 32
    assert "HERMES_HOME" not in os.environ
    assert "HERMES_YACHIYO_HOME" not in os.environ
    assert calls == [
        ("tls", True),
        ("runtime_config", ("127.0.0.1", 8420)),
        ("runtime_start", True),
        ("set_runtime", True),
        ("start_bridge", ("127.0.0.1", 8420)),
        ("runtime_stop", True),
        ("close_runtime", True),
        ("close_profiles", True),
        ("close_chat_store", True),
        ("close_activity", True),
    ]
