"""Shared semantics for distinguishing dispatch receipts from effects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from apps.shell.agent.runtime.app_aliases import APP_ALIASES, compact_app_alias


SEMANTIC_SAFE_SHORTCUT_TOOL = "desktop.safe_shortcut"
SEMANTIC_SAFE_SHORTCUT_TOOLS = frozenset(
    {
        SEMANTIC_SAFE_SHORTCUT_TOOL,
        "app.open_and_safe_shortcut",
        "app.focus_and_safe_shortcut",
    }
)
SEMANTIC_SAFE_KEY_TOOLS = frozenset(
    {"desktop.safe_key", "app.open_and_safe_key", "app.focus_and_safe_key"}
)
_APP_LIFECYCLE_INTRINSIC_RULES: dict[str, dict[str, Any]] = {
    "app.open": {
        "status_key": "launch_status",
        "statuses": frozenset({"running"}),
        "required_true_key": "launch_verified",
        "target_action": "open_app",
        "state": "open",
    },
    "app.focus": {
        "status_key": "focus_status",
        "statuses": frozenset({"focused", "frontmost"}),
        "required_true_key": "focus_verified",
        "observed_app_key": "frontmost_app",
        "target_action": "focus_app",
        "state": "focused",
    },
    "app.show": {
        "status_key": "show_status",
        "statuses": frozenset({"launched", "shown"}),
        "target_action": "show_app",
        "state": "fulfilled",
    },
    "app.hide": {
        "status_key": "hide_status",
        "statuses": frozenset({"hidden"}),
        "target_action": "hide_app",
        "state": "fulfilled",
    },
    "app.minimize": {
        "status_key": "minimize_status",
        "statuses": frozenset({"minimized"}),
        "target_action": "minimize_app",
        "state": "fulfilled",
    },
    "app.quit": {
        "status_key": "quit_status",
        "statuses": frozenset({"quit", "not_running"}),
        "required_true_key": "quit_verified",
        "target_action": "quit_app",
        "state": "fulfilled",
    },
    "app.focus_window": {
        "status_key": "focus_status",
        "statuses": frozenset({"focused"}),
        "target_action": "focus_app_window",
        "state": "fulfilled",
        "window_target": True,
    },
}
_EXACT_NATIVE_DISPATCH_TOOLS = frozenset(
    {
        "desktop.search_submit",
        "desktop.safe_key",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
        "desktop.safe_scroll",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
        "desktop.safe_click",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
        "desktop.open_path",
        "desktop.reveal_path",
        "desktop.open_path_with_app",
        "app.open_path_with_app",
    }
)


def is_semantic_safe_shortcut(
    tool_name: str | None,
    _input_payload: Mapping[str, Any] | None = None,
) -> bool:
    """Return whether a tool names an application-semantic safe shortcut.

    ``desktop.safe_shortcut`` translates an action name such as ``paste`` or
    ``new_event`` into a keystroke.  The executor receipt proves only that the
    keystroke was delivered; it cannot prove the named application effect.
    Unknown or newly-added actions therefore fail closed as effects too.
    """

    return str(tool_name or "").strip() in SEMANTIC_SAFE_SHORTCUT_TOOLS


def is_semantic_safe_key(tool_name: str | None) -> bool:
    """Require independent UI evidence after a foreground key is delivered."""

    return str(tool_name or "").strip() in SEMANTIC_SAFE_KEY_TOOLS


def semantic_safe_shortcut_effect(
    tool_name: str | None,
    result: Any,
) -> str:
    """Return a stable dispatch effect for a successful shortcut receipt."""

    if not is_semantic_safe_shortcut(tool_name) or not isinstance(result, Mapping):
        return ""
    if result.get("ok") is not True:
        return ""
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    action = str(
        data.get("shortcut_action")
        or data.get("requested_action")
        or result.get("shortcut_action")
        or ""
    ).strip().lower()
    if not action:
        return ""
    stable_action = "_".join(action.replace("-", "_").split())
    return f"shortcut_dispatched:{stable_action}" if stable_action else ""



def is_semantic_search_submit(tool_name: str | None) -> bool:
    """Search Return delivery requires a separate query/results observation."""
    return str(tool_name or "").strip() == "desktop.search_submit"

def intrinsic_native_postcondition_state(
    tool_name: str | None,
    input_payload: Mapping[str, Any] | None,
    result: Any,
) -> str:
    """Return state proved by one exact structured native app receipt.

    This is deliberately narrower than ``postcondition_verified``.  The
    provider must report the exact requested app and the action-specific
    read-after-write status; a generic acknowledgement cannot satisfy a Goal.
    Provider ownership and call/run/plan lineage are validated by the caller.
    """

    clean_tool = str(tool_name or "").strip()
    if not isinstance(result, Mapping):
        return ""
    request = input_payload if isinstance(input_payload, Mapping) else {}
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if (
        is_semantic_safe_shortcut(clean_tool, request)
        or is_semantic_safe_key(clean_tool)
        or is_semantic_search_submit(clean_tool)
    ):
        # A shortcut provider owns the mutation and cannot independently
        # attest the UI effect it claims to have caused.  Completion requires
        # a separately trusted, action-specific observation receipt.
        return ""

    rule = _APP_LIFECYCLE_INTRINSIC_RULES.get(clean_tool)
    if rule is None:
        return ""
    requested_app = str(
        request.get("requested_app_name") or request.get("app_name") or ""
    ).strip()
    resolved_app = str(
        request.get("resolved_app_name") or request.get("app_name") or ""
    ).strip()
    observed_app = str(
        data.get("resolved_app_name")
        or data.get("app_name")
        or result.get("resolved_app_name")
        or result.get("app_name")
        or ""
    ).strip()
    status_key = str(rule["status_key"])
    status = str(data.get(status_key) or result.get(status_key) or "").strip()
    required_true_key = str(rule.get("required_true_key") or "").strip()
    if (
        result.get("ok") is not True
        or str(result.get("action") or result.get("tool") or "").strip()
        != clean_tool
        or not requested_app
        or not resolved_app
        or observed_app.casefold() != resolved_app.casefold()
        or status.casefold() not in rule["statuses"]
        or (
            required_true_key
            and not any(
                source.get(required_true_key) is True for source in (result, data)
            )
        )
        or not any(
            source.get("postcondition_verified") is True
            for source in (result, data)
        )
    ):
        return ""
    observed_app_key = str(rule.get("observed_app_key") or "").strip()
    if clean_tool == "app.quit" and not (
        data.get("launch_status") == "not_running" and data.get("running") is False
        and not result.get("permission_error") and not result.get("fallback_used")
    ):
        return ""
    if observed_app_key and str(
        data.get(observed_app_key) or result.get(observed_app_key) or ""
    ).strip().casefold() != resolved_app.casefold():
        return ""
    if rule.get("window_target"):
        expected_title = str(
            request.get("title_contains") or request.get("window_title") or ""
        ).strip()
        observed_title = str(
            data.get("matched_window_title")
            or data.get("window_title")
            or result.get("matched_window_title")
            or result.get("window_title")
            or ""
        ).strip()
        if not (
            expected_title
            and observed_title
            and expected_title.casefold() in observed_title.casefold()
        ):
            return ""
    return str(rule["state"])


def has_intrinsic_native_postcondition_contract(tool_name: str | None) -> bool:
    """Return whether generic success flags must not bypass structured rules."""

    clean_tool = str(tool_name or "").strip()
    return bool(
        clean_tool in _APP_LIFECYCLE_INTRINSIC_RULES
        or is_semantic_safe_shortcut(clean_tool)
        or is_semantic_safe_key(clean_tool)
        or is_semantic_search_submit(clean_tool)
    )


def exact_native_dispatch_receipt_matches(
    tool_name: str | None,
    input_payload: Mapping[str, Any] | None,
    result: Any,
) -> bool:
    """Validate an exact native dispatch receipt without claiming its effect.

    These operations ask the desktop to dispatch a concrete key, scroll,
    click, or open command.  A matching local/provider receipt proves that
    exact dispatch only; any later user-visible effect remains a separate
    observation.  Run/plan/call/provider authority is intentionally checked
    by the caller, while this helper keeps the request/result schema shared by
    the executor and Goal evaluator so the two layers cannot drift apart.
    """

    clean_tool = str(tool_name or "").strip()
    if not clean_tool or not isinstance(result, Mapping):
        return False
    request = input_payload if isinstance(input_payload, Mapping) else {}
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    if (
        result.get("ok") is not True
        or str(result.get("action") or result.get("tool") or "").strip()
        != clean_tool
    ):
        return False

    if clean_tool == "desktop.search_submit":
        modifiers = data.get("modifiers")
        return bool(
            str(data.get("key") or "").strip().casefold() in {"return", "enter"}
            and isinstance(modifiers, (list, tuple))
            and not modifiers
        )

    if clean_tool in {
        "desktop.safe_key",
        "app.open_and_safe_key",
        "app.focus_and_safe_key",
    }:
        requested_repeat = _strict_positive_int(request.get("repeat_count", 1))
        observed_repeat = _strict_positive_int(data.get("repeat_count"))
        return bool(
            _app_scoped_dispatch_target_matches(clean_tool, request, data)
            and str(request.get("action") or "").strip().casefold()
            == str(data.get("key_action") or "").strip().casefold()
            and str(request.get("action") or "").strip()
            and requested_repeat is not None
            and observed_repeat == requested_repeat
        )

    if clean_tool in {
        "desktop.safe_scroll",
        "app.open_and_safe_scroll",
        "app.focus_and_safe_scroll",
    }:
        requested_pages = _strict_positive_int(request.get("pages", 1))
        observed_pages = _strict_positive_int(data.get("pages"))
        return bool(
            _app_scoped_dispatch_target_matches(clean_tool, request, data)
            and str(request.get("direction") or "").strip().casefold()
            == str(data.get("direction") or "").strip().casefold()
            and str(request.get("direction") or "").strip()
            and requested_pages is not None
            and observed_pages == requested_pages
        )

    if clean_tool in {
        "desktop.safe_click",
        "app.open_and_safe_click",
        "app.focus_and_safe_click",
    }:
        requested_count = _strict_positive_int(request.get("click_count", 1))
        observed_count = _strict_positive_int(data.get("click_count"))
        requested_x = _strict_int(request.get("x"))
        requested_y = _strict_int(request.get("y"))
        observed_x = _strict_int(data.get("x"))
        observed_y = _strict_int(data.get("y"))
        if None in {requested_x, requested_y, observed_x, observed_y}:
            return False
        return bool(
            _app_scoped_dispatch_target_matches(clean_tool, request, data)
            and requested_x == observed_x
            and requested_y == observed_y
            and requested_count is not None
            and observed_count == requested_count
        )

    if clean_tool in {"desktop.open_path", "desktop.reveal_path"}:
        requested_path = str(
            request.get("path") or request.get("target_path") or ""
        ).strip()
        observed_path = str(data.get("path") or "").strip()
        expected_target = (
            "system_open" if clean_tool == "desktop.open_path" else "finder_reveal"
        )
        return bool(
            requested_path
            and observed_path == requested_path
            and data.get("exists") is True
            and str(data.get("open_target") or "").strip() == expected_target
        )

    if clean_tool in {
        "desktop.open_path_with_app",
        "app.open_path_with_app",
    }:
        requested_path = str(
            request.get("path") or request.get("target_path") or ""
        ).strip()
        observed_path = str(data.get("path") or "").strip()
        requested_app = str(
            request.get("resolved_app_name")
            or request.get("app_name")
            or ""
        ).strip()
        observed_app = str(
            data.get("resolved_app_name") or data.get("app_name") or ""
        ).strip()
        return bool(
            requested_path
            and observed_path == requested_path
            and data.get("exists") is True
            and str(data.get("open_target") or "").strip() == "app_open"
            and _canonical_app_identity(requested_app)
            and _canonical_app_identity(observed_app)
            == _canonical_app_identity(requested_app)
        )
    return False


def has_exact_native_dispatch_contract(tool_name: str | None) -> bool:
    """Return whether the tool has a fail-closed exact dispatch schema."""

    return str(tool_name or "").strip() in _EXACT_NATIVE_DISPATCH_TOOLS


def intrinsic_native_postcondition_target_matches(
    tool_name: str | None,
    input_payload: Mapping[str, Any] | None,
    target: Mapping[str, Any] | None,
) -> bool:
    """Bind a structured receipt to the source action target chosen by Goal."""

    clean_tool = str(tool_name or "").strip()
    request = input_payload if isinstance(input_payload, Mapping) else {}
    action_target = target if isinstance(target, Mapping) else {}
    if is_semantic_safe_shortcut(clean_tool, request):
        return False
    rule = _APP_LIFECYCLE_INTRINSIC_RULES.get(clean_tool)
    if rule is None:
        return False
    requested_app = str(
        request.get("requested_app_name") or request.get("app_name") or ""
    ).strip()
    return bool(
        requested_app
        and str(action_target.get("kind") or "").strip() == "desktop_app"
        and str(action_target.get("action") or "").strip()
        == str(rule["target_action"])
        and _canonical_app_identity(action_target.get("app_name"))
        == _canonical_app_identity(requested_app)
    )


def _app_scoped_dispatch_target_matches(
    tool_name: str,
    request: Mapping[str, Any],
    data: Mapping[str, Any],
) -> bool:
    if not str(tool_name or "").startswith("app."):
        return True
    requested_app = str(
        request.get("resolved_app_name") or request.get("app_name") or ""
    ).strip()
    observed_app = str(
        data.get("resolved_app_name") or data.get("app_name") or ""
    ).strip()
    return bool(
        _canonical_app_identity(requested_app)
        and _canonical_app_identity(observed_app)
        == _canonical_app_identity(requested_app)
    )


def _strict_positive_int(value: Any) -> int | None:
    parsed = _strict_int(value)
    if parsed is None:
        return None
    return parsed if parsed > 0 else None


def _strict_int(value: Any) -> int | None:
    """Parse an integral receipt field without bool/float coercion."""

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    clean = value.strip()
    if not clean:
        return None
    digits = clean[1:] if clean[:1] in {"+", "-"} else clean
    if not digits.isdigit():
        return None
    return int(clean)


def _canonical_app_identity(value: Any) -> str:
    compact = compact_app_alias(str(value or ""))
    if not compact:
        return ""
    return compact_app_alias(APP_ALIASES.get(compact, str(value or "")))
