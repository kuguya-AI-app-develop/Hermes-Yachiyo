"""IPv6 loopback is an explicitly supported Bridge endpoint and origin."""

import importlib.util
import sys
from pathlib import Path

import pytest

from apps.bridge.server import bridge_request_violation


def test_bridge_accepts_ipv6_loopback_origin(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)
    assert bridge_request_violation(
        "GET", {"host": "[::1]:8420", "origin": "http://[::1]:5174"}
    ) == ""


def test_bridge_rejects_malformed_origin_without_crashing(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)
    assert bridge_request_violation(
        "GET", {"host": "127.0.0.1:8420", "origin": "http://[::1"}
    ) == "untrusted_origin"


def test_ipv6_cors_and_token_checks_use_real_http_middleware(monkeypatch):
    # The shared conftest injects lightweight FastAPI stubs. Import a fresh
    # Bridge against the real dependencies for the full browser CORS path.
    prefixes = ("fastapi", "uvicorn")
    saved = {
        key: module for key, module in list(sys.modules.items())
        if any(key == prefix or key.startswith(prefix + ".") for prefix in prefixes)
    }
    for key in saved:
        del sys.modules[key]
    module_name = "_ipv6_bridge_under_test"
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "synthetic-test-token")
    try:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")
        path = Path(__file__).resolve().parents[1] / "apps/bridge/server.py"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)

        @module.app.post("/probe")
        async def probe():
            return {"ok": True}

        headers = {"host": "[::1]:8420", "origin": "http://[::1]:5174"}
        with TestClient(module.app) as client:
            preflight = client.options("/probe", headers={
                **headers,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "X-Oha-Yachiyo-Bridge-Token",
            })
            assert preflight.status_code == 200
            assert preflight.headers["access-control-allow-origin"] == headers["origin"]
            assert client.post("/probe", headers=headers).status_code == 403
            allowed = client.post("/probe", headers={
                **headers, "X-Oha-Yachiyo-Bridge-Token": "synthetic-test-token",
            })
            assert allowed.status_code == 200
            assert allowed.headers["access-control-allow-origin"] == headers["origin"]
            assert client.post("/probe", headers={
                **headers, "origin": "http://[2001:db8::1]:5174",
                "X-Oha-Yachiyo-Bridge-Token": "synthetic-test-token",
            }).status_code == 403
    finally:
        sys.modules.pop(module_name, None)
        for key in list(sys.modules):
            if any(key == prefix or key.startswith(prefix + ".") for prefix in prefixes):
                del sys.modules[key]
        sys.modules.update(saved)
