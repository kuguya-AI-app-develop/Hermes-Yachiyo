"""Exact native System Settings window and pane observations."""

from collections.abc import Mapping
from typing import Any


def canonical_settings_pane(value: Any) -> str:
    # Share the native dispatcher's bounded pane aliases, rather than infer a
    # pane from arbitrary labels in a sidebar or provider completion flags.
    from apps.shell.agent.tools import desktop

    text = str(value or "").strip()
    pane = desktop._system_settings_target(text)
    if pane is not None:
        return pane[0]
    if desktop._looks_like_system_settings_home(text):
        return "System Settings"
    return ""


def settings_window_identity(data: Mapping[str, Any]) -> tuple[str, int, int] | None:
    name = str(data.get("app_name") or "").strip()
    compact = "".join(name.casefold().split())
    if compact not in {"systemsettings", "systempreferences", "系统设置", "系统偏好设置"}:
        return None
    pid, window = data.get("pid"), data.get("window_id")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in (pid, window)
    ):
        return None
    return name, pid, window


def settings_readback_matches(target: Any, observation: Any) -> bool:
    if not isinstance(observation, Mapping):
        return False
    pane = canonical_settings_pane(target)
    return bool(
        pane
        and observation.get("target") == str(target or "").strip()
        and settings_window_identity(observation)
        and canonical_settings_pane(observation.get("window_title")) == pane
        and canonical_settings_pane(observation.get("ui_title")) == pane
        and observation.get("pane") == pane
    )


def observed_settings_pane(
    target: str,
    before: Mapping[str, Any],
    ui: Mapping[str, Any],
    after: Mapping[str, Any],
) -> dict[str, Any]:
    """Require one stable real foreground window throughout the AX read."""
    results = (before, ui, after)
    actions = ("desktop.active_window", "desktop.ui_elements", "desktop.active_window")
    if any(
        result.get("ok") is not True
        or result.get("action") != action
        or result.get("permission_error")
        or result.get("fallback_used")
        for result, action in zip(results, actions)
    ):
        return {}
    data = [result.get("data") for result in results]
    if any(not isinstance(item, Mapping) for item in data):
        return {}
    identities = [settings_window_identity(item) for item in data]
    if not identities[0] or any(identity != identities[0] for identity in identities):
        return {}
    pane = canonical_settings_pane(target)
    if not pane or any(canonical_settings_pane(item.get("title")) != pane for item in data):
        return {}
    elements = data[1].get("elements")
    if not isinstance(elements, list) or not any(
        isinstance(element, Mapping)
        and str(element.get("name") or element.get("value") or "").strip()
        for element in elements
    ):
        return {}
    app, pid, window = identities[0]
    return {
        "target": str(target).strip(),
        "pane": pane,
        "app_name": app,
        "pid": pid,
        "window_id": window,
        "window_title": data[2]["title"],
        "ui_title": data[1]["title"],
    }
