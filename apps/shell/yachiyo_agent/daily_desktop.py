"""Shared daily desktop runtime helpers for Chat, Bubble, and Live2D."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from apps.shell.agent.runtime.desktop_recovery_metadata import (
    daily_desktop_metadata_tool_request,
    daily_desktop_recovery_prompt,
)
from apps.shell.agent.tools.policy import DAILY_DESKTOP_TOOL_NAMES

from .app_name_hints import explicit_known_app_action_target_hint
from .desktop_execution_policy import (
    daily_entrypoint_desktop_execution_policy,
    desktop_execution_policy_payload,
    desktop_provider_session_auto_start_recommended_for_requests,
)
from .desktop_plan_hints import (
    app_control_tool_candidates,
    app_management_tool_candidates,
    media_non_action_reference_hint,
    media_playback_hint,
)
from .discovered_app_followups import (
    planner_discovered_app_followup_can_direct_execute,
)
from .system_plan_hints import browser_tab_audio_control_request, system_control_hint

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyDesktopEntrypointRuntimePlan:
    decision: Any | None
    entrypoint_requests: list[dict[str, Any]]
    executable_requests: list[dict[str, Any]]
    runtime_execution_envelope: dict[str, Any]
    selected_source: str
    allowed_tools: tuple[str, ...] = ()

    @property
    def has_plan(self) -> bool:
        requests = self.runtime_execution_envelope.get("requests")
        return bool(
            self.entrypoint_requests
            or (isinstance(requests, list) and requests)
        )


_ENTRYPOINT_DISCOVERY_TOOLS = {
    "desktop.list_apps",
    "desktop.inspect_app",
    "desktop.running_apps",
    "desktop.permissions",
}
_ENTRYPOINT_VERIFY_TOOLS = {
    "clipboard.read",
    "desktop.active_window",
    "desktop.verify",
    "desktop.list_windows",
    "desktop.read_ui",
    "desktop.windows",
    "desktop.ui_elements",
    "screen.capture",
}
_ENTRYPOINT_NON_PRIMARY_TOOLS = {
    *_ENTRYPOINT_DISCOVERY_TOOLS,
    *_ENTRYPOINT_VERIFY_TOOLS,
}
_ENTRYPOINT_TIMELINE_CONTEXT_KEYS = (
    "request_id",
    "step_id",
    "capability_id",
    "decision_id",
    "plan_id",
    "tool_plan_id",
    "intent_kind",
    "core_id",
    "workspace_id",
    "group_run_id",
    "run_group_id",
    "group_id",
    "workflow_run_id",
    "workflow_id",
    "workflow_node_id",
    "workflow_node_label",
    "workflow_node_kind",
    "approval_required",
    "depends_on",
    "fallback_tools",
    "legacy_fallback",
    "compatibility_boundary",
    "runtime_doctrine",
    "runtime_stage",
    "runtime_role",
    "requires_observation",
    "requires_post_action_verification",
    "replan_triggers",
    "replan_signal_ids",
    "task_todo",
    "task_checkpoints",
    "task_workspace_items",
    "task_verification_targets",
)
_DESKTOP_AGENT_ENTRYPOINT_EXTRA_TOOLS = (
    "workspace.list",
    "workspace.read",
    "data.analyze",
    "workspace.write_patch",
    "file.organize",
    "terminal.run",
    "python.run",
    "artifact.write",
)
_DIRECT_BROWSER_ENTRYPOINT_TOOLS = frozenset(
    {
        "browser.open_url",
        "browser.open_url_and_extract_text",
        "browser.open_url_and_screenshot",
    }
)


def daily_desktop_allowed_tools(
    allowed_tools: Sequence[str] | None = None,
) -> list[str]:
    if allowed_tools is None:
        allowed_tools = DAILY_DESKTOP_TOOL_NAMES
    return [
        str(tool or "").strip()
        for tool in allowed_tools
        if str(tool or "").strip()
    ]


def desktop_agent_entrypoint_allowed_tools(
    allowed_tools: Sequence[str] | None = None,
) -> list[str]:
    """Fallback tool boundary for Chat/Bubble/Live2D as desktop agent entrypoints."""

    allowed = daily_desktop_allowed_tools(allowed_tools)
    result: list[str] = []
    seen: set[str] = set()
    for tool in [*allowed, *_DESKTOP_AGENT_ENTRYPOINT_EXTRA_TOOLS]:
        clean = str(tool or "").strip()
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def direct_browser_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    text: str = "",
) -> list[dict[str, Any]]:
    """Return direct low-risk browser requests even when artifact tools are available."""

    request_list = [request for request in requests or [] if isinstance(request, Mapping)]
    if not request_list:
        return []
    readback_requests = _direct_browser_readback_entrypoint_requests(request_list, text)
    if readback_requests:
        return readback_requests
    if _looks_like_browser_persistent_artifact_request(text):
        return []
    for index, request in enumerate(request_list):
        normalized = _direct_browser_entrypoint_request(request, text=text)
        if not normalized:
            continue
        if not _direct_browser_entrypoint_suffix_is_deferred_output_only(
            request_list[index + 1:],
        ):
            continue
        return [normalized]
    return []


_DIRECT_BROWSER_DEFERRED_OUTPUT_TOOLS = frozenset(
    {
        "artifact.write",
        "browser.current_page",
        "browser.extract",
        "browser.extract_text",
        "clipboard.write",
        "data.analyze",
    }
)
_DIRECT_BROWSER_READBACK_TOOLS = frozenset(
    {
        "browser.current_page",
        "browser.extract",
        "browser.extract_text",
        "browser.screenshot",
    }
)


def _direct_browser_entrypoint_request(
    request: Mapping[str, Any],
    *,
    text: str = "",
) -> dict[str, Any]:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in _DIRECT_BROWSER_ENTRYPOINT_TOOLS:
        return {}
    source = str(request.get("source") or "").strip()
    if source and source != "runtime_planner":
        return {}
    planning_reason = str(request.get("planning_reason") or "").strip()
    if planning_reason and "web" not in planning_reason:
        return {}
    if bool(request.get("approval_required")):
        return {}
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if not str(payload.get("url") or "").strip():
        return {}
    normalized = dict(request)
    normalized.pop("continue_to_model", None)
    if (
        tool_name == "browser.open_url_and_extract_text"
        and _looks_like_browser_summary_request(text)
    ):
        normalized["presentation"] = "summary"
    return normalized


def _direct_browser_entrypoint_suffix_is_deferred_output_only(
    requests: Sequence[Mapping[str, Any]],
) -> bool:
    for request in requests:
        tool_name = str(request.get("tool") or "").strip()
        if tool_name in _ENTRYPOINT_VERIFY_TOOLS:
            continue
        if tool_name not in _DIRECT_BROWSER_DEFERRED_OUTPUT_TOOLS:
            return False
        if bool(request.get("approval_required")):
            return False
    return True


def _direct_browser_readback_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]],
    text: str,
) -> list[dict[str, Any]]:
    if _looks_like_browser_persistent_artifact_request(text):
        return []
    if not _looks_like_current_page_readback_request(text):
        return []
    for index, request in enumerate(requests):
        normalized = _direct_browser_readback_entrypoint_request(request)
        if not normalized:
            continue
        if not _direct_browser_entrypoint_suffix_is_deferred_output_only(
            requests[index + 1:],
        ):
            continue
        tool_name = str(normalized.get("tool") or "").strip()
        if tool_name in {"browser.extract", "browser.extract_text"} and (
            _looks_like_browser_summary_request(text)
        ):
            normalized.setdefault("presentation", "summary")
        prefix: list[dict[str, Any]] = []
        for earlier in requests[:index]:
            earlier_tool = str(earlier.get("tool") or "").strip()
            if earlier_tool in _ENTRYPOINT_DISCOVERY_TOOLS:
                continue
            open_request = _direct_browser_entrypoint_request(earlier, text=text)
            if open_request and earlier_tool == "browser.open_url":
                prefix.append(open_request)
                continue
            return []
        return [*prefix, normalized]
    return []


def _direct_browser_readback_entrypoint_request(
    request: Mapping[str, Any],
) -> dict[str, Any]:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in _DIRECT_BROWSER_READBACK_TOOLS:
        return {}
    source = str(request.get("source") or "").strip()
    if source and source != "runtime_planner":
        return {}
    planning_reason = str(request.get("planning_reason") or "").strip()
    if planning_reason and "web" not in planning_reason:
        return {}
    if bool(request.get("approval_required")):
        return {}
    normalized = dict(request)
    normalized.pop("continue_to_model", None)
    return normalized


def _looks_like_current_page_readback_request(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    page_context = re.search(
        r"(?:当前(?:网页|页面)|(?:网页|页面)(?:内容)?|"
        r"current\s+(?:webpage|page)|this\s+(?:webpage|page)|browser\s+page)",
        value,
        flags=re.IGNORECASE,
    )
    if not page_context:
        return False
    return bool(
        re.search(
            r"(?:读|读取|看|内容|链接|总结|摘要|概括|截图|截个图|截张图|"
            r"read|extract|link|url|summari[sz]e|summary|recap|screenshot|capture)",
            value,
            flags=re.IGNORECASE,
        )
    )


def _looks_like_browser_summary_request(text: str) -> bool:
    return bool(
        re.search(
            r"(?:总结|摘要|概括|提炼|summari[sz]e|summary|recap)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _looks_like_browser_persistent_artifact_request(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            r"(?:报告|文档|文件|产出|输出|导出|保存|表格|调研|研究|分析|生成\s*(?:一份)?\s*"
            r"(?:报告|文档|文件|表格)|\breport\b|\bartifact\b|\bsave\b|\bexport\b|"
            r"\btable\b|\bresearch\b|\banaly[sz]e\b|\bmarkdown\b|\bmd\s+file\b)",
            value,
            flags=re.IGNORECASE,
        )
    )


def daily_desktop_requests_can_complete_without_model(
    requests: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Return true when deferred planner steps are only deterministic verification."""

    items = [request for request in requests or [] if isinstance(request, Mapping)]
    if not items:
        return False
    deferred = [request for request in items if bool(request.get("continue_to_model"))]
    if not deferred:
        return True
    return all(
        _deferred_request_is_direct_verification(request)
        or _deferred_request_is_deterministic_discovered_app_followup(request)
        for request in deferred
    )


def _deferred_request_is_deterministic_discovered_app_followup(
    request: Mapping[str, Any],
) -> bool:
    if str(request.get("source") or "").strip() != "runtime_planner":
        return False
    target = request.get("followup_target")
    if not isinstance(target, Mapping):
        return False
    if any(key in target for key in ("x", "y", "coordinates", "point")):
        return False
    return planner_discovered_app_followup_can_direct_execute(
        {"followup_target": target},
        [dict(request)],
        DAILY_DESKTOP_TOOL_NAMES,
    )


def _deferred_request_is_direct_verification(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    runtime_stage = str(request.get("runtime_stage") or "").strip()
    runtime_role = str(request.get("runtime_role") or "").strip()
    if runtime_stage == "verify" or runtime_role == "verify_result":
        if bool(request.get("approval_required")):
            return False
        if tool_name == "browser.current_page":
            return bool(
                request.get("source") == "runtime_verification"
                and request.get("step_id") == "verify-web-search-navigation"
                and request.get("input") == {}
                and request.get("depends_on") == ["click-web-search-result"]
            )
        if tool_name == "system.volume":
            payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
            return str(payload.get("action") or "").strip() == "status"
    if (
        tool_name not in _ENTRYPOINT_VERIFY_TOOLS
        and tool_name not in _ENTRYPOINT_DISCOVERY_TOOLS
        and tool_name != "desktop.verify"
    ):
        return False
    if bool(request.get("approval_required")):
        return False
    step_id = str(request.get("step_id") or "").strip()
    if runtime_stage == "verify" or runtime_role == "verify_result":
        return True
    return step_id.startswith("verify-")


_SAFE_DIRECT_ENTRYPOINT_TOOLS = frozenset(
    {
        "app.focus",
        "app.focus_window",
        "app.hide",
        "app.minimize",
        "app.open",
        "app.open_path_with_app",
        "app.show",
        "desktop.active_window",
        "desktop.verify",
        "desktop.focus_app",
        "desktop.list_apps",
        "desktop.list_windows",
        "desktop.open_app",
        "desktop.open_path_with_app",
        "desktop.read_ui",
        "desktop.running_apps",
        "desktop.ui_elements",
        "desktop.windows",
        "media.music_app_control",
        "media.music_app_open_and_play",
        "media.system_control",
        "screen.capture",
        "system.settings_open",
    }
)
_BLOCKED_DIRECT_ENTRYPOINT_TOOLS = frozenset(
    {
        "app.focus_and_click_ui_element",
        "app.focus_and_safe_click",
        "app.focus_and_safe_key",
        "app.focus_and_safe_scroll",
        "app.focus_and_safe_shortcut",
        "app.focus_and_safe_type_text",
        "app.focus_and_type_into_ui_element",
        "app.open_and_click_ui_element",
        "app.open_and_safe_click",
        "app.open_and_safe_key",
        "app.open_and_safe_scroll",
        "app.open_and_safe_shortcut",
        "app.open_and_safe_type_text",
        "app.open_and_type_into_ui_element",
        "browser.click",
        "browser.type_text",
        "desktop.safe_click",
        "desktop.safe_key",
        "desktop.safe_scroll",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.click",
        "desktop.click_ui_element",
        "desktop.type",
        "desktop.type_into_ui_element",
        "desktop.type_text",
    }
)


def daily_desktop_safe_direct_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    executable = daily_desktop_executable_entrypoint_requests(requests or [])
    if not executable:
        return []
    if not daily_desktop_requests_can_complete_without_model(executable):
        return []
    for request in executable:
        if not _daily_desktop_safe_direct_entrypoint_request(request):
            return []
    return executable


def _daily_desktop_safe_direct_entrypoint_request(request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
    if not tool_name or tool_name in _BLOCKED_DIRECT_ENTRYPOINT_TOOLS:
        return False
    if tool_name not in _SAFE_DIRECT_ENTRYPOINT_TOOLS:
        return False
    if bool(request.get("approval_required")) or bool(request.get("requires_approval")):
        return False
    risk_level = str(request.get("risk_level") or "").strip().lower()
    return risk_level not in {"high", "critical"}


def daily_desktop_approval_or_submit_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str = "",
) -> list[dict[str, Any]]:
    """Return approval-preserving foreground requests for Chat/Bubble entrypoints."""

    return _approval_entrypoint_requests(
        requests or [],
        text=text,
    ) or _submit_foreground_entrypoint_request(text)


_APPROVAL_ENTRYPOINT_PREREQUISITE_TOOLS = frozenset(
    {
        "app.open",
        "app.focus",
        "app.focus_window",
        "app.focus_and_safe_shortcut",
        "app.focus_and_safe_type_text",
        "app.open_and_safe_shortcut",
        "app.open_and_safe_type_text",
        "browser.open_url",
        "browser.current_page",
        "browser.extract",
        "browser.extract_text",
        "desktop.active_window",
        "desktop.open_app",
        "desktop.focus_app",
        "desktop.inspect_app",
        "desktop.list_apps",
        "desktop.list_windows",
        "desktop.read_ui",
        "desktop.running_apps",
        "desktop.safe_shortcut",
        "desktop.safe_type_text",
        "desktop.search_submit",
        "desktop.ui_elements",
        "desktop.verify",
        "screen.capture",
    }
)
_APPROVAL_ENTRYPOINT_NO_POSTVERIFY_TOOLS = frozenset(
    {
        "app.open",
        "app.focus",
        "app.focus_window",
        "desktop.open_app",
        "desktop.focus_app",
    }
)


def _approval_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]],
    *,
    text: str = "",
) -> list[dict[str, Any]]:
    request_list = [request for request in requests if isinstance(request, Mapping)]
    selected: list[dict[str, Any]] = []
    for index, request in enumerate(request_list):
        tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
        if not tool_name:
            continue
        if bool(request.get("approval_required")) or bool(request.get("requires_approval")):
            selected = _approval_entrypoint_prerequisite_requests(
                request_list[:index],
                request,
            )
            if _system_ui_open_confirm_is_redundant(selected, request):
                return selected
            selected.append(_entrypoint_request_copy(request))
            selected.extend(
                _entrypoint_request_copy(remaining)
                for remaining in request_list[index + 1 :]
                if str(
                    remaining.get("tool") or remaining.get("tool_name") or ""
                ).strip()
                and (
                    str(remaining.get("tool") or remaining.get("tool_name") or "").strip()
                    != "artifact.write"
                    or _looks_like_browser_persistent_artifact_request(text)
                )
            )
            return selected
    return []


def _approval_entrypoint_prerequisite_requests(
    prefix: Sequence[Mapping[str, Any]],
    approval_request: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Keep the ordered ancestor chain that prepares an approval-required action."""

    by_step_id: dict[str, Mapping[str, Any]] = {}
    for request in prefix:
        step_id = str(request.get("step_id") or request.get("planner_step_id") or "").strip()
        if step_id:
            by_step_id[step_id] = request

    required: set[str] = set()
    pending = [
        str(step_id or "").strip()
        for step_id in list(approval_request.get("depends_on") or [])
        if str(step_id or "").strip()
    ]
    while pending:
        step_id = pending.pop()
        if step_id in required:
            continue
        required.add(step_id)
        dependency = by_step_id.get(step_id)
        if dependency is None:
            continue
        pending.extend(
            str(parent or "").strip()
            for parent in list(dependency.get("depends_on") or [])
            if str(parent or "").strip() and str(parent or "").strip() not in required
        )

    selected: list[dict[str, Any]] = []
    for request in prefix:
        tool_name = str(request.get("tool") or request.get("tool_name") or "").strip()
        step_id = str(request.get("step_id") or request.get("planner_step_id") or "").strip()
        if (required and step_id in required) or (
            not required and tool_name in _APPROVAL_ENTRYPOINT_PREREQUISITE_TOOLS
        ):
            selected.append(_entrypoint_request_copy(request))
    return selected


def _system_ui_open_confirm_is_redundant(
    selected: list[dict[str, Any]],
    approval_request: Mapping[str, Any],
) -> bool:
    tool_name = str(approval_request.get("tool") or approval_request.get("tool_name") or "").strip()
    payload = approval_request.get("input") if isinstance(approval_request.get("input"), Mapping) else {}
    if tool_name != "desktop.submit_foreground" or str(payload.get("action") or "").strip() != "confirm":
        return False
    if len(selected) != 1:
        return False
    open_request = selected[0]
    open_tool = str(open_request.get("tool") or open_request.get("tool_name") or "").strip()
    if open_tool not in {"app.open", "desktop.open_app"}:
        return False
    open_input = open_request.get("input") if isinstance(open_request.get("input"), Mapping) else {}
    app_name = str(open_input.get("app_name") or "").strip().lower()
    return app_name in {"control center", "notification center", "launchpad"}


def _entrypoint_request_copy(request: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(request)
    tool_name = str(payload.get("tool") or payload.get("tool_name") or "").strip()
    tool_input = payload.get("input")
    if isinstance(tool_input, Mapping) and tool_input.get("app_name"):
        clean_input = dict(tool_input)
        clean_input.pop("query", None)
        clean_input.pop("selection_source", None)
        payload["input"] = clean_input
    if tool_name in _APPROVAL_ENTRYPOINT_NO_POSTVERIFY_TOOLS:
        payload["requires_post_action_verification"] = False
    return payload


def _submit_foreground_entrypoint_request(text: str) -> list[dict[str, Any]]:
    value = str(text or "").strip().lower()
    action = ""
    if re.fullmatch(
        r"(?:send)\s+(?:the\s+)?current\s+(?:message|content|input|text)",
        value,
    ):
        action = "send"
    elif re.fullmatch(
        r"(?:submit)\s+(?:the\s+)?current\s+(?:message|content|input|text|form)",
        value,
    ):
        action = "submit"
    if not action:
        return []
    return [
        {
            "protocol": "json_fallback",
            "tool": "desktop.submit_foreground",
            "input": {"action": action},
            "source": "runtime_planner",
            "planning_reason": "planner_submit_foreground_entrypoint",
            "approval_required": True,
            "risk_level": "high",
        }
    ]


def _looks_like_browser_artifact_request(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    return bool(
        re.search(
            r"(?:报告|文档|文件|产出|输出|导出|保存|总结|摘要|表格|调研|研究|分析|阅读|"
            r"读取|读一下|解释|提炼|生成\s*(?:一份)?\s*(?:报告|文档|文件|总结|摘要|表格)|"
            r"\breport\b|\bartifact\b|\bsave\b|\bexport\b|\bsummary\b|\bsummarize\b|"
            r"\bsummarise\b|\btable\b|\bresearch\b|\banaly[sz]e\b|\bread\b|\bextract\b|"
            r"\bdescribe\b|\bexplain\b)",
            value,
            flags=re.IGNORECASE,
        )
    )


def main_chat_entrypoint_allowed_tools(
    runtime: Any | None,
    *,
    fallback: Sequence[str] | None = None,
) -> list[str]:
    for policy in _runtime_main_chat_tool_policies(runtime):
        allowed = policy.get("allowed_tools") if isinstance(policy, Mapping) else None
        if allowed:
            return desktop_agent_entrypoint_allowed_tools(
                daily_desktop_allowed_tools(allowed)
            )
    return desktop_agent_entrypoint_allowed_tools(fallback)


def daily_desktop_entrypoint_requests(
    text: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    allowed_tools: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    allowed = daily_desktop_allowed_tools(allowed_tools)
    planner_requests, planner_owned = (
        _planner_owned_legacy_compatible_entrypoint_projection(
            str(text or ""),
            allowed,
            metadata=metadata,
        )
    )
    if planner_requests:
        return planner_requests
    if planner_owned:
        return []
    from apps.shell.agent.runtime.desktop_intents import (
        daily_desktop_entrypoint_tool_requests,
    )

    return daily_desktop_entrypoint_tool_requests(
        str(text or ""),
        allowed,
        metadata=metadata,
    )


def _planner_owned_legacy_compatible_entrypoint_requests(
    text: str,
    allowed: Sequence[str],
    *,
    metadata: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    requests, _ = _planner_owned_legacy_compatible_entrypoint_projection(
        text,
        allowed,
        metadata=metadata,
    )
    return requests


def _planner_owned_legacy_compatible_entrypoint_projection(
    text: str,
    allowed: Sequence[str],
    *,
    metadata: Mapping[str, Any] | None,
) -> tuple[list[dict[str, Any]], bool]:
    if browser_tab_audio_control_request(text):
        return [], True
    if media_non_action_reference_hint(text):
        return [], True
    if not allowed:
        media_hint = media_playback_hint(text)
        system_hint = system_control_hint(text)
        if str(media_hint.get("action") or "").strip() or str(
            system_hint.get("kind") or ""
        ).strip() == "volume":
            return [], True
    try:
        from .planner_execution import planner_decision_and_tool_requests

        decision, planner_requests = planner_decision_and_tool_requests(
            str(text or ""),
            allowed,
            metadata=metadata,
        )
    except Exception:
        logger.debug("Runtime planner legacy-compatible entrypoint unavailable", exc_info=True)
        return [], False
    context_capture_requests = (
        _legacy_compatible_context_capture_schedule_entrypoint_requests(
            decision,
            planner_requests,
            allowed=allowed,
        )
    )
    if context_capture_requests:
        return context_capture_requests, False
    generic_app_requests = _legacy_compatible_generic_app_discovery_entrypoint_requests(
        decision,
        planner_requests,
        allowed=allowed,
    )
    if generic_app_requests:
        return generic_app_requests, True
    if _generic_app_discovery_projection_query(decision):
        return [], True
    compound_app_requests = (
        _legacy_compatible_compound_app_management_entrypoint_requests(
            decision,
            planner_requests,
            allowed=allowed,
        )
    )
    if compound_app_requests:
        return compound_app_requests, True
    if _compound_app_management_projection_signature(decision):
        return [], True
    volume_requests = _legacy_compatible_system_volume_entrypoint_requests(
        decision,
        planner_requests,
        allowed=allowed,
    )
    if volume_requests:
        return volume_requests, True
    if _system_volume_projection_payload(decision):
        return [], True
    media_requests = _legacy_compatible_media_entrypoint_requests(
        decision,
        planner_requests,
        allowed=allowed,
    )
    if media_requests:
        return media_requests, True
    if _media_playback_projection_inputs(decision):
        return [], True
    search_requests = _legacy_compatible_search_entrypoint_requests(
        planner_requests,
        text=text,
    )
    if search_requests:
        return search_requests, False
    browser_search_requests = _legacy_compatible_browser_search_entrypoint_requests(
        planner_requests,
        text=text,
    )
    if browser_search_requests:
        return browser_search_requests, False
    browser_internal_page_requests = (
        _legacy_compatible_browser_internal_page_entrypoint_requests(
            planner_requests,
        )
    )
    if browser_internal_page_requests:
        return browser_internal_page_requests, False
    foreground_command_requests = (
        _legacy_compatible_foreground_command_entrypoint_requests(
            planner_requests,
        )
    )
    if foreground_command_requests:
        return foreground_command_requests, False
    search_box_requests = _legacy_compatible_context_transfer_search_box_requests(
        planner_requests,
        text=text,
    )
    if search_box_requests:
        return search_box_requests, False
    browser_click_requests = _legacy_compatible_browser_click_entrypoint_requests(
        planner_requests,
        text=text,
    )
    if browser_click_requests:
        return browser_click_requests, False
    semantic_ui_requests = _legacy_compatible_semantic_ui_entrypoint_requests(
        planner_requests,
        text=text,
    )
    if semantic_ui_requests:
        return semantic_ui_requests, False
    foreground_type_requests = _legacy_compatible_foreground_type_entrypoint_requests(
        planner_requests,
    )
    if foreground_type_requests:
        return foreground_type_requests, False
    observation_requests = _legacy_compatible_observation_entrypoint_requests(
        planner_requests,
    )
    if observation_requests:
        return observation_requests, False
    return _legacy_compatible_simple_entrypoint_requests(
        planner_requests,
        text=text,
    ), False


_LEGACY_COMPATIBLE_OBSERVATION_TOOLS = frozenset(
    {
        "desktop.active_window",
        "desktop.list_apps",
        "desktop.permissions",
        "desktop.running_apps",
        "desktop.ui_elements",
        "desktop.windows",
        "screen.capture",
    }
)


def _legacy_compatible_context_capture_schedule_entrypoint_requests(
    decision: Any,
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    allowed: Sequence[str],
) -> list[dict[str, Any]]:
    intent = getattr(decision, "selected_intent", None)
    intent_kind = str(getattr(intent, "kind", "") or "").strip()
    intent_inputs = getattr(intent, "inputs", None)
    if intent_kind not in {"information_capture", "schedule"} or not isinstance(
        intent_inputs,
        Mapping,
    ):
        return []
    source = str(
        intent_inputs.get("source")
        if intent_kind == "information_capture"
        else intent_inputs.get("context_source")
        or ""
    ).strip()
    user_goal = str(getattr(intent, "user_goal", "") or "")
    if source == "visible_text" and re.search(
        r"(?:屏幕|screen)",
        user_goal,
        flags=re.IGNORECASE,
    ):
        return []
    expected_source_tools = {
        "clipboard": ["clipboard.read"],
        "selection": ["desktop.safe_shortcut", "clipboard.read"],
        "current_page_link": ["browser.current_page"],
        "current_page_content": ["browser.extract_text"],
        "visible_text": ["desktop.ui_elements"],
    }
    source_tools = expected_source_tools.get(source)
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    if not source_tools or [str(item.get("tool") or "").strip() for item in items] != source_tools:
        return []
    expected_reason = (
        "planner_prefetch_information_capture_context"
        if intent_kind == "information_capture"
        else "planner_prefetch_schedule_context"
    )
    if any(
        str(item.get("planning_reason") or "").strip() != expected_reason
        for item in items
    ):
        return []
    if source == "selection":
        first_input = items[0].get("input") if isinstance(items[0].get("input"), Mapping) else {}
        if str(first_input.get("action") or "").strip() != "copy":
            return []
    if source == "visible_text":
        source_input = items[0].get("input") if isinstance(items[0].get("input"), Mapping) else {}
        if str(source_input.get("role_filter") or "").strip() != "text":
            return []
    destination = _context_capture_schedule_destination(decision, intent_kind, source)
    if not destination:
        return []
    required_tools = {"app.open_and_safe_shortcut", "desktop.safe_shortcut"}
    if not required_tools.issubset(set(allowed)):
        return []
    app_name, action = destination
    source_actions = {
        "clipboard": [],
        "selection": ["copy"],
        "current_page_link": ["copy_current_page_link"],
        "current_page_content": ["select_all", "copy"],
        "visible_text": ["select_all", "copy"],
    }[source]
    projected = [
        {"tool": "desktop.safe_shortcut", "input": {"action": action_name}}
        for action_name in source_actions
    ]
    projected.extend(
        [
            {
                "tool": "app.open_and_safe_shortcut",
                "input": {"app_name": app_name, "action": action},
            },
            {"tool": "desktop.safe_shortcut", "input": {"action": "paste"}},
        ]
    )
    return [_legacy_shape_request(request) for request in projected]


def _context_capture_schedule_destination(
    decision: Any,
    intent_kind: str,
    source: str,
) -> tuple[str, str] | None:
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = list(getattr(tool_plan, "steps", None) or [])
    if not steps:
        return None
    destination_step = steps[-1]
    tool_name = str(getattr(destination_step, "tool_name", "") or "").strip()
    input_preview = getattr(destination_step, "input_preview", None)
    if not isinstance(input_preview, Mapping) or str(
        input_preview.get("body_source") or ""
    ).strip() != source:
        return None
    destinations = {
        "notes.create": ("Notes", "new_note"),
        "reminders.create": ("Reminders", "new_reminder"),
        "calendar.create_event": ("Calendar", "new_event"),
    }
    destination = destinations.get(tool_name)
    if intent_kind == "information_capture":
        return destination if tool_name == "notes.create" else None
    return destination if tool_name in {"reminders.create", "calendar.create_event"} else None


def _legacy_compatible_foreground_type_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    if not items:
        return []
    if any(
        str(request.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for request in items
    ):
        return []
    tools = [str(request.get("tool") or "").strip() for request in items]
    if tools.count("desktop.safe_type_text") != 1 or any(
        tool
        not in {
            "desktop.running_apps",
            "desktop.safe_type_text",
            "desktop.ui_elements",
        }
        for tool in tools
    ):
        return []
    selected = next(
        request
        for request in items
        if str(request.get("tool") or "").strip() == "desktop.safe_type_text"
    )
    payload = selected.get("input") if isinstance(selected.get("input"), Mapping) else {}
    if not str(payload.get("text") or "").strip():
        return []
    return [_legacy_shape_request(selected)]


def _legacy_compatible_generic_app_discovery_entrypoint_requests(
    decision: Any,
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    allowed: Sequence[str],
) -> list[dict[str, Any]]:
    query = _generic_app_discovery_projection_query(decision)
    if not query:
        return []
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    tools = [str(item.get("tool") or "").strip() for item in items]
    verify_tools = {
        "desktop.verify",
        "desktop.active_window",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.ui_elements",
        "screen.capture",
    }
    if (
        len(tools) not in {2, 3}
        or tools[0] != "desktop.list_apps"
        or tools[1] not in {
            *app_control_tool_candidates("open"),
            *app_control_tool_candidates("focus"),
        }
        or (len(tools) == 3 and tools[2] not in verify_tools)
        or not set(tools).issubset(set(allowed))
    ):
        return []
    if any(
        str(item.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for item in items
    ):
        return []

    discovery_input = items[0].get("input")
    select_input = items[1].get("input")
    verify_input = items[2].get("input") if len(items) == 3 else {}
    if not all(
        isinstance(payload, Mapping)
        for payload in (discovery_input, select_input, verify_input)
    ):
        return []
    if (
        dict(discovery_input) != {"query": query, "limit": 20}
        or str(select_input.get("app_name") or "").strip()
        != "<selected app from desktop.list_apps>"
        or str(select_input.get("selection_source") or "").strip()
        != "desktop.list_apps"
        or str(select_input.get("query") or "").strip() != query
        or bool(verify_input)
    ):
        return []

    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = list(getattr(tool_plan, "steps", None) or [])
    expected_step_ids = [
        "discover_apps-desktop-state",
        "open-selected-discovered-app",
    ]
    if len(items) == 3:
        expected_step_ids.append("verify-desktop-result")
    if [str(getattr(step, "step_id", "") or "") for step in steps] != expected_step_ids:
        return []
    if [str(getattr(step, "tool_name", "") or "") for step in steps] != tools:
        return []
    if list(getattr(steps[1], "depends_on", None) or []) != [
        "discover_apps-desktop-state"
    ]:
        return []
    if len(steps) == 3 and list(getattr(steps[2], "depends_on", None) or []) != [
        "open-selected-discovered-app"
    ]:
        return []
    if any(bool(getattr(step, "approval_required", False)) for step in steps):
        return []
    return [_legacy_shape_request(request) for request in items]


def _generic_app_discovery_projection_query(decision: Any) -> str:
    intent = getattr(decision, "selected_intent", None)
    inputs = getattr(intent, "inputs", None)
    if str(getattr(intent, "kind", "") or "").strip() != "desktop_operation" or not isinstance(
        inputs,
        Mapping,
    ):
        return ""
    discovery_hint = inputs.get("desktop_discovery_hint")
    if not isinstance(discovery_hint, Mapping):
        return ""
    query = str(discovery_hint.get("query") or "").strip()
    if (
        str(discovery_hint.get("action") or "").strip() != "discover_apps"
        or str(inputs.get("operation_hint") or "").strip() != "discover_apps"
        or str(inputs.get("app_name_hint") or "").strip()
    ):
        return ""
    return query


def _legacy_compatible_observation_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    if not items:
        return []
    if any(
        str(request.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for request in items
    ):
        return []
    tools = [str(request.get("tool") or "").strip() for request in items]
    if any(tool not in _LEGACY_COMPATIBLE_OBSERVATION_TOOLS for tool in tools):
        return []
    selected = _legacy_shape_request(items[-1])
    if str(selected.get("tool") or "").strip() == "desktop.ui_elements":
        payload = selected.get("input") if isinstance(selected.get("input"), Mapping) else {}
        selected["input"] = {"role_filter": "", **dict(payload)}
    return [selected]


def _legacy_compatible_media_entrypoint_requests(
    decision: Any,
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    allowed: Sequence[str],
) -> list[dict[str, Any]]:
    inputs = _media_playback_projection_inputs(decision)
    if not inputs:
        return []
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    steps = _matching_planner_projection_steps(
        decision,
        items,
        planning_reason="planner_fallback_media_playback",
    )
    if not steps or not {
        str(item.get("tool") or "").strip() for item in items
    }.issubset(set(allowed)):
        return []
    if any(bool(getattr(step, "approval_required", False)) for step in steps):
        return []

    step_ids = [str(getattr(step, "step_id", "") or "") for step in steps]
    if "type-media-search-query" in step_ids:
        query = str(inputs.get("query") or "").strip()
        if not query or step_ids != [
            "discover-media-app",
            "focus-media-app-search",
            "type-media-search-query",
            "submit-media-search",
            "play-media-search-result",
            "verify-media-search",
        ]:
            return []
        type_index = step_ids.index("type-media-search-query")
        type_input = items[type_index].get("input")
        if not isinstance(type_input, Mapping) or str(
            type_input.get("text") or ""
        ).strip() != query:
            return []
        if any(_request_has_selected_app_placeholder(request) for request in items):
            return [_legacy_shape_request(request) for request in items]
        visible = [
            request
            for request in _visible_entrypoint_plan_requests(items)
            if str(request.get("tool") or "").strip()
            not in {"desktop.ui_elements", "desktop.active_window", "screen.capture"}
        ]
        return [_legacy_shape_request(request) for request in visible]

    if "control-media-playback" not in step_ids:
        return []
    control_index = step_ids.index("control-media-playback")
    control_request = items[control_index]
    control_input = control_request.get("input")
    if not isinstance(control_input, Mapping):
        return []
    action = str(inputs.get("action") or "").strip()
    if action and action != "status" and str(control_input.get("action") or action).strip() != action:
        return []
    return [_legacy_shape_request(control_request)]


def _media_playback_projection_inputs(decision: Any) -> Mapping[str, Any] | None:
    intent = getattr(decision, "selected_intent", None)
    inputs = getattr(intent, "inputs", None)
    if str(getattr(intent, "kind", "") or "").strip() != "media_playback" or not isinstance(
        inputs,
        Mapping,
    ):
        return None
    if str(inputs.get("action") or "").strip() not in {
        "play",
        "pause",
        "next",
        "previous",
        "status",
    }:
        return None
    return inputs


def _legacy_compatible_system_volume_entrypoint_requests(
    decision: Any,
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    allowed: Sequence[str],
) -> list[dict[str, Any]]:
    payload = _system_volume_projection_payload(decision)
    if not payload:
        return []
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    steps = _matching_planner_projection_steps(
        decision,
        items,
        planning_reason="planner_fallback_system_control",
    )
    if not steps or "system.volume" not in set(allowed):
        return []
    if any(bool(getattr(step, "approval_required", False)) for step in steps):
        return []
    expected_step_ids = ["control-system-state"]
    if str(payload.get("action") or "").strip() != "status":
        expected_step_ids.append("verify-system-state")
    if [str(getattr(step, "step_id", "") or "") for step in steps] != expected_step_ids:
        return []
    if not items or any(
        str(item.get("tool") or "").strip() != "system.volume" for item in items
    ):
        return []
    first_input = items[0].get("input")
    if not isinstance(first_input, Mapping) or dict(first_input) != dict(payload):
        return []
    if len(items) == 2:
        verify_input = items[1].get("input")
        if not isinstance(verify_input, Mapping) or dict(verify_input) != {
            "action": "status"
        }:
            return []
    return [_legacy_shape_request(items[0])]


def _system_volume_projection_payload(decision: Any) -> Mapping[str, Any] | None:
    intent = getattr(decision, "selected_intent", None)
    inputs = getattr(intent, "inputs", None)
    if str(getattr(intent, "kind", "") or "").strip() != "system_control" or not isinstance(
        inputs,
        Mapping,
    ):
        return None
    payload = inputs.get("payload")
    if str(inputs.get("kind") or "").strip() != "volume" or not isinstance(
        payload,
        Mapping,
    ):
        return None
    action = str(payload.get("action") or "").strip()
    if action not in {"set", "up", "down", "mute", "unmute", "status"}:
        return None
    if action == "set":
        level = payload.get("level")
        if not isinstance(level, int) or not 0 <= level <= 100:
            return None
    return payload


def _matching_planner_projection_steps(
    decision: Any,
    requests: Sequence[Mapping[str, Any]],
    *,
    planning_reason: str,
) -> list[Any]:
    if not requests or any(
        str(request.get("planning_reason") or "").strip() != planning_reason
        for request in requests
    ):
        return []
    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = [
        step
        for step in list(getattr(tool_plan, "steps", None) or [])
        if str(getattr(step, "tool_name", "") or "").strip()
    ]
    if len(steps) != len(requests):
        return []
    for step, request in zip(steps, requests, strict=True):
        request_input = request.get("input")
        if (
            str(getattr(step, "tool_name", "") or "").strip()
            != str(request.get("tool") or "").strip()
            or not isinstance(request_input, Mapping)
            or dict(getattr(step, "input_preview", None) or {}) != dict(request_input)
        ):
            return []
    return steps


def _legacy_compatible_browser_internal_page_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    if not items or any(
        str(request.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    tools = [str(request.get("tool") or "").strip() for request in visible]
    if tools not in (
        [
            "app.focus_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
        ],
        [
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.search_submit",
        ],
    ):
        return []
    first_input = visible[0].get("input") if isinstance(visible[0].get("input"), Mapping) else {}
    type_input = visible[1].get("input") if isinstance(visible[1].get("input"), Mapping) else {}
    app_name = str(first_input.get("app_name") or "").strip()
    internal_url = str(type_input.get("text") or "").strip()
    allowed_internal_urls = {
        "Google Chrome": r"chrome://(?:bookmarks|downloads|extensions)/",
        "Microsoft Edge": r"edge://(?:downloads|extensions|favorites)/",
        "Brave Browser": r"brave://(?:bookmarks|downloads|extensions)/",
        "Firefox": r"about:(?:addons|downloads)",
    }
    allowed_url = allowed_internal_urls.get(app_name, "")
    if (
        str(first_input.get("action") or "").strip() != "focus_address_bar"
        or not allowed_url
        or not re.fullmatch(allowed_url, internal_url, flags=re.IGNORECASE)
    ):
        return []
    legacy_requests = [_legacy_shape_request(request) for request in visible]
    if internal_url.lower() == "edge://favorites/":
        legacy_requests[1]["input"] = {"text": "edge://bookmarks/"}
    return legacy_requests


def _legacy_compatible_foreground_command_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    if not items or any(
        str(request.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if _legacy_compatible_command_palette_sequence(visible):
        return [_legacy_shape_request(request) for request in visible]
    if _legacy_compatible_app_close_window_sequence(visible):
        return [_legacy_shape_request(request) for request in visible]
    return []


def _legacy_compatible_command_palette_sequence(
    requests: Sequence[Mapping[str, Any]],
) -> bool:
    tools = [str(request.get("tool") or "").strip() for request in requests]
    if tools not in (
        ["app.open_and_safe_shortcut", "desktop.safe_type_text"],
        ["app.focus_and_safe_shortcut", "desktop.safe_type_text"],
        [
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.submit_foreground",
        ],
        [
            "app.focus_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.submit_foreground",
        ],
        [
            "app.open_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.safe_key",
            "desktop.submit_foreground",
        ],
        [
            "app.focus_and_safe_shortcut",
            "desktop.safe_type_text",
            "desktop.safe_key",
            "desktop.submit_foreground",
        ],
    ):
        return False
    first_input = requests[0].get("input")
    type_input = requests[1].get("input")
    if not isinstance(first_input, Mapping) or not isinstance(type_input, Mapping):
        return False
    if (
        str(first_input.get("action") or "").strip()
        not in {"command_palette", "obsidian_command_palette"}
        or not str(first_input.get("app_name") or "").strip()
        or not str(type_input.get("text") or "").strip()
    ):
        return False
    for request in requests[2:]:
        tool_name = str(request.get("tool") or "").strip()
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        if tool_name == "desktop.safe_key":
            if str(payload.get("action") or "").strip() not in {
                "arrow_down",
                "arrow_left",
                "arrow_right",
                "arrow_up",
            }:
                return False
            repeat_count = payload.get("repeat_count", 1)
            if not isinstance(repeat_count, int) or not 1 <= repeat_count <= 10:
                return False
        elif (
            tool_name != "desktop.submit_foreground"
            or str(payload.get("action") or "").strip() != "confirm"
        ):
            return False
    return True


def _legacy_compatible_app_close_window_sequence(
    requests: Sequence[Mapping[str, Any]],
) -> bool:
    if len(requests) != 2:
        return False
    first, second = requests
    first_input = first.get("input") if isinstance(first.get("input"), Mapping) else {}
    second_input = second.get("input") if isinstance(second.get("input"), Mapping) else {}
    return (
        str(first.get("tool") or "").strip()
        in {
            *app_control_tool_candidates("open"),
            *app_control_tool_candidates("focus"),
        }
        and bool(str(first_input.get("app_name") or "").strip())
        and str(second.get("tool") or "").strip() == "desktop.close_window"
        and not second_input
    )


def _legacy_compatible_compound_app_management_entrypoint_requests(
    decision: Any,
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    allowed: Sequence[str],
) -> list[dict[str, Any]]:
    signature = _compound_app_management_projection_signature(decision)
    if not signature:
        return []
    action, prepare_mode, intent_app_name = signature
    items = [dict(request) for request in requests or [] if isinstance(request, Mapping)]
    tools = [str(item.get("tool") or "").strip() for item in items]
    verify_tools = {
        "desktop.running_apps",
        "desktop.active_window",
        "desktop.windows",
        "desktop.list_windows",
        "desktop.verify",
    }
    if (
        len(tools) != 4
        or tools[0] != "desktop.list_apps"
        or tools[1] not in set(app_control_tool_candidates(prepare_mode))
        or tools[2] not in set(app_management_tool_candidates(action))
        or tools[3] not in verify_tools
    ):
        return []
    if any(
        str(item.get("planning_reason") or "").strip()
        != "planner_desktop_operation"
        for item in items
    ):
        return []
    if not set(tools).issubset(set(allowed)):
        return []

    discovery_input = items[0].get("input")
    prepare_input = items[1].get("input")
    manage_input = items[2].get("input")
    verify_input = items[3].get("input")
    if not all(
        isinstance(payload, Mapping)
        for payload in (discovery_input, prepare_input, manage_input, verify_input)
    ):
        return []
    app_name = str(prepare_input.get("app_name") or "").strip()
    discovery_query = str(discovery_input.get("query") or "").strip()
    manage_app_name = str(manage_input.get("app_name") or "").strip()
    verify_app_name = str(verify_input.get("app_name") or "").strip()
    if (
        not app_name
        or discovery_query not in {intent_app_name, app_name}
        or discovery_input.get("limit") != 20
        or (tools[2].startswith("app.") and manage_app_name != app_name)
        or (tools[2].startswith("desktop.") and bool(manage_input))
        or (verify_app_name and verify_app_name != app_name)
    ):
        return []

    plan = getattr(decision, "plan", None)
    tool_plan = getattr(plan, "tool_plan", None)
    steps = list(getattr(tool_plan, "steps", None) or [])
    if [str(getattr(step, "step_id", "") or "") for step in steps] != [
        "discover-desktop-state",
        "open-or-focus-app",
        "manage-app",
        "verify-desktop-result",
    ]:
        return []
    if [str(getattr(step, "tool_name", "") or "") for step in steps] != tools:
        return []
    if list(getattr(steps[1], "depends_on", None) or []) != ["discover-desktop-state"]:
        return []
    if list(getattr(steps[2], "depends_on", None) or []) != ["open-or-focus-app"]:
        return []
    if list(getattr(steps[3], "depends_on", None) or []) != ["manage-app"]:
        return []
    if bool(getattr(steps[2], "approval_required", False)) != (action == "quit"):
        return []
    return [_legacy_shape_request(request) for request in items[1:3]]


def _compound_app_management_projection_signature(
    decision: Any,
) -> tuple[str, str, str] | None:
    intent = getattr(decision, "selected_intent", None)
    intent_inputs = getattr(intent, "inputs", None)
    if str(getattr(intent, "kind", "") or "").strip() != "desktop_operation" or not isinstance(
        intent_inputs,
        Mapping,
    ):
        return None
    management_hint = intent_inputs.get("app_management_hint")
    if not isinstance(management_hint, Mapping):
        return None
    action = str(management_hint.get("action") or "").strip()
    prepare_mode = str(intent_inputs.get("app_management_prepare_mode") or "").strip()
    intent_app_name = str(management_hint.get("app_name") or "").strip()
    if action not in {"hide", "minimize", "quit"}:
        return None
    if prepare_mode not in {"open", "focus"} or not intent_app_name:
        return None
    return action, prepare_mode, intent_app_name


def _legacy_compatible_search_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    items = [dict(request) for request in requests if isinstance(request, Mapping)]
    if not items:
        return []
    if not any(
        str(request.get("planning_reason") or "").strip() == "planner_desktop_operation"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if not visible:
        return []
    if any(_request_has_selected_app_placeholder(request) for request in visible):
        return []
    tools = [str(request.get("tool") or "").strip() for request in visible]
    if tools == ["desktop.search_submit"]:
        return [_legacy_shape_request(visible[0])]
    if not _explicit_spotlight_prompt(text):
        return []
    if tools not in (
        ["desktop.safe_shortcut"],
        ["desktop.safe_shortcut", "desktop.safe_type_text"],
    ):
        return []
    first_input = visible[0].get("input") if isinstance(visible[0].get("input"), Mapping) else {}
    if str(first_input.get("action") or "").strip() != "spotlight_search":
        return []
    if len(visible) == 2:
        second_input = visible[1].get("input") if isinstance(visible[1].get("input"), Mapping) else {}
        if not str(second_input.get("text") or "").strip():
            return []
    return [_legacy_shape_request(request) for request in visible]


def _legacy_compatible_browser_search_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    items = [dict(request) for request in requests if isinstance(request, Mapping)]
    if not items:
        return []
    if not any(
        str(request.get("planning_reason") or "").strip() == "planner_fallback_web_research"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if not visible or any(request.get("continue_to_model") for request in visible):
        return []
    if any(_request_has_selected_app_placeholder(request) for request in visible):
        return []
    tools = [str(request.get("tool") or "").strip() for request in visible]
    if tools == ["browser.open_url"]:
        request = visible[0]
        return [_legacy_shape_request(request)] if _browser_search_open_url_request(request) else []
    if len(visible) != 2 or tools[1] != "browser.open_url":
        return []
    if tools[0] not in {
        "app.focus",
        "app.open",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_shortcut",
    }:
        return []
    first_input = visible[0].get("input") if isinstance(visible[0].get("input"), Mapping) else {}
    if not _browser_app_name(str(first_input.get("app_name") or "")):
        return []
    action = str(first_input.get("action") or "").strip()
    if action and action != "new_tab":
        return []
    if not _browser_search_open_url_request(visible[1]):
        return []
    return [_legacy_shape_request(request) for request in visible]


def _legacy_compatible_context_transfer_search_box_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    items = [dict(request) for request in requests if isinstance(request, Mapping)]
    if not items:
        return []
    if not any(
        str(request.get("planning_reason") or "").strip() == "planner_desktop_operation"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if not visible or any(request.get("continue_to_model") for request in visible):
        return []
    if any(_request_has_selected_app_placeholder(request) for request in visible):
        return []
    if not _looks_like_search_box_transfer_prompt(text):
        return []
    compatible: list[dict[str, Any]] = []
    index = 0
    found_click = False
    while index < len(visible):
        request = visible[index]
        tool_name = str(request.get("tool") or "").strip()
        if tool_name == "desktop.safe_shortcut":
            payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
            if str(payload.get("action") or "").strip() not in _CONTEXT_TRANSFER_SAFE_SHORTCUTS:
                return []
            compatible.append(_legacy_shape_request(request))
            index += 1
            continue
        if tool_name in {"app.focus", "app.open"}:
            if index + 1 >= len(visible):
                return []
            next_request = visible[index + 1]
            click_payload = _search_box_click_payload(next_request)
            if not click_payload:
                return []
            payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
            app_name = str(payload.get("app_name") or "").strip()
            if not app_name or _generic_non_app_name(app_name):
                return []
            combined_tool = (
                "app.open_and_click_ui_element"
                if tool_name == "app.open"
                else "app.focus_and_click_ui_element"
            )
            compatible.append(
                {
                    "protocol": str(next_request.get("protocol") or "json_fallback"),
                    "tool": combined_tool,
                    "input": {"app_name": app_name, **click_payload},
                }
            )
            found_click = True
            index += 2
            continue
        click_payload = _search_box_click_payload(request)
        if click_payload:
            compatible.append(_legacy_shape_request(request))
            found_click = True
            index += 1
            continue
        return []
    return compatible if found_click else []


def _legacy_compatible_browser_click_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    visible = _visible_entrypoint_plan_requests(
        [dict(request) for request in requests if isinstance(request, Mapping)]
    )
    if len(visible) != 1:
        return []
    request = visible[0]
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in {
        "app.focus_and_click_ui_element",
        "app.open_and_click_ui_element",
    }:
        return []
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    app_name = str(payload.get("app_name") or "").strip()
    target = str(payload.get("target") or "").strip()
    if not app_name or not target or not _browser_app_name(app_name):
        return []
    if not re.search(
        r"(?:点|点击|点按|单击|click|press|tap)",
        str(text or ""),
        flags=re.IGNORECASE,
    ):
        return []
    focus_tool = "app.open" if tool_name == "app.open_and_click_ui_element" else "app.focus"
    return [
        {
            "protocol": str(request.get("protocol") or "json_fallback"),
            "tool": focus_tool,
            "input": {"app_name": app_name},
        },
        {
            "protocol": str(request.get("protocol") or "json_fallback"),
            "tool": "browser.click",
            "input": {
                "selector": f"text={target}",
                "click_count": int(payload.get("click_count") or 1),
            },
        },
    ]


def _legacy_compatible_semantic_ui_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    items = [dict(request) for request in requests if isinstance(request, Mapping)]
    if not items:
        return []
    if not any(
        str(request.get("planning_reason") or "").strip() == "planner_desktop_operation"
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if not visible or any(request.get("continue_to_model") for request in visible):
        return []
    if any(_request_has_selected_app_placeholder(request) for request in visible):
        return []
    semantic = [
        request
        for request in visible
        if _semantic_ui_entrypoint_tool(str(request.get("tool") or ""))
    ]
    if len(semantic) != 1:
        return []
    if not _looks_like_semantic_ui_prompt(text):
        return []
    request = semantic[0]
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if not _semantic_ui_payload(payload):
        return []
    # The Runtime Planner keeps discovery, pre-action observation, and
    # post-action verification in the executable envelope.  The legacy-shaped
    # entrypoint projection exposes only the primary semantic action (plus an
    # explicit submit suffix); otherwise a valid planner-owned UI action falls
    # through to the legacy parser merely because it is safely surrounded by
    # observation steps.
    non_semantic = [
        request
        for request in visible
        if request not in semantic
        and str(request.get("tool") or "").strip()
        not in _ENTRYPOINT_NON_PRIMARY_TOOLS
    ]
    if non_semantic and any(
        str(request.get("tool") or "").strip() != "desktop.submit_foreground"
        for request in non_semantic
    ):
        return []
    return [
        _legacy_shape_request(request)
        for request in _legacy_semantic_ui_shape_requests(
            request,
            non_semantic,
            text=text,
        )
    ]


def _legacy_semantic_ui_shape_requests(
    semantic_request: Mapping[str, Any],
    suffix_requests: Sequence[Mapping[str, Any]],
    *,
    text: str,
) -> list[Mapping[str, Any]]:
    tool_name = str(semantic_request.get("tool") or "").strip()
    if (
        tool_name
        in {
            "app.focus_and_type_into_ui_element",
            "app.open_and_type_into_ui_element",
        }
        and _looks_like_click_then_type_prompt(text)
    ):
        payload = (
            semantic_request.get("input")
            if isinstance(semantic_request.get("input"), Mapping)
            else {}
        )
        typed_text = str(payload.get("text") or "").strip()
        app_name = str(payload.get("app_name") or "").strip()
        target = _legacy_semantic_click_target(payload)
        if typed_text and app_name and target:
            click_tool = (
                "app.open_and_click_ui_element"
                if tool_name == "app.open_and_type_into_ui_element"
                else "app.focus_and_click_ui_element"
            )
            click_payload = {
                "app_name": app_name,
                "target": target,
                "role_filter": str(payload.get("role_filter") or "").strip(),
                "limit": payload.get("limit") or 80,
                "click_count": int(payload.get("click_count") or 1),
            }
            return [
                {
                    **dict(semantic_request),
                    "tool": click_tool,
                    "input": click_payload,
                },
                {
                    **dict(semantic_request),
                    "tool": "desktop.safe_type_text",
                    "input": {"text": typed_text},
                },
                *suffix_requests,
            ]
    return [_legacy_normalized_semantic_ui_request(semantic_request), *suffix_requests]


def _legacy_normalized_semantic_ui_request(
    request: Mapping[str, Any],
) -> Mapping[str, Any]:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    target = _legacy_semantic_click_target(payload)
    if not target or target == str(payload.get("target") or "").strip():
        return request
    return {
        **dict(request),
        "input": {
            **dict(payload),
            "target": target,
        },
    }


def _legacy_semantic_click_target(payload: Mapping[str, Any]) -> str:
    target = str(payload.get("target") or "").strip()
    replacements = {
        "消息框": "消息",
        "聊天框": "消息",
        "搜索框": "搜索",
        "搜索栏": "搜索",
        "地址栏": "地址",
    }
    target = replacements.get(target, target)
    return re.sub(
        r"\s*(?:按钮|button)$",
        "",
        target,
        flags=re.IGNORECASE,
    ).strip(" .，,。")


def _looks_like_click_then_type_prompt(text: str) -> bool:
    value = str(text or "")
    return bool(
        re.search(
            r"(?:点击|点一下|点按|单击|点).{0,40}(?:输入|填写|填入|键入|写入|写)",
            value,
            flags=re.IGNORECASE,
        )
        or re.search(
            r"\b(?:click|press|tap).{0,80}\b(?:type|enter|fill|write)\b",
            value,
            flags=re.IGNORECASE,
        )
    )


def _legacy_compatible_simple_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]] | None,
    *,
    text: str,
) -> list[dict[str, Any]]:
    if not requests:
        return []
    items = [dict(request) for request in requests if isinstance(request, Mapping)]
    if not items:
        return []
    if not any(
        str(request.get("planning_reason") or "").strip()
        in {
            "planner_desktop_operation",
            "planner_fallback_file_access",
            "planner_fallback_web_research",
        }
        for request in items
    ):
        return []
    visible = _visible_entrypoint_plan_requests(items)
    if len(visible) != 1:
        return []
    request = visible[0]
    tool_name = str(request.get("tool") or "").strip()
    if tool_name not in _LEGACY_COMPATIBLE_SIMPLE_PLANNER_TOOLS:
        return []
    if _request_has_selected_app_placeholder(request):
        return []
    if not _legacy_compatible_simple_request(text, request):
        return []
    return [_legacy_finder_action_shape(text, _legacy_shape_request(request))]


def _legacy_finder_action_shape(text: str, request: dict[str, Any]) -> dict[str, Any]:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    if str(payload.get("app_name") or "").strip() != "Finder":
        return request
    if (
        str(payload.get("action") or "").strip()
        not in _LEGACY_FINDER_FOCUS_SHAPE_ACTIONS
    ):
        return request
    if str(request.get("tool") or "").strip() != "app.open_and_safe_shortcut":
        return request
    if _explicit_open_finder_action_prompt(text):
        return request
    return {**request, "tool": "app.focus_and_safe_shortcut"}


def _explicit_open_finder_action_prompt(text: str) -> bool:
    value = str(text or "").strip()
    return bool(
        re.match(r"^(?:打开|启动|开启)\s*Finder\b", value, flags=re.IGNORECASE)
        or re.match(r"^(?:open|launch|start)\s+finder\b", value, flags=re.IGNORECASE)
    )


_LEGACY_COMPATIBLE_SIMPLE_PLANNER_TOOLS = frozenset(
    {
        "app.focus",
        "app.focus_and_safe_key",
        "app.focus_and_safe_scroll",
        "app.focus_and_safe_shortcut",
        "app.open",
        "app.open_and_safe_key",
        "app.open_and_safe_scroll",
        "app.open_and_safe_shortcut",
        "app.hide",
        "app.minimize",
        "app.quit",
        "app.show",
        "app.status",
        "browser.open_url",
        "desktop.safe_shortcut",
        "desktop.open_path",
        "desktop.reveal_path",
        "desktop.running_apps",
        "desktop.close_window",
        "desktop.hide_app",
        "desktop.minimize_window",
        "desktop.quit_app",
        "desktop.show_all_apps",
    }
)


def _legacy_compatible_simple_request(text: str, request: Mapping[str, Any]) -> bool:
    tool_name = str(request.get("tool") or "").strip()
    if tool_name in _LEGACY_COMPATIBLE_FOREGROUND_COMMAND_TOOLS:
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        return not payload
    if tool_name in {"desktop.open_path", "desktop.reveal_path", "desktop.running_apps"}:
        return True
    if tool_name == "desktop.safe_shortcut":
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        return str(payload.get("action") or "").strip() in _LEGACY_COMPATIBLE_SAFE_SHORTCUTS
    if tool_name == "browser.open_url":
        return _legacy_compatible_browser_open_request(text, request)
    if tool_name in _LEGACY_COMPATIBLE_APP_ACTION_TOOLS:
        return _legacy_compatible_app_action_request(request)
    if tool_name == "app.status":
        payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
        return bool(str(payload.get("app_name") or "").strip())
    if tool_name not in {
        "app.focus",
        "app.hide",
        "app.minimize",
        "app.open",
        "app.quit",
        "app.show",
    }:
        return False
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name:
        return False
    if _generic_non_app_name(app_name):
        return False
    if tool_name in {"app.hide", "app.minimize", "app.quit"}:
        return True
    if explicit_known_app_action_target_hint(text) == app_name:
        return True
    return not _app_prompt_has_non_launch_followup(text)


_LEGACY_COMPATIBLE_APP_ACTION_TOOLS = frozenset(
    {
        "app.focus_and_safe_key",
        "app.focus_and_safe_scroll",
        "app.focus_and_safe_shortcut",
        "app.open_and_safe_key",
        "app.open_and_safe_scroll",
        "app.open_and_safe_shortcut",
    }
)

_LEGACY_COMPATIBLE_APP_ACTIONS = frozenset(
    {
        "command_palette",
        "copy",
        "escape",
        "find",
        "finder_airdrop",
        "finder_get_info",
        "finder_network",
        "finder_quick_look",
        "finder_recents",
        "focus_address_bar",
        "new_folder",
        "new_event",
        "new_message",
        "new_note",
        "new_private_window",
        "new_reminder",
        "open_devtools",
        "obsidian_command_palette",
        "parent_folder",
        "preferences",
        "rename_selected",
        "show_history",
        "tab",
        "toggle_full_screen",
    }
)

_LEGACY_COMPATIBLE_NEW_MESSAGE_APPS = frozenset(
    {"Mail", "Messages", "Microsoft Outlook", "Slack", "WeChat"}
)

_LEGACY_COMPATIBLE_CREATION_ACTION_APPS = {
    "new_event": "Calendar",
    "new_note": "Notes",
    "new_reminder": "Reminders",
}

_LEGACY_COMPATIBLE_FINDER_ACTIONS = frozenset(
    {
        "copy",
        "finder_airdrop",
        "finder_get_info",
        "finder_network",
        "finder_quick_look",
        "finder_recents",
        "new_folder",
        "parent_folder",
        "rename_selected",
    }
)

_LEGACY_FINDER_FOCUS_SHAPE_ACTIONS = frozenset(
    {"copy", "finder_get_info", "parent_folder", "rename_selected"}
)

_CONTEXT_TRANSFER_SAFE_SHORTCUTS = frozenset(
    {"copy", "copy_current_page_link", "paste", "select_all"}
)

_LEGACY_COMPATIBLE_FOREGROUND_COMMAND_TOOLS = frozenset(
    {
        "desktop.close_window",
        "desktop.hide_app",
        "desktop.minimize_window",
        "desktop.quit_app",
        "desktop.show_all_apps",
    }
)

_LEGACY_COMPATIBLE_SAFE_SHORTCUTS = frozenset(
    {
        *_CONTEXT_TRANSFER_SAFE_SHORTCUTS,
        "application_windows",
        "bookmark_page",
        "browser_back",
        "browser_forward",
        "close_tab",
        "focus_address_bar",
        "force_quit_dialog",
        "hide_other_apps",
        "emoji_picker",
        "find",
        "lock_screen",
        "mission_control",
        "new_private_window",
        "new_tab",
        "new_window",
        "next_tab",
        "next_window",
        "open_devtools",
        "previous_tab",
        "previous_window",
        "redo",
        "refresh",
        "reopen_closed_tab",
        "reset_zoom",
        "show_history",
        "spotlight_search",
        "switch_next_app",
        "switch_previous_app",
        "toggle_full_screen",
        "undo",
        "zoom_in",
        "zoom_out",
    }
)


def _legacy_compatible_app_action_request(request: Mapping[str, Any]) -> bool:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    app_name = str(payload.get("app_name") or "").strip()
    if not app_name or _generic_non_app_name(app_name):
        return False
    action = str(
        payload.get("action")
        or payload.get("direction")
        or ""
    ).strip()
    if not action:
        return False
    tool_name = str(request.get("tool") or "").strip()
    if tool_name.endswith("_safe_scroll"):
        return action in {"down", "left", "right", "up"}
    if action not in _LEGACY_COMPATIBLE_APP_ACTIONS:
        return False
    if action in _LEGACY_COMPATIBLE_FINDER_ACTIONS:
        return app_name == "Finder"
    if action == "new_message":
        return app_name in _LEGACY_COMPATIBLE_NEW_MESSAGE_APPS
    expected_creation_app = _LEGACY_COMPATIBLE_CREATION_ACTION_APPS.get(action)
    if expected_creation_app:
        return app_name == expected_creation_app
    return True


def _generic_non_app_name(app_name: str) -> bool:
    compact = re.sub(r"\s+", "", str(app_name or "").strip().lower())
    return compact in {
        "project",
        "repo",
        "repository",
        "workspace",
        "leave",
        "privatewindow",
        "incognitowindow",
        "项目",
        "仓库",
        "工作区",
        "私密窗口",
        "无痕窗口",
        "隐身窗口",
    }


def _legacy_compatible_browser_open_request(text: str, request: Mapping[str, Any]) -> bool:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    url = str(payload.get("url") or "").strip().lower()
    if not url:
        return False
    if re.search(r"(?:搜索|搜|聚焦|spotlight|\bsearch\b|\bfind\b)", str(text or ""), flags=re.IGNORECASE):
        return False
    if "google.com/search" in url or "/search?" in url:
        return False
    return True


def _looks_like_search_box_transfer_prompt(text: str) -> bool:
    value = str(text or "")
    return bool(
        re.search(r"(?:搜索框|搜索栏|查找框|search\s+(?:box|field|input))", value, flags=re.IGNORECASE)
        and re.search(
            r"(?:输入|填入|粘贴|贴到|填到|type|enter|paste)",
            value,
            flags=re.IGNORECASE,
        )
    )


def _search_box_click_payload(request: Mapping[str, Any]) -> dict[str, Any]:
    if str(request.get("tool") or "").strip() != "desktop.click_ui_element":
        return {}
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    target = str(payload.get("target") or "").strip()
    role_filter = str(payload.get("role_filter") or "").strip()
    if not target or not role_filter:
        return {}
    if not re.search(r"(?:搜索|查找|search|find)", target, flags=re.IGNORECASE):
        return {}
    if role_filter not in {"text", "textbox", "search"}:
        return {}
    return dict(payload)


def _semantic_ui_entrypoint_tool(tool_name: str) -> bool:
    clean = str(tool_name or "").strip()
    return clean in {
        "app.focus_and_click_ui_element",
        "app.open_and_click_ui_element",
        "app.focus_and_type_into_ui_element",
        "app.open_and_type_into_ui_element",
        "desktop.click_ui_element",
        "desktop.type_into_ui_element",
    }


def _looks_like_semantic_ui_prompt(text: str) -> bool:
    value = str(text or "")
    return bool(
        re.search(
            r"(?:点击|点一下|点按|单击|点|按钮|输入|填写|键入|click|press|tap|type|enter|fill)",
            value,
            flags=re.IGNORECASE,
        )
    )


def _semantic_ui_payload(payload: Mapping[str, Any]) -> bool:
    target = str(payload.get("target") or "").strip()
    if not target:
        return False
    role_filter = str(payload.get("role_filter") or "").strip()
    if role_filter and role_filter not in {"button", "text", "textbox", "search"}:
        return False
    text = payload.get("text")
    if text is not None and not str(text).strip():
        return False
    app_name = str(payload.get("app_name") or "").strip()
    return not app_name or not _generic_non_app_name(app_name)


def _app_prompt_has_non_launch_followup(text: str) -> bool:
    value = str(text or "")
    lowered = value.lower()
    return bool(
        re.search(
            r"(?:浏览器|新建|无痕|隐身|开发者|历史|地址栏|搜索|取消|全屏|设置|"
            r"并|然后|之后|再|里|中|上|按|点击|点|输入|滚动|刷新|标签页|页面)",
            value,
        )
        or re.search(
            r"\b(?:browser|new|incognito|private|devtools|developer|history|address|"
            r"search|find|cancel|escape|fullscreen|full\s+screen|settings?|and|then|after|"
            r"in|inside|press|click|type|scroll|refresh|tab|page)\b",
            lowered,
        )
    )


def _explicit_spotlight_prompt(text: str) -> bool:
    return bool(
        re.search(
            r"(?:Spotlight|spotlight|聚焦搜索|系统搜索)",
            str(text or ""),
            flags=re.IGNORECASE,
        )
    )


def _browser_search_open_url_request(request: Mapping[str, Any]) -> bool:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    url = str(payload.get("url") or "").strip().lower()
    return bool(url and ("google.com/search" in url or "/search?" in url))


def _browser_app_name(app_name: str) -> bool:
    compact = re.sub(r"\s+", " ", str(app_name or "").strip().lower())
    return compact in {
        "chrome",
        "google chrome",
        "safari",
        "firefox",
        "edge",
        "microsoft edge",
        "brave",
    }


def _legacy_shape_request(request: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": str(request.get("protocol") or "json_fallback"),
        "tool": str(request.get("tool") or "").strip(),
        "input": dict(request.get("input") if isinstance(request.get("input"), Mapping) else {}),
    }


def _request_has_selected_app_placeholder(request: Mapping[str, Any]) -> bool:
    payload = request.get("input") if isinstance(request.get("input"), Mapping) else {}
    for value in payload.values():
        text = str(value or "").strip()
        if text.startswith("<selected app from "):
            return True
    return False


def planner_first_daily_desktop_entrypoint_requests(
    text: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    allowed_tools: Sequence[str] | None = None,
    metadata_allowed_tools: Sequence[str] | None = None,
    execution_normalized: bool = False,
    include_runtime_context: bool = False,
    allow_legacy_fallback: bool = False,
) -> list[dict[str, Any]]:
    """Return daily entrypoint requests from Runtime Planner by default."""

    allowed = daily_desktop_allowed_tools(allowed_tools)
    direct_tool_request = daily_desktop_direct_metadata_request(
        metadata,
        allowed_tools=metadata_allowed_tools if metadata_allowed_tools is not None else allowed,
    )
    if direct_tool_request:
        return [direct_tool_request]
    try:
        from .planner_execution import planner_execution_tool_requests, planner_tool_requests

        if execution_normalized and include_runtime_context:
            runtime_requests = _runtime_execution_context_entrypoint_requests(
                str(text or ""),
                allowed,
                metadata=metadata,
            )
            if runtime_requests and _entrypoint_requests_have_primary_action(
                runtime_requests,
            ):
                return [dict(request) for request in runtime_requests]

        planner_requests = planner_tool_requests(
            str(text or ""),
            allowed,
            metadata=metadata,
        )
    except Exception:
        logger.debug("Runtime planner daily desktop candidates unavailable", exc_info=True)
        planner_requests = []
    if planner_requests:
        if execution_normalized:
            normalized = (
                planner_execution_tool_requests(planner_requests, allowed)
                or planner_requests
            )
            return [dict(request) for request in normalized]
        return planner_requests
    if not allow_legacy_fallback:
        return []
    return _legacy_entrypoint_compatibility_requests(
        daily_desktop_entrypoint_requests(
            text,
            metadata=metadata,
            allowed_tools=allowed,
        )
    )


def daily_desktop_entrypoint_runtime_plan(
    text: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    allowed_tools: Sequence[str] | None = None,
    metadata_allowed_tools: Sequence[str] | None = None,
    allow_legacy_fallback: bool = False,
) -> DailyDesktopEntrypointRuntimePlan:
    """Plan once, then derive entrypoint and Runtime execution projections."""

    allowed = daily_desktop_allowed_tools(allowed_tools)
    direct_request = daily_desktop_metadata_tool_request(
        metadata,
        daily_desktop_allowed_tools(
            metadata_allowed_tools
            if metadata_allowed_tools is not None
            else allowed
        ),
    )
    if direct_request:
        bound = _structured_recovery_request_runtime_binding(
            str(text or ""),
            direct_request,
            metadata=metadata,
            allowed_tools=allowed,
        )
        if bound is None:
            # A trusted recovery selection is still not permission to execute
            # outside the immutable planner goal.  Leave it non-executable
            # when the exact tool/input cannot be bound to one plan criterion.
            return DailyDesktopEntrypointRuntimePlan(
                decision=None,
                entrypoint_requests=[],
                executable_requests=[],
                runtime_execution_envelope={},
                selected_source="metadata_unbound",
                allowed_tools=tuple(allowed),
            )
        (
            decision,
            requests,
            executable_requests,
            runtime_execution_envelope,
        ) = bound
        return DailyDesktopEntrypointRuntimePlan(
            decision=decision,
            entrypoint_requests=requests,
            executable_requests=executable_requests,
            runtime_execution_envelope=runtime_execution_envelope,
            selected_source="metadata_runtime_planner",
            allowed_tools=tuple(allowed),
        )

    decision = None
    planner_requests: list[dict[str, Any]] = []
    runtime_execution_envelope: dict[str, Any] = {}
    executable_requests: list[dict[str, Any]] = []
    blocked_requests: list[dict[str, Any]] = []
    try:
        from .planner_execution import (
            planner_decision_and_tool_requests,
            planner_execution_tool_requests,
        )
        from .runtime_execution import (
            runtime_execution_blocked_requests_from_envelope_payload,
            runtime_execution_requests_from_envelope_payload,
        )

        decision, planner_requests = planner_decision_and_tool_requests(
            str(text or ""),
            allowed,
            metadata=metadata,
        )
        runtime_execution_envelope = daily_desktop_runtime_execution_envelope(
            text,
            metadata=metadata,
            allowed_tools=allowed,
            decision=decision,
        )
        executable_requests = runtime_execution_requests_from_envelope_payload(
            runtime_execution_envelope,
            allowed_tools=allowed,
        )
        blocked_requests = runtime_execution_blocked_requests_from_envelope_payload(
            runtime_execution_envelope,
            allowed_tools=allowed,
        )
        if executable_requests or blocked_requests:
            return DailyDesktopEntrypointRuntimePlan(
                decision=decision,
                entrypoint_requests=executable_requests or blocked_requests,
                executable_requests=executable_requests,
                runtime_execution_envelope=runtime_execution_envelope,
                selected_source="runtime_execution_envelope",
                allowed_tools=tuple(allowed),
            )
        if planner_requests:
            normalized = planner_execution_tool_requests(planner_requests, allowed)
            executable_requests = [
                dict(request) for request in (normalized or planner_requests)
            ]
    except Exception:
        logger.debug("Runtime planner entrypoint plan unavailable", exc_info=True)

    if executable_requests:
        return DailyDesktopEntrypointRuntimePlan(
            decision=decision,
            entrypoint_requests=executable_requests,
            executable_requests=executable_requests,
            runtime_execution_envelope=runtime_execution_envelope,
            selected_source="runtime_planner",
            allowed_tools=tuple(allowed),
        )
    legacy_requests = (
        _legacy_entrypoint_compatibility_requests(
            daily_desktop_entrypoint_requests(
                text,
                metadata=metadata,
                allowed_tools=allowed,
            )
        )
        if allow_legacy_fallback
        else []
    )
    return DailyDesktopEntrypointRuntimePlan(
        decision=decision,
        entrypoint_requests=legacy_requests,
        executable_requests=legacy_requests,
        runtime_execution_envelope={},
        selected_source="daily_desktop_intent" if legacy_requests else "none",
        allowed_tools=tuple(allowed),
    )


def _structured_recovery_request_runtime_binding(
    text: str,
    direct_request: Mapping[str, Any],
    *,
    metadata: Mapping[str, Any] | None,
    allowed_tools: Sequence[str],
) -> tuple[
    Any,
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
] | None:
    """Compile trusted recovery semantics into one complete Runtime plan."""

    try:
        from apps.shell.agent.runtime.action_targets import action_target_matches

        from .runtime_execution import (
            runtime_execution_blocked_requests_from_envelope_payload,
            runtime_execution_envelope_payload,
            runtime_execution_requests_from_envelope_payload,
        )
        from .runtime_planner import RuntimePlanner

        planner = RuntimePlanner()
        original_decision = planner.decision(
            str(text or ""),
            allowed_tools=allowed_tools,
        )
        original_envelope = runtime_execution_envelope_payload(
            original_decision,
            allowed_tools=allowed_tools,
            full_plan=True,
            metadata=metadata,
        )
        direct_tool = str(direct_request.get("tool") or "").strip()
        direct_input = (
            dict(direct_request.get("input"))
            if isinstance(direct_request.get("input"), Mapping)
            else {}
        )
        original_matches = _structured_recovery_planned_request_matches(
            original_envelope,
            direct_tool=direct_tool,
            direct_input=direct_input,
        )
        if original_matches:
            decision = original_decision
            envelope = original_envelope
            matches = original_matches
        else:
            if direct_tool in {"system.volume", "system.brightness"}:
                # Concrete system controls must already belong to the user's
                # compiled goal; recovery metadata cannot supply that authority.
                return None
            recovery_goal = daily_desktop_recovery_prompt(metadata)
            if not recovery_goal:
                return None
            recovery_decision = planner.decision(
                recovery_goal,
                allowed_tools=allowed_tools,
            )
            recovery_envelope = runtime_execution_envelope_payload(
                recovery_decision,
                allowed_tools=allowed_tools,
                full_plan=True,
                metadata=metadata,
            )
            if _structured_recovery_goal_conflicts(
                original_decision,
                recovery_decision,
            ):
                return None
            matches = _structured_recovery_planned_request_matches(
                recovery_envelope,
                direct_tool=direct_tool,
                direct_input=direct_input,
            )
            if not matches:
                return None
            decision = recovery_decision
            envelope = recovery_envelope
    except (TypeError, ValueError):
        logger.debug("Structured recovery goal binding unavailable", exc_info=True)
        return None
    raw_requests = envelope.get("requests")
    if not isinstance(raw_requests, list):
        return None
    if len(matches) != 1:
        return None
    planned_request = matches[0]
    step_id = str(planned_request.get("step_id") or "").strip()
    capability_id = str(planned_request.get("capability_id") or "").strip()
    plan_id = str(planned_request.get("plan_id") or "").strip()
    action_target = planned_request.get("action_target")
    if (
        not step_id
        or not capability_id
        or not plan_id
        or not isinstance(action_target, Mapping)
        or not action_target
    ):
        return None
    task_core = getattr(getattr(decision, "plan", None), "task_core", None)
    contract = getattr(task_core, "goal_contract", None)
    criteria = list(getattr(contract, "criteria", None) or [])
    criterion_matches = [
        criterion
        for criterion in criteria
        if step_id in list(getattr(criterion, "source_step_ids", None) or [])
        and capability_id
        in list(getattr(criterion, "required_capabilities", None) or [])
        and isinstance(getattr(criterion, "expected", None), Mapping)
        and isinstance(criterion.expected.get("target"), Mapping)
        and action_target_matches(
            criterion.expected["target"],
            action_target,
            capability_ids=(capability_id,),
            source_step_id=step_id,
        )
    ]
    if len(criterion_matches) != 1:
        return None
    criterion = criterion_matches[0]
    contract_id = str(getattr(contract, "contract_id", "") or "").strip()
    criterion_id = str(getattr(criterion, "criterion_id", "") or "").strip()
    if not contract_id or not criterion_id:
        return None

    bound_envelope = dict(envelope)
    bound_raw_requests: list[Any] = []
    selected_step_id = step_id
    for item in raw_requests:
        if not isinstance(item, Mapping):
            bound_raw_requests.append(item)
            continue
        bound_item = dict(item)
        item_step_id = str(bound_item.get("step_id") or "").strip()
        bound_item["goal_contract_id"] = contract_id
        bound_item["root_goal_unchanged"] = True
        matching_criteria = [
            candidate
            for candidate in criteria
            if item_step_id
            and item_step_id
            in {
                *list(getattr(candidate, "source_step_ids", None) or []),
                *list(getattr(candidate, "verifier_step_ids", None) or []),
            }
        ]
        if len(matching_criteria) == 1:
            bound_item["goal_criterion_id"] = str(
                getattr(matching_criteria[0], "criterion_id", "") or ""
            ).strip()
        if item_step_id == selected_step_id:
            bound_item["source"] = str(direct_request.get("source") or "").strip()
            bound_item["planning_reason"] = str(
                direct_request.get("planning_reason") or ""
            ).strip()
        bound_raw_requests.append(bound_item)
    bound_envelope["requests"] = bound_raw_requests
    executable_requests = runtime_execution_requests_from_envelope_payload(
        bound_envelope,
        allowed_tools=allowed_tools,
    )
    blocked_requests = runtime_execution_blocked_requests_from_envelope_payload(
        bound_envelope,
        allowed_tools=allowed_tools,
    )
    bound_by_step = {
        str(item.get("step_id") or "").strip(): item
        for item in bound_raw_requests
        if isinstance(item, Mapping) and str(item.get("step_id") or "").strip()
    }
    for projected in [*executable_requests, *blocked_requests]:
        projected_step_id = str(projected.get("step_id") or "").strip()
        bound_item = bound_by_step.get(projected_step_id)
        if not isinstance(bound_item, Mapping):
            continue
        for key in (
            "goal_contract_id",
            "goal_criterion_id",
            "root_goal_unchanged",
            "source",
            "planning_reason",
        ):
            value = bound_item.get(key)
            if value not in (None, "", [], {}):
                projected[key] = value
    entrypoint_requests = executable_requests or blocked_requests
    if not entrypoint_requests:
        return None
    return (
        decision,
        entrypoint_requests,
        executable_requests,
        bound_envelope,
    )


def _structured_recovery_planned_request_matches(
    envelope: Mapping[str, Any],
    *,
    direct_tool: str,
    direct_input: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    raw_requests = envelope.get("requests")
    if not isinstance(raw_requests, list):
        return []
    return [
        item
        for item in raw_requests
        if isinstance(item, Mapping)
        and str(item.get("tool_name") or item.get("tool") or "").strip()
        == direct_tool
        and isinstance(item.get("input"), Mapping)
        and _structured_recovery_input_is_semantic_subset(
            direct_input,
            item["input"],
        )
    ]


def _structured_recovery_input_is_semantic_subset(
    selected: Mapping[str, Any],
    planned: Mapping[str, Any],
) -> bool:
    # Presentation-only rationale is planner-owned and may be normalized.
    ignored_keys = {"reason", "presentation"}
    return all(
        key in planned and planned[key] == value
        for key, value in selected.items()
        if key not in ignored_keys
    )


def _structured_recovery_goal_conflicts(
    original_decision: Any,
    recovery_decision: Any,
) -> bool:
    """Reject recovery metadata that changes a concrete root-goal identity."""

    identity_keys = {
        "app_name",
        "url",
        "path",
        "target",
        "query",
        "window_title",
        "title_contains",
        "text",
        "level",
    }

    def identities(decision: Any) -> dict[str, set[str]]:
        task_core = getattr(getattr(decision, "plan", None), "task_core", None)
        contract = getattr(task_core, "goal_contract", None)
        values: dict[str, set[str]] = {}
        for criterion in list(getattr(contract, "criteria", None) or []):
            expected = getattr(criterion, "expected", None)
            target = expected.get("target") if isinstance(expected, Mapping) else None
            if not isinstance(target, Mapping):
                continue
            for key in identity_keys:
                value = target.get(key)
                if value in (None, "", [], {}):
                    continue
                values.setdefault(key, set()).add(str(value).strip().casefold())
        return values

    original = identities(original_decision)
    recovery = identities(recovery_decision)
    if not original:
        return False
    for key, original_values in original.items():
        recovery_values = recovery.get(key)
        if recovery_values is None or original_values.isdisjoint(recovery_values):
            return True
    return False


def daily_desktop_runtime_plan_prepared_for_execution(
    plan: DailyDesktopEntrypointRuntimePlan,
    *,
    metadata: Mapping[str, Any] | None = None,
) -> DailyDesktopEntrypointRuntimePlan:
    """Start an approved low-risk provider session without replanning the task."""

    if (
        plan.decision is None
        or not plan.runtime_execution_envelope
        or not desktop_provider_session_auto_start_recommended_for_requests(
            plan.runtime_execution_envelope
        )
    ):
        return plan
    refreshed_envelope = daily_desktop_runtime_execution_envelope(
        "",
        metadata=metadata,
        allowed_tools=plan.allowed_tools,
        decision=plan.decision,
        ensure_provider=True,
    )
    if not refreshed_envelope:
        return plan
    try:
        from .runtime_execution import (
            runtime_execution_blocked_requests_from_envelope_payload,
            runtime_execution_requests_from_envelope_payload,
        )

        executable_requests = runtime_execution_requests_from_envelope_payload(
            refreshed_envelope,
            allowed_tools=plan.allowed_tools,
        )
        blocked_requests = runtime_execution_blocked_requests_from_envelope_payload(
            refreshed_envelope,
            allowed_tools=plan.allowed_tools,
        )
    except Exception:
        logger.debug("Prepared Runtime envelope projection unavailable", exc_info=True)
        return plan
    return DailyDesktopEntrypointRuntimePlan(
        decision=plan.decision,
        entrypoint_requests=(
            executable_requests
            or blocked_requests
            or plan.entrypoint_requests
        ),
        executable_requests=executable_requests,
        runtime_execution_envelope=refreshed_envelope,
        selected_source="runtime_execution_envelope",
        allowed_tools=plan.allowed_tools,
    )


def daily_desktop_runtime_execution_envelope(
    text: str,
    *,
    metadata: Mapping[str, Any] | None = None,
    allowed_tools: Sequence[str] | None = None,
    decision: Any | None = None,
    ensure_provider: bool = False,
) -> dict[str, Any]:
    """Return the full Runtime execution envelope for daily entrypoints."""

    allowed = daily_desktop_allowed_tools(allowed_tools)
    try:
        from .runtime_execution import runtime_execution_envelope_payload
        from .runtime_planner import RuntimePlanner

        selected_decision = (
            decision
            if decision is not None
            else RuntimePlanner().decision(
                str(text or ""), allowed_tools=allowed, metadata=metadata
            )
        )
        envelope = runtime_execution_envelope_payload(
            selected_decision,
            allowed_tools=allowed,
            full_plan=True,
            metadata=metadata,
        )
        if not envelope or not ensure_provider:
            return envelope
        from .isolated_provider_session import (
            annotate_envelope_with_desktop_provider_session,
            ensure_isolated_desktop_provider_session_for_envelope,
        )

        session = ensure_isolated_desktop_provider_session_for_envelope(envelope)
        if session.get("needed") and session.get("running"):
            refreshed = runtime_execution_envelope_payload(
                selected_decision,
                allowed_tools=allowed,
                full_plan=True,
                metadata=metadata,
            )
            if refreshed:
                envelope = refreshed
        return annotate_envelope_with_desktop_provider_session(envelope, session)
    except Exception:
        logger.debug("Runtime execution envelope unavailable for daily desktop", exc_info=True)
        return {}


def daily_desktop_executable_entrypoint_requests(
    requests: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    return [dict(request) for request in requests if isinstance(request, Mapping)]


def daily_desktop_direct_metadata_request(
    metadata: Mapping[str, Any] | None,
    *,
    allowed_tools: Sequence[str] | None = None,
) -> dict[str, Any] | None:
    request = daily_desktop_metadata_tool_request(
        metadata,
        daily_desktop_allowed_tools(allowed_tools),
    )
    if request is None:
        return None
    return _daily_desktop_request_with_execution_policy(request, metadata)


def _daily_desktop_request_with_execution_policy(
    request: Mapping[str, Any],
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    payload = dict(request)
    if desktop_execution_policy_payload(payload.get("desktop_execution_policy")):
        return payload
    metadata_policy = (
        desktop_execution_policy_payload(metadata.get("desktop_execution_policy"))
        if isinstance(metadata, Mapping)
        else {}
    )
    if not metadata_policy and isinstance(metadata, Mapping):
        metadata_policy = desktop_execution_policy_payload(
            metadata.get("yachiyo_desktop_execution_policy")
        )
    payload["desktop_execution_policy"] = metadata_policy or (
        daily_entrypoint_desktop_execution_policy(
            surface=_daily_desktop_surface_from_metadata(metadata),
        )
    )
    return payload


def _daily_desktop_surface_from_metadata(metadata: Mapping[str, Any] | None) -> str:
    if not isinstance(metadata, Mapping):
        return "chat"
    launcher_mode = str(metadata.get("launcher_mode") or "").strip()
    if launcher_mode in {"bubble", "live2d"}:
        return launcher_mode
    source = str(
        metadata.get("entrypoint_source")
        or metadata.get("source")
        or metadata.get("surface")
        or ""
    ).strip()
    if source in {"bubble", "live2d"}:
        return source
    return "chat"


def daily_desktop_recovery_execution_prompt(
    prompt: str,
    metadata: Mapping[str, Any] | None = None,
) -> str:
    return daily_desktop_recovery_prompt(metadata) or str(prompt or "").strip()


def daily_desktop_user_metadata(
    requests: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if not requests:
        return {}
    tools = [
        str(request.get("tool") or "").strip()
        for request in requests
        if str(request.get("tool") or "").strip()
    ]
    if not tools:
        return {}
    first_request = requests[0]
    payload: dict[str, Any] = {
        "daily_desktop_intent": True,
        "daily_desktop_source": str(first_request.get("source") or "daily_desktop_intent"),
        "daily_desktop_planning_reason": str(
            first_request.get("planning_reason") or "clear_daily_desktop_intent"
        ),
        "daily_desktop_tool": tools[0],
        "daily_desktop_tools": tools,
    }
    compatibility_boundary = str(first_request.get("compatibility_boundary") or "").strip()
    if compatibility_boundary:
        payload["daily_desktop_compatibility_boundary"] = compatibility_boundary
    if first_request.get("legacy_fallback"):
        payload["daily_desktop_legacy_fallback"] = True
    return payload


def entrypoint_plan_user_metadata(
    requests: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    """Project planner-first entrypoint metadata while preserving legacy keys."""

    metadata = daily_desktop_user_metadata(_visible_entrypoint_plan_requests(requests))
    if not metadata:
        return {}
    source = str(metadata.get("daily_desktop_source") or "").strip()
    reason = str(metadata.get("daily_desktop_planning_reason") or "").strip()
    tool = str(metadata.get("daily_desktop_tool") or "").strip()
    tools = metadata.get("daily_desktop_tools")
    tool_list = [str(item or "").strip() for item in tools or [] if str(item or "").strip()]
    return {
        **metadata,
        "entrypoint_plan": True,
        "entrypoint_plan_source": source,
        "entrypoint_plan_reason": reason,
        "entrypoint_plan_tool": tool,
        "entrypoint_plan_tools": tool_list,
        "entrypoint_plan_legacy_fallback": source != "runtime_planner",
    }


def _visible_entrypoint_plan_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    items = [request for request in requests or [] if isinstance(request, Mapping)]
    if len(items) <= 1:
        return items
    primary_indexes = [
        index
        for index, request in enumerate(items)
        if str(request.get("tool") or "").strip() not in _ENTRYPOINT_NON_PRIMARY_TOOLS
    ]
    if not primary_indexes:
        visible = list(items)
        while (
            len(visible) > 1
            and str(visible[0].get("tool") or "").strip() in _ENTRYPOINT_DISCOVERY_TOOLS
        ):
            visible = visible[1:]
        return visible
    first_primary = primary_indexes[0]
    last_primary = primary_indexes[-1]
    visible = []
    for index, request in enumerate(items):
        tool_name = str(request.get("tool") or "").strip()
        if tool_name in _ENTRYPOINT_DISCOVERY_TOOLS and (
            index < first_primary or index > last_primary
        ):
            continue
        if tool_name in _ENTRYPOINT_NON_PRIMARY_TOOLS and index > last_primary:
            continue
        visible.append(request)
    return visible or items


def _legacy_entrypoint_compatibility_requests(
    requests: Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    compatible: list[dict[str, Any]] = []
    for request in requests or ():
        if not isinstance(request, Mapping):
            continue
        compatible.append(
            {
                **dict(request),
                "legacy_fallback": True,
                "compatibility_boundary": "legacy_daily_desktop_intent",
            }
        )
    return compatible


def daily_desktop_planned_timeline(
    prompt: str = "",
    *,
    requests: Sequence[Mapping[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    allowed_tools: Sequence[str] | None = None,
    include_runtime_context: bool = False,
) -> list[dict[str, Any]]:
    planned_requests = list(requests or ())
    if not planned_requests:
        planned_requests = planner_first_daily_desktop_entrypoint_requests(
            prompt,
            metadata=metadata,
            allowed_tools=allowed_tools,
            execution_normalized=True,
            include_runtime_context=include_runtime_context,
        )
    if not planned_requests:
        return []
    timeline: list[dict[str, Any]] = []
    for request in planned_requests:
        tool_name = str(request.get("tool") or "").strip()
        if not tool_name:
            continue
        tool_input = request.get("input") if isinstance(request.get("input"), dict) else {}
        event = {
            "event": "agent.desktop.intent_planned",
            "detail": tool_name,
            "tool": tool_name,
            "status": "planned",
            "source": str(request.get("source") or "daily_desktop_intent"),
            "planning_reason": str(
                request.get("planning_reason") or "clear_daily_desktop_intent"
            ),
            "input_preview": dict(tool_input),
        }
        if request.get("continue_to_model"):
            event["continue_to_model"] = True
        for key in _ENTRYPOINT_TIMELINE_CONTEXT_KEYS:
            value = request.get(key)
            if _has_entrypoint_timeline_context_value(value):
                event[key] = value
        timeline.append(event)
    return timeline


def _has_entrypoint_timeline_context_value(value: Any) -> bool:
    return value not in (None, "", [], {}) and value is not False


def _runtime_execution_context_entrypoint_requests(
    text: str,
    allowed_tools: Sequence[str],
    *,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    try:
        from .runtime_execution import runtime_execution_requests_from_envelope_payload

        envelope = daily_desktop_runtime_execution_envelope(
            text,
            metadata=metadata,
            allowed_tools=allowed_tools,
        )
        return runtime_execution_requests_from_envelope_payload(
            envelope,
            allowed_tools=allowed_tools,
        )
    except Exception:
        logger.debug("Runtime execution context entrypoint unavailable", exc_info=True)
        return []


def _entrypoint_requests_have_primary_action(
    requests: Sequence[Mapping[str, Any]] | None,
) -> bool:
    for request in requests or ():
        if not isinstance(request, Mapping):
            continue
        tool_name = str(request.get("tool") or "").strip()
        user_requested_visual_capture = (
            tool_name == "screen.capture"
            and str(request.get("runtime_role") or "").strip()
            == "capture_visual_state"
        )
        if tool_name and (
            tool_name not in _ENTRYPOINT_NON_PRIMARY_TOOLS
            or user_requested_visual_capture
        ):
            return True
    return False


def _runtime_main_chat_tool_policies(runtime: Any | None) -> list[Mapping[str, Any]]:
    if runtime is None:
        return []
    policies: list[Mapping[str, Any]] = []
    main_chat_tool_policy = getattr(runtime, "_main_chat_tool_policy", None)
    if callable(main_chat_tool_policy):
        try:
            policy = main_chat_tool_policy()
            if isinstance(policy, Mapping):
                policies.append(policy)
        except Exception:
            pass
    main_chat_config = getattr(runtime, "main_chat_config", None)
    config_tool_policy = getattr(main_chat_config, "tool_policy", None)
    if callable(config_tool_policy):
        try:
            policy = config_tool_policy()
            if isinstance(policy, Mapping):
                policies.append(policy)
        except Exception:
            pass
    return policies
