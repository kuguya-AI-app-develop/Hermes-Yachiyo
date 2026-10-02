"""Bind native Music readback to its immutable search/play plan."""

from collections.abc import Mapping, Sequence
from typing import Any

_IDS = ("decision_id", "tool_plan_id", "plan_id", "request_id")
_STEPS = (
    "discover-media-app",
    "focus-media-app-search",
    "type-media-search-query",
    "submit-media-search",
    "play-media-search-result",
    "verify-media-search",
)


def _is_tool_event(event: Mapping[str, Any]) -> bool:
    return (event.get("event") or event.get("event_type")) == "agent.tool.call"


def native_music_search_receipt(
    action_event: Mapping[str, Any],
    verifier: Mapping[str, Any],
    timeline: Sequence[Mapping[str, Any]],
    *,
    contract=None,
) -> dict[str, Any]:
    """Only a local, exact Music state/query receipt can replace declared AX."""
    from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES
    from apps.shell.yachiyo_agent.entrypoint_tool_selection import (
        planner_first_direct_tool_selection,
    )
    from apps.shell.yachiyo_agent.runtime_execution import (
        runtime_execution_envelope_payload,
        runtime_execution_requests_from_envelope_payload,
    )

    from . import tool_execution as te
    from .goal_runtime import runtime_goal_contract
    from .model_intent_planning import planner_selection_needs_model_assistance

    run_id = str(verifier.get("run_id") or "")
    try:
        if contract is None:
            contract = runtime_goal_contract(
                run_id=run_id,
                runtime_execution_envelope=None,
                runtime_execution_metadata=None,
                messages=[],
                timeline=timeline,
            )
        if contract is None or not run_id or contract.run_id != run_id:
            return {}
        selection = planner_first_direct_tool_selection(
            contract.original_goal, DAILY_DESKTOP_TOOL_NAMES
        )
        if planner_selection_needs_model_assistance(selection, contract.original_goal):
            return {}
        requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope_payload(
                selection.decision,
                allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
                full_plan=True,
            ),
            allowed_tools=DAILY_DESKTOP_TOOL_NAMES,
        )
    except ValueError:
        return {}
    if tuple(request.get("step_id") for request in requests) != _STEPS:
        return {}
    specs = {request["step_id"]: request for request in requests}
    catalog_query = specs[_STEPS[0]]["input"].get("query")
    if (
        not isinstance(catalog_query, str)
        or catalog_query.strip().casefold() not in {"music", "apple music"}
    ):
        return {}
    source_spec, verify_spec = specs[_STEPS[-2]], specs[_STEPS[-1]]
    if not isinstance(verifier.get("input"), Mapping):
        return {}
    if (
        source_spec.get("tool") != "media.music_app_open_and_play"
        or verify_spec.get("tool") != "desktop.ui_elements"
        or verifier.get("step_id") != _STEPS[-1]
        or verifier.get("tool") != verify_spec.get("tool")
        or any(verifier.get(k) != verify_spec.get(k) for k in (*_IDS, "depends_on"))
        or dict(verifier.get("input") or {})
        not in (
            dict(verify_spec.get("input") or {}),
            {"app_name": "Music", "role_filter": "", "limit": 80},
        )
    ):
        return {}

    found = {}
    for index, event in enumerate(timeline):
        if not _is_tool_event(event) or event.get("step_id") not in _STEPS[:-1]:
            continue
        step = event["step_id"]
        spec = specs[step]
        result = event.get("result") or {}
        if (
            step in found
            or not isinstance(result, Mapping)
            or event.get("run_id") != run_id
            or event.get("actor") != "native_runtime"
            or event.get("execution_authority") != "runtime_tool_executor"
            or event.get("detail", event.get("tool")) != spec.get("tool")
            or any(event.get(k) != spec.get(k) for k in _IDS)
            or not event.get("tool_call_id")
            or result.get("ok") is not True
            or result.get("permission_error")
            or result.get("approval_required")
            or result.get("fallback_used")
            or result.get("truncated")
            or (
                isinstance(result.get("data"), Mapping)
                and result["data"].get("truncated")
            )
            or te._trusted_runtime_execution_provider_identity(event, result)
            != (te.LOCAL_DESKTOP_PROVIDER_KIND, te.LOCAL_DESKTOP_PROVIDER_ID)
        ):
            return {}
        expected_input = dict(spec.get("input") or {})
        if expected_input.get("selection_source") == "desktop.list_apps":
            expected_input.pop("selection_source")
            expected_input.pop("query")
            expected_input["app_name"] = "Music"
        if not isinstance(event.get("input_preview"), Mapping):
            return {}
        if dict(event.get("input_preview") or {}) != expected_input:
            return {}
        found[step] = (index, event)
    if set(found) != set(_STEPS[:-1]):
        return {}
    indices = [found[step][0] for step in _STEPS[:-1]]
    if indices != sorted(indices) or found[_STEPS[-2]][1] is not action_event:
        return {}
    if any(
        _is_tool_event(event) and event.get("step_id") not in _STEPS[:-1]
        for event in timeline[indices[0] :]
    ):
        return {}
    discovery = found[_STEPS[0]][1]["result"]
    catalog = discovery.get("data") or {}
    if not isinstance(catalog, Mapping):
        return {}
    chosen = catalog.get("best_match") or {}
    if not isinstance(chosen, Mapping) or not isinstance(catalog.get("apps"), list):
        return {}
    if (
        discovery.get("action") != "desktop.list_apps"
        or catalog.get("query") != specs[_STEPS[0]]["input"]["query"]
        or chosen.get("name") != "Music"
        or chosen.get("match_confidence") != "high"
        or not any(
            app.get("name") == "Music" and app.get("path") == chosen.get("path")
            for app in catalog.get("apps", [])
            if isinstance(app, Mapping)
        )
        or not str(chosen.get("path") or "").endswith("/Music.app")
    ):
        return {}
    result = action_event.get("result") or {}
    data = result.get("data") or {}
    if not isinstance(data, Mapping):
        return {}
    query = specs[_STEPS[2]]["input"].get("text")
    # Native Music returns the actual current track/artist/album. A different
    # current song cannot fulfil an explicit request just because it is playing.
    if (
        result.get("action") != "media.apple_music_open_and_play"
        or result.get("postcondition_verified") is not True
        or data.get("app_name") != "Music"
        or data.get("open_ok") is not True
        or data.get("playback_ok") is not True
        or data.get("control") != "play"
        or data.get("player_state") != "playing"
        or data.get("playback_state_unverified") is True
        or not isinstance(query, str)
        or not query.strip()
        or not any(
            isinstance(data.get(k), str) and data[k].strip().casefold() == query.strip().casefold()
            for k in ("track", "artist", "album")
        )
    ):
        return {}
    return {
        "source_tool": "media.music_app_open_and_play",
        "source_tool_call_id": action_event["tool_call_id"],
        "source_step_id": _STEPS[-2],
        "source_request_id": action_event["request_id"],
        "provider_kind": te.LOCAL_DESKTOP_PROVIDER_KIND,
        "provider_id": te.LOCAL_DESKTOP_PROVIDER_ID,
        "verified_observed_state": "playing",
        "verification_predicate_kind": "native_music_search_playback",
        "verification_depends_on": list(verify_spec["depends_on"]),
        "verification_input": dict(verifier["input"]),
    }
