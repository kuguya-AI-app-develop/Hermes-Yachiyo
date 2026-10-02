"""AstrBot plugin to Oha Bridge integration smoke tests."""

from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from apps.core.state import AppState
from integrations.astrbot_plugin.config import PluginConfig
from integrations.astrbot_plugin.main import on_y_command
from packages.protocol.schemas import ActiveWindowResponse, ScreenshotResponse


class _RuntimeStub:
    def __init__(self) -> None:
        self.state = AppState()
        self.uptime = 125.0
        self.task_runner = SimpleNamespace(
            executor=SimpleNamespace(
                name="NativeAgentExecutor",
                capabilities={
                    "model": True,
                    "image_input": True,
                    "tools": True,
                    "approval": True,
                },
            )
        )

    def is_native_agent_ready(self) -> bool:
        return True

    def native_agent_readiness(self) -> dict[str, object]:
        return {"ready": True, "reason": "", "executor": "NativeAgentExecutor"}

    def get_status(self) -> dict[str, object]:
        return {"task_counts": self.state.get_task_counts()}


def _module_matches_prefix(name: str, prefixes: tuple[str, ...]) -> bool:
    return any(name == prefix or name.startswith(f"{prefix}.") for prefix in prefixes)


def _detach_from_parent_package(name: str) -> None:
    parent_name, _, attr_name = name.rpartition(".")
    if not parent_name or not attr_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None and getattr(parent, attr_name, None) is sys.modules.get(name):
        delattr(parent, attr_name)


def _attach_to_parent_package(name: str, module: object) -> None:
    parent_name, _, attr_name = name.rpartition(".")
    if not parent_name or not attr_name:
        return
    parent = sys.modules.get(parent_name)
    if parent is not None:
        setattr(parent, attr_name, module)


def _unload_module_prefixes(prefixes: tuple[str, ...]) -> dict[str, object]:
    saved = {
        name: module
        for name, module in list(sys.modules.items())
        if _module_matches_prefix(name, prefixes)
    }
    for name in list(sys.modules):
        if _module_matches_prefix(name, prefixes):
            _detach_from_parent_package(name)
            sys.modules.pop(name, None)
    return saved


def _restore_module_prefixes(prefixes: tuple[str, ...], saved: dict[str, object]) -> None:
    for name in list(sys.modules):
        if _module_matches_prefix(name, prefixes):
            _detach_from_parent_package(name)
            sys.modules.pop(name, None)
    sys.modules.update(saved)
    for name, module in saved.items():
        _attach_to_parent_package(name, module)


@pytest.fixture()
def bridge_asgi_transport(monkeypatch):
    prefixes = ("fastapi", "uvicorn", "apps.bridge.server", "apps.bridge.deps", "apps.bridge.routes")
    saved_modules = _unload_module_prefixes(prefixes)
    from apps.bridge import deps
    from apps.bridge import server

    runtime = _RuntimeStub()
    monkeypatch.setattr(deps, "_runtime", runtime, raising=False)
    server._register_routes()
    app = server.app
    app.state.runtime = runtime

    transport = httpx.ASGITransport(app=app)
    original_async_client = httpx.AsyncClient

    def local_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        kwargs.setdefault("base_url", "http://127.0.0.1:8420")
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", local_async_client)
    try:
        yield runtime
    finally:
        _restore_module_prefixes(prefixes, saved_modules)


@pytest.mark.asyncio
async def test_astrbot_commands_round_trip_through_oha_bridge(
    bridge_asgi_transport,
    monkeypatch,
):
    async def fake_capture_screenshot() -> ScreenshotResponse:
        return ScreenshotResponse(
            image_base64="iVBORw0KGgo=",
            format="png",
            width=2,
            height=1,
            captured_at=datetime(2026, 6, 13, 10, 0, tzinfo=timezone.utc),
        )

    async def fake_active_window() -> ActiveWindowResponse:
        return ActiveWindowResponse(
            title="Oha bridge E2E",
            app_name="Codex",
            pid=4242,
            queried_at=datetime(2026, 6, 13, 10, 1, tzinfo=timezone.utc),
        )

    import apps.locald.active_window as active_window
    import apps.locald.screenshot as screenshot

    monkeypatch.setattr(screenshot, "capture_screenshot", fake_capture_screenshot)
    monkeypatch.setattr(active_window, "get_active_window", fake_active_window)

    config = PluginConfig(oha_url="http://127.0.0.1:8420")

    status = await on_y_command("/y status", sender_id="qq-1", config=config)
    assert "Oha-Yachiyo 状态" in status
    assert "Native Agent: ✅ 已就绪" in status

    created = await on_y_command("/y do 写一条 AstrBot E2E 记录", sender_id="qq-1", config=config)
    created_task_id = re.search(r"ID: ([0-9a-f]{12})", created)
    assert created_task_id is not None, created
    task_id = created_task_id.group(1)
    assert "✅ 任务已提交" in created

    listed = await on_y_command("/y tasks", sender_id="qq-1", config=config)
    assert task_id[:8] in listed
    assert "AstrBot E2E" in listed

    checked = await on_y_command(f"/y check {task_id}", sender_id="qq-1", config=config)
    assert "任务详情" in checked
    assert task_id in checked

    asked = await on_y_command("/y ask 帮我整理一份低风险提醒", sender_id="qq-1", config=config)
    asked_task_id = re.search(r"任务 ID: ([0-9a-f]{12})", asked)
    assert asked_task_id is not None
    assert "create_low_risk_task" in asked

    screen = await on_y_command("/y screen", sender_id="qq-1", config=config)
    assert "截图已获取" in screen
    assert "2×1" in screen

    window = await on_y_command("/y window", sender_id="qq-1", config=config)
    assert "当前活动窗口" in window
    assert "Codex" in window
    assert "Oha bridge E2E" in window

    cancelled = await on_y_command(f"/y cancel {task_id}", sender_id="qq-1", config=config)
    assert "任务已取消" in cancelled
    assert "已取消" in cancelled

    assert len(bridge_asgi_transport.state.list_tasks()) == 2
