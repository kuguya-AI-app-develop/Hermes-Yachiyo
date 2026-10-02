"""Readback proof for a selected search link in one run-owned CDP target."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit


def _http_url(value: Any) -> str:
    if not isinstance(value, str) or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        return ""
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        if parsed.username is not None or parsed.password is not None:
            return ""
        _ = parsed.port
    except ValueError:
        return ""
    return value


def trusted_search_link_navigation_receipt(
    action_tool: str,
    action_event: Mapping[str, Any],
    verifier_request: Mapping[str, Any],
    verifier_result: Mapping[str, Any],
    timeline: list[dict[str, Any]],
) -> dict[str, Any]:
    """Called only after the executor validated source/run/plan/provider scope.

    The native DOM click reports the chosen link's href before dispatch. It
    cannot attest navigation itself. A separate current_page call must read
    that exact URL in the same isolated target; redirects, new tabs and button
    effects deliberately remain unverified by this narrow observer.
    """
    if action_tool != "browser.click" or verifier_request.get("tool") != "browser.current_page":
        return {}
    producers = [
        event
        for event in timeline
        if event.get("event") == "agent.tool.call"
        and event.get("tool_call_id") == verifier_request.get("source_tool_call_id")
        and event.get("approval_resume_result_canonical") is not True
        and isinstance(event.get("result"), Mapping)
        and event["result"].get("ok") is True
    ]
    if len(producers) != 1 or producers[0] is not action_event:
        return {}
    if verifier_request.get("input") != {}:
        return {}
    if (
        action_event.get("actor") != "native_runtime"
        or action_event.get("execution_authority") != "runtime_tool_executor"
        or action_event.get("approved") is not True
        or any(
            not isinstance(verifier_request.get(key), str)
            or not verifier_request[key]
            or action_event.get(key) != verifier_request[key]
            for key in ("run_id", "plan_id", "decision_id", "tool_plan_id")
        )
    ):
        return {}
    if not action_event.get("request_id") or verifier_request.get(
        "source_request_id"
    ) != action_event.get("request_id"):
        return {}
    source_input = action_event.get("input_preview")
    source_result = action_event.get("result")
    if not isinstance(source_input, Mapping) or not isinstance(source_result, Mapping):
        return {}
    selector = source_input.get("selector")
    if not isinstance(selector, str) or not re.fullmatch(r"search-result=[1-9][0-9]*", selector):
        return {}
    if type(source_input.get("click_count")) is not int or dict(source_input) != {
        "selector": selector,
        "click_count": 1,
    }:
        return {}
    if (
        source_result.get("ok") is not True
        or verifier_result.get("ok") is not True
        or source_result.get("action") != "browser.click"
        or verifier_result.get("action") != "browser.current_page"
        or any(
            result.get(key) is True
            for result in (source_result, verifier_result)
            for key in ("fallback_used", "permission_error", "approval_required", "truncated")
        )
    ):
        return {}
    source = source_result.get("data")
    observed = verifier_result.get("data")
    if not isinstance(source, Mapping) or not isinstance(observed, Mapping):
        return {}
    target_id = source.get("target_id")
    if not isinstance(target_id, str) or not target_id or observed.get("target_id") != target_id:
        return {}
    if any(
        data.get(key) is not True
        for data in (source, observed)
        for key in ("target_owned_by_run", "browser_profile_isolated_from_user")
    ):
        return {}
    if (
        source.get("selector") != selector
        or source.get("tag") != "A"
        or type(source.get("click_count")) is not int
        or source.get("click_count") != 1
        or source.get("link_target") not in {"", "_self"}
        or source.get("truncated") is True
        or observed.get("truncated") is True
    ):
        return {}
    source_url = _http_url(source.get("source_url"))
    destination = _http_url(source.get("navigation_url"))
    observed_url = _http_url(observed.get("url"))
    if (
        not source_url
        or not destination
        or source_url == destination
        or observed_url != destination
    ):
        return {}
    return {
        "verified_observed_state": "open",
        "verification_predicate_kind": "exact_search_link_navigation",
        "observed_navigation_url": observed_url,
        "observed_browser_target_id": target_id,
    }


def approved_navigation_projection_duplicates(
    canonical: Mapping[str, Any],
    timeline: list[dict[str, Any]],
) -> bool:
    """Ignore only an exact approved projection's already-present native twin.

    Approval projections include policy metadata in their input preview and
    omit the original browser route. They cannot substitute for that native
    source event when attesting an independent page observation.
    """
    from .approval_resume import _approval_resume_canonical_event_duplicates

    if (
        canonical.get("approval_resume_result_canonical") is not True
        or canonical.get("approved") is not True
        or canonical.get("actor") != "native_runtime"
        or canonical.get("execution_authority") != "runtime_tool_executor"
    ):
        return False
    identity_keys = (
        "run_id",
        "decision_id",
        "plan_id",
        "tool_plan_id",
        "step_id",
        "request_id",
        "tool_call_id",
    )
    if any(not isinstance(canonical.get(key), str) or not canonical[key] for key in identity_keys):
        return False
    candidates = []
    for previous in timeline:
        if previous is canonical:
            break
        if (
            previous.get("detail") != "browser.click"
            or previous.get("approval_resume_result_canonical") is True
            or not isinstance(previous.get("result"), Mapping)
            or previous["result"].get("ok") is not True
            or previous.get("approved") is not True
            or previous.get("actor") != "native_runtime"
            or previous.get("execution_authority") != "runtime_tool_executor"
            or any(previous.get(key) != canonical[key] for key in identity_keys)
        ):
            continue
        canonical_input = canonical.get("input_preview")
        previous_input = previous.get("input_preview")
        if not isinstance(canonical_input, Mapping) or not isinstance(previous_input, Mapping):
            continue
        if type(canonical_input.get("click_count")) is not int:
            continue
        action_keys = {"selector", "click_count", "fallback_x", "fallback_y"}
        if {key: value for key, value in canonical_input.items() if key in action_keys} != dict(
            previous_input
        ):
            continue
        if _approval_resume_canonical_event_duplicates(canonical, previous):
            candidates.append(previous)
    return len(candidates) == 1
