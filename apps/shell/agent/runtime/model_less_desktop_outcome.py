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


def _observed_catalog_app_name(
    request: Mapping[str, Any],
    actual: Mapping[str, Any],
    *,
    planned: list[dict[str, Any]],
    timeline: list[Mapping[str, Any]],
    plan_id: str,
    tool_timeline_start: int,
) -> str:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    query = str(payload.get("query") or "").strip()
    resolved = str(actual.get("app_name") or "").strip()
    if not (
        payload.get("selection_source") == "desktop.list_apps"
        and query and str(payload.get("app_name") or "").strip() == query
        and actual.get("app_resolution_source") == "desktop.list_apps"
        and actual.get("requested_app_name") == query
        and actual.get("resolved_app_name") == resolved and resolved
        and not resolved.startswith("<")
    ):
        return ""
    for discovery in planned:
        inputs = discovery.get("input")
        if not (
            discovery.get("tool") == "desktop.list_apps"
            and isinstance(inputs, Mapping) and inputs.get("query") == query
        ):
            continue
        for event in timeline[max(0, tool_timeline_start):]:
            if not (
                (event.get("event") or event.get("event_type")) == "agent.tool.call"
                and (event.get("detail") or event.get("tool")) == "desktop.list_apps"
                and event.get("plan_id") == plan_id
                and event.get("request_id") == discovery.get("request_id")
                and (event.get("step_id") or event.get("planner_step_id"))
                == (discovery.get("step_id") or discovery.get("planner_step_id"))
            ):
                continue
            result = event.get("result")
            data = result.get("data") if isinstance(result, Mapping) else None
            if not (
                isinstance(result, Mapping) and result.get("ok") is True
                and isinstance(data, Mapping) and data.get("query") == query
            ):
                continue
            apps = data.get("apps")
            candidates = [app for app in apps if isinstance(app, Mapping)] if isinstance(apps, list) else []
            best = data.get("best_match")
            selected = best if isinstance(best, Mapping) else candidates[0] if len(candidates) == 1 else {}
            if len(candidates) > 1:
                score = selected.get("match_score")
                other_scores = [app.get("match_score") for app in candidates if app.get("name") != resolved]
                if not (
                    isinstance(score, (int, float)) and not isinstance(score, bool)
                    and score > 0 and other_scores
                    and all(isinstance(value, (int, float)) and not isinstance(value, bool) and value < score for value in other_scores)
                ):
                    continue
            if selected.get("name") == resolved and any(app.get("name") == resolved for app in candidates):
                return resolved
    return ""


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
        if actual.get("app_name") != app_name:
            resolved_name = _observed_catalog_app_name(
                request, actual, planned=planned, timeline=timeline,
                plan_id=plan_id, tool_timeline_start=tool_timeline_start,
            )
            if not resolved_name:
                return []
            request["input"] = payload = {**payload, "app_name": resolved_name}
        for key in ("app_name", "action", "text", "repeat_count", "direction", "pages", "x", "y"):
            if key in payload and actual.get(key) != payload[key]:
                return []
        primary_count += 1
    return planned if primary_count else []
