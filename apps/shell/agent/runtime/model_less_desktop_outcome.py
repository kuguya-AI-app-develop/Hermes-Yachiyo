"""Recognize an executed bounded desktop chain without granting completion."""

from collections.abc import Iterable, Mapping
from typing import Any

_BOUNDED_ACTIONS = frozenset(
    f"app.{mode}_and_{action}"
    for mode in ("open", "focus")
    for action in ("safe_key", "safe_shortcut", "safe_scroll", "safe_click", "safe_type_text")
)
_OBSERVATIONS = frozenset({
    "desktop.list_apps", "desktop.running_apps", "desktop.active_window",
    "desktop.windows", "desktop.ui_elements", "desktop.read_ui", "desktop.verify", "app.status",
})


def executed_bounded_desktop_requests(
    requests: Iterable[Mapping[str, Any]],
    timeline: list[Mapping[str, Any]],
    *,
    tool_timeline_start: int,
) -> list[dict[str, Any]]:
    """Return a same-plan chain only after every concrete primary action ran.

    This recognizes dispatch evidence for an honest partial result. It neither
    supplies a postcondition nor permits execution of an outstanding action.
    """
    planned = [dict(r) for r in requests if isinstance(r, Mapping)]
    plan_ids = {str(r.get("plan_id") or "").strip() for r in planned}
    if len(plan_ids) != 1 or not next(iter(plan_ids), ""):
        return []
    plan_id = next(iter(plan_ids))
    primary_count = 0
    for request in planned:
        tool = str(request.get("tool") or request.get("tool_name") or "")
        if tool in _OBSERVATIONS:
            continue
        if tool not in _BOUNDED_ACTIONS or request.get("continue_to_model"):
            return []
        if request.get("approval_required") or request.get("risk_level") != "low":
            return []
        request_id = str(request.get("request_id") or "")
        step_id = str(request.get("step_id") or request.get("planner_step_id") or "")
        if not request_id.startswith(f"{plan_id}:request:") or not step_id:
            return []
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        app_name = str(payload.get("app_name") or "").strip()
        if not app_name or app_name.startswith("<"):
            return []
        if any(payload.get(k) for k in ("body_source", "patch_source", "text_source", "model_generated_content")):
            return []
        event = next((e for e in reversed(timeline[max(0, tool_timeline_start):]) if (
            (e.get("event") or e.get("event_type")) == "agent.tool.call"
            and (e.get("detail") or e.get("tool")) == tool
            and e.get("plan_id") == plan_id
            and (e.get("step_id") or e.get("planner_step_id")) == step_id
            and e.get("request_id") == request_id
        )), None)
        if event is None:
            return []
        result = event.get("result") if isinstance(event.get("result"), Mapping) else {}
        actual = event.get("input_preview") if isinstance(event.get("input_preview"), Mapping) else {}
        if result.get("ok") is not True or result.get("approval_required"):
            return []
        for key in ("app_name", "action", "text", "repeat_count", "direction", "pages", "x", "y"):
            if key in payload and actual.get(key) != payload[key]:
                return []
        primary_count += 1
    return planned if primary_count else []
