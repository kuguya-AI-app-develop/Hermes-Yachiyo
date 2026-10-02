"""Shared desktop execution policies for daily and Studio entrypoints."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

from apps.shell.agent.runtime.controlled_desktop_provider import (
    CONTROLLED_DESKTOP_PROVIDER_TOOLS,
)
from apps.shell.agent.runtime.cua_background_provider import (
    CUA_BACKGROUND_PROVIDER_KIND,
    cua_background_provider_status,
)
from apps.shell.agent.runtime.desktop_execution_providers import (
    desktop_execution_provider_status_from_env,
    local_desktop_execution_provider_status,
)
from apps.shell.agent.runtime.desktop_provider_credentials import (
    DESKTOP_PROVIDER_TOKEN_ENV,
    desktop_provider_token_from_manifest,
)
from apps.shell.yachiyo_agent.desktop_provider_contract import (
    OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS,
    virtual_desktop_provider_contract_evidence,
)

_PROVIDER_MANIFEST_ENV = "OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST"
_PROVIDER_MANIFEST_METADATA_KEYS = (
    "provider_manifest",
    "provider_manifest_path",
    "desktop_provider_manifest",
    "desktop_provider_manifest_path",
)
_PROVIDER_MANIFEST_METADATA_CONTAINERS = (
    "desktop_provider_session",
    "desktop_execution_policy",
    "desktop_execution_route",
    "sandbox_provider",
)

_SANDBOX_DESKTOP_PROVIDER_DEFAULT: dict[str, Any] = {
    "available": False,
    "provider_id": "",
    "provider_kind": "sandbox_desktop",
    "status": "provider_required",
    "adapter_ready": False,
    "authentication_configured": False,
    "reason": (
        "No sandbox desktop provider is configured for this runtime yet; "
        "foreground input must stay supervised or use user handoff."
    ),
    "blocking_conditions": ["sandbox_desktop_provider_required"],
    "supported_tools": [],
    "recommended_for": ["foreground_control", "keyboard_mouse_capture"],
    "diagnostic_route": "/yachiyo/studio/tools",
    "source": "runtime",
    "health": {
        "ok": False,
        "checked": False,
        "status": "not_configured",
        "blocking_conditions": ["sandbox_desktop_provider_required"],
        "supported_tools": [],
        "capabilities": [],
    },
    "desktop_session_kind": "",
    "desktop_session_isolated": None,
    "foreground_takeover_required": None,
    "launch_hint": {
        "provider_id": "local-headless-desktop",
        "provider_kind": "sandbox_desktop",
        "execution_mode": "headless_read_only",
        "command": [
            "python",
            "scripts/run_headless_desktop_provider.py",
            "--host",
            "127.0.0.1",
            "--port",
            "19091",
        ],
        "env": {
            "OHA_YACHIYO_DESKTOP_PROVIDER_URL": "http://127.0.0.1:19091",
            "OHA_YACHIYO_DESKTOP_PROVIDER_ID": "local-headless-desktop",
            "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS": (
                "desktop.permissions,desktop.permission_preflight,"
                "desktop.active_window,desktop.running_apps,desktop.list_apps,"
                "desktop.windows,desktop.list_windows,desktop.ui_elements,"
                "desktop.read_ui,desktop.verify,app.status"
            ),
        },
        "smoke_command": ["python", "scripts/smoke_headless_desktop_provider.py"],
        "foreground_mutation_supported": False,
        "desktop_session_kind": "headless_read_only",
        "desktop_session_isolated": True,
        "foreground_takeover_required": False,
        "requires_real_sandbox_for": ["click", "type", "shortcut", "focus"],
        "isolated_provider": {
            "provider_id": "local-isolated-desktop",
            "provider_kind": "sandbox_desktop",
            "execution_mode": "isolated_desktop",
            "command": [
                "python",
                "scripts/run_isolated_desktop_provider.py",
                "--host",
                "127.0.0.1",
                "--port",
                "19093",
            ],
            "smoke_command": ["python", "scripts/smoke_isolated_desktop_provider.py"],
            "env": {
                "OHA_YACHIYO_DESKTOP_PROVIDER_URL": "http://127.0.0.1:19093",
                "OHA_YACHIYO_DESKTOP_PROVIDER_ID": "local-isolated-desktop",
                "OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS": ",".join(
                    CONTROLLED_DESKTOP_PROVIDER_TOOLS
                ),
                "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED": "true",
                "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND": "isolated_desktop",
                "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED": "true",
                "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED": "false",
            },
            "supported_tools": list(CONTROLLED_DESKTOP_PROVIDER_TOOLS),
            "foreground_mutation_supported": True,
            "keyboard_mouse_capture_supported": True,
            "desktop_session_kind": "isolated_desktop",
            "desktop_session_isolated": True,
            "foreground_takeover_required": False,
            "requires_runtime_approval": True,
        },
        "controlled_provider": {
            "provider_id": "local-controlled-desktop",
            "provider_kind": "sandbox_desktop",
            "execution_mode": "controlled_desktop",
            "command": [
                "python",
                "scripts/run_controlled_desktop_provider.py",
                "--host",
                "127.0.0.1",
                "--port",
                "19092",
            ],
            "smoke_command": [
                "python",
                "scripts/run_controlled_desktop_provider.py",
                "--manifest",
            ],
            "env": {
                "OHA_YACHIYO_DESKTOP_PROVIDER_URL": "http://127.0.0.1:19092",
                "OHA_YACHIYO_DESKTOP_PROVIDER_ID": "local-controlled-desktop",
                "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED": "true",
                "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND": "user_foreground",
                "OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED": "false",
                "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED": "true",
            },
            "foreground_mutation_supported": True,
            "keyboard_mouse_capture_supported": True,
            "desktop_session_kind": "user_foreground",
            "desktop_session_isolated": False,
            "foreground_takeover_required": True,
            "requires_runtime_approval": True,
        },
    },
}

_READ_ONLY_DESKTOP_PROVIDER_ROUTE_KEYS = (
    "desktop_provider_route_readonly",
    "desktop_provider_readonly_route",
    "route_readonly_desktop_provider",
)

_FOREGROUND_DESKTOP_PROVIDER_ROUTE_KEYS = (
    "desktop_provider_route_foreground",
    "desktop_provider_foreground_route",
    "route_foreground_desktop_provider",
)

_LOCAL_DESKTOP_PROVIDER_KEYS = (
    "desktop_provider_local_native",
    "desktop_provider_local",
    "local_desktop_provider",
)

_SIMULATED_DESKTOP_PROVIDER_ALLOW_KEYS = (
    "allow_simulated_desktop_provider",
    "desktop_provider_allow_simulated_execution",
    "allow_loopback_desktop_provider_execution",
    "desktop_allow_loopback_provider_execution",
)

_SIMULATED_DESKTOP_PROVIDER_ENV_ALLOW_KEYS = (
    "OHA_YACHIYO_ALLOW_SIMULATED_DESKTOP_PROVIDER",
    "OHA_YACHIYO_DESKTOP_PROVIDER_ALLOW_SIMULATED_EXECUTION",
    "OHA_YACHIYO_ALLOW_LOOPBACK_DESKTOP_PROVIDER_EXECUTION",
)

_READ_ONLY_DESKTOP_PROVIDER_TOOLS = frozenset(
    {
        "desktop.permissions",
        "desktop.permission_preflight",
        "desktop.active_window",
        "desktop.running_apps",
        "desktop.list_apps",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "desktop.read_ui",
        "desktop.inspect_app",
        "desktop.verify",
        "app.status",
    }
)

_KEYBOARD_MOUSE_CAPTURE_TOOLS = frozenset(
    {
        "app.open_and_safe_type_text",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "app.open_and_hotkey",
        "app.focus_and_hotkey",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.safe_shortcut",
        "desktop.safe_key",
        "desktop.safe_type_text",
        "desktop.safe_click",
        "desktop.safe_scroll",
        "desktop.search_submit",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.shortcut",
        "desktop.hotkey",
        "desktop.submit_foreground",
        "desktop.type",
        "desktop.type_text",
        "desktop.click",
        "desktop.close_window",
        "desktop.quit_app",
    }
)

_LOCAL_LOW_RISK_FOREGROUND_TOOLS = frozenset(
    {
        "app.open",
        "app.focus",
        "app.show",
        "app.focus_window",
        "desktop.open_app",
        "desktop.focus_app",
        "media.apple_music_play",
        "media.apple_music_open_and_play",
        "media.apple_music_control",
        "media.music_app_open_and_play",
        "media.music_app_control",
    }
)

# These semantic media tools currently contain local fallbacks that can open or
# focus a real app (for example Apple Music search after a library miss).  When
# a daily surface requests the background lane, keep the whole semantic action
# on that lane instead of letting its nominal ``tool_native`` classification
# bypass desktop routing.  A background adapter may advertise a verified
# composite later; primitive-only adapters must fail closed.
_BACKGROUND_MEDIA_PROVIDER_TOOLS = frozenset(
    {
        "media.apple_music_open_and_play",
        "media.apple_music_control",
        "media.music_app_open_and_play",
        "media.music_app_control",
    }
)

_USER_FOREGROUND_TAKEOVER_TOOLS = frozenset(
    {
        *_KEYBOARD_MOUSE_CAPTURE_TOOLS,
    }
)

_PROVIDER_START_STATUSES = frozenset(
    {
        "provider_required",
        "sandbox_keyboard_mouse_provider_required",
        "sandbox_desktop_session_required",
        "sandbox_adapter_required",
        "sandbox_tool_not_supported",
        "real_virtual_desktop_provider_required",
    }
)

_PROVIDER_START_BLOCKERS = frozenset(
    {
        "sandbox_desktop_provider_required",
        "sandbox_keyboard_mouse_provider_required",
        "sandbox_desktop_session_required",
        "sandbox_desktop_adapter_required",
        "sandbox_tool_not_supported",
        "isolated_desktop_provider_required",
        "loopback_desktop_backend",
        "desktop_backend_not_release_ready",
        "real_virtual_desktop_backend_required",
    }
)


def desktop_execution_policy_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str) and value.strip():
        return {"mode": value.strip()}
    return {}


def daily_entrypoint_desktop_execution_policy(
    *,
    surface: str = "chat",
) -> dict[str, Any]:
    """Default Chat/Bubble/Live2D policy: avoid taking over the user's desktop."""

    clean_surface = str(surface or "chat").strip() or "chat"
    return {
        "mode": "preview_input",
        "allow_live_foreground": False,
        "prefer_background_desktop": True,
        "prefer_isolated_desktop": False,
        "avoid_user_foreground_takeover": True,
        "require_sandbox_for_keyboard_mouse": False,
        "allow_media_control": True,
        "source": f"daily_{clean_surface}",
        "reason": (
            "Daily entrypoints prefer a background or isolated desktop provider; "
            "the user's foreground session requires explicit permission."
        ),
    }


def desktop_provider_session_auto_start_default() -> bool:
    value = os.environ.get("OHA_YACHIYO_DESKTOP_PROVIDER_AUTO_START", "")
    raw = value.strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return any(
        str(os.environ.get(key) or "").strip()
        for key in (
            "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
            "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL",
            "OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL",
            "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL",
            "OHA_YACHIYO_DESKTOP_PROVIDER_START_COMMAND",
            "OHA_YACHIYO_DESKTOP_PROVIDER_MANIFEST",
        )
    )


def desktop_provider_manifest_path_from_metadata(
    metadata: Mapping[str, Any] | None,
) -> str:
    if not isinstance(metadata, Mapping):
        return ""
    for key in _PROVIDER_MANIFEST_METADATA_KEYS:
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    for key in _PROVIDER_MANIFEST_METADATA_CONTAINERS:
        nested = metadata.get(key)
        if not isinstance(nested, Mapping):
            continue
        for manifest_key in _PROVIDER_MANIFEST_METADATA_KEYS:
            value = str(nested.get(manifest_key) or "").strip()
            if value:
                return value
    return ""


def desktop_provider_session_strict_foreground_default(
    metadata: Mapping[str, Any] | None = None,
) -> bool:
    """Prefer the provider path for foreground actions when it is configured."""

    if user_foreground_takeover_allowed(metadata):
        return False
    if _metadata_truthy(
        metadata,
        "desktop_provider_session_strict_foreground",
        "desktop_provider_session_enforce_foreground",
        "require_desktop_provider_for_foreground",
        "require_isolated_desktop_for_foreground",
    ):
        return True
    provider = sandbox_desktop_provider_status(
        _metadata_without_desktop_provider_health_probe(metadata),
        probe_health=False,
    )
    if _sandbox_provider_supports_strict_foreground(provider):
        return True
    return desktop_provider_session_auto_start_default()


def _metadata_without_desktop_provider_health_probe(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    payload = dict(metadata)
    for key in (
        "desktop_provider_health_probe",
        "probe_desktop_provider_health",
        "sandbox_provider_health_probe",
    ):
        payload[key] = False
    return payload


def _sandbox_provider_supports_strict_foreground(
    provider: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(provider, Mapping):
        return False
    if _optional_bool_value(provider.get("foreground_takeover_required")) is True:
        return False
    if _optional_bool_value(provider.get("keyboard_mouse_capture_supported")) is True:
        return True
    if _optional_bool_value(provider.get("foreground_mutation_supported")) is True:
        return True
    supported_tools = set(_string_list(provider.get("supported_tools")))
    return bool(supported_tools & _USER_FOREGROUND_TAKEOVER_TOOLS)


_LOW_RISK_CREATION_SHORTCUT_ACTIONS = frozenset(
    {"new_document", "new_note", "new_task"}
)

_APPROVAL_FIRST_KEYBOARD_MOUSE_TOOLS = frozenset(
    {
        "app.open_and_click_ui_element",
        "app.focus_and_click_ui_element",
        "app.open_and_type_into_ui_element",
        "app.focus_and_type_into_ui_element",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
        "desktop.click",
    }
)


def desktop_provider_session_auto_start_recommended_for_requests(
    requests: Any,
) -> bool:
    """Return true only when a request needs the managed isolated session.

    Background Cua requests are intentionally excluded because their MCP adapter
    owns a separate lazy lifecycle. Approval-first UI actions still wait for
    explicit user approval before an isolated provider session can start.
    """

    if isinstance(requests, Mapping):
        envelope = requests
        if _envelope_has_approval_first_request(envelope):
            return False
        if _execution_strategy_recommends_provider_auto_start(envelope):
            return True
        nested_requests = requests.get("requests")
        if isinstance(nested_requests, list):
            candidates = list(nested_requests)
        else:
            candidates = [requests]
    else:
        try:
            candidates = list(requests or [])
        except TypeError:
            candidates = []
    for request in candidates:
        if not isinstance(request, Mapping):
            continue
        if bool(request.get("approval_required")):
            continue
        if user_foreground_takeover_allowed(request):
            continue
        if _request_prefers_background_desktop(request):
            # The Cua MCP adapter owns its own lazy background lifecycle.  Sending
            # this request to the isolated-provider session manager would start a
            # different provider and can turn a missing background component into
            # an unrelated foreground/sandbox recovery flow.
            continue
        tool_name = _request_tool_name(request)
        if not tool_name:
            continue
        if tool_name in _APPROVAL_FIRST_KEYBOARD_MOUSE_TOOLS:
            continue
        if _low_risk_foreground_request_recommends_provider_auto_start(
            request,
            tool_name,
        ):
            return True
        if _request_readonly_provider_auto_start_recommended(request, tool_name):
            return True
        if _request_policy_recommends_provider_auto_start(request, tool_name):
            return True
        if _low_risk_creation_shortcut_request(tool_name, request):
            return True
        if tool_name in _USER_FOREGROUND_TAKEOVER_TOOLS:
            return True
    return False


def _request_prefers_background_desktop(request: Mapping[str, Any]) -> bool:
    route = request.get("desktop_execution_route")
    if isinstance(route, Mapping):
        provider_kind = str(
            route.get("selected_provider_kind")
            or route.get("provider_kind")
            or ""
        ).strip()
        if provider_kind == CUA_BACKGROUND_PROVIDER_KIND:
            return True
        if route.get("background_desktop_preferred") is True:
            return True
    for key in (
        "sandbox_provider",
        "desktop_execution_provider",
        "sandbox_desktop_provider",
        "desktop_sandbox_provider",
    ):
        provider = request.get(key)
        if isinstance(provider, Mapping) and str(
            provider.get("provider_kind") or ""
        ).strip() == CUA_BACKGROUND_PROVIDER_KIND:
            return True
    policy = desktop_execution_policy_payload(request.get("desktop_execution_policy"))
    if not policy:
        policy = desktop_execution_policy_payload(
            request.get("yachiyo_desktop_execution_policy")
        )
    return policy.get("prefer_background_desktop") is True


def _execution_strategy_recommends_provider_auto_start(
    envelope: Mapping[str, Any],
) -> bool:
    strategy = envelope.get("execution_strategy")
    if not isinstance(strategy, Mapping):
        return False
    if _envelope_has_approval_first_request(envelope):
        return False
    if _envelope_only_uses_local_low_risk_foreground(envelope):
        return False
    preferred_environment = str(
        strategy.get("preferred_environment") or ""
    ).strip()
    if preferred_environment != "isolated_desktop":
        return False
    if bool(strategy.get("approval_required")):
        return False
    if _positive_int(strategy.get("approval_step_count")):
        return False
    if _positive_int(strategy.get("handoff_step_count")):
        return False
    if _positive_int(strategy.get("keyboard_mouse_step_count")):
        return True
    if _positive_int(strategy.get("foreground_control_step_count")):
        return True
    if bool(strategy.get("sandbox_required")):
        return True
    if _positive_int(strategy.get("sandbox_recommended_step_count")):
        return True
    return False


def _envelope_has_approval_first_request(envelope: Mapping[str, Any]) -> bool:
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return False
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        if bool(request.get("approval_required")):
            return True
        if _request_tool_name(request) in _APPROVAL_FIRST_KEYBOARD_MOUSE_TOOLS:
            return True
    return False


def _envelope_only_uses_local_low_risk_foreground(
    envelope: Mapping[str, Any],
) -> bool:
    requests = envelope.get("requests")
    if not isinstance(requests, list):
        return False
    saw_low_risk_foreground = False
    for request in requests:
        if not isinstance(request, Mapping):
            continue
        tool_name = _request_tool_name(request)
        if not tool_name:
            continue
        if is_readonly_desktop_provider_tool(tool_name):
            continue
        if _local_low_risk_foreground_tool_allowed(tool_name, request):
            saw_low_risk_foreground = True
            continue
        return False
    return saw_low_risk_foreground


def _request_tool_name(request: Mapping[str, Any]) -> str:
    return str(request.get("tool") or request.get("tool_name") or "").strip()


def _positive_int(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        return int(value or 0) > 0
    except (TypeError, ValueError):
        return False


def _low_risk_creation_shortcut_request(
    tool_name: str,
    request: Mapping[str, Any],
) -> bool:
    if tool_name not in {
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
        "desktop.safe_shortcut",
    }:
        return False
    payload = request.get("input")
    if not isinstance(payload, Mapping):
        payload = request.get("input_preview")
    if not isinstance(payload, Mapping):
        return False
    action = str(payload.get("action") or "").strip()
    return action in _LOW_RISK_CREATION_SHORTCUT_ACTIONS


def _low_risk_foreground_request_recommends_provider_auto_start(
    request: Mapping[str, Any],
    tool_name: str,
) -> bool:
    if tool_name not in _LOCAL_LOW_RISK_FOREGROUND_TOOLS:
        return False
    policy = desktop_execution_policy_payload(request.get("desktop_execution_policy"))
    if not policy:
        policy = desktop_execution_policy_payload(
            request.get("yachiyo_desktop_execution_policy")
        )
    if policy.get("allow_live_foreground") is True:
        return False
    mode = str(policy.get("mode") or "").strip().lower().replace("-", "_")
    return mode not in {"allow", "supervised_live", "live", "foreground"}


def _request_policy_recommends_provider_auto_start(
    request: Mapping[str, Any],
    tool_name: str,
) -> bool:
    if not _desktop_provider_session_candidate_tool(tool_name):
        return False
    if is_readonly_desktop_provider_tool(tool_name):
        return False
    if _local_low_risk_foreground_tool_allowed(tool_name, request):
        return False
    policy = desktop_execution_policy_payload(request.get("desktop_execution_policy"))
    if not policy:
        policy = desktop_execution_policy_payload(
            request.get("yachiyo_desktop_execution_policy")
        )
    if not policy:
        return False
    if policy.get("allow_live_foreground") is True:
        return False
    return any(
        bool(policy.get(key))
        for key in (
            "prefer_isolated_desktop",
            "avoid_user_foreground_takeover",
            "require_sandbox_for_keyboard_mouse",
        )
    )


def _request_readonly_provider_auto_start_recommended(
    request: Mapping[str, Any],
    tool_name: str,
) -> bool:
    if not is_readonly_desktop_provider_tool(tool_name):
        return False
    route = request.get("desktop_execution_route")
    route_payload = dict(route) if isinstance(route, Mapping) else {}
    if route_payload:
        status = str(route_payload.get("status") or "").strip()
        blockers = set(_string_list(route_payload.get("blocking_conditions")))
        if route_payload.get("can_auto_start") is True and (
            status in _PROVIDER_START_STATUSES
            or bool(blockers & _PROVIDER_START_BLOCKERS)
        ):
            return True
    if not desktop_readonly_provider_route_requested(request):
        policy = desktop_execution_policy_payload(request.get("desktop_execution_policy"))
        if not _metadata_truthy(
            policy,
            "prefer_isolated_desktop",
            "desktop_provider_route_readonly",
            "provider_route_readonly",
        ):
            return False
    return _desktop_provider_session_auto_start_requested(request)


def _local_low_risk_foreground_tool_allowed(
    tool_name: str,
    request: Mapping[str, Any],
) -> bool:
    if str(tool_name or "").strip() not in _LOCAL_LOW_RISK_FOREGROUND_TOOLS:
        return False
    policy = desktop_execution_policy_payload(request.get("desktop_execution_policy"))
    if not policy:
        policy = desktop_execution_policy_payload(
            request.get("yachiyo_desktop_execution_policy")
        )
    source = str(policy.get("source") or request.get("source") or "").strip()
    if source and not source.startswith("daily_"):
        return False
    if _metadata_truthy(
        request,
        "desktop_provider_session_strict_foreground",
        "desktop_provider_session_enforce_foreground",
        "require_desktop_provider_for_foreground",
        "require_isolated_desktop_for_foreground",
    ):
        return False
    return True


def _desktop_provider_session_candidate_tool(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return (
        clean_tool in _USER_FOREGROUND_TAKEOVER_TOOLS
        or clean_tool.startswith("app.")
        or clean_tool.startswith("desktop.")
        or clean_tool.startswith("media.")
    )


def agent_studio_desktop_execution_policy() -> dict[str, Any]:
    """Studio is the explicit supervised execution surface."""

    return {
        "mode": "supervised_live",
        "allow_live_foreground": False,
        "prefer_isolated_desktop": True,
        "avoid_user_foreground_takeover": True,
        "require_sandbox_for_keyboard_mouse": True,
        "source": "agent_studio",
        "reason": (
            "Agent Studio is the supervised desktop execution and debugging surface; "
            "keyboard/mouse actions prefer an isolated desktop provider."
        ),
    }


def sandbox_desktop_provider_status(
    metadata: Mapping[str, Any] | None = None,
    *,
    probe_health: bool = False,
) -> dict[str, Any]:
    """Return the runtime-visible sandbox desktop provider status."""

    should_probe_health = probe_health or _metadata_truthy(
        metadata,
        "desktop_provider_health_probe",
        "probe_desktop_provider_health",
        "sandbox_provider_health_probe",
    )
    explicit_provider = _sandbox_provider_payload(metadata)
    background_requested = _background_desktop_provider_requested(metadata)
    explicit_provider_kind = str(
        explicit_provider.get("provider_kind") or ""
    ).strip()
    # Daily requests select the background lane before local or VM providers.
    # A persisted local-provider snapshot must not silently override that choice.
    if background_requested and explicit_provider_kind in {"", "local_desktop"}:
        explicit_provider = {}
    configured_isolated_provider: dict[str, Any] = {}
    if not explicit_provider:
        configured_isolated_provider = (
            _sandbox_provider_payload_from_env(probe_health=should_probe_health)
            or _sandbox_provider_payload_from_manifest(probe_health=should_probe_health)
        )
    if explicit_provider:
        provider = explicit_provider
    elif background_requested:
        background_provider = _background_desktop_provider_payload(
            metadata,
            probe_health=should_probe_health,
        )
        isolated_provider_ready = (
            str(configured_isolated_provider.get("provider_kind") or "").strip()
            == "sandbox_desktop"
            and bool(configured_isolated_provider.get("available"))
            and bool(configured_isolated_provider.get("adapter_ready"))
        )
        provider = (
            background_provider
            if bool(background_provider.get("available"))
            and bool(background_provider.get("adapter_ready"))
            else (
                configured_isolated_provider
                if isolated_provider_ready
                else background_provider
            )
        )
    else:
        provider = configured_isolated_provider or _local_desktop_provider_payload(metadata)
    if provider:
        return _normalized_sandbox_desktop_provider_status(provider)
    return dict(_SANDBOX_DESKTOP_PROVIDER_DEFAULT)


def _normalized_sandbox_desktop_provider_status(
    provider: Mapping[str, Any],
) -> dict[str, Any]:
    payload = {**_SANDBOX_DESKTOP_PROVIDER_DEFAULT, **dict(provider)}
    payload["available"] = bool(payload.get("available"))
    payload["adapter_ready"] = bool(payload.get("adapter_ready"))
    if payload["available"]:
        if str(payload.get("status") or "").strip() == "provider_required":
            payload["status"] = "available"
        payload["blocking_conditions"] = _string_list(
            provider.get("blocking_conditions")
        )
    else:
        blockers = _string_list(payload.get("blocking_conditions"))
        payload["blocking_conditions"] = blockers or [
            "sandbox_desktop_provider_required"
        ]
    payload["supported_tools"] = _string_list(payload.get("supported_tools"))
    payload["recommended_for"] = _string_list(payload.get("recommended_for"))
    payload["health"] = _health_payload(provider.get("health"))
    payload["provider_contract"] = _provider_contract_payload(payload)
    return _sandbox_provider_public_payload(payload)


def desktop_execution_policy_mode(policy: Mapping[str, Any] | str | None) -> str:
    payload = desktop_execution_policy_payload(policy)
    if not payload:
        return "allow"
    if payload.get("allow_live_foreground") is True:
        return "allow"
    if payload.get("allow_live_foreground") is False:
        return "preview"
    raw = str(
        payload.get("mode")
        or payload.get("live_foreground")
        or payload.get("foreground_input")
        or ""
    ).strip().lower().replace("-", "_")
    if raw in {
        "allow",
        "allowed",
        "live",
        "supervised_live",
        "foreground",
        "live_foreground",
    }:
        return "allow"
    if raw in {
        "handoff",
        "handoff_required",
        "user_handoff",
        "user_handoff_required",
    }:
        return "handoff"
    if raw in {
        "preview_input",
        "input_preview",
        "foreground_input_preview",
    }:
        return "preview_input"
    if raw in {
        "preview",
        "dry_run",
        "dryrun",
        "observe_only",
        "observation_only",
        "read_only",
        "no_live_foreground",
        "sandbox_preferred",
    }:
        return "preview"
    return "allow"


def desktop_execution_route_decision(
    tool_name: str,
    *,
    policy: Mapping[str, Any] | str | None = None,
    execution_mode: Mapping[str, Any] | Any | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    clean_tool = str(tool_name or "").strip()
    policy_payload = desktop_execution_policy_payload(policy)
    policy_mode = desktop_execution_policy_mode(policy_payload)
    mode_payload = _execution_mode_payload(execution_mode)
    decision_context = _route_policy_metadata_context(policy_payload, metadata)
    foreground_takeover_allowed = (
        user_foreground_takeover_allowed(metadata)
        or user_foreground_takeover_allowed(
            {"desktop_execution_policy": policy_payload}
        )
        or policy_payload.get("allow_live_foreground") is True
    )
    readonly_provider_requested = (
        desktop_readonly_provider_route_requested(metadata)
        or _metadata_truthy(
            policy_payload,
            "prefer_isolated_desktop",
            "desktop_provider_route_readonly",
            "provider_route_readonly",
        )
    )
    foreground_provider_requested = (
        desktop_foreground_provider_route_requested(metadata)
        or _metadata_truthy(
            policy_payload,
            "prefer_background_desktop",
            "prefer_isolated_desktop",
            "avoid_user_foreground_takeover",
            "require_sandbox_for_keyboard_mouse",
            "desktop_provider_route_foreground",
            "provider_route_foreground",
        )
        or foreground_takeover_allowed
    )
    foreground_control = bool(mode_payload.get("foreground_control"))
    keyboard_mouse_capture = bool(mode_payload.get("keyboard_mouse_capture"))
    foreground_required = foreground_control or keyboard_mouse_capture
    passive_permission_diagnostic = (
        clean_tool == "desktop.permissions"
        and mode_payload.get("mode") == "read_only_observation"
        and not foreground_required
    )
    sandbox_required = bool(mode_payload.get("sandbox_recommended")) or (
        str(policy_payload.get("mode") or "").strip().lower().replace("-", "_")
        == "sandbox_preferred"
    )
    background_desktop_preferred = _metadata_truthy(
        decision_context,
        "prefer_background_desktop",
    )
    # Route evaluation stays side-effect free unless the execution boundary
    # explicitly requests a provider liveness probe.  Planning and projection
    # paths call this function too, so inferring a probe from the policy alone
    # would perform network I/O while merely describing a route.
    should_probe_provider_health = _metadata_truthy(
        decision_context,
        "desktop_provider_health_probe",
        "probe_desktop_provider_health",
        "sandbox_provider_health_probe",
    )
    sandbox_provider = {} if passive_permission_diagnostic else sandbox_desktop_provider_status(
        decision_context,
        probe_health=should_probe_provider_health,
    )
    if (
        str(sandbox_provider.get("provider_kind") or "").strip()
        == CUA_BACKGROUND_PROVIDER_KIND
        and not sandbox_desktop_provider_can_execute_tool(
            sandbox_provider,
            clean_tool,
        )
    ):
        # Provider choice is per capability, not merely per availability.  A
        # healthy but intentionally narrow background lane must not shadow a
        # configured isolated provider that can execute this particular tool.
        isolated_fallback = _configured_isolated_provider_for_tool(
            decision_context,
            clean_tool,
        )
        if isolated_fallback:
            sandbox_provider = isolated_fallback
    local_fallback_provider_required = _metadata_truthy(
        policy_payload,
        "require_background_provider_for_local_fallback_tools",
    )
    isolated_desktop_preferred = _metadata_truthy(
        decision_context,
        "prefer_isolated_desktop",
        "require_sandbox_for_keyboard_mouse",
    ) or _desktop_provider_session_isolation_requested(decision_context)
    direct_desktop_preferred = bool(
        not background_desktop_preferred
        and not isolated_desktop_preferred
        and (foreground_takeover_allowed or policy_mode == "allow")
    )
    isolation = str(mode_payload.get("isolation") or "none").strip() or "none"
    execution_mode_name = str(mode_payload.get("mode") or "tool_native").strip()
    structured_apple_music_control_allowed = bool(
        clean_tool == "media.apple_music_control"
        and policy_payload.get("allow_media_control") is True
        and execution_mode_name == "tool_native"
        and isolation == "process"
        and not foreground_required
    )
    route = {
        "route_id": f"desktop-route:{clean_tool or 'tool'}",
        "tool_name": clean_tool,
        "requested_mode": str(policy_payload.get("mode") or policy_mode or "allow"),
        "selected_provider_kind": isolation,
        "selected_provider_id": _provider_id_for_isolation(isolation),
        "status": "ready",
        "can_execute": True,
        "can_auto_start": True,
        "sandbox_required": False,
        "background_desktop_preferred": background_desktop_preferred,
        "isolated_desktop_preferred": isolated_desktop_preferred,
        "foreground_takeover_allowed": foreground_takeover_allowed,
        "desktop_execution_session_policy": (
            "explicit_user_foreground"
            if foreground_takeover_allowed
            else (
                "background_preferred"
                if background_desktop_preferred
                else (
                    "isolated_preferred"
                    if isolated_desktop_preferred
                    else "structured_runtime"
                )
            )
        ),
        "user_foreground_takeover_risk": False,
        "requires_user_foreground_session": False,
        "fallback_mode": "",
        "reason": "Tool can run through its structured runtime provider.",
        "blocking_conditions": [],
        "source": "runtime",
    }
    if not clean_tool:
        return {
            **route,
            "status": "missing_tool",
            "can_execute": False,
            "can_auto_start": False,
            "reason": "No executable tool was selected.",
            "blocking_conditions": ["missing_tool"],
        }

    if passive_permission_diagnostic:
        # This adapter reads cached host permission readiness. It neither
        # observes a provider-owned desktop target nor sends foreground input,
        # and must remain available to diagnose a missing desktop backend.
        return {
            **route,
            "selected_provider_kind": "none",
            "selected_provider_id": "",
            "provider_execution_required": False,
            "reason": "Read passive host permission readiness through the local broker.",
        }

    if background_desktop_preferred and (
        (
            clean_tool in _BACKGROUND_MEDIA_PROVIDER_TOOLS
            and not structured_apple_music_control_allowed
        )
        or (
            local_fallback_provider_required
            and is_local_low_risk_foreground_tool(clean_tool)
        )
    ):
        # Do not execute the legacy local media implementation under a
        # background-first policy: several of its error/search paths call
        # app.open/app.focus and would contend with the user's foreground.
        # _sandbox_route_decision also preserves a user-handoff fallback for a
        # missing/primitive-only Cua provider.
        return _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
    local_provider = _local_desktop_provider_payload(decision_context)
    if (
        local_provider
        and direct_desktop_preferred
        and sandbox_desktop_provider_can_execute_tool(local_provider, clean_tool)
        and (
            is_readonly_desktop_provider_tool(clean_tool)
            or foreground_required
            or execution_mode_name == "supervised_live"
            or is_local_low_risk_foreground_tool(clean_tool)
        )
    ):
        local_route = _sandbox_route_decision(
            route,
            local_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            local_route,
            (
                _readonly_desktop_provider_route_reason(local_provider)
                if is_readonly_desktop_provider_tool(clean_tool)
                else _foreground_desktop_provider_route_reason(local_provider)
            ),
        )
    if _running_isolated_sandbox_provider_can_execute(sandbox_provider, clean_tool):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            sandbox_route,
            _desktop_provider_ready_reason(sandbox_provider),
        )
    if (
        local_provider
        and not background_desktop_preferred
        and not isolated_desktop_preferred
        and _local_desktop_fallback_allowed(sandbox_provider, clean_tool)
        and is_readonly_desktop_provider_tool(clean_tool)
        and sandbox_desktop_provider_can_execute_tool(local_provider, clean_tool)
    ):
        local_route = _sandbox_route_decision(
            route,
            local_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            local_route,
            _readonly_desktop_provider_route_reason(local_provider),
        )
    if (
        local_provider
        and not background_desktop_preferred
        and not isolated_desktop_preferred
        and _local_desktop_fallback_allowed(sandbox_provider, clean_tool)
        and _local_low_risk_foreground_tool_allowed(clean_tool, decision_context)
        and sandbox_desktop_provider_can_execute_tool(local_provider, clean_tool)
    ):
        local_route = _sandbox_route_decision(
            route,
            local_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            local_route,
            _foreground_desktop_provider_route_reason(local_provider),
        )
    if (
        foreground_provider_requested
        and foreground_required
        and not direct_desktop_preferred
        and not bool(sandbox_provider.get("available"))
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        if (
            str(sandbox_provider.get("provider_kind") or "").strip()
            != CUA_BACKGROUND_PROVIDER_KIND
            and _desktop_provider_session_auto_start_requested(decision_context)
        ):
            provider_description = (
                "background desktop provider"
                if str(sandbox_provider.get("provider_kind") or "").strip()
                == CUA_BACKGROUND_PROVIDER_KIND
                else "isolated desktop provider"
            )
            return _route_with_provider_auto_start(
                sandbox_route,
                reason=(
                    f"Foreground desktop action should auto-start the {provider_description} "
                    "instead of taking over the user's session."
                ),
            )
        return sandbox_route
    if (
        readonly_provider_requested
        and is_readonly_desktop_provider_tool(clean_tool)
        and sandbox_desktop_provider_can_execute_tool(sandbox_provider, clean_tool)
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            sandbox_route,
            _readonly_desktop_provider_route_reason(sandbox_provider),
        )
    if (
        readonly_provider_requested
        and is_readonly_desktop_provider_tool(clean_tool)
        and _desktop_provider_session_auto_start_requested(decision_context)
        and str(sandbox_provider.get("provider_kind") or "").strip()
        != CUA_BACKGROUND_PROVIDER_KIND
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_provider_auto_start(
            sandbox_route,
            reason=(
                "Readonly desktop discovery should use the configured isolated "
                "desktop provider when it can be auto-started."
            ),
        )
    if (
        readonly_provider_requested
        and is_readonly_desktop_provider_tool(clean_tool)
        and not direct_desktop_preferred
    ):
        # A background/isolated observation request must never fall through to
        # the local broker merely because the selected provider is partial.
        # That would mix the agent-owned target with the user's foreground
        # desktop and invalidate every later verifier.
        return _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
    if (
        foreground_provider_requested
        and is_local_low_risk_foreground_tool(clean_tool)
        and sandbox_desktop_provider_can_execute_tool(sandbox_provider, clean_tool)
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            sandbox_route,
            _foreground_desktop_provider_route_reason(sandbox_provider),
        )
    if (
        foreground_provider_requested
        and (foreground_required or execution_mode_name == "supervised_live")
        and not direct_desktop_preferred
        and _sandbox_provider_requires_keyboard_mouse_sandbox(
            sandbox_provider,
            clean_tool,
        )
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return {
            **sandbox_route,
            "reason": (
                "Keyboard and mouse foreground actions require the controlled "
                "desktop provider so the agent does not take over the user's session."
            ),
        }
    if (
        foreground_provider_requested
        and (foreground_required or execution_mode_name == "supervised_live")
        and not direct_desktop_preferred
        and _sandbox_provider_requires_isolated_foreground_session(
            sandbox_provider,
            clean_tool,
            decision_context,
        )
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return {
            **sandbox_route,
            "status": "sandbox_desktop_session_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": True,
            "fallback_mode": "supervised_live",
            "reason": (
                "Foreground desktop actions must run inside an isolated "
                "desktop session unless the user explicitly allows foreground takeover."
            ),
            "blocking_conditions": ["sandbox_desktop_session_required"],
        }
    if (
        foreground_provider_requested
        and (foreground_required or execution_mode_name == "supervised_live")
        and sandbox_desktop_provider_can_execute_tool(sandbox_provider, clean_tool)
    ):
        sandbox_route = _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
        return _route_with_ready_reason(
            sandbox_route,
            _foreground_desktop_provider_route_reason(sandbox_provider),
        )
    if not foreground_required and execution_mode_name != "supervised_live":
        return route
    if policy_mode == "allow":
        return {
            **route,
            "selected_provider_kind": "none",
            "selected_provider_id": "",
            "status": "supervised_live",
            "reason": "Foreground desktop execution is allowed by the current policy.",
            "fallback_mode": "supervised_live",
        }
    if policy_mode == "handoff":
        return {
            **route,
            "status": "handoff_required",
            "can_execute": False,
            "can_auto_start": False,
            "fallback_mode": "user_handoff",
            "reason": "The current policy requires the user to perform foreground desktop input.",
            "blocking_conditions": ["desktop_execution_handoff_required"],
        }
    if sandbox_required:
        return _sandbox_route_decision(
            route,
            sandbox_provider,
            clean_tool,
            decision_context,
        )
    return {
        **route,
        "status": "preview_required",
        "can_execute": False,
        "can_auto_start": False,
        "fallback_mode": "supervised_live",
        "reason": "Foreground desktop execution is blocked by preview policy.",
        "blocking_conditions": ["desktop_execution_preview_required"],
    }


def with_agent_studio_desktop_execution_policy(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(metadata) if isinstance(metadata, Mapping) else {}
    payload.setdefault("desktop_provider_health_probe", True)
    payload.setdefault("desktop_provider_route_readonly", True)
    payload.setdefault("desktop_provider_route_foreground", True)
    payload.setdefault("desktop_provider_local_native", True)
    payload.setdefault("runtime_planner_preflight_ui_before_action", True)
    if _has_desktop_execution_policy(payload):
        return payload
    payload["desktop_execution_policy"] = agent_studio_desktop_execution_policy()
    return payload


def desktop_readonly_provider_route_requested(
    metadata: Mapping[str, Any] | None,
) -> bool:
    return _metadata_truthy(metadata, *_READ_ONLY_DESKTOP_PROVIDER_ROUTE_KEYS)


def desktop_foreground_provider_route_requested(
    metadata: Mapping[str, Any] | None,
) -> bool:
    return _metadata_truthy(metadata, *_FOREGROUND_DESKTOP_PROVIDER_ROUTE_KEYS)


def is_readonly_desktop_provider_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in _READ_ONLY_DESKTOP_PROVIDER_TOOLS


def is_user_foreground_takeover_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in _USER_FOREGROUND_TAKEOVER_TOOLS


def is_local_low_risk_foreground_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in _LOCAL_LOW_RISK_FOREGROUND_TOOLS


def local_low_risk_foreground_tool_allowed(
    tool_name: str,
    request: Mapping[str, Any],
) -> bool:
    return _local_low_risk_foreground_tool_allowed(tool_name, request)


def user_foreground_takeover_allowed(metadata: Mapping[str, Any] | None) -> bool:
    policy: dict[str, Any] = {}
    if isinstance(metadata, Mapping):
        for key in (
            "desktop_execution_policy",
            "yachiyo_desktop_execution_policy",
            "desktop_interaction_policy",
        ):
            policy = desktop_execution_policy_payload(metadata.get(key))
            if policy:
                break
    isolated_keys = (
        "prefer_isolated_desktop",
        "avoid_user_foreground_takeover",
        "require_sandbox_for_keyboard_mouse",
        "desktop_provider_session_strict_foreground",
        "desktop_provider_session_enforce_foreground",
        "require_desktop_provider_for_foreground",
        "require_isolated_desktop_for_foreground",
    )
    if (
        _desktop_provider_session_isolation_requested(metadata)
        or _metadata_truthy(metadata, *isolated_keys)
        or _metadata_truthy(
            policy,
            *isolated_keys,
        )
    ):
        return False
    foreground_keys = (
        "allow_user_foreground_takeover",
        "desktop_allow_user_foreground_takeover",
        "allow_nonisolated_desktop_provider",
        "allow_live_foreground",
    )
    return _metadata_truthy(metadata, *foreground_keys) or _metadata_truthy(
        policy,
        *foreground_keys,
    )


def _desktop_provider_session_isolation_requested(
    metadata: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    session = metadata.get("desktop_provider_session")
    if isinstance(session, Mapping) and bool(session.get("needed")):
        session_kind = str(session.get("desktop_session_kind") or "").strip()
        return bool(
            _optional_bool_value(session.get("desktop_session_isolated")) is True
            or session_kind in {"isolated_desktop", "sandbox_desktop", "virtual_desktop"}
        )
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _desktop_provider_session_isolation_requested(nested_metadata)
    return False


def _desktop_provider_session_auto_start_requested(
    metadata: Mapping[str, Any] | None,
) -> bool:
    explicit = _metadata_bool(
        metadata,
        "desktop_provider_session_auto_start",
        "desktop_provider_auto_start",
        "auto_start_desktop_provider_session",
        "auto_start_isolated_desktop_provider",
    )
    if explicit is not None:
        return explicit
    return desktop_provider_session_auto_start_default()


def sandbox_desktop_provider_can_execute_tool(
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
) -> bool:
    if not bool(sandbox_provider.get("available")):
        return False
    if not bool(sandbox_provider.get("adapter_ready")):
        return False
    supported_tools = _string_list(sandbox_provider.get("supported_tools"))
    return not supported_tools or str(tool_name or "").strip() in supported_tools


def _configured_isolated_provider_for_tool(
    metadata: Mapping[str, Any] | None,
    tool_name: str,
) -> dict[str, Any]:
    should_probe_health = _metadata_truthy(
        metadata,
        "desktop_provider_health_probe",
        "probe_desktop_provider_health",
        "sandbox_provider_health_probe",
    )
    candidate = _sandbox_provider_payload_from_env(
        probe_health=should_probe_health,
    ) or _sandbox_provider_payload_from_manifest(
        probe_health=should_probe_health,
    )
    if not candidate:
        return {}
    provider = _normalized_sandbox_desktop_provider_status(candidate)
    provider_kind = str(provider.get("provider_kind") or "").strip()
    session_kind = str(provider.get("desktop_session_kind") or "").strip()
    isolated = bool(
        _optional_bool_value(provider.get("desktop_session_isolated")) is True
        or provider_kind in {"sandbox_desktop", "isolated_desktop", "virtual_desktop"}
        or session_kind in {"sandbox_desktop", "isolated_desktop", "virtual_desktop"}
    )
    if (
        not isolated
        or provider_kind in {CUA_BACKGROUND_PROVIDER_KIND, "local_desktop"}
        or not sandbox_desktop_provider_can_execute_tool(provider, tool_name)
    ):
        return {}
    return provider


def with_daily_entrypoint_desktop_execution_policy(
    metadata: Mapping[str, Any] | None,
    *,
    surface: str = "chat",
) -> dict[str, Any]:
    payload = dict(metadata) if isinstance(metadata, Mapping) else {}
    payload.setdefault("desktop_provider_health_probe", True)
    payload.setdefault("desktop_provider_route_readonly", True)
    payload.setdefault("desktop_provider_route_foreground", True)
    payload.setdefault("desktop_provider_local_native", True)
    payload.setdefault("runtime_planner_preflight_ui_before_action", True)
    if user_foreground_takeover_allowed(payload):
        payload["desktop_execution_policy"] = {
            **daily_entrypoint_desktop_execution_policy(surface=surface),
            "mode": "allow",
            "allow_live_foreground": True,
            "prefer_background_desktop": False,
            "prefer_isolated_desktop": False,
            "avoid_user_foreground_takeover": False,
            "require_sandbox_for_keyboard_mouse": False,
            "reason": (
                "Daily entrypoint is explicitly allowed to use the user's foreground "
                "desktop session for this request."
            ),
        }
        return payload
    if _has_desktop_execution_policy(payload):
        return payload
    payload["desktop_execution_policy"] = daily_entrypoint_desktop_execution_policy(
        surface=surface,
    )
    return payload


def runtime_execution_envelope_with_desktop_execution_policy(
    envelope: Mapping[str, Any],
    policy: Mapping[str, Any] | str,
) -> dict[str, Any]:
    payload = dict(envelope)
    clean_policy = desktop_execution_policy_payload(policy)
    if not clean_policy:
        return payload
    inherited_envelope_policy = not _has_desktop_execution_policy(payload)
    _ensure_canonical_desktop_execution_policy(payload, clean_policy)
    effective_policy = (
        desktop_execution_policy_payload(payload.get("desktop_execution_policy"))
        or clean_policy
    )
    if inherited_envelope_policy:
        # An aggregate route projected before the task policy existed is only
        # cached execution context, not immutable planner authority.  Keeping it
        # can filter requests before the execution boundary gets a chance to
        # route them against the inherited policy.
        payload.pop("desktop_execution_route", None)
    requests = payload.get("requests")
    if isinstance(requests, list):
        payload["requests"] = [
            _runtime_execution_request_with_desktop_execution_policy(
                request,
                effective_policy,
            )
            if isinstance(request, Mapping)
            else request
            for request in requests
        ]
    return payload


def _has_desktop_execution_policy(payload: Mapping[str, Any]) -> bool:
    return any(
        bool(desktop_execution_policy_payload(payload.get(key)))
        for key in (
            "desktop_execution_policy",
            "yachiyo_desktop_execution_policy",
            "desktop_interaction_policy",
        )
    )


def _runtime_execution_request_with_desktop_execution_policy(
    request: Mapping[str, Any],
    policy: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(request)
    inherited_request_policy = not _has_desktop_execution_policy(payload)
    _ensure_canonical_desktop_execution_policy(payload, policy)
    if inherited_request_policy:
        # A route without a request policy was evaluated in a different
        # context. Drop only that derived snapshot; tool, input, dependency,
        # approval, and explicit provider authority remain unchanged. The
        # execution boundary will recompute the route from the inherited policy.
        payload.pop("desktop_execution_route", None)
    return payload


def _ensure_canonical_desktop_execution_policy(
    payload: dict[str, Any],
    fallback_policy: Mapping[str, Any],
) -> None:
    if desktop_execution_policy_payload(payload.get("desktop_execution_policy")):
        return
    for key in ("yachiyo_desktop_execution_policy", "desktop_interaction_policy"):
        policy = desktop_execution_policy_payload(payload.get(key))
        if policy:
            payload["desktop_execution_policy"] = policy
            return
    payload["desktop_execution_policy"] = dict(fallback_policy)


def _sandbox_provider_payload(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(metadata, Mapping):
        return {}
    explicit_provider: dict[str, Any] = {}
    for key in (
        "desktop_execution_provider",
        "sandbox_desktop_provider",
        "sandbox_provider",
        "desktop_sandbox_provider",
    ):
        value = metadata.get(key)
        if isinstance(value, Mapping):
            explicit_provider = dict(value)
            break
    provider_session = _sandbox_provider_payload_from_desktop_provider_session(metadata)
    if explicit_provider and provider_session:
        return _sandbox_provider_payload_with_session_context(
            explicit_provider,
            provider_session,
        )
    if explicit_provider:
        return explicit_provider
    if provider_session:
        return provider_session
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _sandbox_provider_payload(nested_metadata)
    return {}


def _background_desktop_provider_payload(
    metadata: Mapping[str, Any] | None,
    *,
    probe_health: bool = False,
) -> dict[str, Any]:
    if not _background_desktop_provider_requested(metadata):
        return {}
    provider_status = cua_background_provider_status(probe_health=probe_health)
    return {
        **provider_status,
        "provider_kind": CUA_BACKGROUND_PROVIDER_KIND,
        "recommended_for": ["background_control", "keyboard_mouse_capture"],
        "diagnostic_route": "/yachiyo/studio/tools",
        "source": str(provider_status.get("source") or "cua_driver_discovery"),
    }


def _background_desktop_provider_requested(
    metadata: Mapping[str, Any] | None,
) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    if _metadata_truthy(metadata, "prefer_background_desktop"):
        return True
    for key in ("desktop_execution_policy", "desktop_execution_route", "metadata"):
        nested = metadata.get(key)
        if nested is metadata or not isinstance(nested, Mapping):
            continue
        if _background_desktop_provider_requested(nested):
            return True
    return False


def _sandbox_provider_payload_with_session_context(
    provider: Mapping[str, Any],
    provider_session: Mapping[str, Any],
) -> dict[str, Any]:
    payload = dict(provider)
    for key in (
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "keyboard_mouse_capture_supported",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "provider_contract",
        "provider_manifest_evidence",
        "provider_conformance",
    ):
        value = provider_session.get(key)
        if payload.get(key) in (None, "", [], {}) and value not in (None, "", [], {}):
            payload[key] = value
    return payload


def _sandbox_provider_payload_from_desktop_provider_session(
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    session = metadata.get("desktop_provider_session")
    if not isinstance(session, Mapping):
        return {}
    running = bool(session.get("running"))
    provider_id = str(session.get("provider_id") or "").strip()
    url = str(session.get("url") or "").strip()
    tool_names = _string_list(session.get("tool_names")) or _string_list(
        session.get("supported_tools")
    )
    blockers = [] if running else ["sandbox_desktop_provider_required"]
    payload = {
        "available": running,
        "provider_id": provider_id,
        "provider_kind": "sandbox_desktop",
        "status": "available" if running else "provider_required",
        "adapter_ready": running and bool(url),
        "reason": str(
            session.get("reason")
            or (
                "Isolated desktop provider session is running."
                if running
                else "Isolated desktop provider session is required."
            )
        ),
        "blocking_conditions": blockers,
        "supported_tools": tool_names,
        "recommended_for": ["foreground_control", "keyboard_mouse_capture"],
        "source": "desktop_provider_session",
        "desktop_session_kind": str(
            session.get("desktop_session_kind") or "isolated_desktop"
        ),
        "desktop_session_isolated": _optional_bool_value(
            session.get("desktop_session_isolated")
        )
        if "desktop_session_isolated" in session
        else True,
        "foreground_takeover_required": _optional_bool_value(
            session.get("foreground_takeover_required")
        )
        if "foreground_takeover_required" in session
        else False,
        "desktop_backend_kind": str(session.get("desktop_backend_kind") or ""),
        "desktop_backend_is_loopback": _optional_bool_value(
            session.get("desktop_backend_is_loopback")
        ),
        "desktop_backend_ready_for_public_release": _optional_bool_value(
            session.get("desktop_backend_ready_for_public_release")
        ),
        "requires_real_virtual_desktop_backend": _optional_bool_value(
            session.get("requires_real_virtual_desktop_backend")
        ),
        "keyboard_mouse_capture_supported": _optional_bool_value(
            session.get("keyboard_mouse_capture_supported")
        )
        if "keyboard_mouse_capture_supported" in session
        else True,
    }
    for key in (
        "provider_contract",
        "provider_manifest_evidence",
        "provider_conformance",
    ):
        value = session.get(key)
        if isinstance(value, Mapping) and value:
            payload[key] = dict(value)
    return payload


def _metadata_truthy(
    metadata: Mapping[str, Any] | None,
    *keys: str,
) -> bool:
    if not isinstance(metadata, Mapping):
        return False
    for key in keys:
        value = metadata.get(key)
        if _optional_bool_value(value) is True:
            return True
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _metadata_truthy(nested_metadata, *keys)
    return False


def _metadata_bool(
    metadata: Mapping[str, Any] | None,
    *keys: str,
) -> bool | None:
    if not isinstance(metadata, Mapping):
        return None
    for key in keys:
        if key in metadata:
            parsed = _optional_bool_value(metadata.get(key))
            if parsed is not None:
                return parsed
    nested_metadata = metadata.get("metadata")
    if isinstance(nested_metadata, Mapping) and nested_metadata is not metadata:
        return _metadata_bool(nested_metadata, *keys)
    return None


def _route_policy_metadata_context(
    policy_payload: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    context = dict(policy_payload)
    if isinstance(metadata, Mapping):
        context.update(metadata)
    return context


def _sandbox_provider_public_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    allowed_keys = {
        "available",
        "provider_id",
        "provider_kind",
        "status",
        "adapter_ready",
        "authentication_configured",
        "reason",
        "blocking_conditions",
        "supported_tools",
        "recommended_for",
        "diagnostic_route",
        "source",
        "health",
        "launch_hint",
        "foreground_mutation_supported",
        "keyboard_mouse_capture_supported",
        "desktop_session_kind",
        "desktop_session_isolated",
        "foreground_takeover_required",
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
        "provider_contract",
        "provider_manifest_evidence",
        "provider_conformance",
        "requires_real_sandbox_for",
    }
    return {
        key: payload[key]
        for key in allowed_keys
        if key in payload
    }


def _sandbox_provider_payload_from_env(
    *,
    probe_health: bool = False,
) -> dict[str, Any]:
    provider_url = _first_env_value(
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_URL",
            "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_URL",
            "OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL",
            "OHA_YACHIYO_SANDBOX_DESKTOP_PROVIDER_EXECUTE_URL",
        )
    )
    if not provider_url:
        return {}
    provider_status = desktop_execution_provider_status_from_env(
        probe_health=probe_health,
    )
    if str(provider_status.get("provider_kind") or "") != "sandbox_desktop":
        return {}
    if not _truthy_env_value(
        "OHA_YACHIYO_DESKTOP_PROVIDER_ALLOW_REMOTE"
    ) and not _is_loopback_url(provider_url):
        return {
            **provider_status,
            "provider_kind": "sandbox_desktop",
            "recommended_for": ["foreground_control", "keyboard_mouse_capture"],
            "diagnostic_route": "/yachiyo/studio/tools",
        }
    provider_kind = (
        _first_env_value(("OHA_YACHIYO_DESKTOP_PROVIDER_KIND",))
        or "sandbox_desktop"
    )
    if provider_kind.strip().lower().replace("-", "_") != "sandbox_desktop":
        return {}
    supported_tools = _string_list(
        _first_env_value(("OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS",))
    )
    return {
        **provider_status,
        "available": bool(provider_status.get("available")),
        "provider_id": str(provider_status.get("provider_id") or ""),
        "provider_kind": "sandbox_desktop",
        "status": str(provider_status.get("status") or "available"),
        "adapter_ready": bool(provider_status.get("adapter_ready")),
        "reason": str(
            provider_status.get("reason")
            or "Sandbox desktop provider is configured through runtime environment."
        ),
        "blocking_conditions": _string_list(provider_status.get("blocking_conditions")),
        "supported_tools": _string_list(provider_status.get("supported_tools"))
        or supported_tools,
        "recommended_for": ["foreground_control", "keyboard_mouse_capture"],
        "diagnostic_route": "/yachiyo/studio/tools",
        "source": "runtime_env",
        "desktop_session_kind": str(
            provider_status.get("desktop_session_kind") or ""
        ),
        "desktop_session_isolated": _optional_bool_value(
            provider_status.get("desktop_session_isolated")
        ),
        "foreground_takeover_required": _optional_bool_value(
            provider_status.get("foreground_takeover_required")
        ),
        "desktop_backend_kind": str(provider_status.get("desktop_backend_kind") or ""),
        "desktop_backend_is_loopback": _optional_bool_value(
            provider_status.get("desktop_backend_is_loopback")
        ),
        "desktop_backend_ready_for_public_release": _optional_bool_value(
            provider_status.get("desktop_backend_ready_for_public_release")
        ),
        "requires_real_virtual_desktop_backend": _optional_bool_value(
            provider_status.get("requires_real_virtual_desktop_backend")
        ),
    }


def _sandbox_provider_payload_from_manifest(
    *,
    probe_health: bool = False,
) -> dict[str, Any]:
    manifest = _configured_provider_manifest()
    if not manifest:
        return {}
    static_payload = _manifest_provider_static_payload(manifest)
    manifest_env = _provider_env_from_manifest(manifest)
    if not (
        manifest_env.get("OHA_YACHIYO_DESKTOP_PROVIDER_URL")
        or manifest_env.get("OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL")
    ):
        return {
            **static_payload,
            "available": False,
            "adapter_ready": False,
            "status": "provider_required",
            "reason": (
                "Desktop provider manifest is configured, but no provider endpoint "
                "is available yet."
            ),
            "blocking_conditions": ["sandbox_desktop_provider_required"],
        }
    provider_status = desktop_execution_provider_status_from_env(
        manifest_env,
        probe_health=probe_health,
    )
    if str(provider_status.get("provider_kind") or "") != "sandbox_desktop":
        return {}
    return {
        **static_payload,
        **provider_status,
        "available": bool(provider_status.get("available")),
        "adapter_ready": bool(provider_status.get("adapter_ready")),
        "provider_kind": "sandbox_desktop",
        "provider_id": str(
            provider_status.get("provider_id")
            or static_payload.get("provider_id")
            or ""
        ),
        "supported_tools": _string_list(provider_status.get("supported_tools"))
        or _string_list(static_payload.get("supported_tools")),
        "blocking_conditions": _string_list(provider_status.get("blocking_conditions")),
        "reason": str(
            provider_status.get("reason")
            or static_payload.get("reason")
            or "Sandbox desktop provider is configured through provider manifest."
        ),
        "source": "provider_manifest",
    }


def _configured_provider_manifest() -> dict[str, Any]:
    raw_path = str(os.environ.get(_PROVIDER_MANIFEST_ENV) or "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _manifest_provider_static_payload(manifest: Mapping[str, Any]) -> dict[str, Any]:
    safety = _mapping(manifest.get("safety"))
    supported_tools = _string_list(manifest.get("supported_tools"))
    capabilities = _string_list(manifest.get("capabilities"))
    return {
        "provider_id": str(manifest.get("provider_id") or "").strip(),
        "provider_kind": str(manifest.get("provider_kind") or "sandbox_desktop").strip(),
        "status": "provider_required",
        "recommended_for": ["foreground_control", "keyboard_mouse_capture"],
        "diagnostic_route": "/yachiyo/studio/tools",
        "source": "provider_manifest",
        "supported_tools": supported_tools,
        "capabilities": capabilities,
        "launch_hint": {"isolated_provider": dict(manifest)},
        "foreground_mutation_supported": _optional_bool_value(
            manifest.get("foreground_mutation_supported")
            if "foreground_mutation_supported" in manifest
            else safety.get("foreground_mutation_supported")
        ),
        "keyboard_mouse_capture_supported": _optional_bool_value(
            manifest.get("keyboard_mouse_capture_supported")
            if "keyboard_mouse_capture_supported" in manifest
            else safety.get("keyboard_mouse_capture_supported")
        ),
        "desktop_session_kind": str(
            manifest.get("desktop_session_kind")
            or safety.get("desktop_session_kind")
            or ""
        ).strip(),
        "desktop_session_isolated": _optional_bool_value(
            manifest.get("desktop_session_isolated")
            if "desktop_session_isolated" in manifest
            else safety.get("desktop_session_isolated")
        ),
        "foreground_takeover_required": _optional_bool_value(
            manifest.get("foreground_takeover_required")
            if "foreground_takeover_required" in manifest
            else safety.get("foreground_takeover_required")
        ),
        "desktop_backend_kind": str(
            manifest.get("desktop_backend_kind")
            or safety.get("desktop_backend_kind")
            or ""
        ).strip(),
        "desktop_backend_is_loopback": _optional_bool_value(
            manifest.get("desktop_backend_is_loopback")
            if "desktop_backend_is_loopback" in manifest
            else safety.get("desktop_backend_is_loopback")
        ),
        "desktop_backend_ready_for_public_release": _optional_bool_value(
            manifest.get("desktop_backend_ready_for_public_release")
            if "desktop_backend_ready_for_public_release" in manifest
            else safety.get("desktop_backend_ready_for_public_release")
        ),
        "requires_real_virtual_desktop_backend": _optional_bool_value(
            manifest.get("requires_real_virtual_desktop_backend")
            if "requires_real_virtual_desktop_backend" in manifest
            else safety.get("requires_real_virtual_desktop_backend")
        ),
    }


def _provider_env_from_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    endpoint_urls = _mapping(manifest.get("endpoint_urls"))
    if not endpoint_urls:
        endpoint_urls = {
            key: value
            for key, value in _mapping(manifest.get("endpoints")).items()
            if str(value or "").startswith(("http://", "https://"))
        }
    execute_url = _first_mapping_value(
        endpoint_urls,
        "execute",
        "tools_execute",
        "tools.execute",
        "tools/execute",
        "execute_url",
    )
    status_url = _first_mapping_value(endpoint_urls, "status", "health", "status_url")
    base_url = (
        _first_mapping_value(manifest, "url", "endpoint_origin", "base_url")
        or _first_mapping_value(endpoint_urls, "url", "base_url", "base", "origin")
        or _url_origin(str(execute_url or status_url or ""))
    )
    env: dict[str, str] = {}
    if base_url:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_URL"] = str(base_url)
    if execute_url:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_EXECUTE_URL"] = str(execute_url)
    if status_url:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_STATUS_URL"] = str(status_url)
    provider_id = str(manifest.get("provider_id") or "").strip()
    if provider_id:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_ID"] = provider_id
    provider_kind = str(manifest.get("provider_kind") or "sandbox_desktop").strip()
    if provider_kind:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_KIND"] = provider_kind
    supported_tools = _string_list(manifest.get("supported_tools"))
    if supported_tools:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_TOOLS"] = ",".join(supported_tools)
    capabilities = _string_list(manifest.get("capabilities"))
    if capabilities:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_CAPABILITIES"] = ",".join(capabilities)
    for env_key, manifest_key in (
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_KEYBOARD_MOUSE_CAPTURE_SUPPORTED",
            "keyboard_mouse_capture_supported",
        ),
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_MUTATION_SUPPORTED",
            "foreground_mutation_supported",
        ),
        ("OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_ISOLATED", "desktop_session_isolated"),
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_FOREGROUND_TAKEOVER_REQUIRED",
            "foreground_takeover_required",
        ),
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_IS_LOOPBACK",
            "desktop_backend_is_loopback",
        ),
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_READY_FOR_PUBLIC_RELEASE",
            "desktop_backend_ready_for_public_release",
        ),
        (
            "OHA_YACHIYO_DESKTOP_PROVIDER_REQUIRES_REAL_VIRTUAL_DESKTOP_BACKEND",
            "requires_real_virtual_desktop_backend",
        ),
    ):
        bool_value = _manifest_bool_env(manifest, manifest_key)
        if bool_value:
            env[env_key] = bool_value
    for env_key, manifest_key in (
        ("OHA_YACHIYO_DESKTOP_PROVIDER_SESSION_KIND", "desktop_session_kind"),
        ("OHA_YACHIYO_DESKTOP_PROVIDER_BACKEND_KIND", "desktop_backend_kind"),
    ):
        value = str(manifest.get(manifest_key) or "").strip()
        if value:
            env[env_key] = value
    if _optional_bool_value(manifest.get("allow_remote")) is True:
        env["OHA_YACHIYO_DESKTOP_PROVIDER_ALLOW_REMOTE"] = "true"
    provider_token = desktop_provider_token_from_manifest(manifest)
    if provider_token:
        env[DESKTOP_PROVIDER_TOKEN_ENV] = provider_token
    return env


def _manifest_bool_env(manifest: Mapping[str, Any], key: str) -> str:
    safety = _mapping(manifest.get("safety"))
    raw = manifest.get(key) if key in manifest else safety.get(key)
    parsed = _optional_bool_value(raw)
    if parsed is None:
        return ""
    return "true" if parsed else "false"


def _local_desktop_provider_payload(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    if not _metadata_truthy(metadata, *_LOCAL_DESKTOP_PROVIDER_KEYS):
        return {}
    runtime_probe = (
        metadata.get("local_desktop_runtime_probe")
        if isinstance(metadata, Mapping)
        and isinstance(metadata.get("local_desktop_runtime_probe"), Mapping)
        else None
    )
    return local_desktop_execution_provider_status(runtime_probe=runtime_probe)


def _sandbox_route_decision(
    route: Mapping[str, Any],
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    blockers = _string_list(sandbox_provider.get("blocking_conditions")) or [
        "sandbox_desktop_provider_required"
    ]
    provider_kind = str(sandbox_provider.get("provider_kind") or "sandbox_desktop")
    provider_id = str(sandbox_provider.get("provider_id") or "")
    background_provider = provider_kind == CUA_BACKGROUND_PROVIDER_KIND
    blocked_fallback_mode = "user_handoff" if background_provider else "supervised_live"
    provider_context = _desktop_provider_route_context(sandbox_provider, tool_name)
    if _desktop_route_requires_real_virtual_backend(metadata):
        provider_context["requires_real_virtual_desktop_backend"] = True
    if not bool(sandbox_provider.get("available")):
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "provider_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": not background_provider,
            "fallback_mode": blocked_fallback_mode,
            "reason": str(sandbox_provider.get("reason") or ""),
            "blocking_conditions": blockers,
            **provider_context,
        }
    supported_tools = _string_list(sandbox_provider.get("supported_tools"))
    if _sandbox_provider_requires_keyboard_mouse_sandbox(sandbox_provider, tool_name):
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "sandbox_keyboard_mouse_provider_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": True,
            "fallback_mode": blocked_fallback_mode,
            "reason": (
                "Current desktop provider can open or focus apps, but keyboard and "
                "mouse capture must run through a real sandbox/control provider."
            ),
            "blocking_conditions": ["sandbox_keyboard_mouse_provider_required"],
            **provider_context,
        }
    if supported_tools and tool_name not in supported_tools:
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "sandbox_tool_not_supported",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": not background_provider,
            "fallback_mode": blocked_fallback_mode,
            "reason": "Desktop provider is available but does not support this tool.",
            "blocking_conditions": ["sandbox_tool_not_supported"],
            **provider_context,
        }
    simulated_blockers = _simulated_desktop_provider_blockers(sandbox_provider)
    if simulated_blockers and not _simulated_desktop_provider_execution_allowed(
        metadata,
        sandbox_provider,
    ):
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "real_virtual_desktop_provider_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": True,
            "fallback_mode": blocked_fallback_mode,
            "reason": (
                "Current desktop provider is a loopback or simulated harness. "
                "Real desktop execution requires a non-loopback virtual desktop "
                "provider before the agent can act on apps."
            ),
            "blocking_conditions": simulated_blockers,
            **provider_context,
        }
    route_context = {
        **dict(route),
        "selected_provider_kind": provider_kind,
        "selected_provider_id": provider_id,
    }
    if _sandbox_route_requires_isolated_foreground_session(
        route_context,
        provider_context,
    ):
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "sandbox_desktop_session_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": True,
            "fallback_mode": blocked_fallback_mode,
            "reason": (
                "Current desktop provider uses the user's foreground session; "
                "this foreground action must run inside an isolated desktop session "
                "unless foreground takeover is explicitly allowed."
            ),
            "blocking_conditions": ["sandbox_desktop_session_required"],
            **provider_context,
        }
    if not bool(sandbox_provider.get("adapter_ready")):
        return {
            **dict(route),
            "selected_provider_kind": provider_kind,
            "selected_provider_id": provider_id,
            "status": "sandbox_adapter_required",
            "can_execute": False,
            "can_auto_start": False,
            "sandbox_required": not background_provider,
            "fallback_mode": blocked_fallback_mode,
            "reason": "Desktop provider is available but no executable adapter is registered yet.",
            "blocking_conditions": ["sandbox_desktop_adapter_required"],
            **provider_context,
        }
    ready_status = _desktop_provider_ready_status(sandbox_provider)
    sandbox_required = ready_status == "sandbox_ready"
    return {
        **dict(route),
        "selected_provider_kind": provider_kind,
        "selected_provider_id": provider_id,
        "status": ready_status,
        "can_execute": True,
        "can_auto_start": True,
        "provider_execution_required": True,
        "sandbox_required": sandbox_required,
        "fallback_mode": "",
        "reason": _desktop_provider_ready_reason(sandbox_provider),
        "blocking_conditions": [],
        **provider_context,
    }


def _route_with_ready_reason(route: Mapping[str, Any], reason: str) -> dict[str, Any]:
    payload = dict(route)
    if (
        payload.get("can_execute") is True
        and not _string_list(payload.get("blocking_conditions"))
    ):
        payload["reason"] = reason
    return payload


def _local_desktop_fallback_allowed(
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
) -> bool:
    provider_kind = str(sandbox_provider.get("provider_kind") or "").strip()
    if provider_kind == "local_desktop":
        return True
    if not bool(sandbox_provider.get("available")):
        return True
    if _simulated_desktop_provider_blockers(sandbox_provider):
        return True
    return not sandbox_desktop_provider_can_execute_tool(sandbox_provider, tool_name)


def _route_with_provider_auto_start(
    route: Mapping[str, Any],
    *,
    reason: str,
) -> dict[str, Any]:
    payload = dict(route)
    if payload.get("can_execute") is True:
        payload["reason"] = reason
        return payload
    payload["can_auto_start"] = True
    if reason:
        payload["reason"] = reason
    return payload


def _desktop_provider_route_context(
    sandbox_provider: Mapping[str, Any],
    tool_name: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    provider_readiness_status = str(sandbox_provider.get("status") or "").strip()
    if provider_readiness_status:
        # Route status answers "may this tool execute?" while provider
        # readiness answers "has the selected adapter been probed yet?".
        # Keeping both prevents side-effect-free planning from turning an
        # unprobed provider into a permanent execution blocker.
        payload["provider_readiness_status"] = provider_readiness_status
    for key in (
        "foreground_mutation_supported",
        "keyboard_mouse_capture_supported",
        "desktop_session_isolated",
        "foreground_takeover_required",
    ):
        if key in sandbox_provider:
            payload[key] = _optional_bool_value(sandbox_provider.get(key))
    session_kind = str(sandbox_provider.get("desktop_session_kind") or "").strip()
    if session_kind:
        payload["desktop_session_kind"] = session_kind
    for key in (
        "desktop_backend_kind",
        "desktop_backend_is_loopback",
        "desktop_backend_ready_for_public_release",
        "requires_real_virtual_desktop_backend",
    ):
        if key not in sandbox_provider:
            continue
        value = sandbox_provider.get(key)
        if key == "desktop_backend_kind":
            payload[key] = str(value or "").strip()
        else:
            payload[key] = _optional_bool_value(value)
    provider_contract = sandbox_provider.get("provider_contract")
    contract_blockers = (
        _string_list(provider_contract.get("blocking_conditions"))
        if isinstance(provider_contract, Mapping)
        else []
    )
    if contract_blockers:
        payload["provider_contract_blocking_conditions"] = contract_blockers
    payload["simulated_desktop_provider"] = bool(
        _simulated_desktop_provider_blockers(sandbox_provider)
    )
    takeover_required = (
        _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True
    )
    isolated_session = (
        _optional_bool_value(sandbox_provider.get("desktop_session_isolated")) is True
        or session_kind in {"isolated_desktop", "sandbox_desktop", "virtual_desktop"}
    )
    requires_user_session = takeover_required or session_kind == "user_foreground"
    payload["requires_user_foreground_session"] = requires_user_session and not isolated_session
    payload["user_foreground_takeover_risk"] = bool(
        payload["requires_user_foreground_session"]
        and _tool_can_take_over_user_foreground(tool_name)
    )
    return payload


def _desktop_route_requires_real_virtual_backend(
    metadata: Mapping[str, Any] | None,
) -> bool:
    if _metadata_truthy(
        metadata,
        "requires_real_virtual_desktop_backend",
        "require_real_virtual_desktop_backend",
        "real_virtual_desktop_backend_required",
    ):
        return True
    if not isinstance(metadata, Mapping):
        return False
    return str(metadata.get("source") or "").strip() == "agent_studio"


def _desktop_provider_ready_status(sandbox_provider: Mapping[str, Any]) -> str:
    provider_kind = str(sandbox_provider.get("provider_kind") or "").strip()
    if provider_kind != "sandbox_desktop":
        return "provider_ready"
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return "provider_ready"
    return "sandbox_ready"


def _running_isolated_sandbox_provider_can_execute(
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
) -> bool:
    if str(sandbox_provider.get("provider_kind") or "").strip() != "sandbox_desktop":
        return False
    if not bool(sandbox_provider.get("available")):
        return False
    if not bool(sandbox_provider.get("adapter_ready")):
        return False
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return False
    if _optional_bool_value(sandbox_provider.get("desktop_backend_is_loopback")) is True:
        return False
    if (
        _optional_bool_value(
            sandbox_provider.get("requires_real_virtual_desktop_backend")
        )
        is True
    ):
        return False
    session_kind = str(sandbox_provider.get("desktop_session_kind") or "").strip()
    session_isolated = _optional_bool_value(
        sandbox_provider.get("desktop_session_isolated")
    )
    if session_isolated is not True and session_kind not in {
        "isolated_desktop",
        "sandbox_desktop",
        "virtual_desktop",
    }:
        return False
    return sandbox_desktop_provider_can_execute_tool(sandbox_provider, tool_name)


def _desktop_provider_ready_reason(sandbox_provider: Mapping[str, Any]) -> str:
    if _desktop_provider_ready_status(sandbox_provider) == "sandbox_ready":
        return "Foreground desktop action can be routed through the sandbox provider."
    provider_kind = str(sandbox_provider.get("provider_kind") or "desktop provider").strip()
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return (
            f"Desktop action can be routed through the {provider_kind} provider, "
            "but that provider uses the user's foreground desktop session."
        )
    return f"Desktop action can be routed through the {provider_kind} provider."


def _readonly_desktop_provider_route_reason(
    sandbox_provider: Mapping[str, Any],
) -> str:
    if _desktop_provider_ready_status(sandbox_provider) == "sandbox_ready":
        return (
            "Read-only desktop discovery can be routed through the sandbox "
            "desktop provider without taking over foreground input."
        )
    provider_kind = str(sandbox_provider.get("provider_kind") or "desktop provider").strip()
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return (
            f"Read-only desktop discovery can be routed through the {provider_kind} "
            "provider. It does not request keyboard or mouse capture, but it "
            "observes the user's desktop session."
        )
    return f"Read-only desktop discovery can be routed through the {provider_kind} provider."


def _foreground_desktop_provider_route_reason(
    sandbox_provider: Mapping[str, Any],
) -> str:
    if _desktop_provider_ready_status(sandbox_provider) == "sandbox_ready":
        return (
            "Foreground desktop action can be routed through the sandbox "
            "desktop provider instead of the user's foreground session."
        )
    provider_kind = str(sandbox_provider.get("provider_kind") or "desktop provider").strip()
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return (
            f"Foreground desktop action can be routed through the {provider_kind} "
            "provider, but that provider may use the user's foreground desktop "
            "session."
        )
    return f"Foreground desktop action can be routed through the {provider_kind} provider."


def _simulated_desktop_provider_blockers(
    sandbox_provider: Mapping[str, Any],
) -> list[str]:
    blockers: list[str] = []
    backend_kind = str(sandbox_provider.get("desktop_backend_kind") or "").strip()
    if backend_kind == "loopback_session_harness":
        blockers.append("loopback_desktop_backend")
    if _optional_bool_value(sandbox_provider.get("desktop_backend_is_loopback")) is True:
        blockers.append("loopback_desktop_backend")
    if (
        _optional_bool_value(
            sandbox_provider.get("requires_real_virtual_desktop_backend")
        )
        is True
    ):
        blockers.append("real_virtual_desktop_backend_required")
    return _unique_strings(blockers)


def _simulated_desktop_provider_execution_allowed(
    metadata: Mapping[str, Any] | None,
    sandbox_provider: Mapping[str, Any] | None = None,
) -> bool:
    if _metadata_truthy(metadata, *_SIMULATED_DESKTOP_PROVIDER_ALLOW_KEYS):
        return True
    if _metadata_truthy(sandbox_provider, *_SIMULATED_DESKTOP_PROVIDER_ALLOW_KEYS):
        return True
    return any(
        _truthy_env_value(key)
        for key in _SIMULATED_DESKTOP_PROVIDER_ENV_ALLOW_KEYS
    )


def _sandbox_provider_requires_keyboard_mouse_sandbox(
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
) -> bool:
    clean_tool = str(tool_name or "").strip()
    if clean_tool not in _KEYBOARD_MOUSE_CAPTURE_TOOLS:
        return False
    if "keyboard_mouse_capture_supported" not in sandbox_provider:
        return False
    raw_capture_supported = sandbox_provider.get("keyboard_mouse_capture_supported")
    if (
        raw_capture_supported is not False
        and str(raw_capture_supported).strip().lower()
        not in {"0", "false", "no", "off"}
    ):
        return False
    required = _string_list(sandbox_provider.get("requires_real_sandbox_for"))
    return not required or clean_tool in required or "keyboard_mouse_capture" in required


def _sandbox_provider_requires_isolated_foreground_session(
    sandbox_provider: Mapping[str, Any],
    tool_name: str,
    metadata: Mapping[str, Any] | None,
) -> bool:
    clean_tool = str(tool_name or "").strip()
    if clean_tool not in _USER_FOREGROUND_TAKEOVER_TOOLS:
        return False
    if (
        str(sandbox_provider.get("provider_kind") or "").strip() == "local_desktop"
        and clean_tool not in _KEYBOARD_MOUSE_CAPTURE_TOOLS
    ):
        return False
    if _metadata_truthy(
        metadata,
        "allow_user_foreground_takeover",
        "desktop_allow_user_foreground_takeover",
        "allow_nonisolated_desktop_provider",
        "allow_live_foreground",
    ):
        return False
    if _optional_bool_value(sandbox_provider.get("foreground_takeover_required")) is True:
        return True
    if (
        _optional_bool_value(sandbox_provider.get("foreground_takeover_required"))
        is False
        and _optional_bool_value(sandbox_provider.get("desktop_session_isolated"))
        is False
        and (
            str(sandbox_provider.get("provider_kind") or "").strip()
            == "background_desktop"
            and str(sandbox_provider.get("desktop_session_kind") or "").strip()
            == "background_desktop"
        )
    ):
        if clean_tool not in _KEYBOARD_MOUSE_CAPTURE_TOOLS:
            return False
        return _optional_bool_value(
            sandbox_provider.get("keyboard_mouse_capture_supported")
        ) is not True
    if _optional_bool_value(sandbox_provider.get("desktop_session_isolated")) is True:
        return False
    session_kind = str(sandbox_provider.get("desktop_session_kind") or "").strip()
    if session_kind in {"isolated_desktop", "sandbox_desktop", "virtual_desktop"}:
        return False
    if _optional_bool_value(sandbox_provider.get("desktop_session_isolated")) is False:
        return True
    if clean_tool not in _KEYBOARD_MOUSE_CAPTURE_TOOLS:
        return False
    return _optional_bool_value(
        sandbox_provider.get("keyboard_mouse_capture_supported")
    ) is True and str(sandbox_provider.get("provider_kind") or "") != "sandbox_desktop"


def _sandbox_route_requires_isolated_foreground_session(
    route: Mapping[str, Any],
    provider_context: Mapping[str, Any],
) -> bool:
    if not bool(provider_context.get("user_foreground_takeover_risk")):
        return False
    if bool(route.get("foreground_takeover_allowed")):
        return False
    requested_mode = (
        str(route.get("requested_mode") or "").strip().lower().replace("-", "_")
    )
    return (
        bool(route.get("background_desktop_preferred"))
        or bool(route.get("isolated_desktop_preferred"))
        or requested_mode == "sandbox_preferred"
    )


def _tool_can_take_over_user_foreground(tool_name: str) -> bool:
    clean_tool = str(tool_name or "").strip()
    return (
        clean_tool in _USER_FOREGROUND_TAKEOVER_TOOLS
        or clean_tool in _LOCAL_LOW_RISK_FOREGROUND_TOOLS
    )


def _execution_mode_payload(value: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(mode="json")
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def _provider_id_for_isolation(isolation: str) -> str:
    if isolation in {"process", "browser_profile", "browser_target", "headless"}:
        return isolation
    return ""


def _first_env_value(keys: tuple[str, ...]) -> str:
    for key in keys:
        value = str(os.getenv(key, "") or "").strip()
        if value:
            return value
    return ""


def _truthy_env_value(key: str) -> bool:
    return str(os.getenv(key, "") or "").strip().lower() in {"1", "true", "yes", "on"}


def _is_loopback_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.startswith("127.")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_mapping_value(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _url_origin(value: str) -> str:
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return ""


def _health_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {
            "ok": False,
            "checked": False,
            "status": "not_checked",
            "blocking_conditions": [],
            "supported_tools": [],
            "capabilities": [],
        }
    payload = dict(value)
    payload["ok"] = bool(payload.get("ok"))
    payload["checked"] = bool(payload.get("checked"))
    payload["status"] = str(payload.get("status") or "")
    payload["blocking_conditions"] = _string_list(payload.get("blocking_conditions"))
    payload["supported_tools"] = _string_list(payload.get("supported_tools"))
    payload["capabilities"] = _string_list(payload.get("capabilities"))
    if "foreground_mutation_supported" in payload:
        payload["foreground_mutation_supported"] = _optional_bool_value(
            payload.get("foreground_mutation_supported")
        )
    if "keyboard_mouse_capture_supported" in payload:
        payload["keyboard_mouse_capture_supported"] = _optional_bool_value(
            payload.get("keyboard_mouse_capture_supported")
        )
    payload["desktop_session_kind"] = str(payload.get("desktop_session_kind") or "")
    if "desktop_session_isolated" in payload:
        payload["desktop_session_isolated"] = _optional_bool_value(
            payload.get("desktop_session_isolated")
        )
    if "foreground_takeover_required" in payload:
        payload["foreground_takeover_required"] = _optional_bool_value(
            payload.get("foreground_takeover_required")
        )
    payload["desktop_backend_kind"] = str(payload.get("desktop_backend_kind") or "")
    if "desktop_backend_is_loopback" in payload:
        payload["desktop_backend_is_loopback"] = _optional_bool_value(
            payload.get("desktop_backend_is_loopback")
        )
    if "desktop_backend_ready_for_public_release" in payload:
        payload["desktop_backend_ready_for_public_release"] = _optional_bool_value(
            payload.get("desktop_backend_ready_for_public_release")
        )
    if "requires_real_virtual_desktop_backend" in payload:
        payload["requires_real_virtual_desktop_backend"] = _optional_bool_value(
            payload.get("requires_real_virtual_desktop_backend")
        )
    payload["requires_real_sandbox_for"] = _string_list(
        payload.get("requires_real_sandbox_for")
    )
    return payload


def _provider_contract_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    if str(payload.get("provider_kind") or "").strip() != "sandbox_desktop":
        return {}
    status = dict(payload)
    status.setdefault(
        "configured",
        bool(status.get("provider_id")) or bool(status.get("available")),
    )
    return virtual_desktop_provider_contract_evidence(
        status,
        required_tools=OHA_DESKTOP_AGENT_RELEASE_PROVIDER_TOOLS,
    )


def _optional_bool_value(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        clean = value.strip().lower()
        if clean in {"1", "true", "yes", "on", "supported", "ready"}:
            return True
        if clean in {"0", "false", "no", "off", "unsupported", "blocked"}:
            return False
    return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_items = value.split(",")
    elif isinstance(value, list):
        raw_items = value
    else:
        return []
    items: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text and text not in items:
            items.append(text)
    return items


def _unique_strings(values: Iterable[Any]) -> list[str]:
    items: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in items:
            items.append(text)
    return items
