"""Bridge Server 测试。"""

import asyncio
import base64
import importlib.util
import inspect
import json
import re
import shlex
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import apps.core.activity_store as activity_store_mod
import apps.core.chat_store as chat_store_mod
import apps.locald.screenshot as screenshot_mod
from apps.bridge.routes import agents as agent_routes
from apps.bridge.routes import live2d as live2d_route
from apps.bridge.routes import model_profiles as model_profile_routes
from apps.bridge.routes import runs as run_routes
from apps.bridge.routes import ui as ui_routes
from apps.bridge.server import (
    _bridge_access_log_enabled,
    app,
    bridge_request_violation,
    debug_routes_enabled,
    get_live2d_asset_token,
    regenerate_live2d_asset_token,
)
import apps.shell.chat_api as chat_api_mod
from apps.core.activity_store import ActivityStore
from apps.core.chat_session import ChatSession, MessageStatus
from apps.core.chat_store import ChatStore
from apps.core.executor import NativeAgentExecutor
from apps.core.state import AppState
from apps.core.task_runner import TaskRunner
from apps.shell.agent_runtime import AgentRuntimeError, AgentRuntimeService
from apps.shell.credential_store import MemoryCredentialStore
from packages.protocol.enums import TaskStatus
from scripts.verify_secret_redaction import verify_secret_redaction
from tests.runtime_contract_fixtures import semantic_fixture_model_plan


class _FakeDefaultProfileService:
    def get_defaults(self):
        return {"chat": "profile_default"}

    def get_profile_private(self, profile_id):
        assert profile_id == "profile_default"
        return {
            "profile_id": profile_id,
            "provider": "openai_compatible",
            "base_url": "https://api.example.test/v1",
            "model": "demo-model",
            "api_key": "sk-secret",
            "capability": "chat",
            "status": "available",
            "enabled": True,
        }


def _load_status_route_module():
    path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "status.py"
    spec = importlib.util.spec_from_file_location("_oha_status_route_under_test", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _module_matches_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _unload_module_prefixes(prefixes: tuple[str, ...]) -> dict[str, object]:
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if _module_matches_prefix(name, prefixes)
    }
    for name in list(sys.modules):
        if _module_matches_prefix(name, prefixes):
            sys.modules.pop(name, None)
    return saved


def _restore_module_prefixes(prefixes: tuple[str, ...], saved: dict[str, object]) -> None:
    for name in list(sys.modules):
        if _module_matches_prefix(name, prefixes):
            sys.modules.pop(name, None)
    sys.modules.update(saved)


def test_bridge_app_enables_local_webview_cors():
    middleware = getattr(app, "user_middleware", [])
    cors_entry = next(
        (item for item in middleware if getattr(getattr(item, "cls", None), "__name__", "") == "CORSMiddleware"),
        None,
    )

    assert cors_entry is not None
    assert cors_entry.options["allow_origins"] == []
    assert cors_entry.options["allow_origin_regex"] == r"^https?://(127\.0\.0\.1|localhost)(:\d+)?$"
    assert cors_entry.options["allow_methods"] == ["*"]
    assert cors_entry.options["allow_headers"] == ["*"]


def test_bridge_registers_redacting_http_exception_handler():
    try:
        from fastapi import HTTPException
    except ModuleNotFoundError:
        pytest.skip("FastAPI is not installed")

    handlers = getattr(app, "exception_handlers", None)
    if handlers is None:
        pytest.skip("FastAPI test double does not expose exception handler registry")

    assert HTTPException in handlers


def test_bridge_bad_request_helpers_redact_secret_details():
    secret_error = RuntimeError("provider failed api_key=sk-route-secret123456")

    agent_exc = agent_routes._bad_request(secret_error)
    model_exc = model_profile_routes._bad_request(secret_error)

    assert "sk-route-secret123456" not in agent_exc.detail
    assert "api_key=[redacted]" in agent_exc.detail
    assert "sk-route-secret123456" not in model_exc.detail
    assert "api_key=[redacted]" in model_exc.detail


def test_set_runtime_populates_bridge_app_state():
    deps_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "deps.py"
    spec = importlib.util.spec_from_file_location("_oha_bridge_deps_under_test", deps_path)
    assert spec is not None
    assert spec.loader is not None
    bridge_deps = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = bridge_deps
    spec.loader.exec_module(bridge_deps)

    runtime = SimpleNamespace(agent_runtime_service=object())
    previous_runtime = getattr(bridge_deps, "_runtime", None)
    state = getattr(app, "state", None)
    sentinel = object()
    previous_app_runtime = getattr(state, "runtime", sentinel) if state is not None else sentinel

    try:
        bridge_deps.set_runtime(runtime)

        assert bridge_deps.get_runtime() is runtime
        if state is not None:
            assert state.runtime is runtime
    finally:
        bridge_deps._runtime = previous_runtime
        if state is not None:
            if previous_app_runtime is sentinel:
                try:
                    delattr(state, "runtime")
                except AttributeError:
                    pass
            else:
                state.runtime = previous_app_runtime
        sys.modules.pop("_oha_bridge_deps_under_test", None)


def test_run_events_http_route_paginates_and_hides_non_user_events(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "runs.py"
        spec = importlib.util.spec_from_file_location("_oha_runs_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        run_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = run_route_module
        spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(run_route_module.router)
        try:
            run = service._insert_run(kind="main_chat_run", runnable_id="builtin:yachiyo-main", user_goal="http replay")
            service.append_run_event(run["run_id"], "public.one", {"value": "one"})
            service.append_run_event(run["run_id"], "internal.hidden", {"value": "two"}, visibility="internal")
            service.append_run_event(
                run["run_id"],
                "secret.hidden",
                {"value": "sk-route-secret123456"},
                sensitivity="secret",
            )
            service.append_run_event(run["run_id"], "public.two", {"value": "three"})

            with TestClient(route_app) as client:
                page = client.get(f"/runs/{run['run_id']}/events?after_sequence=1&limit=1")
                clamped = client.get(f"/runs/{run['run_id']}/events?after_sequence=-10&limit=5000")

            assert page.status_code == 200
            assert page.json()["limit"] == 1
            assert [event["event_type"] for event in page.json()["events"]] == ["public.two"]
            assert clamped.status_code == 200
            assert clamped.json()["after_sequence"] == 0
            assert clamped.json()["limit"] == 1000
            assert [event["event_type"] for event in clamped.json()["events"]] == ["public.one", "public.two"]
            assert "sk-route-secret123456" not in json.dumps(clamped.json(), ensure_ascii=False)
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_runs_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_post_runs_http_route_maps_idempotency_key(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "runs.py"
        spec = importlib.util.spec_from_file_location("_oha_runs_create_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        run_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = run_route_module
        spec.loader.exec_module(run_route_module)

        recorded: dict[str, str] = {}

        class FakeRunEngine:
            def create_run_for_runnable(self, **kwargs):
                recorded.update({key: str(value) for key, value in kwargs.items()})
                return {
                    "ok": True,
                    "run_id": "run_post_runs_http",
                    "client_request_id": kwargs.get("client_run_id") or kwargs.get("client_request_id") or "",
                }

        service = FakeRunEngine()
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(run_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/runs",
                json={"runnable_id": "agent_coding", "user_goal": "Run from HTTP"},
                headers={"Idempotency-Key": "post-runs-http-client-1"},
            )

        assert response.status_code == 200
        assert response.json()["run_id"] == "run_post_runs_http"
        assert response.json()["client_request_id"] == "post-runs-http-client-1"
        assert recorded["runnable_id"] == "agent_coding"
        assert recorded["user_goal"] == "Run from HTTP"
        assert recorded["client_run_id"] == "post-runs-http-client-1"
    finally:
        sys.modules.pop("_oha_runs_create_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_post_runs_http_route_rejects_run_group_attachment(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "runs.py"
        spec = importlib.util.spec_from_file_location(
            "_oha_runs_group_authority_route_http_under_test",
            route_path,
        )
        assert spec is not None
        assert spec.loader is not None
        run_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = run_route_module
        spec.loader.exec_module(run_route_module)

        class FakeRunEngine:
            def create_run_for_runnable(self, **_kwargs):
                raise AssertionError("public Group attachment must fail before runtime")

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=FakeRunEngine())
        route_app.include_router(run_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/runs",
                json={
                    "runnable_id": "agent-coding",
                    "user_goal": "join unrelated group",
                    "run_group_id": "run-group-unrelated",
                },
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "public_run_group_attachment_forbidden"
    finally:
        sys.modules.pop("_oha_runs_group_authority_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_post_runs_http_route_rejects_sensitive_idempotency_key_before_persistence(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "runs.py"
        spec = importlib.util.spec_from_file_location("_oha_runs_sensitive_id_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        run_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = run_route_module
        spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        try:
            agent = service.create_agent(
                {
                    "name": "HTTP Generic Run Sensitive Id Agent",
                    "model_mode": "custom_api",
                    "model_config": {
                        "base_url": "https://api.example.test/v1",
                        "model": "demo-model",
                        "api_key": "sk-agent-secret",
                    },
                }
            )
            monkeypatch.setattr(
                run_route_module,
                "get_native_run_engine",
                lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
            )
            route_app = FastAPI()
            route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
            route_app.include_router(run_route_module.router)
            leaked_idempotency_key = "sk-post-runs-http-id-secret123456"

            with TestClient(route_app) as client:
                response = client.post(
                    "/runs",
                    json={"runnable_id": agent["agent_id"], "user_goal": "Run from HTTP"},
                    headers={"Idempotency-Key": leaked_idempotency_key},
                )

            body = json.dumps(response.json(), ensure_ascii=False)
            run_count = service._conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
            assert response.status_code == 400
            assert "client_run_id/idempotency_key" in body
            assert leaked_idempotency_key not in body
            assert run_count == 0
            assert verify_secret_redaction(paths=[tmp_path]) == []
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_runs_sensitive_id_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_screen_current_http_route_returns_structured_permission_error(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "screen.py"
        spec = importlib.util.spec_from_file_location("_oha_screen_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        screen_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = screen_route_module
        spec.loader.exec_module(screen_route_module)

        async def fake_capture():
            raise screenshot_mod.ScreenCapturePermissionError("没有屏幕录制权限，请授权")

        monkeypatch.setattr(screenshot_mod, "capture_screenshot", fake_capture)
        route_app = FastAPI()
        route_app.include_router(screen_route_module.router)

        with TestClient(route_app) as client:
            response = client.get("/screen/current")

        assert response.status_code == 403
        detail = response.json()["detail"]
        assert detail["error"] == "screen_capture_permission_denied"
        assert detail["message"].startswith("屏幕录制权限不足")
        assert "授权" in detail["detail"]
    finally:
        sys.modules.pop("_oha_screen_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_bridge_http_middleware_enforces_host_origin_and_session_token(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi", "uvicorn"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "token-123")
    try:
        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        server_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "server.py"
        spec = importlib.util.spec_from_file_location("_oha_bridge_server_http_under_test", server_path)
        assert spec is not None
        assert spec.loader is not None
        bridge_server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge_server
        spec.loader.exec_module(bridge_server)

        @bridge_server.app.get("/probe")
        async def get_probe():
            return {"ok": True}

        @bridge_server.app.post("/probe")
        async def post_probe():
            return {"ok": True}

        trusted_headers = {"host": "127.0.0.1:8420", "origin": "http://localhost:5174"}
        token_headers = {**trusted_headers, "X-Oha-Yachiyo-Bridge-Token": "token-123"}
        with TestClient(bridge_server.app) as client:
            get_response = client.get("/probe", headers=trusted_headers)
            bad_host_response = client.get(
                "/probe",
                headers={"host": "0.0.0.0:8420", "origin": "http://localhost:5174"},
            )
            bad_origin_response = client.get(
                "/probe",
                headers={"host": "127.0.0.1:8420", "origin": "https://evil.example"},
            )
            blocked_response = client.post("/probe", headers=trusted_headers)
            allowed_response = client.post("/probe", headers=token_headers)

        assert get_response.status_code == 200
        assert get_response.json() == {"ok": True}
        assert get_response.headers["access-control-allow-origin"] == "http://localhost:5174"
        assert bad_host_response.status_code == 403
        assert bad_host_response.json() == {"ok": False, "error": "untrusted_host"}
        assert bad_origin_response.status_code == 403
        assert bad_origin_response.json() == {"ok": False, "error": "untrusted_origin"}
        assert blocked_response.status_code == 403
        assert blocked_response.json() == {"ok": False, "error": "invalid_bridge_token"}
        assert allowed_response.status_code == 200
        assert allowed_response.json() == {"ok": True}
    finally:
        sys.modules.pop("_oha_bridge_server_http_under_test", None)
        _restore_module_prefixes(("fastapi", "uvicorn"), saved_modules)


def test_all_registered_mutating_routes_require_bridge_token(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi", "uvicorn", "apps.bridge.routes"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "token-123")
    try:
        try:
            from fastapi import APIRouter, routing as fastapi_routing
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        server_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "server.py"
        spec = importlib.util.spec_from_file_location("_oha_bridge_server_routes_under_test", server_path)
        assert spec is not None
        assert spec.loader is not None
        bridge_server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge_server
        spec.loader.exec_module(bridge_server)
        bridge_server._register_routes()

        # Keep hidden and nested routes in the security sweep. OpenAPI alone
        # omits include_in_schema=False endpoints and cannot prove coverage.
        nested = APIRouter()
        nested.add_api_route("/mutation", lambda: {"ok": True}, methods=["POST"], include_in_schema=False)
        parent = APIRouter()
        parent.include_router(nested, prefix="/security")
        bridge_server.app.include_router(parent, prefix="/_smoke")
        route_contexts = getattr(fastapi_routing, "iter_route_contexts", None)
        routes = route_contexts(bridge_server.app.routes) if callable(route_contexts) else bridge_server.app.routes

        mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
        trusted_headers = {"host": "127.0.0.1:8420", "origin": "http://localhost:5174"}
        checked: list[tuple[str, str]] = []
        failures: list[tuple[str, str, int, object]] = []

        with TestClient(bridge_server.app) as client:
            for route in routes:
                methods = sorted((getattr(route, "methods", None) or set()) & mutating_methods)
                path = getattr(route, "path", "")
                if not methods or not path:
                    continue
                sample_path = re.sub(r"\{[^{}]+?\}", "test-id", path)
                for method in methods:
                    response = client.request(method, sample_path, headers=trusted_headers)
                    checked.append((method, path))
                    if response.status_code != 403 or response.json().get("error") != "invalid_bridge_token":
                        failures.append((method, path, response.status_code, response.json()))

        assert len(checked) >= 80
        assert ("POST", "/_smoke/security/mutation") in checked
        assert failures == []
    finally:
        sys.modules.pop("_oha_bridge_server_routes_under_test", None)
        _restore_module_prefixes(("fastapi", "uvicorn", "apps.bridge.routes"), saved_modules)


def test_chat_message_http_route_maps_idempotency_key_header(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def send_message_in_session(
                self,
                session_id,
                text,
                attachments=None,
                *,
                runnable_id="",
                client_message_id="",
            ):
                assert session_id == fake_runtime.chat_session.session_id
                return {
                    "ok": True,
                    "text": text,
                    "attachments": attachments or [],
                    "runnable_id": runnable_id,
                    "client_message_id": client_message_id,
                }

        fake_runtime = SimpleNamespace(
            chat_session=SimpleNamespace(session_id="session-current")
        )
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/ui/chat/messages",
                json={"text": "hello"},
                headers={"Idempotency-Key": "header-message-1"},
            )

        assert response.status_code == 200
        assert response.json()["client_message_id"] == "header-message-1"
    finally:
        sys.modules.pop("_oha_ui_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_message_http_route_rejects_sensitive_idempotency_key_header(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    leaked_idempotency_key = "sk-http-chat-id-secret123456"
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_sensitive_id_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        session = ChatSession(session_id="http-sensitive-client-message-id-session")
        session.attach_store(store, load_existing=False)
        runtime = SimpleNamespace(state=AppState(), chat_session=session, store=store)
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: runtime)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/ui/chat/messages",
                json={"text": "hello"},
                headers={"Idempotency-Key": leaked_idempotency_key},
            )

        body = response.json()
        assert response.status_code == 200
        assert body["ok"] is False
        assert "client_message_id/idempotency_key" in body["error"]
        assert leaked_idempotency_key not in json.dumps(body, ensure_ascii=False)
        assert session.get_messages() == []
        assert runtime.state.list_tasks() == []
        assert verify_secret_redaction(paths=[tmp_path]) == []
    finally:
        store.close()
        sys.modules.pop("_oha_ui_sensitive_id_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_retry_http_route_returns_retry_projection(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_retry_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        calls: list[tuple[str, str]] = []

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def retry_message(self, message_id, *, client_message_id=""):
                calls.append(("retry_message", message_id, client_message_id))
                return {
                    "ok": True,
                    "message_id": message_id,
                    "client_message_id": client_message_id,
                    "task_id": "retry-task-1",
                    "status": "processing",
                }

        fake_runtime = SimpleNamespace()
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/ui/chat/messages/retry",
                json={
                    "message_id": "failed-message-1",
                    "client_message_id": "retry-client-1",
                },
            )

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "message_id": "failed-message-1",
            "client_message_id": "retry-client-1",
            "task_id": "retry-task-1",
            "status": "processing",
        }
        assert calls == [
            ("retry_message", "failed-message-1", "retry-client-1"),
        ]
    finally:
        sys.modules.pop("_oha_ui_retry_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_attachment_http_route_streams_file_and_maps_missing(monkeypatch, tmp_path):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_attachment_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        image_path = tmp_path / "screen.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
        calls: list[str] = []

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def get_attachment_file(self, attachment_id):
                calls.append(attachment_id)
                if attachment_id == "screen-image":
                    return {
                        "ok": True,
                        "path": str(image_path),
                        "mime_type": "image/png",
                        "name": "screen.png",
                    }
                return {"ok": False, "error": "附件不存在"}

        fake_runtime = SimpleNamespace()
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.get("/ui/chat/attachments/screen-image")
            missing = client.get("/ui/chat/attachments/missing-image")

        assert response.status_code == 200
        assert response.content == image_path.read_bytes()
        assert response.headers["content-type"].startswith("image/png")
        assert "screen.png" in response.headers.get("content-disposition", "")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "附件不存在"}
        assert calls == ["screen-image", "missing-image"]
    finally:
        sys.modules.pop("_oha_ui_attachment_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_session_http_routes_preserve_lifecycle_payloads(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_session_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        calls: list[tuple[str, dict[str, object]]] = []

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def get_messages_in_session(self, session_id, limit, anchor_message_id=""):
                assert session_id == fake_runtime.chat_session.session_id
                payload = {"limit": limit, "anchor_message_id": anchor_message_id}
                calls.append(("get_messages", payload))
                return {"ok": True, "messages": [], **payload}

            def get_session_info(self):
                calls.append(("get_session_info", {}))
                return {
                    "ok": True,
                    "session_id": "session-current",
                    "message_count": 2,
                    "is_processing": False,
                }

            def list_sessions(self, limit=20, query=""):
                payload = {"limit": limit, "query": query}
                calls.append(("list_sessions", payload))
                return {
                    "ok": True,
                    "current_session_id": "session-current",
                    "sessions": [{"session_id": "session-current", "title": query}],
                    **payload,
                }

            def load_session(self, session_id):
                calls.append(("load_session", {"session_id": session_id}))
                return {"ok": True, "session_id": session_id, "message_count": 4}

            def clear_session(self):
                calls.append(("clear_session", {}))
                fake_runtime.chat_session.session_id = "session-new"
                return {
                    "ok": True,
                    "session_id": "session-new",
                    "previous_session_id": "session-current",
                }

            def discard_empty_current_session(self, session_id):
                assert session_id == "session-new"
                calls.append(("discard_empty_current_session", {}))
                fake_runtime.chat_session.session_id = "session-current"
                return {
                    "ok": True,
                    "discarded": True,
                    "deleted_session_id": "session-new",
                    "session_id": "session-current",
                }

            def delete_session(self, session_id):
                assert session_id == "session-current"
                calls.append(("delete_current_session", {}))
                fake_runtime.chat_session.session_id = "session-after-delete"
                return {
                    "ok": True,
                    "deleted_session_id": "session-current",
                    "session_id": "session-after-delete",
                    "remaining_sessions": 0,
                }

        fake_runtime = SimpleNamespace(
            chat_session=SimpleNamespace(session_id="session-current")
        )
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            messages = client.get("/ui/chat/messages?limit=12&anchor_message_id=message-anchor")
            info = client.get("/ui/chat/session")
            sessions = client.get("/ui/chat/sessions?limit=3&query=NativeRunEngine")
            loaded = client.post("/ui/chat/sessions/load", json={"session_id": "session-archived"})
            cleared = client.post("/ui/chat/session/clear")
            discarded = client.post("/ui/chat/session/discard-empty")
            deleted = client.post("/ui/chat/session/delete")

        assert messages.status_code == 200
        assert messages.json() == {
            "ok": True,
            "messages": [],
            "limit": 12,
            "anchor_message_id": "message-anchor",
        }
        assert info.status_code == 200
        assert info.json()["session_id"] == "session-current"
        assert sessions.status_code == 200
        assert sessions.json()["limit"] == 3
        assert sessions.json()["query"] == "NativeRunEngine"
        assert sessions.json()["sessions"][0]["title"] == "NativeRunEngine"
        assert loaded.status_code == 200
        assert loaded.json() == {"ok": True, "session_id": "session-archived", "message_count": 4}
        assert cleared.status_code == 200
        assert cleared.json()["previous_session_id"] == "session-current"
        assert discarded.status_code == 200
        assert discarded.json()["discarded"] is True
        assert deleted.status_code == 200
        assert deleted.json()["session_id"] == "session-after-delete"
        assert calls == [
            ("get_messages", {"limit": 12, "anchor_message_id": "message-anchor"}),
            ("get_session_info", {}),
            ("list_sessions", {"limit": 3, "query": "NativeRunEngine"}),
            ("load_session", {"session_id": "session-archived"}),
            ("clear_session", {}),
            ("discard_empty_current_session", {}),
            ("delete_current_session", {}),
        ]
    finally:
        sys.modules.pop("_oha_ui_session_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_activity_http_routes_preserve_feed_detail_and_delete_payloads(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_activity_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        calls: list[tuple[str, dict[str, object]]] = []

        def fake_list_activity_events(**kwargs):
            calls.append(("list_activity_events", dict(kwargs)))
            return {
                "ok": True,
                "events": [{"event_id": "activity-1", "phase": "tool_start"}],
                "statuses": ["running"],
                "phases": ["tool_start"],
            }

        def fake_get_activity_event_detail(event_id, *, limit=200):
            calls.append(("get_activity_event_detail", {"event_id": event_id, "limit": limit}))
            return {
                "ok": True,
                "event": {"event_id": event_id},
                "trace": [{"event_id": "activity-trace-1"}],
            }

        def fake_delete_activity_event(event_id):
            calls.append(("delete_activity_event", {"event_id": event_id}))
            return {"ok": True, "deleted": True, "event_id": event_id}

        def fake_delete_activity_events(event_ids):
            calls.append(("delete_activity_events", {"event_ids": event_ids}))
            return {"ok": True, "deleted": len(event_ids), "requested": len(event_ids)}

        monkeypatch.setattr(ui_route_module, "list_activity_events", fake_list_activity_events)
        monkeypatch.setattr(ui_route_module, "get_activity_event_detail", fake_get_activity_event_detail)
        monkeypatch.setattr(ui_route_module, "delete_activity_event", fake_delete_activity_event)
        monkeypatch.setattr(ui_route_module, "delete_activity_events", fake_delete_activity_events)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            feed = client.get(
                "/ui/activity?"
                "query=terminal&status=running&tool=terminal.run&phase=tool_start&"
                "session_id=session-1&task_id=task-1&limit=25"
            )
            detail = client.get("/ui/activity/activity-1?limit=50")
            deleted_one = client.delete("/ui/activity/activity-1")
            deleted_many = client.request(
                "DELETE",
                "/ui/activity",
                json={"event_ids": ["activity-2", "activity-3"]},
            )

        assert feed.status_code == 200
        assert feed.json()["events"] == [{"event_id": "activity-1", "phase": "tool_start"}]
        assert feed.json()["statuses"] == ["running"]
        assert detail.status_code == 200
        assert detail.json()["trace"] == [{"event_id": "activity-trace-1"}]
        assert deleted_one.status_code == 200
        assert deleted_one.json() == {"ok": True, "deleted": True, "event_id": "activity-1"}
        assert deleted_many.status_code == 200
        assert deleted_many.json() == {"ok": True, "deleted": 2, "requested": 2}
        assert calls == [
            (
                "list_activity_events",
                {
                    "query": "terminal",
                    "status": "running",
                    "tool": "terminal.run",
                    "phase": "tool_start",
                    "session_id": "session-1",
                    "task_id": "task-1",
                    "limit": 25,
                },
            ),
            ("get_activity_event_detail", {"event_id": "activity-1", "limit": 50}),
            ("delete_activity_event", {"event_id": "activity-1"}),
            ("delete_activity_events", {"event_ids": ["activity-2", "activity-3"]}),
        ]
    finally:
        sys.modules.pop("_oha_ui_activity_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_cancel_http_route_returns_ui_cancel_projection(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_cancel_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def cancel_current_tasks(self):
                return {
                    "ok": True,
                    "cancelled_tasks": 1,
                    "processing_count": 0,
                    "is_processing": False,
                    "token_count": 128,
                    "messages": [
                        {
                            "id": "assistant-cancelled",
                            "role": "assistant",
                            "task_id": "task-cancelled",
                            "status": "failed",
                            "content": "任务已取消",
                        }
                    ],
                }

        fake_runtime = SimpleNamespace()
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.post("/ui/chat/session/cancel")

        payload = response.json()
        assert response.status_code == 200
        assert payload["ok"] is True
        assert payload["cancelled_tasks"] == 1
        assert payload["processing_count"] == 0
        assert payload["is_processing"] is False
        assert payload["token_count"] == 128
        assert payload["messages"][0]["task_id"] == "task-cancelled"
        assert payload["messages"][0]["status"] == "failed"
        assert "任务已取消" in payload["messages"][0]["content"]
    finally:
        sys.modules.pop("_oha_ui_cancel_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_message_image_attachment_http_roundtrip_maps_idempotency_and_file_response(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_chat_image_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        attachment_path = tmp_path / "screen.png"
        attachment_bytes = b"\x89PNG\r\n\x1a\nfake-image"
        attachment_path.write_bytes(attachment_bytes)
        calls: list[tuple[str, dict]] = []

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def send_message_in_session(
                self,
                session_id,
                text,
                attachments=None,
                *,
                runnable_id="",
                client_message_id="",
            ):
                assert session_id == fake_runtime.chat_session.session_id
                calls.append(
                    (
                        "send",
                        {
                            "text": text,
                            "attachments": attachments or [],
                            "runnable_id": runnable_id,
                            "client_message_id": client_message_id,
                        },
                    )
                )
                return {"ok": True, **calls[-1][1]}

            def get_attachment_file(self, attachment_id):
                calls.append(("get_attachment_file", {"attachment_id": attachment_id}))
                return {
                    "ok": True,
                    "path": str(attachment_path),
                    "mime_type": "image/png",
                    "name": "screen.png",
                }

        fake_runtime = SimpleNamespace(
            chat_session=SimpleNamespace(session_id="session-current")
        )
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        image_attachment = {
            "id": "pending-image",
            "name": "screen.png",
            "mime_type": "image/png",
            "data_url": "data:image/png;base64,ZmFrZS1pbWFnZQ==",
        }
        with TestClient(route_app) as client:
            sent = client.post(
                "/ui/chat/messages",
                json={
                    "text": "请看这张图",
                    "attachments": [image_attachment],
                    "runnable_id": "agent_design",
                },
                headers={"Idempotency-Key": "http-image-message-1"},
            )
            attachment = client.get("/ui/chat/attachments/pending-image")

        assert sent.status_code == 200
        assert sent.json() == {
            "ok": True,
            "text": "请看这张图",
            "attachments": [image_attachment],
            "runnable_id": "agent_design",
            "client_message_id": "http-image-message-1",
        }
        assert attachment.status_code == 200
        assert attachment.content == attachment_bytes
        assert attachment.headers["content-type"].startswith("image/png")
        assert "inline" in attachment.headers["content-disposition"]
        assert "screen.png" in attachment.headers["content-disposition"]
        assert calls == [
            (
                "send",
                {
                    "text": "请看这张图",
                    "attachments": [image_attachment],
                    "runnable_id": "agent_design",
                    "client_message_id": "http-image-message-1",
                },
            ),
            ("get_attachment_file", {"attachment_id": "pending-image"}),
        ]
    finally:
        sys.modules.pop("_oha_ui_chat_image_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_delegated_summary_http_route_returns_followup_projection(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_delegated_summary_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def summarize_delegated_run(self, run_id, *, conversation_id=""):
                assert run_id == "run_delegate_http"
                assert conversation_id == ""
                return {
                    "ok": True,
                    "summary_created": True,
                    "run_id": run_id,
                    "run_group_id": "run_group_delegate_http",
                    "run_status": "completed",
                    "source_task_id": "source-task-http",
                    "task_id": "summary-task-http",
                }

        fake_runtime = SimpleNamespace()
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            response = client.post(
                "/ui/chat/delegated-run-summary",
                json={"run_id": "run_delegate_http"},
            )

        payload = response.json()
        assert response.status_code == 200
        assert payload == {
            "ok": True,
            "summary_created": True,
            "run_id": "run_delegate_http",
            "run_group_id": "run_group_delegate_http",
            "run_status": "completed",
            "source_task_id": "source-task-http",
            "task_id": "summary-task-http",
        }
    finally:
        sys.modules.pop("_oha_ui_delegated_summary_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_group_http_routes_preserve_create_update_payloads(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_group_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        calls: list[tuple[str, dict[str, object]]] = []

        class FakeChatAPI:
            def __init__(self, runtime):
                assert runtime is fake_runtime

            def create_group_session(self, *, name="", avatar_url="", participant_ids=None):
                payload = {
                    "name": name,
                    "avatar_url": avatar_url,
                    "participant_ids": participant_ids or [],
                }
                calls.append(("create", payload))
                return {"ok": True, "session_id": "group-http-1", **payload}

            def update_group_session(self, session_id, *, name="", avatar_url="", participant_ids=None):
                payload = {
                    "session_id": session_id,
                    "name": name,
                    "avatar_url": avatar_url,
                    "participant_ids": participant_ids or [],
                }
                calls.append(("update", payload))
                return {"ok": True, **payload}

        fake_runtime = SimpleNamespace()
        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: fake_runtime)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        avatar = "data:image/png;base64,AAAA"
        with TestClient(route_app) as client:
            created = client.post(
                "/ui/chat/groups",
                json={
                    "name": "HTTP 产品群聊",
                    "avatar_url": avatar,
                    "participant_ids": ["agent_design", "agent_coding"],
                },
            )
            updated = client.patch(
                "/ui/chat/groups/group-http-1",
                json={
                    "name": "HTTP 产品群聊 v2",
                    "avatar_url": "https://example.test/group.png",
                    "participant_ids": ["agent_design"],
                },
            )

        assert created.status_code == 200
        assert created.json() == {
            "ok": True,
            "session_id": "group-http-1",
            "name": "HTTP 产品群聊",
            "avatar_url": avatar,
            "participant_ids": ["agent_design", "agent_coding"],
        }
        assert updated.status_code == 200
        assert updated.json() == {
            "ok": True,
            "session_id": "group-http-1",
            "name": "HTTP 产品群聊 v2",
            "avatar_url": "https://example.test/group.png",
            "participant_ids": ["agent_design"],
        }
        assert calls == [
            (
                "create",
                {
                    "name": "HTTP 产品群聊",
                    "avatar_url": avatar,
                    "participant_ids": ["agent_design", "agent_coding"],
                },
            ),
            (
                "update",
                {
                    "session_id": "group-http-1",
                    "name": "HTTP 产品群聊 v2",
                    "avatar_url": "https://example.test/group.png",
                    "participant_ids": ["agent_design"],
                },
            ),
        ]
    finally:
        sys.modules.pop("_oha_ui_group_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_launcher_http_routes_preserve_session_summary_and_quick_message(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "ui.py"
        spec = importlib.util.spec_from_file_location("_oha_ui_launcher_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        ui_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = ui_route_module
        spec.loader.exec_module(ui_route_module)

        calls: list[tuple[str, dict[str, object]]] = []
        runtime = SimpleNamespace(
            config=SimpleNamespace(
                bubble_mode=SimpleNamespace(
                    summary_count=2,
                    default_display="summary",
                    show_unread_dot=True,
                    auto_hide=False,
                    opacity=0.9,
                ),
                live2d_mode=SimpleNamespace(
                    show_reply_bubble=True,
                    enable_quick_input=True,
                    click_action="open_chat",
                    default_open_behavior="reply_bubble",
                ),
            )
        )

        class FakeChatBridge:
            def __init__(self, received_runtime):
                assert received_runtime is runtime

            def get_conversation_overview(self, summary_count, session_limit):
                calls.append(
                    (
                        "overview",
                        {"summary_count": summary_count, "session_limit": session_limit},
                    )
                )
                return {
                    "ok": True,
                    "session_id": "session-current",
                    "empty": False,
                    "is_processing": False,
                    "status_label": f"最近 {summary_count} 条",
                    "latest_reply": "短回复",
                    "latest_reply_full": "完整回复",
                    "recent_sessions": [
                        {
                            "session_id": "session-current",
                            "summary": "用户：保留会话总结；回复：已同步到 Launcher。",
                        }
                    ],
                }

            def send_quick_message(self, text, *, metadata=None):
                calls.append(("quick_message", {"text": text, "metadata": metadata}))
                return {
                    "ok": True,
                    "text": text,
                    "session_id": "session-current",
                    "agent_task": {
                        "task_id": "task-launcher",
                        "status": "running",
                        "open_in_studio_url": "#/agents?run_id=run-launcher",
                    },
                }

        class FakeChatAPI:
            def __init__(self, received_runtime):
                assert received_runtime is runtime

            def load_session(self, session_id):
                calls.append(("load_session", {"session_id": session_id}))
                return {"ok": True, "session_id": session_id}

        class FakeNotificationTracker:
            def update(self, chat, external_attention=False):
                calls.append(
                    (
                        "notification_update",
                        {
                            "latest_reply": chat.get("latest_reply", ""),
                            "external_attention": external_attention,
                        },
                    )
                )
                return {"has_unread": True, "latest_message": {"status": "completed"}}

            def acknowledge(self, chat=None):
                calls.append(
                    (
                        "notification_ack",
                        {"session_id": (chat or {}).get("session_id", "")},
                    )
                )

        monkeypatch.setattr(ui_route_module, "get_runtime", lambda: runtime)
        monkeypatch.setattr(ui_route_module, "ChatBridge", FakeChatBridge)
        monkeypatch.setattr(ui_route_module, "ChatAPI", FakeChatAPI)
        monkeypatch.setattr(ui_route_module, "LauncherNotificationTracker", FakeNotificationTracker)
        monkeypatch.setattr(
            ui_route_module,
            "_launcher_proactive_state",
            lambda _runtime, mode_id, _mode_config: {
                "ok": True,
                "status": "disabled",
                "mode": mode_id,
                "has_attention": False,
            },
        )
        monkeypatch.setattr(ui_route_module, "_maybe_trigger_proactive_tts", lambda *_args: {})
        monkeypatch.setattr(ui_route_module, "_bubble_avatar_url", lambda _config: "data:image/png;base64,AAAA")
        monkeypatch.setattr(ui_route_module, "_live2d_preview_url", lambda _config: "data:image/png;base64,BBBB")
        monkeypatch.setattr(
            ui_route_module,
            "_live2d_resource_payload",
            lambda _config: {"state": "ready", "status_label": "Live2D ready"},
        )
        monkeypatch.setattr(
            ui_route_module,
            "_live2d_renderer_payload",
            lambda _config, _resource: {"enabled": True, "model_url": "http://127.0.0.1/live2d.model3.json"},
        )
        ui_route_module._launcher_notifications.clear()
        ui_route_module._launcher_proactive_services.clear()
        route_app = FastAPI()
        route_app.include_router(ui_route_module.router)

        with TestClient(route_app) as client:
            bubble = client.get("/ui/launcher?mode=bubble")
            live2d = client.get("/ui/launcher?mode=live2d")
            ack = client.post("/ui/launcher/ack", json={"mode": "live2d"})
            quick = client.post(
                "/ui/launcher/quick-message",
                json={"mode": "live2d", "session_id": "session-current", "text": "继续整理会话"},
            )

        assert bubble.status_code == 200
        assert bubble.json()["mode"] == "bubble"
        assert bubble.json()["chat"]["recent_sessions"][0]["summary"] == "用户：保留会话总结；回复：已同步到 Launcher。"
        assert bubble.json()["launcher"]["status_label"] == "最近 2 条"
        assert bubble.json()["launcher"]["latest_reply"] == "短回复"
        assert bubble.json()["launcher"]["latest_status"] == "completed"
        assert bubble.json()["launcher"]["avatar_url"].startswith("data:image/")

        assert live2d.status_code == 200
        assert live2d.json()["mode"] == "live2d"
        assert live2d.json()["launcher"]["show_reply_bubble"] is True
        assert live2d.json()["launcher"]["enable_quick_input"] is True
        assert live2d.json()["launcher"]["status_label"] == "最近 3 条"
        assert live2d.json()["launcher"]["renderer"]["enabled"] is True

        assert ack.status_code == 200
        assert ack.json() == {"ok": True, "mode": "live2d", "session_id": "session-current"}
        assert quick.status_code == 200
        assert quick.json() == {
            "ok": True,
            "text": "继续整理会话",
            "session_id": "session-current",
            "agent_task": {
                "task_id": "task-launcher",
                "status": "running",
                "open_in_studio_url": "#/agents?run_id=run-launcher",
            },
        }
        assert calls == [
            ("overview", {"summary_count": 2, "session_limit": 3}),
            ("notification_update", {"latest_reply": "短回复", "external_attention": False}),
            ("overview", {"summary_count": 3, "session_limit": 3}),
            ("notification_update", {"latest_reply": "短回复", "external_attention": False}),
            ("overview", {"summary_count": 3, "session_limit": 3}),
            ("notification_ack", {"session_id": "session-current"}),
            ("load_session", {"session_id": "session-current"}),
            (
                "quick_message",
                {
                    "text": "继续整理会话",
                    "metadata": {
                        "source": "launcher",
                        "launcher_mode": "live2d",
                        "launcher_surface": "quick_message",
                        "runnable_kind": "main",
                    },
                },
            ),
        ]
    finally:
        sys.modules.pop("_oha_ui_launcher_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_chat_image_bridge_route_reaches_native_run_events(tmp_path, monkeypatch):
    try:
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
    except ModuleNotFoundError:
        FastAPI = None  # type: ignore[assignment]
        TestClient = None  # type: ignore[assignment]

    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_URL", "http://127.0.0.1:9999")
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="http-chat-image-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    captured_messages: list[list[dict]] = []
    image_bytes = b"fake-http-image"
    data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        captured_messages.append(messages)
        content = messages[-1]["content"]
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "请识别并分析这张图片。"}
        image_parts = [part for part in content if part.get("type") == "image_url"]
        assert len(image_parts) == 1
        assert image_parts[0]["image_url"]["url"] == data_url
        return {"role": "assistant", "content": "HTTP route image-only reply"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(chat_api_mod, "get_native_image_input_capability", lambda: {"can_attach_images": True, "route": "chat"})
    monkeypatch.setattr(
        "apps.shell.native_capabilities.get_native_image_input_capability",
        lambda: {"can_attach_images": True, "route": "chat"},
    )
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    try:
        if FastAPI is not None and TestClient is not None:
            route_app = FastAPI()
            route_app.include_router(ui_routes.router)
            route_app.include_router(run_routes.router)
            with TestClient(route_app) as client:
                sent = client.post(
                    "/ui/chat/messages",
                    json={
                        "text": "",
                        "attachments": [{"name": "screen.png", "data_url": data_url}],
                        "client_message_id": "http-image-client-1",
                    },
                )
                assert sent.status_code == 200
                payload = sent.json()
        else:
            payload = asyncio.run(
                ui_routes.send_chat_message(
                    ui_routes.SendChatMessageRequest(
                        text="",
                        attachments=[{"name": "screen.png", "data_url": data_url}],
                        client_message_id="http-image-client-1",
                    )
                )
            )

        assert payload["ok"] is True
        assert payload["status"] == "pending"
        assert payload["attachments"][0]["url"].startswith("http://127.0.0.1:9999/ui/chat/attachments/")
        assert "path" not in payload["attachments"][0]

        task = state.get_task(payload["task_id"])
        assert task is not None
        assert task.description == "请识别并分析这张图片。"
        assert task.attachments[0]["kind"] == "image"

        asyncio.run(runner._execute_with_state(task.task_id))

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.COMPLETED
        assert updated.result == "HTTP route image-only reply"
        run = service.get_run(service.get_task_run_link(task.task_id)["run_id"])

        if FastAPI is not None and TestClient is not None:
            route_app = FastAPI()
            route_app.include_router(ui_routes.router)
            route_app.include_router(run_routes.router)
            with TestClient(route_app) as client:
                replay_payload = client.get(f"/runs/{run['run_id']}/events?after_sequence=0&limit=200").json()
                messages_payload = client.get("/ui/chat/messages").json()
        else:
            replay_payload = asyncio.run(run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200))
            messages_payload = asyncio.run(ui_routes.get_chat_messages())

        assert len(captured_messages) == 1
        event_types = [event["event_type"] for event in replay_payload["events"]]
        assert "task.linked" in event_types
        assert event_types.count("model.output.completed") == 1
        assert "run.completed" in event_types
        user = next(message for message in messages_payload["messages"] if message["role"] == "user")
        assert user["content"] == "请识别并分析这张图片。"
        assert user["attachments"][0]["url"].startswith("http://127.0.0.1:9999/ui/chat/attachments/")
        assert "path" not in user["attachments"][0]
        assistant = next(message for message in messages_payload["messages"] if message["role"] == "assistant")
        assert assistant["task_id"] == task.task_id
        assert assistant["content"] == "HTTP route image-only reply"
        assert assistant["status"] == "completed"
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_approval_bridge_route_resumes_native_run(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    workdir = tmp_path / "workspace"
    (workdir / "src").mkdir(parents=True)
    target_file = workdir / "src" / "app.txt"
    target_file.write_text("before\n", encoding="utf-8")
    session = ChatSession(session_id="bridge-approval-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        if len(model_calls) == 1:
            assert any((tool.get("function") or {}).get("name") == "workspace_write_patch" for tool in tools or [])
            return {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_patch",
                        "type": "function",
                        "function": {
                            "name": "workspace_write_patch",
                            "arguments": json.dumps(
                                {
                                    "path": "src/app.txt",
                                    "patch": "--- src/app.txt\n+++ src/app.txt\n@@ -1 +1 @@\n-before\n+after\n",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
            }
        return {"role": "assistant", "content": "Bridge approval route finished."}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {
            "allowed_tools": ["workspace.read", "workspace.write_patch"],
            "approval_required": {"workspace.write_patch": True},
        },
        workspace_policy_getter=lambda: {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
            "writable_scopes": ["."],
        },
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def scenario():
        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="请把 src/app.txt 改成 after",
                client_message_id="bridge-approval-client-1",
            )
        )
        assert sent["ok"] is True
        task = state.get_task(sent["task_id"])
        assert task is not None
        runner_task = asyncio.create_task(runner._execute_with_state(task.task_id))
        try:
            run = None
            candidate = None
            for _ in range(150):
                try:
                    candidate = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
                except KeyError:
                    await asyncio.sleep(0.02)
                    continue
                if candidate["status"] == "approval_required":
                    run = candidate
                    break
                await asyncio.sleep(0.02)
            assert run is not None, (
                candidate.get("status"),
                candidate.get("result"),
                candidate.get("pending_approval"),
            )

            waiting_messages = await ui_routes.get_chat_messages()
            assistant = next(message for message in waiting_messages["messages"] if message["role"] == "assistant")
            assert waiting_messages["approval_count"] == 1
            assert assistant["status"] == "processing"
            assert assistant["metadata"]["run_id"] == run["run_id"]
            assert assistant["metadata"]["run_status"] == "approval_required"
            assert assistant["metadata"]["pending_approval"]["tool"] == "workspace.write_patch"

            approved = await agent_routes.approve_run_approval(
                run["run_id"],
                agent_routes.ApprovalRejectRequest(
                    approval_id=run["pending_approval"]["approval_id"]
                ),
            )
            assert approved["status"] == "running"
            await runner_task

            updated = state.get_task(task.task_id)
            assert updated is not None
            assert updated.status == TaskStatus.COMPLETED
            assert updated.result == "Bridge approval route finished."
            assert target_file.read_text(encoding="utf-8") == "after\n"

            replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
            event_types = [event["event_type"] for event in replay["events"]]
            assert "agent.tool.approval_required" in event_types
            assert "agent.tool.approval_approved" in event_types
            assert "agent.tool.call" in event_types
            assert "model.output.completed" in event_types
            assert "run.completed" in event_types

            final_messages = await ui_routes.get_chat_messages()
            final_assistant = next(
                message
                for message in final_messages["messages"]
                if message["role"] == "assistant" and message["task_id"] == task.task_id
            )
            assert final_messages["approval_count"] == 0
            assert final_assistant["status"] == "completed"
            assert final_assistant["content"] == "Bridge approval route finished."
            assert final_assistant["metadata"].get("pending_approval") == {}
        finally:
            if not runner_task.done():
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_approval_bridge_route_projects_failed_approved_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    service.custom_api_agent_loop._max_tool_iterations = 3
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    session = ChatSession(session_id="bridge-approval-failure-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []
    command_code = 'print("bridge-terminal-failure", end=""); raise SystemExit(7)'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(command_code)}"
    fixture_actions = [("terminal_run", {"command": command})]
    fixture_goal = f"Use the local terminal with these inputs. Execute `{command}`"

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        planning = semantic_fixture_model_plan(messages, tools, fixture_actions, fixture_goal)
        if planning is not None:
            return planning
        return {"content": "The requested process failed with exit code 7."}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {
            "allowed_tools": ["terminal.run"],
            "approval_required": {"terminal.run": True},
        },
        workspace_policy_getter=lambda: {
            "default_workdir": str(workdir),
            "readable_scopes": ["."],
        },
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def scenario():
        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text=fixture_goal,
                client_message_id="bridge-approval-failure-client-1",
            )
        )
        assert sent["ok"] is True
        task = state.get_task(sent["task_id"])
        assert task is not None
        run = None
        candidate = None
        for _ in range(150):
            try:
                candidate = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
            except KeyError:
                await asyncio.sleep(0.02)
                continue
            if candidate["status"] == "approval_required":
                run = candidate
                break
            await asyncio.sleep(0.02)
        assert run is not None, (
            candidate.get("status"),
            candidate.get("result"),
            candidate.get("pending_approval"),
        )

        waiting_messages = await ui_routes.get_chat_messages()
        assistant = next(message for message in waiting_messages["messages"] if message["role"] == "assistant")
        assert waiting_messages["approval_count"] == 1
        assert assistant["status"] == "processing"
        assert assistant["metadata"]["run_id"] == run["run_id"]
        assert assistant["metadata"]["run_status"] == "approval_required"
        assert assistant["metadata"]["pending_approval"]["tool"] == "terminal.run"

        approved = await agent_routes.approve_run_approval(
            run["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=run["pending_approval"]["approval_id"]
            ),
        )
        assert approved["status"] == "failed"
        assert approved["pending_approval"] == {}
        assert "terminal.run" in approved["result"]
        assert "退出码：7" in approved["result"]
        await ui_routes.get_chat_messages()

        updated = state.get_task(task.task_id)
        assert updated is not None
        assert updated.status == TaskStatus.FAILED
        assert updated.error is not None
        assert "退出码：7" in updated.error

        detail = await agent_routes.get_any_run(run["run_id"])
        assert detail["status"] == "failed"
        assert detail["pending_approval"] == {}
        assert detail["task_id"] == task.task_id
        assert detail["task_run_link_run_status"] == "failed"

        replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
        event_types = [event["event_type"] for event in replay["events"]]
        assert "agent.tool.approval_required" in event_types
        assert "agent.tool.approval_approved" in event_types
        assert "agent.tool.call" not in event_types
        private_events = service.list_run_events(run["run_id"], include_internal=True, limit=200)["events"]
        assert "run.failed" in event_types
        failed_fact = next(event for event in replay["events"] if event["event_type"] == "run.failed")
        assert "退出码：7" in failed_fact["payload"]["error"]
        tool_fact = next(
            event
            for event in private_events
            if event["event_type"] == "agent.tool.call" and event["payload"].get("approved") is True
        )
        assert tool_fact["payload"]["tool"] == "terminal.run"
        assert tool_fact["payload"]["result"]["ok"] is False
        assert tool_fact["payload"]["result"]["returncode"] == 7
        assert tool_fact["payload"]["result"]["stdout"] == "bridge-terminal-failure"

        final_messages = await ui_routes.get_chat_messages()
        final_assistant = next(
            message
            for message in final_messages["messages"]
            if message["role"] == "assistant" and message["task_id"] == task.task_id
        )
        assert final_messages["approval_count"] == 0
        assert final_assistant["status"] == "failed"
        assert final_assistant["metadata"].get("run_status") == "failed"
        assert final_assistant["metadata"].get("pending_approval") == {}
        assert len(model_calls) == 2

    try:
        asyncio.run(scenario())
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_delegated_summary_bridge_route_runs_native_followup(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-delegated-summary-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    captured_messages: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        captured_messages.append(messages)
        content = str(messages[-1].get("content") or "")
        assert "[Oha-Yachiyo 自动委派 Run 汇总]" in content
        assert "NativeRunEngine delegated route result." in content
        assert "run_oha_agent" not in content
        return {"role": "assistant", "content": "主模型总结：Native delegated route result 已整理。"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        activity_store=activity_store,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    try:
        run_group = service._insert_run_group(
            title="Bridge delegated summary",
            source="delegation",
        )
        delegated = service._insert_run(
            kind="agent_run",
            runnable_id="agent_bridge_summary",
            user_goal="整理 Bridge delegated summary evidence",
            run_group_id=run_group["run_group_id"],
        )
        service._update_run(
            delegated["run_id"],
            status="completed",
            result="NativeRunEngine delegated route result.",
            timeline=[
                {
                    "event": "agent.tool.call",
                    "detail": "artifact.write",
                    "input_preview": {"path": "reports/bridge-summary.md"},
                    "result": {"ok": True, "path": "reports/bridge-summary.md"},
                },
                {
                    "event": "agent.run.completed",
                    "detail": "done",
                    "result": "NativeRunEngine delegated route result.",
                },
            ],
            artifacts=[
                {"path": "agent-context.md", "kind": "context"},
                {"path": "reports/bridge-summary.md", "kind": "report"},
            ],
        )

        async def scenario():
            sent = await ui_routes.send_chat_message(
                ui_routes.SendChatMessageRequest(
                    text="请自动委派一个 Agent 整理 Bridge summary evidence",
                    client_message_id="bridge-delegated-summary-source",
                )
            )
            assert sent["ok"] is True
            source_task = state.get_task(sent["task_id"])
            assert source_task is not None
            if source_task.status not in {
                TaskStatus.COMPLETED,
                TaskStatus.CANCELLED,
                TaskStatus.FAILED,
            }:
                state.update_task_status(
                    source_task.task_id,
                    TaskStatus.COMPLETED,
                    result="我会交给 Bridge Summary Agent 处理。",
                )
            else:
                assert source_task.status == TaskStatus.COMPLETED
            session.upsert_assistant_message(
                task_id=source_task.task_id,
                content=(
                    "我会交给 Bridge Summary Agent 处理。\n"
                    '<oha_delegation>{"action":"run_oha_agent","agent":"Bridge Summary Agent",'
                    '"goal":"整理 Bridge summary evidence"}</oha_delegation>'
                ),
                status=MessageStatus.COMPLETED,
            )
            activity_store.record_event(
                session_id=session.session_id,
                task_id=source_task.task_id,
                tool_name="oha.delegation",
                phase="subagent",
                title="Bridge Summary Agent completed",
                detail=f"run_id={delegated['run_id']}",
                status="completed",
                metadata={
                    "run_id": delegated["run_id"],
                    "run_group_id": run_group["run_group_id"],
                    "run_status": "completed",
                },
            )

            summary = await ui_routes.summarize_delegated_run(
                ui_routes.SummarizeDelegatedRunRequest(run_id=delegated["run_id"])
            )
            repeat = await ui_routes.summarize_delegated_run(
                ui_routes.SummarizeDelegatedRunRequest(run_id=delegated["run_id"])
            )
            assert summary["ok"] is True
            assert summary["summary_created"] is True
            assert summary["run_status"] == "completed"
            assert summary["run_group_id"] == run_group["run_group_id"]
            assert summary["source_task_id"] == source_task.task_id
            assert repeat["summary_created"] is False
            assert repeat["message_id"] == summary["message_id"]
            assert repeat["task_id"] == summary["task_id"]
            assert repeat["run_group_id"] == run_group["run_group_id"]
            assert repeat["source_task_id"] == source_task.task_id
            summary_task = state.get_task(summary["task_id"])
            assert summary_task is not None
            assert summary_task.chat_session_id == session.session_id
            assert "[Oha-Yachiyo 自动委派 Run 汇总]" in summary_task.description
            assert "用户原始请求：请自动委派一个 Agent 整理 Bridge summary evidence" in summary_task.description
            assert "run_oha_agent" not in summary_task.description
            assert "agent_bridge_summary：已完成" in summary_task.description
            assert "reports/bridge-summary.md" in summary_task.description
            assert "agent-context.md" not in summary_task.description

            await runner._execute_with_state(summary_task.task_id)

            completed_task = state.get_task(summary_task.task_id)
            assert completed_task is not None
            assert completed_task.status == TaskStatus.COMPLETED
            assert completed_task.result == "主模型总结：Native delegated route result 已整理。"
            summary_link = service.get_task_run_link(summary_task.task_id)
            summary_run = service.get_run(summary_link["run_id"])
            detail = await agent_routes.get_any_run(summary_run["run_id"])
            replay = await run_routes.list_run_events(summary_run["run_id"], after_sequence=0, limit=200)
            replay_last_sequence = max(event["sequence"] for event in replay["events"])
            event_types = [event["event_type"] for event in replay["events"]]

            assert summary_link["task_id"] == summary_task.task_id
            assert summary_link["session_id"] == session.session_id
            assert summary_link["run_status"] == "completed"
            assert summary_link["last_event_sequence"] == replay_last_sequence
            assert summary_run["run_id"] != delegated["run_id"]
            assert detail["kind"] == "main_chat_run"
            assert detail["status"] == "completed"
            assert detail["task_id"] == summary_task.task_id
            assert detail["session_id"] == session.session_id
            assert detail["task_run_link_created_at"]
            assert detail["task_run_link_run_status"] == "completed"
            assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
            assert event_types.count("model.output.completed") == 1
            assert "task.linked" in event_types
            assert "run.completed" in event_types

            messages = await ui_routes.get_chat_messages()
            final_summary = next(
                message
                for message in messages["messages"]
                if message["task_id"] == summary_task.task_id
            )
            assert final_summary["status"] == "completed"
            assert final_summary["content"] == "主模型总结：Native delegated route result 已整理。"
            assert final_summary["metadata"]["delegated_run_summary_for_run_id"] == delegated["run_id"]
            assert final_summary["metadata"]["delegated_run_source_task_id"] == source_task.task_id
            assert final_summary["metadata"]["run_group_id"] == run_group["run_group_id"]
            assert final_summary["metadata"]["run_status"] == "completed"

        asyncio.run(scenario())
        assert len(captured_messages) == 1
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_group_dispatch_bridge_route_runs_native_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-group-dispatch-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组 Agent 汇总]" in last_content:
            assert "Coding：已完成" in last_content
            assert "汇报：Coding bridge dispatch result" in last_content
            return {"role": "assistant", "content": "群组总结：Coding 已完成 Bridge route Native 派发。"}
        if "# Agent\nName: Coding Agent" in last_content:
            assert "# User Goal\n做 Bridge route Native 群聊派发验证" in last_content
            assert "[Oha-Yachiyo 群组执行约定]" in last_content
            assert "你在群内身份是：Coding" in last_content
            return {"role": "assistant", "content": "Coding bridge dispatch result"}
        assert "请安排 Coding 做 Bridge route Native 群聊派发验证" in last_content
        assert "oha.group_dispatch" in str(messages[0]["content"])
        return {
            "role": "assistant",
            "content": (
                "我会让 Coding 处理这件事。\n"
                '{"tool":"oha.group_dispatch","input":{"tasks":[{"kind":"agent","target":"Coding",'
                '"goal":"做 Bridge route Native 群聊派发验证"}]}}'
            ),
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    coding = service.create_agent(
        {
            "name": "Coding Agent",
            "nickname": "Coding",
            "description": "runs native group dispatch route tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        activity_store=activity_store,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def wait_for(condition):
        last = None
        for _ in range(150):
            try:
                last = condition()
            except Exception as exc:
                last = exc
            if last:
                return last
            await asyncio.sleep(0.02)
        raise AssertionError(f"condition not met; last={last!r}")

    async def scenario():
        created = await ui_routes.create_chat_group(
            ui_routes.CreateChatGroupRequest(
                name="Bridge Native Dispatch Group",
                participant_ids=[coding["agent_id"]],
            )
        )
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"
        assert created["session_context"]["participants"][1]["id"] == coding["agent_id"]

        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="@主模型 请安排 Coding 做 Bridge route Native 群聊派发验证",
                client_message_id="bridge-group-dispatch-client-1",
            )
        )
        assert sent["ok"] is True
        await runner._execute_with_state(sent["task_id"])

        main_task = state.get_task(sent["task_id"])
        assert main_task is not None
        assert main_task.status == TaskStatus.COMPLETED
        main_run = service.get_run(service.get_task_run_link(sent["task_id"])["run_id"])
        assert main_run["kind"] == "main_chat_run"
        assert main_run["status"] == "completed"

        dispatch_payload = await ui_routes.get_chat_messages()
        parent = next(
            message
            for message in dispatch_payload["messages"]
            if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
        )
        agent_message = next(
            message
            for message in dispatch_payload["messages"]
            if message["role"] == "assistant"
            and message["metadata"].get("sender", {}).get("nickname") == "Coding"
        )
        assert parent["metadata"]["group_dispatch_count"] == 1
        assert parent["metadata"]["group_dispatch_run_group_id"] == agent_message["metadata"]["run_group_id"]
        assert "oha.group_dispatch" not in parent["content"]
        assert agent_message["metadata"]["runnable_id"] == coding["agent_id"]
        assert agent_message["metadata"]["delegated_by_task_id"] == sent["task_id"]
        assert agent_message["metadata"]["delegated_goal"] == "做 Bridge route Native 群聊派发验证"

        agent_run_id = agent_message["metadata"]["run_id"]
        agent_run = await wait_for(
            lambda: (
                service.get_run(agent_run_id)
                if service.get_run(agent_run_id)["status"] in {"completed", "failed", "cancelled", "approval_required"}
                else None
            )
        )
        assert agent_run["status"] == "completed"
        assert agent_run["runnable_id"] == coding["agent_id"]
        assert agent_run["result"] == "Coding bridge dispatch result"

        completed_agent = None
        for _ in range(150):
            payload = await ui_routes.get_chat_messages()
            completed_agent = next(
                (
                    message
                    for message in payload["messages"]
                    if message["role"] == "assistant"
                    and "Coding bridge dispatch result" in str(message["content"] or "")
                ),
                None,
            )
            if completed_agent is not None:
                break
            await asyncio.sleep(0.02)
        assert completed_agent is not None
        assert completed_agent["metadata"]["run_id"] == agent_run_id
        assert completed_agent["metadata"]["run_status"] == "completed"
        assert completed_agent["metadata"]["agent_report"] == "Coding bridge dispatch result"

        final_payload = await ui_routes.get_chat_messages()
        summary_message = next(
            message
            for message in final_payload["messages"]
            if message["metadata"].get("group_agent_summary_for_task_id") == sent["task_id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_message["status"] == "processing"
        assert summary_task is not None
        assert summary_task.chat_session_id == runtime.chat_session.session_id
        assert "[Oha-Yachiyo 群组 Agent 汇总]" in summary_task.description
        assert "Coding：已完成" in summary_task.description
        assert "汇报：Coding bridge dispatch result" in summary_task.description

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "群组总结：Coding 已完成 Bridge route Native 派发。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        detail = await agent_routes.get_any_run(summary_run["run_id"])
        replay = await run_routes.list_run_events(summary_run["run_id"], after_sequence=0, limit=200)
        replay_last_sequence = max(event["sequence"] for event in replay["events"])
        summary_event_types = [event["event_type"] for event in replay["events"]]
        assert summary_link["task_id"] == summary_task.task_id
        assert summary_link["session_id"] == runtime.chat_session.session_id
        assert summary_link["run_status"] == "completed"
        assert summary_link["last_event_sequence"] == replay_last_sequence
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["run_id"] != main_run["run_id"]
        assert detail["task_id"] == summary_task.task_id
        assert detail["session_id"] == runtime.chat_session.session_id
        assert detail["task_run_link_run_status"] == "completed"
        assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        settled_parent = None
        settled_summary = None
        for _ in range(150):
            settled_payload = await ui_routes.get_chat_messages()
            settled_parent = next(
                message
                for message in settled_payload["messages"]
                if message["role"] == "assistant" and message["task_id"] == sent["task_id"]
            )
            settled_summary = next(
                message
                for message in settled_payload["messages"]
                if message["task_id"] == summary_task.task_id
            )
            if (
                "group_agent_summary_pending" not in settled_parent["metadata"]
                and settled_parent["metadata"].get("group_agent_summary_status") == "completed"
                and settled_summary["status"] == "completed"
            ):
                break
            await asyncio.sleep(0.02)
        assert settled_parent is not None
        assert settled_summary is not None
        assert "group_agent_summary_pending" not in settled_parent["metadata"]
        assert settled_parent["metadata"]["group_agent_summary_status"] == "completed"
        assert settled_parent["metadata"]["group_agent_summary_task_id"] == summary_task.task_id
        assert settled_parent["metadata"]["group_dispatch_run_group_id"] == agent_message["metadata"]["run_group_id"]
        assert settled_summary["status"] == "completed"
        assert settled_summary["content"] == "群组总结：Coding 已完成 Bridge route Native 派发。"

    try:
        asyncio.run(scenario())
        assert len(model_calls) == 3
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_direct_group_agent_bridge_route_runs_native_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-direct-group-agent-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组直接 Agent 汇总]" in last_content:
            assert "Design：已完成" in last_content
            assert "汇报：Design bridge direct result" in last_content
            assert "不要再派发新的 Agent 任务" in last_content
            return {"role": "assistant", "content": "主模型整理：Design 的 Bridge 直接结果已归档。"}
        assert "# Agent\nName: Design Agent" in last_content
        assert "# User Goal\n做 Bridge route Native 直接点名验证" in last_content
        assert "[Oha-Yachiyo 群组执行约定]" in last_content
        assert "你在群内身份是：Design" in last_content
        return {"role": "assistant", "content": "Design bridge direct result"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    design = service.create_agent(
        {
            "name": "Design Agent",
            "nickname": "Design",
            "description": "runs native direct group bridge route tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        activity_store=activity_store,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def wait_for(condition):
        last = None
        for _ in range(150):
            try:
                last = condition()
            except Exception as exc:
                last = exc
            if last:
                return last
            await asyncio.sleep(0.02)
        raise AssertionError(f"condition not met; last={last!r}")

    async def scenario():
        created = await ui_routes.create_chat_group(
            ui_routes.CreateChatGroupRequest(
                name="Bridge Native Direct Group",
                participant_ids=[design["agent_id"]],
            )
        )
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"

        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="做 Bridge route Native 直接点名验证",
                runnable_id=design["agent_id"],
                client_message_id="bridge-direct-group-agent-client-1",
            )
        )
        assert sent["ok"] is True
        assert sent["agent_run_id"]

        agent_run = service.get_run(sent["agent_run_id"])
        for _ in range(150):
            if agent_run["status"] in {
                "completed",
                "failed",
                "cancelled",
                "approval_required",
            }:
                break
            await asyncio.sleep(0.02)
            agent_run = service.get_run(sent["agent_run_id"])
        else:
            event_facts = [
                (
                    event["event_type"],
                    (event.get("payload") or {}).get("tool"),
                )
                for event in service.list_run_events(agent_run["run_id"])["events"]
            ]
            raise AssertionError(
                "direct agent run did not become terminal: "
                f"{agent_run['status']} events={event_facts!r}"
            )
        assert agent_run["kind"] == "agent_run"
        assert agent_run["status"] == "completed"
        assert agent_run["result"] == "Design bridge direct result"

        completed_agent = None
        for _ in range(150):
            payload = await ui_routes.get_chat_messages()
            completed_agent = next(
                (
                    message
                    for message in payload["messages"]
                    if message["role"] == "assistant"
                    and message["metadata"].get("sender", {}).get("nickname") == "Design"
                    and message["metadata"].get("run_status") == "completed"
                ),
                None,
            )
            if completed_agent is not None:
                break
            await asyncio.sleep(0.02)
        assert completed_agent is not None
        assert completed_agent["metadata"]["run_id"] == sent["agent_run_id"]
        assert completed_agent["metadata"]["agent_report"] == "Design bridge direct result"
        assert completed_agent["metadata"]["group_agent_summary_pending"] is True

        summary_payload = await ui_routes.get_chat_messages()
        summary_message = next(
            message
            for message in summary_payload["messages"]
            if message["metadata"].get("group_direct_agent_summary_for_message_id") == completed_agent["id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_message["status"] == "processing"
        assert summary_task is not None
        assert summary_task.chat_session_id == runtime.chat_session.session_id
        assert "[Oha-Yachiyo 群组直接 Agent 汇总]" in summary_task.description
        assert "Design：已完成" in summary_task.description
        assert "汇报：Design bridge direct result" in summary_task.description

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "主模型整理：Design 的 Bridge 直接结果已归档。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        detail = await agent_routes.get_any_run(summary_run["run_id"])
        replay = await run_routes.list_run_events(summary_run["run_id"], after_sequence=0, limit=200)
        replay_last_sequence = max(event["sequence"] for event in replay["events"])
        summary_event_types = [event["event_type"] for event in replay["events"]]
        assert summary_link["task_id"] == summary_task.task_id
        assert summary_link["session_id"] == runtime.chat_session.session_id
        assert summary_link["run_status"] == "completed"
        assert summary_link["last_event_sequence"] == replay_last_sequence
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["run_id"] != agent_run["run_id"]
        assert detail["task_id"] == summary_task.task_id
        assert detail["session_id"] == runtime.chat_session.session_id
        assert detail["task_run_link_run_status"] == "completed"
        assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        final_agent = None
        final_summary = None
        for _ in range(150):
            final_payload = await ui_routes.get_chat_messages()
            final_agent = next(
                message
                for message in final_payload["messages"]
                if message["id"] == completed_agent["id"]
            )
            final_summary = next(
                message
                for message in final_payload["messages"]
                if message["task_id"] == summary_task.task_id
            )
            if (
                final_agent["metadata"].get("group_agent_summary_pending") is False
                and final_agent["metadata"].get("group_agent_summary_status") == "completed"
                and final_summary["status"] == "completed"
            ):
                break
            await asyncio.sleep(0.02)
        assert final_agent is not None
        assert final_summary is not None
        assert final_agent["metadata"]["group_agent_summary_pending"] is False
        assert final_agent["metadata"]["group_agent_summary_status"] == "completed"
        assert final_summary["status"] == "completed"
        assert final_summary["content"] == "主模型整理：Design 的 Bridge 直接结果已归档。"

    try:
        asyncio.run(scenario())
        assert len(model_calls) == 2
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_direct_group_agent_bridge_route_runs_rejected_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-direct-group-agent-rejected-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组直接 Agent 汇总]" in last_content:
            assert "Design：已取消" in last_content
            assert "汇报：工具审批已拒绝：Rejected from Bridge direct group" in last_content
            assert "不要再派发新的 Agent 任务" in last_content
            return {"role": "assistant", "content": "主模型整理：Design 的 Bridge 审批拒绝已告知用户。"}
        assert "# Agent\nName: Design Agent" in last_content
        assert "# User Goal\n运行 Bridge route 需要审批的终端命令" in last_content
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_bridge_direct_terminal",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps(
                            {
                                "command": "printf should-not-run",
                                "timeout_seconds": 30,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    design = service.create_agent(
        {
            "name": "Design Agent",
            "nickname": "Design",
            "description": "runs native rejected direct group bridge route tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
            "tool_policy": {
                "allowed_tools": ["terminal.run"],
                "approval_required": {"terminal.run": True},
            },
            "workspace_policy": {
                "default_workdir": str(tmp_path),
                "readable_scopes": ["."],
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        activity_store=activity_store,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def wait_for(condition):
        last = None
        for _ in range(150):
            try:
                last = condition()
            except Exception as exc:
                last = exc
            if last:
                return last
            await asyncio.sleep(0.02)
        raise AssertionError(f"condition not met; last={last!r}")

    async def scenario():
        created = await ui_routes.create_chat_group(
            ui_routes.CreateChatGroupRequest(
                name="Bridge Native Direct Reject Group",
                participant_ids=[design["agent_id"]],
            )
        )
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"

        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="运行 Bridge route 需要审批的终端命令",
                runnable_id=design["agent_id"],
                client_message_id="bridge-direct-group-agent-rejected-client-1",
            )
        )
        assert sent["ok"] is True
        assert sent["agent_run_id"]

        waiting = await wait_for(
            lambda: (
                service.get_run(sent["agent_run_id"])
                if service.get_run(sent["agent_run_id"])["status"] == "approval_required"
                else None
            )
        )
        assert waiting["kind"] == "agent_run"
        assert waiting["pending_approval"]["tool"] == "terminal.run"

        rejected = await agent_routes.reject_run_approval(
            waiting["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=waiting["pending_approval"]["approval_id"],
                reason="Rejected from Bridge direct group",
            ),
        )
        assert rejected["status"] == "cancelled"
        assert rejected["result"] == "工具审批已拒绝：Rejected from Bridge direct group"
        rejected_replay = await run_routes.list_run_events(waiting["run_id"], after_sequence=0, limit=200)
        rejected_event_types = [event["event_type"] for event in rejected_replay["events"]]
        assert "agent.tool.approval_required" in rejected_event_types
        assert "agent.tool.approval_rejected" in rejected_event_types
        assert "agent.run.cancelled" in rejected_event_types

        rejected_agent = None
        for _ in range(150):
            payload = await ui_routes.get_chat_messages()
            rejected_agent = next(
                (
                    message
                    for message in payload["messages"]
                    if message["role"] == "assistant"
                    and message["metadata"].get("sender", {}).get("nickname") == "Design"
                    and message["metadata"].get("run_status") == "cancelled"
                ),
                None,
            )
            if rejected_agent is not None:
                break
            await asyncio.sleep(0.02)
        assert rejected_agent is not None
        assert rejected_agent["status"] == "failed"
        assert rejected_agent["metadata"]["run_id"] == sent["agent_run_id"]
        assert rejected_agent["metadata"]["agent_report"] == "工具审批已拒绝：Rejected from Bridge direct group"
        assert rejected_agent["metadata"]["group_agent_summary_pending"] is True

        summary_payload = await ui_routes.get_chat_messages()
        summary_message = next(
            message
            for message in summary_payload["messages"]
            if message["metadata"].get("group_direct_agent_summary_for_message_id") == rejected_agent["id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_message["status"] == "processing"
        assert summary_task is not None
        assert summary_task.chat_session_id == runtime.chat_session.session_id
        assert "[Oha-Yachiyo 群组直接 Agent 汇总]" in summary_task.description
        assert "Design：已取消" in summary_task.description
        assert "汇报：工具审批已拒绝：Rejected from Bridge direct group" in summary_task.description

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "主模型整理：Design 的 Bridge 审批拒绝已告知用户。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        detail = await agent_routes.get_any_run(summary_run["run_id"])
        replay = await run_routes.list_run_events(summary_run["run_id"], after_sequence=0, limit=200)
        replay_last_sequence = max(event["sequence"] for event in replay["events"])
        summary_event_types = [event["event_type"] for event in replay["events"]]
        assert summary_link["task_id"] == summary_task.task_id
        assert summary_link["session_id"] == runtime.chat_session.session_id
        assert summary_link["run_status"] == "completed"
        assert summary_link["last_event_sequence"] == replay_last_sequence
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["run_id"] != waiting["run_id"]
        assert detail["task_id"] == summary_task.task_id
        assert detail["session_id"] == runtime.chat_session.session_id
        assert detail["task_run_link_run_status"] == "completed"
        assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        final_agent = None
        final_summary = None
        for _ in range(150):
            final_payload = await ui_routes.get_chat_messages()
            final_agent = next(
                message
                for message in final_payload["messages"]
                if message["id"] == rejected_agent["id"]
            )
            final_summary = next(
                message
                for message in final_payload["messages"]
                if message["task_id"] == summary_task.task_id
            )
            if (
                final_agent["metadata"].get("group_agent_summary_pending") is False
                and final_agent["metadata"].get("group_agent_summary_status") == "completed"
                and final_summary["status"] == "completed"
            ):
                break
            await asyncio.sleep(0.02)
        assert final_agent is not None
        assert final_summary is not None
        assert final_agent["metadata"]["group_agent_summary_pending"] is False
        assert final_agent["metadata"]["group_agent_summary_status"] == "completed"
        assert final_summary["status"] == "completed"
        assert final_summary["content"] == "主模型整理：Design 的 Bridge 审批拒绝已告知用户。"

    try:
        asyncio.run(scenario())
        assert len(model_calls) == 2
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_direct_group_agent_bridge_route_runs_approved_summary(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-direct-group-agent-approved-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        last_content = str(messages[-1]["content"])
        if "[Oha-Yachiyo 群组直接 Agent 汇总]" in last_content:
            assert "Design：已完成" in last_content
            assert "汇报：Design bridge approved result" in last_content
            assert "不要再派发新的 Agent 任务" in last_content
            return {"role": "assistant", "content": "主模型整理：Design 的 Bridge 审批通过结果已归档。"}
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if tool_messages:
            assert tool_messages[-1]["tool_call_id"] == "call_bridge_direct_terminal_approve"
            assert "bridge-approved" in str(tool_messages[-1]["content"])
            return {"role": "assistant", "content": "Design bridge approved result"}
        assert "# Agent\nName: Design Agent" in last_content
        assert "# User Goal\n运行 Bridge route 需要批准的终端命令" in last_content
        assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_bridge_direct_terminal_approve",
                    "type": "function",
                    "function": {
                        "name": "terminal_run",
                        "arguments": json.dumps(
                            {
                                "command": "printf bridge-approved",
                                "timeout_seconds": 30,
                            },
                            ensure_ascii=False,
                        ),
                    },
                }
            ],
        }

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    design = service.create_agent(
        {
            "name": "Design Agent",
            "nickname": "Design",
            "description": "runs native approved direct group bridge route tests",
            "model_mode": "custom_api",
            "model_config": {
                "base_url": "https://api.example.test/v1",
                "model": "demo-model",
                "api_key": "sk-secret",
            },
            "tool_policy": {
                "allowed_tools": ["terminal.run"],
                "approval_required": {"terminal.run": True},
            },
            "workspace_policy": {
                "default_workdir": str(tmp_path),
                "readable_scopes": ["."],
            },
        }
    )
    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        activity_store=activity_store,
        agent_runtime_service=service,
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def wait_for(condition):
        last = None
        for _ in range(150):
            try:
                last = condition()
            except Exception as exc:
                last = exc
            if last:
                return last
            await asyncio.sleep(0.02)
        raise AssertionError(f"condition not met; last={last!r}")

    async def scenario():
        created = await ui_routes.create_chat_group(
            ui_routes.CreateChatGroupRequest(
                name="Bridge Native Direct Approve Group",
                participant_ids=[design["agent_id"]],
            )
        )
        assert created["ok"] is True
        assert created["session_context"]["conversation_kind"] == "group"

        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="运行 Bridge route 需要批准的终端命令",
                runnable_id=design["agent_id"],
                client_message_id="bridge-direct-group-agent-approved-client-1",
            )
        )
        assert sent["ok"] is True
        assert sent["agent_run_id"]

        waiting = await wait_for(
            lambda: (
                service.get_run(sent["agent_run_id"])
                if service.get_run(sent["agent_run_id"])["status"] == "approval_required"
                else None
            )
        )
        assert waiting["kind"] == "agent_run"
        assert waiting["pending_approval"]["tool"] == "terminal.run"
        assert waiting["pending_approval"]["input_preview"]["command"] == "printf bridge-approved"

        approved = await agent_routes.approve_run_approval(
            waiting["run_id"],
            agent_routes.ApprovalRejectRequest(
                approval_id=waiting["pending_approval"]["approval_id"]
            ),
        )
        assert approved["status"] == "completed"
        assert approved["result"] == "Design bridge approved result"
        approved_replay = await run_routes.list_run_events(waiting["run_id"], after_sequence=0, limit=200)
        approved_event_types = [event["event_type"] for event in approved_replay["events"]]
        assert "agent.tool.approval_required" in approved_event_types
        assert "agent.tool.approval_approved" in approved_event_types
        assert "agent.tool.call" in approved_event_types
        assert "agent.run.completed" in approved_event_types
        tool_fact = next(
            event
            for event in approved_replay["events"]
            if event["event_type"] == "agent.tool.call" and event["payload"].get("approved") is True
        )
        assert tool_fact["payload"]["tool"] == "terminal.run"
        assert "bridge-approved" in str(tool_fact["payload"]["result"])

        approved_agent = None
        for _ in range(150):
            payload = await ui_routes.get_chat_messages()
            approved_agent = next(
                (
                    message
                    for message in payload["messages"]
                    if message["role"] == "assistant"
                    and message["metadata"].get("sender", {}).get("nickname") == "Design"
                    and message["metadata"].get("run_status") == "completed"
                ),
                None,
            )
            if approved_agent is not None:
                break
            await asyncio.sleep(0.02)
        assert approved_agent is not None
        assert approved_agent["status"] == "completed"
        assert approved_agent["metadata"]["run_id"] == sent["agent_run_id"]
        assert approved_agent["metadata"]["agent_report"] == "Design bridge approved result"
        assert approved_agent["metadata"]["group_agent_summary_pending"] is True

        summary_payload = await ui_routes.get_chat_messages()
        summary_message = next(
            message
            for message in summary_payload["messages"]
            if message["metadata"].get("group_direct_agent_summary_for_message_id") == approved_agent["id"]
        )
        summary_task = state.get_task(summary_message["task_id"])
        assert summary_message["status"] == "processing"
        assert summary_task is not None
        assert summary_task.chat_session_id == runtime.chat_session.session_id
        assert "[Oha-Yachiyo 群组直接 Agent 汇总]" in summary_task.description
        assert "Design：已完成" in summary_task.description
        assert "汇报：Design bridge approved result" in summary_task.description

        await runner._execute_with_state(summary_task.task_id)

        completed_summary_task = state.get_task(summary_task.task_id)
        assert completed_summary_task is not None
        assert completed_summary_task.status == TaskStatus.COMPLETED
        assert completed_summary_task.result == "主模型整理：Design 的 Bridge 审批通过结果已归档。"
        summary_link = service.get_task_run_link(summary_task.task_id)
        summary_run = service.get_run(summary_link["run_id"])
        detail = await agent_routes.get_any_run(summary_run["run_id"])
        replay = await run_routes.list_run_events(summary_run["run_id"], after_sequence=0, limit=200)
        replay_last_sequence = max(event["sequence"] for event in replay["events"])
        summary_event_types = [event["event_type"] for event in replay["events"]]
        assert summary_link["task_id"] == summary_task.task_id
        assert summary_link["session_id"] == runtime.chat_session.session_id
        assert summary_link["run_status"] == "completed"
        assert summary_link["last_event_sequence"] == replay_last_sequence
        assert summary_run["kind"] == "main_chat_run"
        assert summary_run["status"] == "completed"
        assert summary_run["run_id"] != waiting["run_id"]
        assert detail["task_id"] == summary_task.task_id
        assert detail["session_id"] == runtime.chat_session.session_id
        assert detail["task_run_link_run_status"] == "completed"
        assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
        assert "task.linked" in summary_event_types
        assert summary_event_types.count("model.output.completed") == 1
        assert "run.completed" in summary_event_types

        final_agent = None
        final_summary = None
        for _ in range(150):
            final_payload = await ui_routes.get_chat_messages()
            final_agent = next(
                message
                for message in final_payload["messages"]
                if message["id"] == approved_agent["id"]
            )
            final_summary = next(
                message
                for message in final_payload["messages"]
                if message["task_id"] == summary_task.task_id
            )
            if (
                final_agent["metadata"].get("group_agent_summary_pending") is False
                and final_agent["metadata"].get("group_agent_summary_status") == "completed"
                and final_summary["status"] == "completed"
            ):
                break
            await asyncio.sleep(0.02)
        assert final_agent is not None
        assert final_summary is not None
        assert final_agent["metadata"]["group_agent_summary_pending"] is False
        assert final_agent["metadata"]["group_agent_summary_status"] == "completed"
        assert final_summary["status"] == "completed"
        assert final_summary["content"] == "主模型整理：Design 的 Bridge 审批通过结果已归档。"

    try:
        asyncio.run(scenario())
        assert len(model_calls) == 3
    finally:
        service.close()
        activity_store.close()
        store.close()


def test_chat_cancel_bridge_route_cancels_native_run_and_ignores_late_output(tmp_path, monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_HOME", str(tmp_path / "oha-yachiyo-home"))
    store = ChatStore(db_path=str(tmp_path / "chat.db"))
    activity_store = ActivityStore(db_path=str(tmp_path / "activity.db"))
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "agent-runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    session = ChatSession(session_id="bridge-cancel-session")
    session.attach_store(store, load_existing=False)
    state = AppState()
    model_started = threading.Event()
    release_model = threading.Event()
    model_calls: list[list[dict]] = []

    def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
        model_calls.append(messages)
        model_started.set()
        assert release_model.wait(timeout=3), "test did not release slow model call"
        return {"role": "assistant", "content": "late model output should be ignored"}

    monkeypatch.setattr(chat_store_mod, "get_chat_store", lambda: store)
    monkeypatch.setattr(activity_store_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_activity_store", lambda: activity_store)
    monkeypatch.setattr(chat_api_mod, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    monkeypatch.setattr(run_routes, "get_native_run_engine", lambda: service)
    monkeypatch.setattr(
        "apps.shell.agent_runtime.get_model_profile_service",
        lambda: _FakeDefaultProfileService(),
    )
    monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)

    executor = NativeAgentExecutor(
        chat_session=session,
        runtime_service_getter=lambda: service,
        tool_policy_getter=lambda: {"allowed_tools": []},
        workspace_policy_getter=lambda: {},
        activity_store_getter=lambda: activity_store,
    )
    runner = TaskRunner(state, executor=executor, activity_store=activity_store)
    runtime = SimpleNamespace(
        state=state,
        chat_session=session,
        store=store,
        task_runner=runner,
        agent_runtime_service=service,
        cancel_task_runner_task=lambda task_id: runner.cancel_task(task_id),
    )
    monkeypatch.setattr(ui_routes, "get_runtime", lambda: runtime)

    async def scenario():
        sent = await ui_routes.send_chat_message(
            ui_routes.SendChatMessageRequest(
                text="请开始一个可以被停止的慢请求",
                client_message_id="bridge-cancel-client-1",
            )
        )
        assert sent["ok"] is True
        task = state.get_task(sent["task_id"])
        assert task is not None
        runner_task = asyncio.create_task(runner._execute_with_state(task.task_id))
        runner._in_progress[task.task_id] = runner_task
        runner_task.add_done_callback(lambda _future: runner._in_progress.pop(task.task_id, None))
        try:
            if not await asyncio.to_thread(model_started.wait, 3):
                link = service.get_task_run_link(task.task_id)
                stalled_run = service.get_run(link["run_id"])
                event_facts = [
                    (
                        event["event_type"],
                        (event.get("payload") or {}).get("tool"),
                    )
                    for event in service.list_run_events(stalled_run["run_id"])["events"]
                ]
                raise AssertionError(
                    "slow model call did not start: "
                    f"status={stalled_run['status']} events={event_facts!r}"
                )
            run = service.get_run(service.get_task_run_link(task.task_id)["run_id"])
            assert run["status"] == "running"

            cancelled = await ui_routes.cancel_chat_session_tasks()
            assert cancelled["ok"] is True
            assert cancelled["cancelled_tasks"] == 1
            assert cancelled["processing_count"] == 0
            assert cancelled["is_processing"] is False

            cancelled_task = state.get_task(task.task_id)
            assert cancelled_task is not None
            assert cancelled_task.status == TaskStatus.CANCELLED
            cancelled_assistant = next(message for message in cancelled["messages"] if message["role"] == "assistant")
            assert cancelled_assistant["task_id"] == task.task_id
            assert cancelled_assistant["status"] == "failed"
            assert "任务已取消" in cancelled_assistant["content"]

            for _ in range(150):
                stored = service.get_run(run["run_id"])
                if stored["status"] == "cancelled":
                    break
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("linked Native Run did not enter cancelled state")

            release_model.set()
            await asyncio.gather(runner_task, return_exceptions=True)

            replay = await run_routes.list_run_events(run["run_id"], after_sequence=0, limit=200)
            event_types = [event["event_type"] for event in replay["events"]]
            stored = service.get_run(run["run_id"])
            assert stored["status"] == "cancelled"
            assert "run.cancelled" in event_types
            assert "model.output.completed" not in event_types
            assert "run.completed" not in event_types

            listed_runs = await agent_routes.list_runs(limit=20)
            detail = await agent_routes.get_any_run(run["run_id"])
            listed = next(item for item in listed_runs["runs"] if item["run_id"] == run["run_id"])
            replay_last_sequence = replay["events"][-1]["sequence"]
            assert listed["kind"] == "main_chat_run"
            assert listed["status"] == "cancelled"
            assert listed["task_id"] == task.task_id
            assert listed["session_id"] == session.session_id
            assert listed["task_run_link_created_at"]
            assert listed["task_run_link_run_status"] == "cancelled"
            assert listed["task_run_link_last_event_sequence"] == replay_last_sequence
            assert detail["run_id"] == run["run_id"]
            assert detail["kind"] == "main_chat_run"
            assert detail["status"] == "cancelled"
            assert detail["task_id"] == task.task_id
            assert detail["session_id"] == session.session_id
            assert detail["task_run_link_created_at"]
            assert detail["task_run_link_run_status"] == "cancelled"
            assert detail["task_run_link_last_event_sequence"] == replay_last_sequence
            assert any(event.get("event") == "run.cancelled" for event in detail["timeline"])

            final_messages = await ui_routes.get_chat_messages()
            final_assistant = next(message for message in final_messages["messages"] if message["role"] == "assistant")
            assert final_messages["processing_count"] == 0
            assert final_assistant["task_id"] == task.task_id
            assert final_assistant["status"] == "failed"
            assert "late model output" not in final_assistant["content"]
        finally:
            release_model.set()
            if not runner_task.done():
                runner_task.cancel()
                await asyncio.gather(runner_task, return_exceptions=True)

    try:
        asyncio.run(scenario())
        assert len(model_calls) == 1
    finally:
        release_model.set()
        service.close()
        activity_store.close()
        store.close()


def test_agent_and_workflow_run_http_routes_map_idempotency_key_header(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_agent_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        class FakeRuntimeService:
            def __init__(self):
                self.calls: list[tuple[str, dict]] = []

            def create_agent_run(self, payload):
                self.calls.append(("create_agent_run", dict(payload)))
                return {"ok": True, "client_run_id": payload.get("client_run_id", "")}

            def create_workflow_run(self, payload):
                self.calls.append(("create_workflow_run", dict(payload)))
                return {"ok": True, "client_run_id": payload.get("client_run_id", "")}

            def list_runs(self, limit):
                self.calls.append(("list_runs", {"limit": limit}))
                return {"runs": [{"run_id": "run-1"}], "limit": limit}

            def get_run(self, run_id):
                self.calls.append(("get_run", {"run_id": run_id}))
                return {"run_id": run_id, "status": "approval_required"}

            def approve_run_approval(self, run_id, approval_id):
                self.calls.append(
                    (
                        "approve_run_approval",
                        {"run_id": run_id, "approval_id": approval_id},
                    )
                )
                return {"run_id": run_id, "status": "running"}

            def reject_run_approval(self, run_id, reason, approval_id):
                self.calls.append(
                    (
                        "reject_run_approval",
                        {
                            "run_id": run_id,
                            "reason": reason,
                            "approval_id": approval_id,
                        },
                    )
                )
                return {"run_id": run_id, "status": "cancelled", "result": reason}

            def cancel_run(self, run_id):
                self.calls.append(("cancel_run", {"run_id": run_id}))
                return {"run_id": run_id, "status": "cancelled"}

        service = FakeRuntimeService()
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)

        with TestClient(route_app) as client:
            agent_response = client.post(
                "/ui/agent-runs",
                json={"agent_id": "agent-1", "user_goal": "hello"},
                headers={"Idempotency-Key": "header-run-1"},
            )
            workflow_response = client.post(
                "/ui/workflow-runs",
                json={"workflow_id": "workflow-1", "user_goal": "hello"},
                headers={"Idempotency-Key": "header-workflow-run-1"},
            )
            runs_response = client.get("/ui/runs?limit=7")
            detail_response = client.get("/ui/runs/run-1")
            approve_response = client.post(
                "/ui/runs/run-1/approval/approve",
                json={"approval_id": "approval-1"},
            )
            reject_response = client.post(
                "/ui/runs/run-1/approval/reject",
                json={
                    "approval_id": "approval-1",
                    "reason": "Rejected from HTTP",
                },
            )
            cancel_response = client.post("/ui/runs/run-1/cancel")

        assert agent_response.status_code == 200
        assert agent_response.json()["client_run_id"] == "header-run-1"
        assert workflow_response.status_code == 200
        assert workflow_response.json()["client_run_id"] == "header-workflow-run-1"
        assert runs_response.status_code == 200
        assert runs_response.json()["limit"] == 7
        assert detail_response.status_code == 200
        assert detail_response.json()["run_id"] == "run-1"
        assert approve_response.status_code == 200
        assert approve_response.json()["status"] == "running"
        assert reject_response.status_code == 200
        assert reject_response.json()["result"] == "Rejected from HTTP"
        assert cancel_response.status_code == 200
        assert cancel_response.json()["status"] == "cancelled"
        assert service.calls == [
            (
                "create_agent_run",
                {
                    "agent_id": "agent-1",
                    "user_goal": "hello",
                    "client_run_id": "header-run-1",
                },
            ),
            (
                "create_workflow_run",
                {
                    "workflow_id": "workflow-1",
                    "user_goal": "hello",
                    "client_run_id": "header-workflow-run-1",
                },
            ),
            ("list_runs", {"limit": 7}),
            ("get_run", {"run_id": "run-1"}),
            (
                "approve_run_approval",
                {"run_id": "run-1", "approval_id": "approval-1"},
            ),
            (
                "reject_run_approval",
                {
                    "run_id": "run-1",
                    "reason": "Rejected from HTTP",
                    "approval_id": "approval-1",
                },
            ),
            ("cancel_run", {"run_id": "run-1"}),
        ]
    finally:
        sys.modules.pop("_oha_agent_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_public_run_routes_reject_run_group_attachment(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location(
            "_oha_run_group_authority_route_under_test",
            route_path,
        )
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        class FakeRuntimeService:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict]] = []

            def create_agent_run(self, payload: dict) -> dict:
                self.calls.append(("create_agent_run", dict(payload)))
                return {"ok": True}

            def create_workflow_run(self, payload: dict) -> dict:
                self.calls.append(("create_workflow_run", dict(payload)))
                return {"ok": True}

        service = FakeRuntimeService()
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)

        with TestClient(route_app) as client:
            agent_response = client.post(
                "/ui/agent-runs",
                json={
                    "agent_id": "agent-1",
                    "user_goal": "join unrelated group",
                    "run_group_id": "run-group-unrelated",
                },
            )
            workflow_response = client.post(
                "/ui/workflow-runs",
                json={
                    "workflow_id": "workflow-1",
                    "user_goal": "join unrelated group",
                    "run_group_id": "run-group-unrelated",
                },
            )

        assert agent_response.status_code == 400
        assert workflow_response.status_code == 400
        assert agent_response.json()["detail"] == "public_run_group_attachment_forbidden"
        assert workflow_response.json()["detail"] == "public_run_group_attachment_forbidden"
        assert service.calls == []
    finally:
        sys.modules.pop("_oha_run_group_authority_route_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_and_workflow_run_http_routes_redact_sensitive_idempotency_key_errors(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_agent_sensitive_id_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        class FakeRuntimeService:
            def __init__(self):
                self.calls: list[tuple[str, dict]] = []

            def create_agent_run(self, payload):
                self.calls.append(("create_agent_run", dict(payload)))
                raise AgentRuntimeError(f"bad client_run_id {payload.get('client_run_id', '')}")

            def create_workflow_run(self, payload):
                self.calls.append(("create_workflow_run", dict(payload)))
                raise AgentRuntimeError(f"bad client_run_id {payload.get('client_run_id', '')}")

        service = FakeRuntimeService()
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        agent_secret = "sk-http-agent-run-id-secret123456"
        workflow_secret = "sk-http-workflow-run-id-secret123456"

        with TestClient(route_app) as client:
            agent_response = client.post(
                "/ui/agent-runs",
                json={"agent_id": "agent-1", "user_goal": "hello"},
                headers={"Idempotency-Key": agent_secret},
            )
            workflow_response = client.post(
                "/ui/workflow-runs",
                json={"workflow_id": "workflow-1", "user_goal": "hello"},
                headers={"Idempotency-Key": workflow_secret},
            )

        assert agent_response.status_code == 400
        assert workflow_response.status_code == 400
        agent_body = json.dumps(agent_response.json(), ensure_ascii=False)
        workflow_body = json.dumps(workflow_response.json(), ensure_ascii=False)
        assert agent_secret not in agent_body
        assert workflow_secret not in workflow_body
        assert "[redacted]" in agent_body
        assert "[redacted]" in workflow_body
        assert service.calls == [
            (
                "create_agent_run",
                {"agent_id": "agent-1", "user_goal": "hello", "client_run_id": agent_secret},
            ),
            (
                "create_workflow_run",
                {"workflow_id": "workflow-1", "user_goal": "hello", "client_run_id": workflow_secret},
            ),
        ]
    finally:
        sys.modules.pop("_oha_agent_sensitive_id_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_run_http_route_rejects_sensitive_idempotency_key_before_persistence(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_agent_sensitive_id_real_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        try:
            agent = service.create_agent(
                {
                    "name": "HTTP Sensitive Idempotency Agent",
                    "model_mode": "custom_api",
                    "model_config": {
                        "base_url": "https://api.example.test/v1",
                        "model": "demo-model",
                        "api_key": "sk-agent-secret",
                    },
                }
            )
            monkeypatch.setattr(
                agent_route_module,
                "get_agent_runtime_service",
                lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
            )
            route_app = FastAPI()
            route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
            route_app.include_router(agent_route_module.router)
            leaked_idempotency_key = "sk-real-http-agent-id-secret123456"

            with TestClient(route_app) as client:
                response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent["agent_id"], "user_goal": "hello"},
                    headers={"Idempotency-Key": leaked_idempotency_key},
                )

            body = json.dumps(response.json(), ensure_ascii=False)
            run_count = service._conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
            assert response.status_code == 400
            assert "client_run_id/idempotency_key" in body
            assert leaked_idempotency_key not in body
            assert run_count == 0
            assert verify_secret_redaction(paths=[tmp_path]) == []
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_sensitive_id_real_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_run_http_route_rejects_sensitive_idempotency_key_before_persistence(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_workflow_sensitive_id_real_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        try:
            agent = service.create_agent(
                {
                    "name": "HTTP Workflow Sensitive Id Agent",
                    "model_mode": "custom_api",
                    "model_config": {
                        "base_url": "https://api.example.test/v1",
                        "model": "demo-model",
                        "api_key": "sk-agent-secret",
                    },
                }
            )
            workflow = service.create_workflow(
                {
                    "name": "HTTP Workflow Sensitive Id Flow",
                    "nodes": [
                        {"id": "start", "type": "start", "data": {"label": "Start"}},
                        {
                            "id": "agent",
                            "type": "agent",
                            "data": {"label": "HTTP Workflow Sensitive Id Agent", "agent_id": agent["agent_id"]},
                        },
                        {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                    ],
                    "edges": [
                        {"source": "start", "target": "agent"},
                        {"source": "agent", "target": "summary"},
                    ],
                }
            )
            monkeypatch.setattr(
                agent_route_module,
                "get_agent_runtime_service",
                lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
            )
            route_app = FastAPI()
            route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
            route_app.include_router(agent_route_module.router)
            leaked_idempotency_key = "sk-real-http-workflow-id-secret123456"

            with TestClient(route_app) as client:
                response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow["workflow_id"], "user_goal": "hello"},
                    headers={"Idempotency-Key": leaked_idempotency_key},
                )

            body = json.dumps(response.json(), ensure_ascii=False)
            run_count = service._conn.execute("SELECT COUNT(*) AS count FROM runs").fetchone()["count"]
            assert response.status_code == 400
            assert "client_run_id/idempotency_key" in body
            assert leaked_idempotency_key not in body
            assert run_count == 0
            assert verify_secret_redaction(paths=[tmp_path]) == []
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_sensitive_id_real_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_run_detail_management_http_routes_use_app_runtime_service(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_agent_run_detail_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        class FakeRuntimeService:
            def __init__(self):
                self.calls: list[tuple[str, dict[str, object]]] = []

            def list_run_groups(self, limit):
                self.calls.append(("list_run_groups", {"limit": limit}))
                return {
                    "run_groups": [
                        {
                            "run_group_id": "group-http-1",
                            "child_run_ids": ["run-http-1", "run-http-child"],
                        }
                    ],
                    "limit": limit,
                }

            def get_run_group(self, run_group_id):
                self.calls.append(("get_run_group", {"run_group_id": run_group_id}))
                return {"run_group_id": run_group_id, "status": "completed"}

            def read_run_artifact(self, run_id, artifact_path):
                self.calls.append(
                    (
                        "read_run_artifact",
                        {"run_id": run_id, "artifact_path": artifact_path},
                    )
                )
                return {"ok": True, "run_id": run_id, "path": artifact_path, "content": "# Final"}

            def rerun_run(self, run_id, request=None):
                call = {"run_id": run_id}
                if request is not None:
                    call["request"] = dict(request or {})
                self.calls.append(("rerun_run", call))
                return {"run_id": "run-http-2", "rerun_of_run_id": run_id, "status": "completed"}

            def delete_run(self, run_id):
                self.calls.append(("delete_run", {"run_id": run_id}))
                return {
                    "ok": True,
                    "deleted_run_ids": [run_id, "run-http-child"],
                    "deleted_run_count": 2,
                }

        service = FakeRuntimeService()
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)

        with TestClient(route_app) as client:
            groups_response = client.get("/ui/run-groups?limit=7")
            group_response = client.get("/ui/run-groups/group-http-1")
            artifact_response = client.get("/ui/runs/run-http-1/artifacts/reports/final.md")
            rerun_response = client.post("/ui/runs/run-http-1/rerun")
            delete_response = client.delete("/ui/runs/run-http-1")

        assert groups_response.status_code == 200
        assert groups_response.json()["limit"] == 7
        assert groups_response.json()["run_groups"][0]["child_run_ids"] == ["run-http-1", "run-http-child"]
        assert group_response.status_code == 200
        assert group_response.json() == {"run_group_id": "group-http-1", "status": "completed"}
        assert artifact_response.status_code == 200
        assert artifact_response.json() == {
            "ok": True,
            "run_id": "run-http-1",
            "path": "reports/final.md",
            "content": "# Final",
        }
        assert rerun_response.status_code == 200
        assert rerun_response.json() == {
            "run_id": "run-http-2",
            "rerun_of_run_id": "run-http-1",
            "status": "completed",
        }
        assert delete_response.status_code == 200
        assert delete_response.json() == {
            "ok": True,
            "deleted_run_ids": ["run-http-1", "run-http-child"],
            "deleted_run_count": 2,
        }
        assert service.calls == [
            ("list_run_groups", {"limit": 7}),
            ("get_run_group", {"run_group_id": "group-http-1"}),
            ("read_run_artifact", {"run_id": "run-http-1", "artifact_path": "reports/final.md"}),
            ("rerun_run", {"run_id": "run-http-1"}),
            ("delete_run", {"run_id": "run-http-1"}),
        ]
    finally:
        sys.modules.pop("_oha_agent_run_detail_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_rerun_http_roundtrip_detail_artifact_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_rerun_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_rerun_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": f"Workflow HTTP rerun result {len(model_calls)}"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Workflow Rerun Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Rerun Flow",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "Rerun Agent", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Run and rerun through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-rerun-original-1"},
                )
                run_response.raise_for_status()
                original = run_response.json()
                original_run_id = original["run_id"]

                original_detail = client.get(f"/ui/workflow-runs/{original_run_id}")
                original_replay = client.get(f"/runs/{original_run_id}/events?after_sequence=0&limit=200")
                original_artifact = client.get(f"/ui/runs/{original_run_id}/artifacts/summary.md")
                rerun_response = client.post(f"/ui/runs/{original_run_id}/rerun")
                rerun = rerun_response.json()
                rerun_run_id = rerun["run_id"]
                rerun_detail = client.get(f"/ui/workflow-runs/{rerun_run_id}")
                rerun_replay = client.get(f"/runs/{rerun_run_id}/events?after_sequence=0&limit=200")
                rerun_artifact = client.get(f"/ui/runs/{rerun_run_id}/artifacts/summary.md")
                rerun_group = client.get(f"/ui/run-groups/{rerun['run_group_id']}")

            assert original["client_request_id"] == "http-workflow-rerun-original-1"
            assert original["status"] == "completed"
            assert original["result"] == "Workflow HTTP rerun result 1"
            assert original_detail.status_code == 200
            assert original_detail.json()["status"] == "completed"
            assert original_replay.status_code == 200
            original_replay_types = [event["event_type"] for event in original_replay.json()["events"]]
            assert "workflow.node.agent" in original_replay_types
            assert "workflow.node.artifact" in original_replay_types
            assert "workflow.run.completed" in original_replay_types
            assert original_artifact.status_code == 200
            assert original_artifact.json()["content"] == "Workflow HTTP rerun result 1"

            assert rerun_response.status_code == 200
            assert rerun_run_id != original_run_id
            assert rerun["status"] == "completed"
            assert rerun["result"] == "Workflow HTTP rerun result 2"
            assert rerun["run_group_source"] == "rerun"
            assert rerun["timeline"][0]["event"] == "run.rerun.started"
            assert rerun["timeline"][0]["rerun_of_run_id"] == original_run_id
            assert rerun_detail.status_code == 200
            assert rerun_detail.json()["run_id"] == rerun_run_id
            assert rerun_detail.json()["run_group_source"] == "rerun"
            assert rerun_group.status_code == 200
            assert rerun_group.json()["source"] == "rerun"

            rerun_replay_payload = rerun_replay.json()
            rerun_replay_types = [event["event_type"] for event in rerun_replay_payload["events"]]
            assert rerun_replay.status_code == 200
            assert rerun_replay_types.count("run.rerun.started") == 1
            assert "workflow.node.agent" in rerun_replay_types
            assert "workflow.node.artifact" in rerun_replay_types
            assert "workflow.run.completed" in rerun_replay_types
            rerun_fact = next(
                event
                for event in rerun_replay_payload["events"]
                if event["event_type"] == "run.rerun.started"
            )
            assert rerun_fact["payload"]["rerun_of_run_id"] == original_run_id
            assert rerun_fact["payload"]["input_preview"]["original_status"] == "completed"
            assert rerun_fact["payload"]["input_preview"]["original_goal"] == original["user_goal"]
            assert rerun_artifact.status_code == 200
            assert rerun_artifact.json()["content"] == "Workflow HTTP rerun result 2"
            assert len(model_calls) == 2
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_rerun_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_workflow_rerun_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_delete_http_roundtrip_removes_group_child_runs_and_artifacts(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_delete_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_delete_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": "Workflow HTTP delete result"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Workflow Delete Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Delete Flow",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "Delete Agent", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Delete workflow run through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-delete-1"},
                )
                run_response.raise_for_status()
                run = run_response.json()
                run_id = run["run_id"]
                run_group_id = run["run_group_id"]

                group_before = client.get(f"/ui/run-groups/{run_group_id}")
                group_before.raise_for_status()
                child_run_id = next(
                    child_id
                    for child_id in group_before.json()["child_run_ids"]
                    if child_id != run_id
                )
                artifact_before = client.get(f"/ui/runs/{run_id}/artifacts/summary.md")
                parent_replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                child_detail_before = client.get(f"/ui/runs/{child_run_id}")
                delete_response = client.delete(f"/ui/runs/{run_id}")
                parent_after = client.get(f"/ui/runs/{run_id}")
                child_after = client.get(f"/ui/runs/{child_run_id}")
                group_after = client.get(f"/ui/run-groups/{run_group_id}")
                artifact_after = client.get(f"/ui/runs/{run_id}/artifacts/summary.md")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert run["client_request_id"] == "http-workflow-delete-1"
            assert run["status"] == "completed"
            assert group_before.json()["status"] == "completed"
            assert artifact_before.status_code == 200
            assert artifact_before.json()["content"] == "Workflow HTTP delete result"
            assert parent_replay_before.status_code == 200
            assert "workflow.run.completed" in [
                event["event_type"] for event in parent_replay_before.json()["events"]
            ]
            assert child_detail_before.status_code == 200
            assert child_detail_before.json()["status"] == "completed"

            assert delete_response.status_code == 200
            deleted = delete_response.json()
            assert deleted["ok"] is True
            assert deleted["deleted_run_count"] == 2
            assert set(deleted["deleted_run_ids"]) == {run_id, child_run_id}
            assert parent_after.status_code == 404
            assert child_after.status_code == 404
            assert group_after.status_code == 404
            assert artifact_after.status_code == 404
            assert replay_after.status_code == 404
            assert len(model_calls) == 1
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_delete_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_workflow_delete_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_rerun_http_roundtrip_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_rerun_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_agent_rerun_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": f"Agent HTTP rerun result {len(model_calls)}"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Agent Rerun",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                run_response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent_id, "user_goal": "Run and rerun agent through HTTP"},
                    headers={"Idempotency-Key": "http-agent-rerun-original-1"},
                )
                run_response.raise_for_status()
                original = run_response.json()
                original_run_id = original["run_id"]

                original_detail = client.get(f"/ui/runs/{original_run_id}")
                original_replay = client.get(f"/runs/{original_run_id}/events?after_sequence=0&limit=200")
                rerun_response = client.post(f"/ui/runs/{original_run_id}/rerun")
                rerun = rerun_response.json()
                rerun_run_id = rerun["run_id"]
                rerun_detail = client.get(f"/ui/runs/{rerun_run_id}")
                rerun_replay = client.get(f"/runs/{rerun_run_id}/events?after_sequence=0&limit=200")

            assert original["client_request_id"] == "http-agent-rerun-original-1"
            assert original["status"] == "completed"
            assert original["result"] == "Agent HTTP rerun result 1"
            assert original_detail.status_code == 200
            assert original_detail.json()["status"] == "completed"
            assert original_replay.status_code == 200
            original_replay_types = [event["event_type"] for event in original_replay.json()["events"]]
            assert "agent.run.completed" in original_replay_types

            assert rerun_response.status_code == 200
            assert rerun_run_id != original_run_id
            assert rerun["agent_run_id"] == rerun_run_id
            assert rerun["status"] == "completed"
            assert rerun["result"] == "Agent HTTP rerun result 2"
            assert rerun["run_group_source"] == "rerun"
            assert rerun["timeline"][0]["event"] == "run.rerun.started"
            assert rerun["timeline"][0]["rerun_of_run_id"] == original_run_id
            assert rerun["timeline"][0]["rerun_of_kind"] == "agent_run"
            assert rerun_detail.status_code == 200
            assert rerun_detail.json()["run_id"] == rerun_run_id
            assert rerun_detail.json()["run_group_source"] == "rerun"

            rerun_replay_payload = rerun_replay.json()
            rerun_replay_types = [event["event_type"] for event in rerun_replay_payload["events"]]
            assert rerun_replay.status_code == 200
            assert rerun_replay_types.count("run.rerun.started") == 1
            assert "agent.run.completed" in rerun_replay_types
            rerun_fact = next(
                event
                for event in rerun_replay_payload["events"]
                if event["event_type"] == "run.rerun.started"
            )
            assert rerun_fact["payload"]["rerun_of_run_id"] == original_run_id
            assert rerun_fact["payload"]["rerun_of_kind"] == "agent_run"
            assert rerun_fact["payload"]["input_preview"]["original_status"] == "completed"
            assert rerun_fact["payload"]["input_preview"]["original_goal"] == original["user_goal"]
            assert len(model_calls) == 2
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_rerun_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_agent_rerun_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_delete_http_roundtrip_removes_group_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_delete_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_agent_delete_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": "Agent HTTP delete result"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Agent Delete",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                run_response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent_id, "user_goal": "Delete agent run through HTTP"},
                    headers={"Idempotency-Key": "http-agent-delete-1"},
                )
                run_response.raise_for_status()
                run = run_response.json()
                run_id = run["run_id"]
                run_group_id = run["run_group_id"]

                detail_before = client.get(f"/ui/runs/{run_id}")
                group_before = client.get(f"/ui/run-groups/{run_group_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                delete_response = client.delete(f"/ui/runs/{run_id}")
                detail_after = client.get(f"/ui/runs/{run_id}")
                group_after = client.get(f"/ui/run-groups/{run_group_id}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert run["client_request_id"] == "http-agent-delete-1"
            assert run["status"] == "completed"
            assert run["result"] == "Agent HTTP delete result"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "completed"
            assert group_before.status_code == 200
            assert group_before.json()["child_run_ids"] == [run_id]
            assert replay_before.status_code == 200
            assert "agent.run.completed" in [
                event["event_type"] for event in replay_before.json()["events"]
            ]

            assert delete_response.status_code == 200
            deleted = delete_response.json()
            assert deleted["ok"] is True
            assert deleted["deleted_run_ids"] == [run_id]
            assert deleted["deleted_run_count"] == 1
            assert detail_after.status_code == 404
            assert group_after.status_code == 404
            assert replay_after.status_code == 404
            assert len(model_calls) == 1
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_delete_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_agent_delete_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_run_http_routes_roundtrip_approval_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_route_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_route_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            if len(model_calls) == 1:
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_terminal_http",
                            "type": "function",
                            "function": {
                                "name": "terminal_run",
                                "arguments": json.dumps({"command": "printf route-approved"}),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "route-approved" in messages[-1]["content"]
            return {"content": "HTTP approved command complete"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Approval Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                run_response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent_id, "user_goal": "Run printf route-approved"},
                    headers={"Idempotency-Key": "http-approval-run-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                approve_response = client.post(
                    f"/ui/runs/{run_id}/approval/approve",
                    json={
                        "approval_id": waiting["pending_approval"]["approval_id"]
                    },
                )
                public_replay = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                assert public_replay.status_code == 200
                assert not any(event["event_type"] == "agent.tool.call"
                               for event in public_replay.json()["events"])
                detail_after = client.get(f"/ui/runs/{run_id}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert waiting["client_request_id"] == "http-approval-run-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "terminal.run"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["tool"] == "terminal.run"
            assert replay_before.status_code == 200
            assert "agent.tool.approval_required" in [
                event["event_type"] for event in replay_before.json()["events"]
            ]

            assert approve_response.status_code == 200
            approved = approve_response.json()
            assert approved["status"] == "completed"
            assert approved["pending_approval"] == {}
            assert "输出：route-approved" in approved["result"]
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "completed"
            assert detail_after.json()["result"] == approved["result"]
            assert detail_after.json()["pending_approval"] == {}

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("agent.tool.approval_required") == 1
            assert replay_after_types.count("agent.tool.approval_approved") == 1
            assert "agent.tool.call" not in replay_after_types
            assert "agent.run.completed" in replay_after_types
            approved_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "agent.tool.approval_approved"
            )
            tool_facts = [
                event
                for event in service.list_run_events(run_id, include_internal=True, limit=200)["events"]
                if event["event_type"] == "agent.tool.call"
            ]
            assert approved_fact["payload"]["tool"] == "terminal.run"
            assert approved_fact["payload"]["input_preview"]["command"] == "printf route-approved"
            assert tool_facts[-1]["payload"]["tool"] == "terminal.run"
            assert tool_facts[-1]["payload"]["approved"] is True
            assert len(model_calls) == 0
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_route_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_route_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_run_http_routes_roundtrip_reject_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_route_http_reject_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_route_http_reject_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal_http_reject",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf route-rejected"}),
                        },
                    }
                ],
            }

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Reject Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                run_response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent_id, "user_goal": "Run printf route-rejected"},
                    headers={"Idempotency-Key": "http-reject-run-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                reject_response = client.post(
                    f"/ui/runs/{run_id}/approval/reject",
                    json={
                        "approval_id": waiting["pending_approval"]["approval_id"],
                        "reason": "Rejected from HTTP",
                    },
                )
                public_replay = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                assert public_replay.status_code == 200
                assert not any(event["event_type"] == "agent.tool.call"
                               for event in public_replay.json()["events"])
                detail_after = client.get(f"/ui/runs/{run_id}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert waiting["client_request_id"] == "http-reject-run-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "terminal.run"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["input_preview"]["command"] == "printf route-rejected"
            assert replay_before.status_code == 200
            assert "agent.tool.approval_required" in [
                event["event_type"] for event in replay_before.json()["events"]
            ]

            assert reject_response.status_code == 200
            rejected = reject_response.json()
            assert rejected["status"] == "cancelled"
            assert rejected["pending_approval"] == {}
            assert rejected["result"] == "工具审批已拒绝：Rejected from HTTP"
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "cancelled"
            assert detail_after.json()["result"] == "工具审批已拒绝：Rejected from HTTP"
            assert detail_after.json()["pending_approval"] == {}

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("agent.tool.approval_required") == 1
            assert replay_after_types.count("agent.tool.approval_rejected") == 1
            assert replay_after_types.count("agent.run.cancelled") == 1
            tool_facts = [
                event
                for event in service.list_run_events(run_id, include_internal=True, limit=200)["events"]
                if event["event_type"] == "agent.tool.call"
            ]
            assert not any(event["payload"].get("approved") is True for event in tool_facts)
            rejected_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "agent.tool.approval_rejected"
            )
            cancelled_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "agent.run.cancelled"
            )
            assert rejected_fact["payload"]["tool"] == "terminal.run"
            assert rejected_fact["payload"]["input_preview"]["command"] == "printf route-rejected"
            assert rejected_fact["payload"]["reason"] == "Rejected from HTTP"
            assert cancelled_fact["payload"]["result"] == "工具审批已拒绝：Rejected from HTTP"
            assert len(model_calls) == 0
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_route_http_reject_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_route_http_reject_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_run_http_routes_roundtrip_cancel_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_route_http_cancel_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_route_http_cancel_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_terminal_http_cancel",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf route-cancelled"}),
                        },
                    }
                ],
            }

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Cancel Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                run_response = client.post(
                    "/ui/agent-runs",
                    json={"agent_id": agent_id, "user_goal": "Run printf route-cancelled"},
                    headers={"Idempotency-Key": "http-cancel-run-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                cancel_response = client.post(f"/ui/runs/{run_id}/cancel")
                public_replay = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                assert public_replay.status_code == 200
                assert not any(event["event_type"] == "agent.tool.call"
                               for event in public_replay.json()["events"])
                detail_after = client.get(f"/ui/runs/{run_id}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert waiting["client_request_id"] == "http-cancel-run-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "terminal.run"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["input_preview"]["command"] == "printf route-cancelled"
            assert replay_before.status_code == 200
            assert "agent.tool.approval_required" in [
                event["event_type"] for event in replay_before.json()["events"]
            ]

            assert cancel_response.status_code == 200
            cancelled = cancel_response.json()
            assert cancelled["status"] == "cancelled"
            assert cancelled["pending_approval"] == {}
            assert cancelled["result"] == "Run cancelled"
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "cancelled"
            assert detail_after.json()["result"] == "Run cancelled"
            assert detail_after.json()["pending_approval"] == {}

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("agent.tool.approval_required") == 1
            assert replay_after_types.count("run.cancelled") == 1
            assert "agent.tool.approval_approved" not in replay_after_types
            assert "agent.tool.approval_rejected" not in replay_after_types
            assert "agent.run.completed" not in replay_after_types
            tool_facts = [
                event
                for event in service.list_run_events(run_id, include_internal=True, limit=200)["events"]
                if event["event_type"] == "agent.tool.call"
            ]
            assert not any(event["payload"].get("approved") is True for event in tool_facts)
            cancelled_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "run.cancelled"
            )
            assert cancelled_fact["payload"]["kind"] == "agent_run"
            assert cancelled_fact["payload"]["status"] == "cancelled"
            assert cancelled_fact["payload"]["result"] == "Run cancelled"
            assert len(model_calls) == 0
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_route_http_cancel_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_route_http_cancel_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_run_http_routes_roundtrip_child_approval_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_workflow_route_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_workflow_route_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            if len(model_calls) == 1:
                assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
                return {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_workflow_terminal_http",
                            "type": "function",
                            "function": {
                                "name": "terminal_run",
                                "arguments": json.dumps({"command": "printf workflow-http-approved"}),
                            },
                        }
                    ],
                }
            assert messages[-1]["role"] == "tool"
            assert "workflow-http-approved" in messages[-1]["content"]
            return {"content": "Workflow HTTP approved result"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Workflow Approval Child",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Approval Flow",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "HTTP Workflow Approval Child", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Run workflow approval through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-approval-run-1"},
                )
                run_response.raise_for_status()
                waiting_parent = run_response.json()
                parent_run_id = waiting_parent["run_id"]

                group_response = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                group_response.raise_for_status()
                child_run_id = next(
                    run_id
                    for run_id in group_response.json()["child_run_ids"]
                    if run_id != parent_run_id
                )

                parent_detail_before = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_before = client.get(f"/ui/runs/{child_run_id}")
                parent_replay_before = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_before = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")
                approve_response = client.post(
                    f"/ui/runs/{child_run_id}/approval/approve",
                    json={
                        "approval_id": child_detail_before.json()["pending_approval"][
                            "approval_id"
                        ]
                    },
                )
                parent_detail_after = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_after = client.get(f"/ui/runs/{child_run_id}")
                group_after = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                parent_replay_after = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_after = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")
                artifact_response = client.get(f"/ui/runs/{parent_run_id}/artifacts/summary.md")

            assert waiting_parent["client_request_id"] == "http-workflow-approval-run-1"
            assert waiting_parent["status"] == "approval_required"
            assert waiting_parent["pending_approval"] == {}
            assert parent_detail_before.status_code == 200
            assert parent_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.status_code == 200
            assert child_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.json()["pending_approval"]["tool"] == "terminal.run"
            assert child_detail_before.json()["pending_approval"]["input_preview"]["command"] == (
                "printf workflow-http-approved"
            )

            parent_before_types = [event["event_type"] for event in parent_replay_before.json()["events"]]
            child_before_types = [event["event_type"] for event in child_replay_before.json()["events"]]
            assert parent_replay_before.status_code == 200
            assert child_replay_before.status_code == 200
            assert "workflow.run.approval_required" in parent_before_types
            assert "agent.tool.approval_required" in child_before_types

            assert approve_response.status_code == 200
            assert approve_response.json()["status"] == "completed"
            assert approve_response.json()["result"] == "Workflow HTTP approved result"
            assert parent_detail_after.status_code == 200
            assert parent_detail_after.json()["status"] == "completed"
            assert parent_detail_after.json()["result"] == "Workflow HTTP approved result"
            assert child_detail_after.status_code == 200
            assert child_detail_after.json()["status"] == "completed"
            assert child_detail_after.json()["pending_approval"] == {}
            assert group_after.status_code == 200
            assert group_after.json()["status"] == "completed"

            parent_after_events = parent_replay_after.json()["events"]
            parent_after_types = [event["event_type"] for event in parent_after_events]
            child_after_types = [event["event_type"] for event in child_replay_after.json()["events"]]
            assert parent_replay_after.status_code == 200
            assert child_replay_after.status_code == 200
            assert parent_after_types.count("workflow.run.approval_required") == 1
            assert parent_after_types.count("workflow.run.child_resumed") == 1
            assert parent_after_types.count("workflow.run.resumed") == 1
            assert "workflow.run.completed" in parent_after_types
            assert child_after_types.count("agent.tool.approval_required") == 1
            assert child_after_types.count("agent.tool.approval_approved") == 1
            assert "agent.run.completed" in child_after_types
            agent_node_events = [
                event
                for event in parent_after_events
                if event["event_type"] == "workflow.node.agent"
                and event["payload"].get("child_run_id") == child_run_id
            ]
            assert [event["payload"].get("status") for event in agent_node_events] == [
                "approval_required",
                "running",
                "completed",
            ]
            assert artifact_response.status_code == 200
            assert artifact_response.json()["path"] == "summary.md"
            assert artifact_response.json()["content"] == "Workflow HTTP approved result"
            assert len(model_calls) == 2
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_workflow_route_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_workflow_route_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_run_http_routes_roundtrip_child_reject_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_workflow_route_http_reject_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_workflow_route_http_reject_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_workflow_terminal_http_reject",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf workflow-http-rejected"}),
                        },
                    }
                ],
            }

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Workflow Reject Child",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Reject Flow",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "HTTP Workflow Reject Child", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Reject workflow child through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-reject-run-1"},
                )
                run_response.raise_for_status()
                waiting_parent = run_response.json()
                parent_run_id = waiting_parent["run_id"]

                group_response = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                group_response.raise_for_status()
                child_run_id = next(
                    run_id
                    for run_id in group_response.json()["child_run_ids"]
                    if run_id != parent_run_id
                )

                parent_detail_before = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_before = client.get(f"/ui/runs/{child_run_id}")
                parent_replay_before = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_before = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")
                reject_response = client.post(
                    f"/ui/runs/{child_run_id}/approval/reject",
                    json={
                        "approval_id": child_detail_before.json()["pending_approval"][
                            "approval_id"
                        ],
                        "reason": "Rejected child from HTTP",
                    },
                )
                parent_detail_after = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_after = client.get(f"/ui/runs/{child_run_id}")
                group_after = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                parent_replay_after = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_after = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")

            assert waiting_parent["client_request_id"] == "http-workflow-reject-run-1"
            assert waiting_parent["status"] == "approval_required"
            assert waiting_parent["pending_approval"] == {}
            assert parent_detail_before.status_code == 200
            assert parent_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.status_code == 200
            assert child_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.json()["pending_approval"]["tool"] == "terminal.run"
            assert child_detail_before.json()["pending_approval"]["input_preview"]["command"] == (
                "printf workflow-http-rejected"
            )

            parent_before_types = [event["event_type"] for event in parent_replay_before.json()["events"]]
            child_before_types = [event["event_type"] for event in child_replay_before.json()["events"]]
            assert parent_replay_before.status_code == 200
            assert child_replay_before.status_code == 200
            assert "workflow.run.approval_required" in parent_before_types
            assert "agent.tool.approval_required" in child_before_types

            assert reject_response.status_code == 200
            rejected_child = reject_response.json()
            assert rejected_child["status"] == "cancelled"
            assert rejected_child["pending_approval"] == {}
            assert rejected_child["result"] == "工具审批已拒绝：Rejected child from HTTP"
            assert child_detail_after.status_code == 200
            assert child_detail_after.json()["status"] == "cancelled"
            assert child_detail_after.json()["pending_approval"] == {}
            assert parent_detail_after.status_code == 200
            assert parent_detail_after.json()["status"] == "cancelled"
            assert parent_detail_after.json()["result"] == "工具审批已拒绝：Rejected child from HTTP"
            assert group_after.status_code == 200
            assert group_after.json()["status"] == "cancelled"

            parent_after_events = parent_replay_after.json()["events"]
            parent_after_types = [event["event_type"] for event in parent_after_events]
            child_after_events = child_replay_after.json()["events"]
            child_after_types = [event["event_type"] for event in child_after_events]
            assert parent_replay_after.status_code == 200
            assert child_replay_after.status_code == 200
            assert parent_after_types.count("workflow.run.approval_required") == 1
            assert parent_after_types.count("workflow.run.cancelled") == 1
            assert "workflow.run.completed" not in parent_after_types
            assert child_after_types.count("agent.tool.approval_required") == 1
            assert child_after_types.count("agent.tool.approval_rejected") == 1
            assert child_after_types.count("agent.run.cancelled") == 1
            agent_node_events = [
                event
                for event in parent_after_events
                if event["event_type"] == "workflow.node.agent"
                and event["payload"].get("child_run_id") == child_run_id
            ]
            assert [event["payload"].get("status") for event in agent_node_events] == [
                "approval_required",
                "cancelled",
            ]
            child_rejected_fact = next(
                event
                for event in child_after_events
                if event["event_type"] == "agent.tool.approval_rejected"
            )
            parent_cancelled_fact = next(
                event
                for event in parent_after_events
                if event["event_type"] == "workflow.run.cancelled"
            )
            assert child_rejected_fact["payload"]["tool"] == "terminal.run"
            assert child_rejected_fact["payload"]["reason"] == "Rejected child from HTTP"
            assert parent_cancelled_fact["payload"]["child_run_id"] == child_run_id
            assert parent_cancelled_fact["payload"]["workflow_node_id"] == "agent"
            assert len(model_calls) == 1
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_workflow_route_http_reject_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_workflow_route_http_reject_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_run_http_routes_roundtrip_child_cancel_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_agent_workflow_route_http_child_cancel_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_run_workflow_route_http_child_cancel_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        workdir = tmp_path / "workspace"
        workdir.mkdir()
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            assert any((tool.get("function") or {}).get("name") == "terminal_run" for tool in tools or [])
            return {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_workflow_terminal_http_cancel",
                        "type": "function",
                        "function": {
                            "name": "terminal_run",
                            "arguments": json.dumps({"command": "printf workflow-http-cancelled"}),
                        },
                    }
                ],
            }

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Workflow Cancel Child",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                        "tool_policy": {"allowed_tools": ["terminal.run"]},
                        "workspace_policy": {
                            "default_workdir": str(workdir),
                            "readable_scopes": ["."],
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Cancel Child Flow",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "HTTP Workflow Cancel Child", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Cancel workflow child through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-child-cancel-run-1"},
                )
                run_response.raise_for_status()
                waiting_parent = run_response.json()
                parent_run_id = waiting_parent["run_id"]

                group_response = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                group_response.raise_for_status()
                child_run_id = next(
                    run_id
                    for run_id in group_response.json()["child_run_ids"]
                    if run_id != parent_run_id
                )

                parent_detail_before = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_before = client.get(f"/ui/runs/{child_run_id}")
                parent_replay_before = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_before = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")
                cancel_response = client.post(f"/ui/runs/{child_run_id}/cancel")
                parent_detail_after = client.get(f"/ui/workflow-runs/{parent_run_id}")
                child_detail_after = client.get(f"/ui/runs/{child_run_id}")
                group_after = client.get(f"/ui/run-groups/{waiting_parent['run_group_id']}")
                parent_replay_after = client.get(f"/runs/{parent_run_id}/events?after_sequence=0&limit=200")
                child_replay_after = client.get(f"/runs/{child_run_id}/events?after_sequence=0&limit=200")

            assert waiting_parent["client_request_id"] == "http-workflow-child-cancel-run-1"
            assert waiting_parent["status"] == "approval_required"
            assert waiting_parent["pending_approval"] == {}
            assert parent_detail_before.status_code == 200
            assert parent_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.status_code == 200
            assert child_detail_before.json()["status"] == "approval_required"
            assert child_detail_before.json()["pending_approval"]["tool"] == "terminal.run"
            assert child_detail_before.json()["pending_approval"]["input_preview"]["command"] == (
                "printf workflow-http-cancelled"
            )

            parent_before_types = [event["event_type"] for event in parent_replay_before.json()["events"]]
            child_before_types = [event["event_type"] for event in child_replay_before.json()["events"]]
            assert parent_replay_before.status_code == 200
            assert child_replay_before.status_code == 200
            assert "workflow.run.approval_required" in parent_before_types
            assert "agent.tool.approval_required" in child_before_types

            assert cancel_response.status_code == 200
            cancelled_child = cancel_response.json()
            assert cancelled_child["status"] == "cancelled"
            assert cancelled_child["pending_approval"] == {}
            assert cancelled_child["result"] == "Run cancelled"
            assert child_detail_after.status_code == 200
            assert child_detail_after.json()["status"] == "cancelled"
            assert child_detail_after.json()["pending_approval"] == {}
            assert child_detail_after.json()["result"] == "Run cancelled"
            assert parent_detail_after.status_code == 200
            assert parent_detail_after.json()["status"] == "cancelled"
            assert parent_detail_after.json()["result"] == "Run cancelled"
            assert group_after.status_code == 200
            assert group_after.json()["status"] == "cancelled"
            assert group_after.json()["summary"] == "Run cancelled"

            parent_after_events = parent_replay_after.json()["events"]
            parent_after_types = [event["event_type"] for event in parent_after_events]
            child_after_events = child_replay_after.json()["events"]
            child_after_types = [event["event_type"] for event in child_after_events]
            assert parent_replay_after.status_code == 200
            assert child_replay_after.status_code == 200
            assert parent_after_types.count("workflow.run.approval_required") == 1
            assert parent_after_types.count("workflow.run.cancelled") == 1
            assert "workflow.run.completed" not in parent_after_types
            assert child_after_types.count("agent.tool.approval_required") == 1
            assert child_after_types.count("run.cancelled") == 1
            assert "agent.tool.approval_approved" not in child_after_types
            assert "agent.tool.approval_rejected" not in child_after_types
            assert "agent.run.completed" not in child_after_types
            agent_node_events = [
                event
                for event in parent_after_events
                if event["event_type"] == "workflow.node.agent"
                and event["payload"].get("child_run_id") == child_run_id
            ]
            assert [event["payload"].get("status") for event in agent_node_events] == [
                "approval_required",
                "cancelled",
            ]
            child_cancelled_fact = next(
                event
                for event in child_after_events
                if event["event_type"] == "run.cancelled"
            )
            parent_cancelled_fact = next(
                event
                for event in parent_after_events
                if event["event_type"] == "workflow.run.cancelled"
            )
            assert child_cancelled_fact["payload"]["kind"] == "agent_run"
            assert child_cancelled_fact["payload"]["result"] == "Run cancelled"
            assert parent_cancelled_fact["payload"]["child_run_id"] == child_run_id
            assert parent_cancelled_fact["payload"]["workflow_node_id"] == "agent"
            assert parent_cancelled_fact["payload"]["result"] == "Run cancelled"
            assert len(model_calls) == 1
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_agent_workflow_route_http_child_cancel_roundtrip_under_test", None)
        sys.modules.pop("_oha_run_workflow_route_http_child_cancel_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_approval_node_http_roundtrip_approve_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": "Workflow approval node HTTP approved"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP After Workflow Gate",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Manual Gate",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "gate",
                                "type": "approval",
                                "data": {
                                    "label": "HTTP Manual Gate",
                                    "criteria": "Confirm before running the Agent.",
                                },
                            },
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "After Gate", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "gate"},
                            {"source": "gate", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Approve workflow gate through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-gate-approve-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/workflow-runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                approve_response = client.post(
                    f"/ui/runs/{run_id}/approval/approve",
                    json={
                        "approval_id": waiting["pending_approval"]["approval_id"]
                    },
                )
                detail_after = client.get(f"/ui/workflow-runs/{run_id}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                artifact_response = client.get(f"/ui/runs/{run_id}/artifacts/summary.md")

            assert waiting["client_request_id"] == "http-workflow-gate-approve-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "workflow.approval"
            assert waiting["pending_approval"]["input_preview"]["checkpoint"] == "HTTP Manual Gate"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["tool"] == "workflow.approval"
            assert replay_before.status_code == 200
            replay_before_types = [event["event_type"] for event in replay_before.json()["events"]]
            assert "workflow.node.approval_required" in replay_before_types

            assert approve_response.status_code == 200
            approved = approve_response.json()
            assert approved["status"] == "completed"
            assert approved["pending_approval"] == {}
            assert approved["result"] == "Workflow approval node HTTP approved"
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "completed"
            assert detail_after.json()["result"] == "Workflow approval node HTTP approved"
            assert detail_after.json()["pending_approval"] == {}

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("workflow.node.approval_required") == 1
            assert replay_after_types.count("workflow.node.approval_approved") == 1
            assert "workflow.node.agent" in replay_after_types
            assert "workflow.node.artifact" in replay_after_types
            assert "workflow.run.completed" in replay_after_types
            approval_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "workflow.node.approval_approved"
            )
            assert approval_fact["payload"]["workflow_node_id"] == "gate"
            assert approval_fact["payload"]["workflow_node_label"] == "HTTP Manual Gate"
            assert approval_fact["payload"]["input_preview"]["criteria"] == "Confirm before running the Agent."
            assert artifact_response.status_code == 200
            assert artifact_response.json()["path"] == "summary.md"
            assert artifact_response.json()["content"] == "Workflow approval node HTTP approved"
            assert len(model_calls) == 1
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_approval_node_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_workflow_approval_node_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_approval_node_http_roundtrip_reject_detail_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_reject_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_reject_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": "Should not run after rejected approval node"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Rejected Workflow Gate Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Manual Gate Reject",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "gate",
                                "type": "approval",
                                "data": {
                                    "label": "HTTP Manual Gate",
                                    "criteria": "Confirm before running the Agent.",
                                },
                            },
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "After Gate", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "gate"},
                            {"source": "gate", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Reject workflow gate through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-gate-reject-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/workflow-runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                reject_response = client.post(
                    f"/ui/runs/{run_id}/approval/reject",
                    json={
                        "approval_id": waiting["pending_approval"]["approval_id"],
                        "reason": "Rejected workflow gate from HTTP",
                    },
                )
                detail_after = client.get(f"/ui/workflow-runs/{run_id}")
                group_after = client.get(f"/ui/run-groups/{waiting['run_group_id']}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert waiting["client_request_id"] == "http-workflow-gate-reject-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "workflow.approval"
            assert waiting["pending_approval"]["input_preview"]["checkpoint"] == "HTTP Manual Gate"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["tool"] == "workflow.approval"
            assert replay_before.status_code == 200
            replay_before_types = [event["event_type"] for event in replay_before.json()["events"]]
            assert "workflow.node.approval_required" in replay_before_types

            assert reject_response.status_code == 200
            rejected = reject_response.json()
            assert rejected["status"] == "cancelled"
            assert rejected["pending_approval"] == {}
            assert rejected["result"] == "Workflow 审批已拒绝：Rejected workflow gate from HTTP"
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "cancelled"
            assert detail_after.json()["result"] == "Workflow 审批已拒绝：Rejected workflow gate from HTTP"
            assert detail_after.json()["pending_approval"] == {}
            assert group_after.status_code == 200
            assert group_after.json()["status"] == "cancelled"

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("workflow.node.approval_required") == 1
            assert replay_after_types.count("workflow.node.approval_rejected") == 1
            assert replay_after_types.count("workflow.run.cancelled") == 1
            assert "workflow.node.agent" not in replay_after_types
            assert "workflow.node.artifact" not in replay_after_types
            assert "workflow.run.completed" not in replay_after_types
            rejection_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "workflow.node.approval_rejected"
            )
            cancelled_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "workflow.run.cancelled"
            )
            assert rejection_fact["payload"]["workflow_node_id"] == "gate"
            assert rejection_fact["payload"]["workflow_node_label"] == "HTTP Manual Gate"
            assert rejection_fact["payload"]["reason"] == "Rejected workflow gate from HTTP"
            assert cancelled_fact["payload"]["workflow_node_id"] == "gate"
            assert cancelled_fact["payload"]["reason"] == "Rejected workflow gate from HTTP"
            assert len(model_calls) == 0
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_approval_node_reject_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_workflow_approval_node_reject_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_workflow_approval_node_http_roundtrip_cancel_detail_group_and_replay(tmp_path, monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_root = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes"
        agent_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_cancel_http_roundtrip_under_test",
            route_root / "agents.py",
        )
        run_spec = importlib.util.spec_from_file_location(
            "_oha_workflow_approval_node_cancel_run_http_roundtrip_under_test",
            route_root / "runs.py",
        )
        assert agent_spec is not None and agent_spec.loader is not None
        assert run_spec is not None and run_spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(agent_spec)
        run_route_module = importlib.util.module_from_spec(run_spec)
        sys.modules[agent_spec.name] = agent_route_module
        sys.modules[run_spec.name] = run_route_module
        agent_spec.loader.exec_module(agent_route_module)
        run_spec.loader.exec_module(run_route_module)

        service = AgentRuntimeService(
            db_path=tmp_path / "agent-runtime.db",
            workspace_dir=tmp_path / "runtime",
            credential_store=MemoryCredentialStore(),
            seed_templates=False,
        )
        model_calls: list[list[dict[str, object]]] = []

        def fake_chat(_base_url, _model, _api_key, messages, *, tools=None):
            model_calls.append(messages)
            return {"content": "Should not run after cancelled approval node"}

        monkeypatch.setattr("apps.shell.agent_runtime.openai_compatible_chat_message", fake_chat)
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent routes should use AppRuntime service")),
        )
        monkeypatch.setattr(
            run_route_module,
            "get_native_run_engine",
            lambda: (_ for _ in ()).throw(AssertionError("run routes should use AppRuntime service")),
        )

        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)
        route_app.include_router(run_route_module.router)
        try:
            with TestClient(route_app) as client:
                agent_response = client.post(
                    "/ui/agents",
                    json={
                        "name": "HTTP Cancelled Workflow Gate Agent",
                        "model_mode": "custom_api",
                        "model_config": {
                            "base_url": "https://api.example.test/v1",
                            "model": "demo-model",
                            "api_key": "sk-secret",
                        },
                    },
                )
                agent_response.raise_for_status()
                agent_id = agent_response.json()["agent_id"]

                workflow_response = client.post(
                    "/ui/workflows",
                    json={
                        "name": "HTTP Workflow Manual Gate Cancel",
                        "nodes": [
                            {"id": "start", "type": "start", "data": {"label": "Start"}},
                            {
                                "id": "gate",
                                "type": "approval",
                                "data": {
                                    "label": "HTTP Manual Gate",
                                    "criteria": "Confirm before running the Agent.",
                                },
                            },
                            {
                                "id": "agent",
                                "type": "agent",
                                "data": {"label": "After Gate", "agent_id": agent_id},
                            },
                            {"id": "summary", "type": "artifact", "data": {"label": "Summary"}},
                        ],
                        "edges": [
                            {"source": "start", "target": "gate"},
                            {"source": "gate", "target": "agent"},
                            {"source": "agent", "target": "summary"},
                        ],
                    },
                )
                workflow_response.raise_for_status()
                workflow_id = workflow_response.json()["workflow_id"]

                run_response = client.post(
                    "/ui/workflow-runs",
                    json={"workflow_id": workflow_id, "user_goal": "Cancel workflow gate through HTTP"},
                    headers={"Idempotency-Key": "http-workflow-gate-cancel-1"},
                )
                run_response.raise_for_status()
                waiting = run_response.json()
                run_id = waiting["run_id"]

                detail_before = client.get(f"/ui/workflow-runs/{run_id}")
                replay_before = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")
                cancel_response = client.post(f"/ui/runs/{run_id}/cancel")
                detail_after = client.get(f"/ui/workflow-runs/{run_id}")
                group_after = client.get(f"/ui/run-groups/{waiting['run_group_id']}")
                replay_after = client.get(f"/runs/{run_id}/events?after_sequence=0&limit=200")

            assert waiting["client_request_id"] == "http-workflow-gate-cancel-1"
            assert waiting["status"] == "approval_required"
            assert waiting["pending_approval"]["tool"] == "workflow.approval"
            assert waiting["pending_approval"]["input_preview"]["checkpoint"] == "HTTP Manual Gate"
            assert detail_before.status_code == 200
            assert detail_before.json()["status"] == "approval_required"
            assert detail_before.json()["pending_approval"]["tool"] == "workflow.approval"
            assert replay_before.status_code == 200
            replay_before_types = [event["event_type"] for event in replay_before.json()["events"]]
            assert "workflow.node.approval_required" in replay_before_types

            assert cancel_response.status_code == 200
            cancelled = cancel_response.json()
            assert cancelled["status"] == "cancelled"
            assert cancelled["pending_approval"] == {}
            assert cancelled["result"] == "Workflow 已取消：HTTP Manual Gate"
            cancelled_timeline = next(
                event for event in cancelled["timeline"] if event["event"] == "workflow.run.cancelled"
            )
            assert cancelled_timeline["detail"] == "HTTP Manual Gate cancelled"
            assert cancelled_timeline["workflow_node_id"] == "gate"
            assert cancelled_timeline["workflow_node_kind"] == "approval"
            assert cancelled_timeline["workflow_node_label"] == "HTTP Manual Gate"
            assert detail_after.status_code == 200
            assert detail_after.json()["status"] == "cancelled"
            assert detail_after.json()["result"] == "Workflow 已取消：HTTP Manual Gate"
            assert detail_after.json()["pending_approval"] == {}
            assert group_after.status_code == 200
            assert group_after.json()["status"] == "cancelled"
            assert group_after.json()["summary"] == "Workflow 已取消：HTTP Manual Gate"

            replay_after_payload = replay_after.json()
            replay_after_types = [event["event_type"] for event in replay_after_payload["events"]]
            assert replay_after.status_code == 200
            assert replay_after_types.count("workflow.node.approval_required") == 1
            assert replay_after_types.count("workflow.run.cancelled") == 1
            assert "workflow.node.approval_approved" not in replay_after_types
            assert "workflow.node.approval_rejected" not in replay_after_types
            assert "workflow.node.agent" not in replay_after_types
            assert "workflow.node.artifact" not in replay_after_types
            assert "workflow.run.completed" not in replay_after_types
            cancelled_fact = next(
                event
                for event in replay_after_payload["events"]
                if event["event_type"] == "workflow.run.cancelled"
            )
            assert cancelled_fact["payload"]["kind"] == "workflow_run"
            assert cancelled_fact["payload"]["status"] == "cancelled"
            assert cancelled_fact["payload"]["result"] == "Workflow 已取消：HTTP Manual Gate"
            assert len(model_calls) == 0
        finally:
            service.close()
    finally:
        sys.modules.pop("_oha_workflow_approval_node_cancel_http_roundtrip_under_test", None)
        sys.modules.pop("_oha_workflow_approval_node_cancel_run_http_roundtrip_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_agent_studio_crud_http_routes_use_app_runtime_service(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi",))
    try:
        try:
            from fastapi import FastAPI
            from fastapi.testclient import TestClient
        except ModuleNotFoundError as exc:
            pytest.skip(f"FastAPI/TestClient dependency is not installed: {exc.name}")

        route_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "routes" / "agents.py"
        spec = importlib.util.spec_from_file_location("_oha_agent_crud_route_http_under_test", route_path)
        assert spec is not None
        assert spec.loader is not None
        agent_route_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = agent_route_module
        spec.loader.exec_module(agent_route_module)

        class FakeRuntimeService:
            def __init__(self):
                self.calls: list[str] = []

            def list_agents(self):
                self.calls.append("list_agents")
                return {"agents": [{"agent_id": "agent-1"}]}

            def create_agent(self, payload):
                self.calls.append("create_agent")
                return {"agent_id": "agent-1", **payload}

            def get_agent(self, agent_id):
                self.calls.append("get_agent")
                return {"agent_id": agent_id}

            def update_agent(self, agent_id, payload):
                self.calls.append("update_agent")
                return {"agent_id": agent_id, **payload}

            def delete_agent(self, agent_id):
                self.calls.append("delete_agent")
                return {"ok": True, "agent_id": agent_id}

            def test_agent_model(self, agent_id):
                self.calls.append("test_agent_model")
                return {"ok": True, "agent_id": agent_id}

            def attach_skill(self, agent_id, skill_id):
                self.calls.append("attach_skill")
                return {"ok": True, "agent_id": agent_id, "skill_id": skill_id}

            def detach_skill(self, agent_id, skill_id):
                self.calls.append("detach_skill")
                return {"ok": True, "agent_id": agent_id, "skill_id": skill_id}

            def list_skills(self):
                self.calls.append("list_skills")
                return {"skills": [{"skill_id": "skill-1"}]}

            def import_skill(self, source_path, folder_id):
                self.calls.append("import_skill")
                return {"ok": True, "source_path": source_path, "folder_id": folder_id}

            def list_native_skill_sources(self):
                self.calls.append("list_native_skill_sources")
                return {"sources": [{"source": "installed"}]}

            def list_skill_folders(self):
                self.calls.append("list_skill_folders")
                return {"folders": [{"folder_id": "folder-1"}]}

            def create_skill_folder(self, payload):
                self.calls.append("create_skill_folder")
                return {"folder_id": "folder-1", **payload}

            def update_skill_folder(self, folder_id, payload):
                self.calls.append("update_skill_folder")
                return {"folder_id": folder_id, **payload}

            def delete_skill_folder(self, folder_id, *, delete_skills=False):
                self.calls.append("delete_skill_folder")
                return {"ok": True, "folder_id": folder_id, "delete_skills": delete_skills}

            def sync_native_skills(self):
                self.calls.append("sync_native_skills")
                return {"ok": True}

            def install_skill_command(self, command, folder_id):
                self.calls.append("install_skill_command")
                return {"ok": True, "command": command, "folder_id": folder_id}

            def get_skill(self, skill_id):
                self.calls.append("get_skill")
                return {"skill_id": skill_id}

            def update_skill(self, skill_id, payload):
                self.calls.append("update_skill")
                return {"skill_id": skill_id, **payload}

            def delete_skill(self, skill_id):
                self.calls.append("delete_skill")
                return {"ok": True, "skill_id": skill_id}

            def list_workflows(self):
                self.calls.append("list_workflows")
                return {"workflows": [{"workflow_id": "workflow-1"}]}

            def create_workflow(self, payload):
                self.calls.append("create_workflow")
                return {"workflow_id": "workflow-1", **payload}

            def get_workflow(self, workflow_id):
                self.calls.append("get_workflow")
                return {"workflow_id": workflow_id}

            def update_workflow(self, workflow_id, payload):
                self.calls.append("update_workflow")
                return {"workflow_id": workflow_id, **payload}

            def delete_workflow(self, workflow_id):
                self.calls.append("delete_workflow")
                return {"ok": True, "workflow_id": workflow_id}

            def list_runnables(self):
                self.calls.append("list_runnables")
                return {"runnables": [{"id": "agent-1"}]}

        service = FakeRuntimeService()
        monkeypatch.setattr(
            agent_route_module,
            "get_agent_runtime_service",
            lambda: (_ for _ in ()).throw(AssertionError("agent studio CRUD routes should use AppRuntime service")),
        )
        route_app = FastAPI()
        route_app.state.runtime = SimpleNamespace(agent_runtime_service=service)
        route_app.include_router(agent_route_module.router)

        with TestClient(route_app) as client:
            responses = [
                client.get("/ui/agents"),
                client.post("/ui/agents", json={"name": "Agent 1"}),
                client.get("/ui/agents/agent-1"),
                client.patch("/ui/agents/agent-1", json={"name": "Agent 2"}),
                client.delete("/ui/agents/agent-1"),
                client.post("/ui/agents/agent-1/test-model"),
                client.post("/ui/agents/agent-1/skills", json={"skill_id": "skill-1"}),
                client.delete("/ui/agents/agent-1/skills/skill-1"),
                client.get("/ui/skills"),
                client.post("/ui/skills", json={"source_path": "/tmp/skill.md", "folder_id": "folder-1"}),
                client.post("/ui/skills/import", json={"source_path": "/tmp/skill-2.md"}),
                client.get("/ui/skills/sources"),
                client.get("/ui/skill-folders"),
                client.post("/ui/skill-folders", json={"name": "Folder"}),
                client.patch("/ui/skill-folders/folder-1", json={"name": "Folder 2"}),
                client.delete("/ui/skill-folders/folder-1?delete_skills=true"),
                client.post("/ui/skills/sync"),
                client.post("/ui/skills/install", json={"command": "install skill", "folder_id": "folder-1"}),
                client.get("/ui/skills/skill-1"),
                client.patch("/ui/skills/skill-1", json={"enabled": False}),
                client.delete("/ui/skills/skill-1"),
                client.get("/ui/workflows"),
                client.post("/ui/workflows", json={"name": "Workflow 1"}),
                client.get("/ui/workflows/workflow-1"),
                client.patch("/ui/workflows/workflow-1", json={"name": "Workflow 2"}),
                client.delete("/ui/workflows/workflow-1"),
                client.get("/ui/runnables"),
            ]

        assert all(response.status_code == 200 for response in responses)
        assert responses[0].json() == {"agents": [{"agent_id": "agent-1"}]}
        assert responses[1].json() == {"agent_id": "agent-1", "name": "Agent 1"}
        assert responses[2].json() == {"agent_id": "agent-1"}
        assert responses[3].json() == {"agent_id": "agent-1", "name": "Agent 2"}
        assert responses[4].json() == {"ok": True, "agent_id": "agent-1"}
        assert responses[5].json() == {"ok": True, "agent_id": "agent-1"}
        assert responses[6].json() == {"ok": True, "agent_id": "agent-1", "skill_id": "skill-1"}
        assert responses[7].json() == {"ok": True, "agent_id": "agent-1", "skill_id": "skill-1"}
        assert service.calls == [
            "list_agents",
            "create_agent",
            "get_agent",
            "update_agent",
            "delete_agent",
            "test_agent_model",
            "attach_skill",
            "detach_skill",
            "list_skills",
            "import_skill",
            "import_skill",
            "list_native_skill_sources",
            "list_skill_folders",
            "create_skill_folder",
            "update_skill_folder",
            "delete_skill_folder",
            "sync_native_skills",
            "install_skill_command",
            "get_skill",
            "update_skill",
            "delete_skill",
            "list_workflows",
            "create_workflow",
            "get_workflow",
            "update_workflow",
            "delete_workflow",
            "list_runnables",
        ]
    finally:
        sys.modules.pop("_oha_agent_crud_route_http_under_test", None)
        _restore_module_prefixes(("fastapi",), saved_modules)


def test_run_cancel_route_handler_is_idempotent(tmp_path, monkeypatch):
    service = AgentRuntimeService(
        db_path=tmp_path / "agent-runtime.db",
        workspace_dir=tmp_path / "runtime",
        credential_store=MemoryCredentialStore(),
        seed_templates=False,
    )
    monkeypatch.setattr(agent_routes, "get_agent_runtime_service", lambda: service)
    try:
        run = service._insert_run(
            kind="main_chat_run",
            runnable_id="builtin:yachiyo-main",
            user_goal="cancel via http",
        )

        first = asyncio.run(agent_routes.cancel_run(run["run_id"]))
        second = asyncio.run(agent_routes.cancel_run(run["run_id"]))

        assert first["status"] == "cancelled"
        assert second["status"] == "cancelled"

        stored = service.get_run(run["run_id"])
        events = service.list_run_events(run["run_id"])["events"]
        cancel_facts = [event for event in events if event["event_type"] == "run.cancelled"]
        cancel_timeline = [event for event in stored["timeline"] if event["event"] == "run.cancelled"]

        assert len(cancel_facts) == 1
        assert len(cancel_timeline) == 1
    finally:
        service.close()


def test_fastapi_request_parameters_do_not_use_optional_union_annotations():
    endpoints = [
        agent_routes.create_agent_run,
        agent_routes.create_workflow_run,
        ui_routes.send_chat_message,
    ]

    for endpoint in endpoints:
        parameter = inspect.signature(endpoint).parameters["http_request"]
        assert parameter.annotation in {"Request", agent_routes.Request, ui_routes.Request}


@pytest.mark.asyncio
async def test_status_route_uses_app_version(monkeypatch):
    status_route = _load_status_route_module()
    monkeypatch.setattr(status_route, "get_app_version", lambda: "9.8.7")
    monkeypatch.setattr(
        status_route,
        "load_build_metadata",
        lambda: {
            "commit": "abc1234567890abc1234567890abc1234567890a",
            "short_commit": "abc1234",
        },
    )
    monkeypatch.setattr(
        status_route,
        "get_runtime",
        lambda: SimpleNamespace(
            uptime=1.25,
            state=SimpleNamespace(get_task_counts=lambda: {}),
            is_native_agent_ready=lambda: True,
        ),
    )

    response = await status_route.get_status()

    assert response.version == "9.8.7"
    assert response.service == "oha-yachiyo"
    assert response.native_agent_ready is True
    assert response.build_metadata["short_commit"] == "abc1234"


def test_live2d_asset_token_can_rotate():
    first = get_live2d_asset_token()
    second = regenerate_live2d_asset_token()

    assert second
    assert second != first


def test_bridge_access_log_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_ACCESS_LOG", raising=False)

    assert _bridge_access_log_enabled() is False


def test_bridge_access_log_can_be_enabled_for_http_debug(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_ACCESS_LOG", "1")

    assert _bridge_access_log_enabled() is True


def test_bridge_start_and_restart_reject_non_loopback_host_before_binding(monkeypatch):
    saved_modules = _unload_module_prefixes(("fastapi", "uvicorn"))
    try:
        server_path = Path(__file__).resolve().parents[1] / "apps" / "bridge" / "server.py"
        spec = importlib.util.spec_from_file_location("_oha_bridge_server_host_guard_under_test", server_path)
        assert spec is not None
        assert spec.loader is not None
        bridge_server = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = bridge_server
        spec.loader.exec_module(bridge_server)

        with pytest.raises(ValueError, match="回环地址"):
            bridge_server.start_bridge(host="0.0.0.0", port=8420)
        assert bridge_server.get_bridge_state() == "failed"

        class ExistingThread:
            def is_alive(self):
                return True

            def join(self, timeout=None):
                raise AssertionError("invalid host must be rejected before stopping the current bridge")

        class NewThread:
            def __init__(self, *args, **kwargs):
                raise AssertionError("invalid host must be rejected before starting a bridge thread")

        running_server = SimpleNamespace(should_exit=False)
        bridge_server._server = running_server
        bridge_server._state = "running"
        bridge_server._bridge_thread = ExistingThread()
        monkeypatch.setattr(bridge_server.threading, "Thread", NewThread)

        result = bridge_server.restart_bridge(host="192.168.1.20", port=8420)

        assert result == {"ok": False, "error": "Bridge 只允许监听回环地址"}
        assert running_server.should_exit is False
        assert bridge_server.get_bridge_state() == "running"
    finally:
        sys.modules.pop("_oha_bridge_server_host_guard_under_test", None)
        _restore_module_prefixes(("fastapi", "uvicorn"), saved_modules)


@pytest.mark.parametrize("channel", ["release", "alpha", "stable"])
def test_bridge_debug_routes_are_disabled_for_release_metadata(monkeypatch, tmp_path, channel):
    metadata_path = tmp_path / "oha-yachiyo-build.json"
    metadata_path.write_text(json.dumps({"channel": channel}), encoding="utf-8")
    monkeypatch.setenv("OHA_YACHIYO_DEV", "1")
    monkeypatch.setenv("OHA_YACHIYO_BUILD_METADATA", str(metadata_path))

    assert debug_routes_enabled() is False


@pytest.mark.parametrize("channel", ["release", "alpha", "stable"])
def test_bridge_debug_routes_are_disabled_for_release_channel_env(monkeypatch, channel):
    monkeypatch.setenv("OHA_YACHIYO_DEV", "1")
    monkeypatch.setenv("OHA_YACHIYO_BUILD_CHANNEL", channel)

    assert debug_routes_enabled() is False


@pytest.mark.parametrize("env_name", ["OHA_YACHIYO_RELEASE_BUILD", "OHA_YACHIYO_ALPHA_BUILD"])
def test_bridge_debug_routes_are_disabled_for_release_flag_env(monkeypatch, env_name):
    monkeypatch.setenv("OHA_YACHIYO_DEV", "1")
    monkeypatch.setenv(env_name, "1")

    assert debug_routes_enabled() is False


def test_bridge_debug_routes_are_disabled_for_packaged_build_env(monkeypatch, tmp_path):
    metadata_path = tmp_path / "oha-yachiyo-build.json"
    metadata_path.write_text(json.dumps({"channel": "experimental"}), encoding="utf-8")
    monkeypatch.setenv("OHA_YACHIYO_DEV", "1")
    monkeypatch.setenv("OHA_YACHIYO_PACKAGED_BUILD", "1")
    monkeypatch.setenv("OHA_YACHIYO_BUILD_METADATA", str(metadata_path))

    assert debug_routes_enabled() is False


def test_bridge_security_accepts_loopback_without_token_when_disabled(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)

    assert bridge_request_violation(
        "POST",
        {"host": "127.0.0.1:8420", "origin": "http://localhost:5174"},
    ) == ""


def test_bridge_security_rejects_untrusted_host(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)

    assert bridge_request_violation("GET", {"host": "0.0.0.0:8420"}) == "untrusted_host"


def test_bridge_security_rejects_untrusted_origin(monkeypatch):
    monkeypatch.delenv("OHA_YACHIYO_BRIDGE_TOKEN", raising=False)

    assert bridge_request_violation(
        "GET",
        {"host": "127.0.0.1:8420", "origin": "https://evil.example"},
    ) == "untrusted_origin"


def test_bridge_security_requires_token_for_mutating_requests(monkeypatch):
    monkeypatch.setenv("OHA_YACHIYO_BRIDGE_TOKEN", "token-123")

    assert bridge_request_violation("POST", {"host": "127.0.0.1:8420"}) == "invalid_bridge_token"
    assert bridge_request_violation(
        "POST",
        {"host": "127.0.0.1:8420", "x-oha-yachiyo-bridge-token": "token-123"},
    ) == ""


def test_rewrite_live2d_manifest_paths_appends_token():
    manifest = {
        "Version": 3,
        "FileReferences": {
            "Moc": "model.moc3",
            "Textures": ["textures/tex_00.png"],
            "Physics": "physics.json",
            "Motions": {
                "Idle": [{"File": "motions/idle.motion3.json", "Sound": "sounds/idle.wav"}],
            },
        },
    }

    rewritten = live2d_route._rewrite_live2d_manifest_paths(manifest, "token-123")
    refs = rewritten["FileReferences"]

    assert refs["Moc"].endswith("model.moc3?token=token-123")
    assert refs["Textures"][0].endswith("textures/tex_00.png?token=token-123")
    assert refs["Physics"].endswith("physics.json?token=token-123")
    assert refs["Motions"]["Idle"][0]["File"].endswith("motions/idle.motion3.json?token=token-123")
    assert refs["Motions"]["Idle"][0]["Sound"].endswith("sounds/idle.wav?token=token-123")


def test_resolve_live2d_asset_rejects_path_escape(tmp_path, monkeypatch):
    model_root = tmp_path / "models"
    model_root.mkdir()
    (model_root / "ok.txt").write_text("ok", encoding="utf-8")

    monkeypatch.setattr(live2d_route, "_get_live2d_model_root", lambda: model_root)

    with pytest.raises(live2d_route.HTTPException) as exc_info:
        live2d_route._resolve_live2d_asset("../secret.txt")

    assert exc_info.value.status_code == 403


def test_render_live2d_manifest_keeps_json_structure(tmp_path):
    manifest_path = tmp_path / "model.model3.json"
    manifest_path.write_text(
        '{"FileReferences":{"Moc":"model.moc3","Textures":["tex.png"]}}',
        encoding="utf-8",
    )

    payload = live2d_route._render_live2d_manifest(manifest_path, "token-xyz")
    decoded = json.loads(payload.decode("utf-8"))

    assert decoded["FileReferences"]["Moc"].endswith("model.moc3?token=token-xyz")
    assert decoded["FileReferences"]["Textures"][0].endswith("tex.png?token=token-xyz")
    assert "Expressions" not in decoded["FileReferences"]
    assert "Motions" not in decoded["FileReferences"]


def test_render_live2d_manifest_injects_sidecar_expressions(tmp_path, monkeypatch):
    manifest_path = tmp_path / "model.model3.json"
    manifest_path.write_text(
        '{"FileReferences":{"Moc":"model.moc3","Textures":[]}}',
        encoding="utf-8",
    )
    (tmp_path / "笑咪咪.exp3.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        live2d_route,
        "get_runtime",
        lambda: SimpleNamespace(config=SimpleNamespace(live2d_mode=SimpleNamespace(enable_physics=True))),
    )

    payload = live2d_route._render_live2d_manifest(manifest_path, "token-xyz")
    refs = json.loads(payload.decode("utf-8"))["FileReferences"]

    assert refs["Expressions"] == [{"Name": "笑咪咪", "File": "笑咪咪.exp3.json?token=token-xyz"}]


def test_render_live2d_manifest_respects_physics_toggle(tmp_path, monkeypatch):
    manifest_path = tmp_path / "model.model3.json"
    manifest_path.write_text(
        '{"FileReferences":{"Moc":"model.moc3","Textures":[],"Physics":"model.physics3.json"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live2d_route,
        "get_runtime",
        lambda: SimpleNamespace(config=SimpleNamespace(live2d_mode=SimpleNamespace(enable_physics=False))),
    )

    payload = live2d_route._render_live2d_manifest(manifest_path, "token-xyz")
    refs = json.loads(payload.decode("utf-8"))["FileReferences"]

    assert "Physics" not in refs


def test_render_live2d_manifest_keeps_physics_when_enabled(tmp_path, monkeypatch):
    manifest_path = tmp_path / "model.model3.json"
    manifest_path.write_text(
        '{"FileReferences":{"Moc":"model.moc3","Textures":[],"Physics":"model.physics3.json"}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        live2d_route,
        "get_runtime",
        lambda: SimpleNamespace(config=SimpleNamespace(live2d_mode=SimpleNamespace(enable_physics=True))),
    )

    payload = live2d_route._render_live2d_manifest(manifest_path, "token-xyz")
    refs = json.loads(payload.decode("utf-8"))["FileReferences"]

    assert refs["Physics"].endswith("model.physics3.json?token=token-xyz")


def test_live2d_runtime_script_sources_prefer_cache(tmp_path, monkeypatch):
    cached_script = tmp_path / "pixi.min.js"
    cached_script.write_text("window.PIXI = {};", encoding="utf-8")
    missing_script = tmp_path / "cubism.js"

    monkeypatch.setattr(
        live2d_route.live2d_runtime,
        "get_live2d_runtime_dependency_specs",
        lambda: {
            "pixi_js": ("https://example.test/pixi.js", cached_script),
            "live2d_cubism_core": ("https://example.test/core.js", missing_script),
        },
    )

    scripts = live2d_route._live2d_runtime_script_sources()

    assert scripts == [
        {"id": "pixi_js", "source": "cache", "url": "/live2d/runtime/pixi_js"},
        {
            "id": "live2d_cubism_core",
            "source": "cdn",
            "url": "https://example.test/core.js",
        },
    ]


@pytest.mark.asyncio
async def test_get_live2d_runtime_primes_dependencies(tmp_path, monkeypatch):
    cached_script = tmp_path / "pixi.min.js"
    cached_script.write_text("window.PIXI = {};", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        live2d_route.live2d_runtime,
        "get_live2d_runtime_dependency_specs",
        lambda: {"pixi_js": ("https://example.test/pixi.js", cached_script)},
    )
    monkeypatch.setattr(
        live2d_route.live2d_runtime,
        "prime_live2d_runtime_dependencies",
        lambda: calls.append("prime") or (True, ""),
    )

    payload = await live2d_route.get_live2d_runtime()

    assert calls == ["prime"]
    assert payload["ready"] is True
    assert payload["scripts"] == [
        {"id": "pixi_js", "source": "cache", "url": "/live2d/runtime/pixi_js"}
    ]
