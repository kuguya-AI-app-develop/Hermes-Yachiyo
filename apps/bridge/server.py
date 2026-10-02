"""Bridge/API 服务器

内部 FastAPI 实例，运行在后台线程中。
仅供 Shell UI 层和 AstrBot 插件调用，不是产品本体。

连通方式：
  shell/app.py 调用 deps.set_runtime() 注入 Core Runtime 实例，
  bridge 路由通过 deps.get_runtime() 访问 core 层的 state/task 能力。
"""

from __future__ import annotations

import hmac
import logging
import os
import secrets
import threading
import time
from typing import Any
from urllib.parse import urlparse

from apps.core.build_metadata import development_features_enabled
from apps.core.version import get_app_version

_FastAPIClass: Any
_CORSMiddlewareClass: Any
_ResponseClass: Any

try:
    import uvicorn
    from fastapi import FastAPI, HTTPException
    from fastapi.middleware.cors import CORSMiddleware
    from starlette.responses import JSONResponse, Response

    _FastAPIClass = FastAPI
    _CORSMiddlewareClass = CORSMiddleware
    _ResponseClass = Response
    _HTTPExceptionClass = HTTPException
    _JSONResponseClass = JSONResponse
except ModuleNotFoundError:
    uvicorn = None  # type: ignore[assignment]

    class _FastAPIStub:
        def __init__(self, *args, **kwargs) -> None:
            self.routes: list[Any] = []

        def include_router(self, *args, **kwargs) -> None:
            return None

        def add_middleware(self, *args, **kwargs) -> None:
            return None

    class _CORSMiddlewareStub:
        pass

    _FastAPIClass = _FastAPIStub
    _CORSMiddlewareClass = _CORSMiddlewareStub
    _ResponseClass = None
    _HTTPExceptionClass = Exception
    _JSONResponseClass = None

logger = logging.getLogger(__name__)

_FASTAPI_AVAILABLE = uvicorn is not None
_routes_registered = False
_DEBUG_ROUTE_MODULES: tuple[str, ...] = ()


app = _FastAPIClass(
    title="Oha-Yachiyo Bridge",
    description="内部通信 API，非产品本体",
    version=get_app_version(),
)

if _FASTAPI_AVAILABLE:
    from packages.security import redact_api_error_detail

    # 仅放行回环地址；null-origin 的 Live2D 资源请求在专用路由里做 token 校验后单独处理。
    app.add_middleware(
        _CORSMiddlewareClass,
        allow_origins=[],
        allow_origin_regex=r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?$",
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if hasattr(app, "middleware"):
        @app.middleware("http")
        async def _bridge_security_middleware(request, call_next):
            violation = bridge_request_violation(request.method, request.headers)
            if violation:
                return _ResponseClass(
                    content=f'{{"ok":false,"error":"{violation}"}}',
                    media_type="application/json",
                    status_code=403,
                )
            return await call_next(request)

    if hasattr(app, "exception_handler"):
        @app.exception_handler(_HTTPExceptionClass)
        async def _bridge_http_exception_handler(request, exc):
            return _JSONResponseClass(
                status_code=exc.status_code,
                content={"detail": redact_api_error_detail(exc.detail)},
                headers=getattr(exc, "headers", None),
            )

        @app.exception_handler(Exception)
        async def _bridge_unhandled_exception_handler(request, exc):
            logger.exception("Bridge route 未处理异常")
            return _JSONResponseClass(
                status_code=500,
                content={"detail": "internal_server_error"},
            )


def _register_routes() -> None:
    global _routes_registered
    if not _FASTAPI_AVAILABLE or _routes_registered:
        return

    import apps.bridge.routes.agents as agents
    import apps.bridge.routes.assistant as assistant
    import apps.bridge.routes.live2d as live2d
    import apps.bridge.routes.model_profiles as model_profiles
    import apps.bridge.routes.runs as runs
    import apps.bridge.routes.screen as screen
    import apps.bridge.routes.status as status
    import apps.bridge.routes.system as system
    import apps.bridge.routes.tasks as tasks
    import apps.bridge.routes.ui as ui
    import apps.bridge.routes.yachiyo as yachiyo

    app.include_router(status.router)
    app.include_router(tasks.router)
    app.include_router(screen.router)
    app.include_router(system.router)
    app.include_router(live2d.router)
    app.include_router(agents.router)
    app.include_router(model_profiles.router)
    app.include_router(runs.router)
    app.include_router(assistant.router)
    app.include_router(yachiyo.router)
    app.include_router(ui.router)
    if debug_routes_enabled():
        for module_path in _DEBUG_ROUTE_MODULES:
            module = __import__(module_path, fromlist=["router"])
            app.include_router(module.router)
    _routes_registered = True

_server: uvicorn.Server | None = None
_bridge_thread: threading.Thread | None = None

# Bridge 运行状态：not_started | running | failed
_state: str = "not_started"
# 当前实际使用的 host/port
_running_host: str = ""
_running_port: int = 0
_live2d_asset_token: str = secrets.token_urlsafe(24)
_ACCESS_LOG_ENV = "OHA_YACHIYO_BRIDGE_ACCESS_LOG"
_ACCESS_LOG_TRUE_VALUES = {"1", "true", "yes", "on", "debug"}
_BRIDGE_TOKEN_ENV = "OHA_YACHIYO_BRIDGE_TOKEN"
_BRIDGE_TOKEN_HEADER = "x-oha-yachiyo-bridge-token"
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _bridge_access_log_enabled() -> bool:
    """是否输出 uvicorn access log。

    默认关闭轮询请求日志，避免 Electron 前端通讯刷屏；排查 HTTP 层问题时可通过
    OHA_YACHIYO_BRIDGE_ACCESS_LOG=1 临时开启。
    """
    return os.getenv(_ACCESS_LOG_ENV, "").strip().lower() in _ACCESS_LOG_TRUE_VALUES


def debug_routes_enabled() -> bool:
    return development_features_enabled()


def bridge_session_token() -> str:
    return os.getenv(_BRIDGE_TOKEN_ENV, "").strip()


def bridge_security_enabled() -> bool:
    return bool(bridge_session_token())


def _header_value(headers: Any, name: str) -> str:
    if headers is None:
        return ""
    header_variants = (
        name,
        name.lower(),
        name.upper(),
        "-".join(part.capitalize() for part in name.split("-")),
    )
    for key in header_variants:
        try:
            value = headers.get(key)
        except AttributeError:
            value = None
        if value:
            return str(value)
    return ""


def _host_without_port(value: str) -> str:
    host = str(value or "").strip()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        return host[1:end].lower() if end > 0 else host.lower()
    return host.rsplit(":", 1)[0].lower()


def _trusted_loopback_host(value: str) -> bool:
    return _host_without_port(value) in _LOOPBACK_HOSTS


def _validate_bridge_host(host: str) -> str:
    normalized = str(host or "").strip().lower()
    if normalized not in _LOOPBACK_HOSTS:
        raise ValueError("Bridge 只允许监听回环地址")
    return normalized


def _trusted_origin(value: str) -> bool:
    origin = str(value or "").strip()
    if not origin:
        return True
    if origin == "null" or origin.startswith("file://"):
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    # urlparse.hostname has already removed both brackets and the port. Parsing
    # it as a Host header again would truncate the IPv6 address ::1 to just :.
    return parsed.hostname in _LOOPBACK_HOSTS


def bridge_request_violation(method: str, headers: Any) -> str:
    if not _trusted_loopback_host(_header_value(headers, "host")):
        return "untrusted_host"
    if not _trusted_origin(_header_value(headers, "origin")):
        return "untrusted_origin"
    if str(method or "").upper() in _MUTATING_METHODS and bridge_security_enabled():
        expected = bridge_session_token()
        token = _header_value(headers, _BRIDGE_TOKEN_HEADER)
        if not token or not hmac.compare_digest(token, expected):
            return "invalid_bridge_token"
    return ""


def get_bridge_state() -> str:
    """返回 Bridge 当前运行状态"""
    return _state


def get_running_config() -> dict[str, object]:
    """返回 Bridge 当前运行时使用的配置。"""
    return {"host": _running_host, "port": _running_port}


def get_live2d_asset_token() -> str:
    """返回当前进程内的 Live2D 资源访问令牌。"""
    return _live2d_asset_token


def regenerate_live2d_asset_token() -> str:
    """为 Live2D 资源路由生成新的进程内访问令牌。"""
    global _live2d_asset_token
    _live2d_asset_token = secrets.token_urlsafe(24)
    return _live2d_asset_token


def start_bridge(host: str = "127.0.0.1", port: int = 8420) -> None:
    """启动 Bridge API（阻塞，应在后台线程调用）"""
    global _server, _state, _running_host, _running_port
    try:
        host = _validate_bridge_host(host)
    except ValueError:
        _state = "failed"
        raise
    if uvicorn is None:
        _state = "failed"
        raise RuntimeError("Bridge 依赖未安装：缺少 fastapi/uvicorn")
    regenerate_live2d_asset_token()
    _register_routes()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=_bridge_access_log_enabled(),
        loop="asyncio",
        http="h11",
        ws="none",
        lifespan="on",
    )
    _server = uvicorn.Server(config)
    _state = "running"
    _running_host = host
    _running_port = port
    logger.info("Bridge API 启动: http://%s:%d", host, port)
    try:
        _server.run()
    except Exception:
        _state = "failed"
        logger.exception("Bridge API 异常退出")
        raise
    finally:
        # uvicorn.run() 正常退出（被 should_exit 停止）时也走到这里
        if _state == "running":
            _state = "not_started"


def stop_bridge() -> None:
    """请求停止 Bridge API"""
    if _server is not None:
        _server.should_exit = True
        logger.info("Bridge API 停止请求已发送")


def restart_bridge(host: str, port: int) -> dict[str, object]:
    """停止当前 Bridge 并用新配置重启。

    在调用者线程中执行停止，然后启动新的后台线程。
    返回操作结果字典，供 UI 层消费。
    """
    global _bridge_thread, _state

    # 1. 基本校验必须先于停止旧实例，避免非法重启请求破坏正在运行的 Bridge。
    try:
        host = _validate_bridge_host(host)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not (1024 <= port <= 65535):
        return {"ok": False, "error": f"端口 {port} 不在有效范围 (1024-65535)"}

    # 2. 停止旧实例
    if _server is not None and _state == "running":
        logger.info("正在停止 Bridge 以便重启...")
        stop_bridge()
        # 等待旧线程退出（最多 5 秒）
        if _bridge_thread is not None and _bridge_thread.is_alive():
            _bridge_thread.join(timeout=5.0)
            if _bridge_thread.is_alive():
                logger.warning("Bridge 旧线程未在 5 秒内退出")
                return {
                    "ok": False,
                    "error": "Bridge 停止超时，请稍后重试或重启应用",
                }

    # 3. 启动新线程
    _state = "not_started"
    _bridge_thread = threading.Thread(
        target=start_bridge,
        kwargs={"host": host, "port": port},
        daemon=True,
        name="bridge-api",
    )
    _bridge_thread.start()

    # 4. 等待短暂时间确认启动成功
    time.sleep(0.8)
    if _state == "running":
        logger.info("Bridge 已重启: http://%s:%d", host, port)
        return {"ok": True, "host": host, "port": port}
    elif _state == "failed":
        return {"ok": False, "error": f"Bridge 启动失败，请检查端口 {port} 是否被占用"}
    else:
        # 可能仍在启动中
        return {"ok": True, "host": host, "port": port, "pending": True}
